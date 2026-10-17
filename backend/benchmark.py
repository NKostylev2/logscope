import os
import sys
import time
import tracemalloc
import sqlite3
import tempfile
from pathlib import Path

# Add backend directory to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.db import init_db, connect
from app.importer import import_folder
from app.main import app
from fastapi.testclient import TestClient


def generate_synthetic_logs(dir_path: Path, count: int = 100_000):
    """Generate 3 overlapping sources:
    1. app.log (text log with request_ids and levels)
    2. jmeter.csv (JMeter JTL samples)
    3. metrics.csv (system metrics)
    """
    app_log = dir_path / "app.log"
    jmeter_csv = dir_path / "jmeter.csv"
    metrics_csv = dir_path / "metrics.csv"

    base_time_ms = 1792059800000  # 2026-10-15T10:23:20
    share = count // 3

    print(f"Generating {count} synthetic events across 3 sources...")

    with open(app_log, "w", encoding="utf-8") as f:
        for i in range(share):
            t_ms = base_time_ms + i * 20
            # format as ISO
            sec = t_ms // 1000
            ms = t_ms % 1000
            lvl = "ERROR" if i % 100 == 0 else ("WARN" if i % 20 == 0 else "INFO")
            req = f"req-{i % 1000}"
            f.write(f"2026-10-15 10:00:{i % 60:02d}.{ms:03d} {lvl} request_id={req} Operation processed item {i}\n")

    with open(jmeter_csv, "w", encoding="utf-8") as f:
        f.write("timeStamp,elapsed,label,responseCode,responseMessage,threadName,success,bytes,sentBytes,Latency,Connect\n")
        for i in range(share):
            t_ms = base_time_ms + i * 20
            success = "false" if i % 150 == 0 else "true"
            rc = "500" if success == "false" else "200"
            f.write(f"{t_ms},{i % 500 + 10},GET /api/item/{i % 50},{rc},OK,TG-1,{success},1200,200,15,5\n")

    with open(metrics_csv, "w", encoding="utf-8") as f:
        f.write("timestamp,host,cpu_percent,memory_percent\n")
        for i in range(share):
            t_ms = base_time_ms + i * 20
            f.write(f"{t_ms},server-node-{i % 4},{(i * 7) % 100},{(i * 13) % 100}\n")

    total_bytes = app_log.stat().st_size + jmeter_csv.stat().st_size + metrics_csv.stat().st_size
    print(f"Generated 3 files, total size: {total_bytes / 1024 / 1024:.2f} MB")
    return total_bytes


def run_benchmark():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        inbox = tmp_dir / "inbox"
        inbox.mkdir()
        db_path = tmp_dir / "bench.sqlite3"

        # Patch environment
        import app.config as config
        import app.db as db_mod
        import app.importer as importer_mod
        import app.main as main_mod

        config.DB_PATH = db_path
        db_mod.DB_PATH = db_path
        config.ALLOWED_ROOT = inbox
        importer_mod.ALLOWED_ROOT = inbox
        main_mod.ALLOWED_ROOT = inbox

        init_db()

        # 1. 100,000 events benchmark
        file_bytes = generate_synthetic_logs(inbox, 100_000)

        tracemalloc.start()
        start_time = time.perf_counter()

        res = import_folder(str(inbox))

        elapsed_import = time.perf_counter() - start_time
        current_ram, peak_ram = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        db_size_mb = db_path.stat().st_size / 1024 / 1024
        total_events = sum(f.get("event_count", 0) for f in res["files"] if f.get("status") == "imported")
        speed = total_events / max(elapsed_import, 0.001)

        print("\n=== Benchmark Results (100,000 Events) ===")
        print(f"Total Imported Events: {total_events:,}")
        print(f"Raw Files Size:        {file_bytes / 1024 / 1024:.2f} MB")
        print(f"Import Elapsed Time:   {elapsed_import:.2f} s ({speed:,.0f} events/sec)")
        print(f"SQLite DB Size:        {db_size_mb:.2f} MB")
        print(f"Peak Traced RAM:       {peak_ram / 1024 / 1024:.2f} MB")

        # 2. Re-import unchanged files
        t_reimport_start = time.perf_counter()
        res_reimport = import_folder(str(inbox))
        t_reimport_elapsed = time.perf_counter() - t_reimport_start
        unchanged_count = sum(1 for f in res_reimport["files"] if f.get("status") == "unchanged")
        print(f"Re-import (unchanged): {t_reimport_elapsed:.4f} s ({unchanged_count} files skipped)")

        # 3. API Query Latencies
        with TestClient(app) as client:
            # Query 1: Narrow time interval (1 second)
            base_t = 1792059800000
            t_q1_start = time.perf_counter()
            r1 = client.get(f"/api/events?start_ms={base_t}&end_ms={base_t + 1000}")
            t_q1 = (time.perf_counter() - t_q1_start) * 1000
            cnt1 = r1.json()["total"]
            print(f"Query 1 [Narrow 1s window]:  {t_q1:.2f} ms ({cnt1} events matched)")

            # Query 2: Request ID search
            t_q2_start = time.perf_counter()
            r2 = client.get("/api/events?request_id=req-42")
            t_q2 = (time.perf_counter() - t_q2_start) * 1000
            cnt2 = r2.json()["total"]
            print(f"Query 2 [Request ID req-42]: {t_q2:.2f} ms ({cnt2} events matched)")

            # Query 3: Correlation endpoint
            t_q3_start = time.perf_counter()
            r3 = client.get("/api/correlations/req-42")
            t_q3 = (time.perf_counter() - t_q3_start) * 1000
            print(f"Query 3 [Correlation req-42]:{t_q3:.2f} ms (span={r3.json()['time_span_ms']} ms)")

            # Query 4: Filter by Level (ERROR)
            t_q4_start = time.perf_counter()
            r4 = client.get("/api/events?level=ERROR&limit=200")
            t_q4 = (time.perf_counter() - t_q4_start) * 1000
            cnt4 = r4.json()["total"]
            print(f"Query 4 [Level=ERROR]:       {t_q4:.2f} ms ({cnt4} events matched)")

            # Query 5: Timeline Aggregation (60 buckets)
            t_q5_start = time.perf_counter()
            r5 = client.get(f"/api/timeline?start_ms={base_t}&end_ms={base_t + 200000}&buckets=60")
            t_q5 = (time.perf_counter() - t_q5_start) * 1000
            b_cnt = len(r5.json()["buckets"])
            print(f"Query 5 [Timeline 60 buckets]:{t_q5:.2f} ms ({b_cnt} buckets returned)")

            # Query 6: Full-text search
            t_q6_start = time.perf_counter()
            r6 = client.get("/api/events?q=Operation processed item 42")
            t_q6 = (time.perf_counter() - t_q6_start) * 1000
            cnt6 = r6.json()["total"]
            print(f"Query 6 [Full-text Search]:  {t_q6:.2f} ms ({cnt6} events matched)")


if __name__ == "__main__":
    run_benchmark()
