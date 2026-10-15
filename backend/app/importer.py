import os
from pathlib import Path
from .config import ALLOWED_ROOT, MAX_FILE_BYTES, SUPPORTED_EXTENSIONS, PROJECT_DIR
from .db import transaction
from .parsers import iter_file_events

def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False

def import_folder(folder: str):
    root = ALLOWED_ROOT.resolve()
    raw_candidate = Path(folder)
    candidate = (PROJECT_DIR / raw_candidate).resolve() if not raw_candidate.is_absolute() else raw_candidate.resolve()
    if not _within(candidate, root):
        raise ValueError(f"Путь должен находиться внутри разрешённой папки: {root}")
    if not candidate.is_dir():
        raise ValueError("Указанный путь не является папкой")
    results = []
    with transaction() as conn:
        for path in sorted(candidate.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            stat = path.stat()
            if stat.st_size > MAX_FILE_BYTES:
                results.append({"path": str(path), "status": "skipped", "reason": "file_too_large"})
                continue
            existing = conn.execute("SELECT id,size_bytes,mtime_ns FROM sources WHERE path=?", (str(path),)).fetchone()
            if existing and existing["size_bytes"] == stat.st_size and existing["mtime_ns"] == stat.st_mtime_ns:
                results.append({"path": str(path), "status": "unchanged", "source_id": existing["id"]})
                continue
            if existing:
                conn.execute("DELETE FROM events WHERE source_id=?", (existing["id"],))
                conn.execute("DELETE FROM sources WHERE id=?", (existing["id"],))
            cur = conn.execute(
                "INSERT INTO sources(path,name,size_bytes,mtime_ns) VALUES(?,?,?,?)",
                (str(path), path.name, stat.st_size, stat.st_mtime_ns)
            )
            source_id = cur.lastrowid
            count = 0
            batch = []
            try:
                for event in iter_file_events(path):
                    batch.append((
                        source_id, event["timestamp_ms"], event["timestamp_raw"], event["level"],
                        event["message"], event["line_start"], event["line_end"],
                        event["request_id"], event["fields_json"]
                    ))
                    count += 1
                    if len(batch) >= 1000:
                        conn.executemany("""INSERT INTO events
                            (source_id,timestamp_ms,timestamp_raw,level,message,line_start,line_end,request_id,fields_json)
                            VALUES(?,?,?,?,?,?,?,?,?)""", batch)
                        batch.clear()
                if batch:
                    conn.executemany("""INSERT INTO events
                        (source_id,timestamp_ms,timestamp_raw,level,message,line_start,line_end,request_id,fields_json)
                        VALUES(?,?,?,?,?,?,?,?,?)""", batch)
                conn.execute("UPDATE sources SET event_count=? WHERE id=?", (count, source_id))
                results.append({"path": str(path), "status": "imported", "source_id": source_id, "event_count": count})
            except Exception as exc:
                conn.execute("DELETE FROM events WHERE source_id=?", (source_id,))
                conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
                results.append({"path": str(path), "status": "error", "reason": str(exc)})
    return {"root": str(candidate), "files": results}
