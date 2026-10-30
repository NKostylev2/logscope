import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from app.db import init_db, transaction
from app.importer import import_folder
from app.main import app


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path: Path, monkeypatch):
    test_db = tmp_path / "api_test.sqlite3"
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    monkeypatch.setattr("app.db.DB_PATH", test_db)
    monkeypatch.setattr("app.config.DB_PATH", test_db)
    monkeypatch.setattr("app.config.ALLOWED_ROOT", inbox)
    monkeypatch.setattr("app.importer.ALLOWED_ROOT", inbox)
    monkeypatch.setattr("app.main.ALLOWED_ROOT", inbox)

    init_db()
    yield {"inbox": inbox, "db": test_db}


def test_health():
    with TestClient(app) as client:
        res = client.get("/api/health")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"


def test_index_page():
    with TestClient(app) as client:
        res = client.get("/")
        assert res.status_code == 200
        assert "LogScope" in res.text
        assert "Хронология событий" in res.text


def test_sources_and_events_flow(setup_test_env):
    inbox = setup_test_env["inbox"]
    log_file = inbox / "app.log"
    log_file.write_text(
        "Plain unformatted line without level or timestamp\n"
        "2026-10-15 10:00:00.000 INFO request_id=req-100 User logged in\n"
        "2026-10-15 10:00:05.000 WARN request_id=req-100 High memory usage\n"
        "2026-10-15 10:00:10.000 ERROR request_id=req-100 Database connection failed\n",
        encoding="utf-8"
    )

    import_folder(str(inbox))

    with TestClient(app) as client:
        # 1. Sources check
        sources_res = client.get("/api/sources")
        assert sources_res.status_code == 200
        sources = sources_res.json()
        assert len(sources) == 1
        assert sources[0]["name"] == "app.log"
        assert sources[0]["event_count"] == 4
        source_id = sources[0]["id"]

        # 2. Source detail
        src_detail = client.get(f"/api/sources/{source_id}")
        assert src_detail.status_code == 200
        assert src_detail.json()["name"] == "app.log"

        # 3. Events listing
        events_res = client.get("/api/events")
        assert events_res.status_code == 200
        data = events_res.json()
        assert data["total"] == 4
        assert len(data["items"]) == 4

        # 4. Filter by level
        err_res = client.get("/api/events?level=ERROR")
        assert err_res.json()["total"] == 1
        assert err_res.json()["items"][0]["level"] == "ERROR"

        # 5. Filter by UNKNOWN level (events where level is null)
        unknown_res = client.get("/api/events?level=UNKNOWN")
        assert unknown_res.json()["total"] == 1
        assert unknown_res.json()["items"][0]["level"] is None

        # 6. Full-text search
        search_res = client.get("/api/events?q=memory")
        assert search_res.json()["total"] == 1
        assert "High memory usage" in search_res.json()["items"][0]["message"]

        # 7. Request ID filter
        rid_res = client.get("/api/events?request_id=req-100")
        assert rid_res.json()["total"] == 3

        # 8. Pagination
        page_res = client.get("/api/events?limit=2&offset=0")
        assert page_res.json()["total"] == 4
        assert len(page_res.json()["items"]) == 2
        assert page_res.json()["offset"] == 0


def test_time_interval_filtering(setup_test_env):
    inbox = setup_test_env["inbox"]
    log_file = inbox / "timeline.log"
    log_file.write_text(
        "2026-10-15 10:00:00.000 INFO T0\n"
        "2026-10-15 10:00:10.000 INFO T10\n"
        "2026-10-15 10:00:20.000 INFO T20\n",
        encoding="utf-8"
    )
    import_folder(str(inbox))

    with TestClient(app) as client:
        all_events = client.get("/api/events").json()["items"]
        t0 = all_events[0]["timestamp_utc_ms"]
        t10 = all_events[1]["timestamp_utc_ms"]
        t20 = all_events[2]["timestamp_utc_ms"]

        # Half-open interval: start_ms <= t < end_ms
        # [t0, t20) should include t0 and t10, but EXCLUDE t20
        res = client.get(f"/api/events?start_ms={t0}&end_ms={t20}")
        assert res.status_code == 200
        items = res.json()["items"]
        assert len(items) == 2
        assert items[0]["timestamp_utc_ms"] == t0
        assert items[1]["timestamp_utc_ms"] == t10


def test_event_context(setup_test_env):
    inbox = setup_test_env["inbox"]
    log_file = inbox / "context.log"
    lines = [f"2026-10-15 10:00:{i:02d}.000 INFO Line number {i}" for i in range(20)]
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    import_folder(str(inbox))

    with TestClient(app) as client:
        events = client.get("/api/events?limit=20").json()["items"]
        target_event = events[5]  # Line 6

        ctx_res = client.get(f"/api/events/{target_event['id']}/context?lines=3")
        assert ctx_res.status_code == 200
        ctx = ctx_res.json()
        assert ctx["event_id"] == target_event["id"]
        assert ctx["target_start"] == target_event["line_start"]
        # Context includes lines before and after
        assert len(ctx["lines"]) >= 5
        # The target line must have is_target=True
        target_line = next(l for l in ctx["lines"] if l["line_no"] == target_event["line_start"])
        assert target_line["is_target"] is True


def test_correlations_and_timeline(setup_test_env):
    inbox = setup_test_env["inbox"]
    app_log = inbox / "app.log"
    app_log.write_text(
        "2026-10-15 10:00:00.000 INFO request_id=order-555 Order received\n"
        "2026-10-15 10:00:04.000 INFO request_id=order-555 Order completed\n",
        encoding="utf-8"
    )
    db_log = inbox / "db.log"
    db_log.write_text(
        "2026-10-15 10:00:01.000 INFO request_id=order-555 Query order status\n",
        encoding="utf-8"
    )
    import_folder(str(inbox))

    with TestClient(app) as client:
        # Correlation test
        corr_res = client.get("/api/correlations/order-555")
        assert corr_res.status_code == 200
        corr = corr_res.json()
        assert corr["request_id"] == "order-555"
        assert corr["total"] == 3
        # Sources involved must span app.log and db.log
        assert "app.log" in corr["sources"]
        assert "db.log" in corr["sources"]
        # Time span should be 4000 ms (10:00:04 - 10:00:00)
        assert corr["time_span_ms"] == 4000

        # Timeline test
        events = client.get("/api/events").json()["items"]
        t_min = min(e["timestamp_utc_ms"] for e in events)
        t_max = max(e["timestamp_utc_ms"] for e in events) + 1000

        tl_res = client.get(f"/api/timeline?start_ms={t_min}&end_ms={t_max}&buckets=10")
        assert tl_res.status_code == 200
        tl_data = tl_res.json()
        assert len(tl_data["buckets"]) > 0


def test_import_api_endpoints(setup_test_env):
    inbox = setup_test_env["inbox"]
    sub_dir = inbox / "sub"
    sub_dir.mkdir()
    (sub_dir / "sample.log").write_text("2026-10-15 10:00:00.000 INFO Hello\n", encoding="utf-8")

    with TestClient(app) as client:
        # Start background import
        start_res = client.post("/api/import/start", json={"path": str(sub_dir)})
        assert start_res.status_code == 200
        assert start_res.json()["status"] == "started"

        # Check status
        status_res = client.get("/api/import/status")
        assert status_res.status_code == 200
        assert "status" in status_res.json()


def test_not_found_and_validation(setup_test_env):
    with TestClient(app) as client:
        assert client.get("/api/events/999999").status_code == 404
        assert client.get("/api/sources/999999").status_code == 404
        assert client.get("/api/events?limit=999999").status_code == 422


def test_events_slice(setup_test_env):
    inbox = setup_test_env["inbox"]
    # Source 1: events at 10:00:00.000 and 10:00:05.000
    s1 = inbox / "srv1.log"
    s1.write_text(
        "2026-10-15 10:00:00.000 INFO Server 1 step 0\n"
        "2026-10-15 10:00:05.000 WARN Server 1 step 5\n",
        encoding="utf-8"
    )
    # Source 2: event at 10:00:00.150 and 10:00:04.900
    s2 = inbox / "srv2.log"
    s2.write_text(
        "2026-10-15 10:00:00.150 INFO Server 2 step 0.15\n"
        "2026-10-15 10:00:04.900 ERROR Server 2 step 4.9\n",
        encoding="utf-8"
    )
    import_folder(str(inbox))

    with TestClient(app) as client:
        # Get timestamp of srv1 first event
        ev = client.get("/api/events?q=step+0").json()["items"][0]
        target_t = ev["timestamp_utc_ms"]

        # Call slice at target_t
        res = client.get(f"/api/events/slice?timestamp_ms={target_t}")
        assert res.status_code == 200
        data = res.json()
        assert data["target_timestamp_ms"] == target_t
        assert data["total_sources"] == 2

        # Check exact and nearest neighbor
        items = data["items"]
        srv1_item = next(i for i in items if i["source_name"] == "srv1.log")
        srv2_item = next(i for i in items if i["source_name"] == "srv2.log")

        # srv1 has exact event at target_t
        assert srv1_item["is_exact"] is True
        assert srv1_item["delta_ms"] == 0
        assert "Server 1 step 0" in srv1_item["message"]

        # srv2 has nearest neighbor 150ms after target_t
        assert srv2_item["is_exact"] is False
        assert srv2_item["delta_ms"] == 150
        assert "Server 2 step 0.15" in srv2_item["message"]

        # Stepping: next timestamp should be target_t + 150
        assert data["next_timestamp_ms"] == target_t + 150

        # Filter slice by level=ERROR: only srv2 has an ERROR event
        res_err = client.get(f"/api/events/slice?timestamp_ms={target_t}&level=ERROR")
        assert res_err.status_code == 200
        data_err = res_err.json()
        assert len(data_err["items"]) == 1
        assert data_err["items"][0]["source_name"] == "srv2.log"
        assert data_err["items"][0]["level"] == "ERROR"
        assert data_err["items"][0]["event_id"] is not None

        # Filter slice by search query: only srv1 has 'step 5'
        res_q = client.get(f"/api/events/slice?timestamp_ms={target_t}&q=step+5")
        assert res_q.status_code == 200
        data_q = res_q.json()
        assert len(data_q["items"]) == 1
        assert data_q["items"][0]["source_name"] == "srv1.log"
        assert "step 5" in data_q["items"][0]["message"]


def test_events_multi_level_and_http_codes(setup_test_env):
    inbox = setup_test_env["inbox"]

    # 1. Access log with HTTP codes in message
    access_log = inbox / "access.log"
    access_log.write_text(
        "2026-10-15 12:00:00.000 INFO GET /api/v1/users HTTP/1.1 200 OK\n"
        "2026-10-15 12:00:01.000 WARN GET /api/v1/missing HTTP/1.1 404 Not Found\n"
        "2026-10-15 12:00:02.000 ERROR POST /api/v1/checkout HTTP/1.1 500 Server Error\n",
        encoding="utf-8"
    )

    # 2. File with unlevelled event
    plain_log = inbox / "plain.txt"
    plain_log.write_text("Plain unlevelled message with 200 OK status\n", encoding="utf-8")

    # 3. JMeter JTL CSV with responseCode column
    jtl = inbox / "test.jtl"
    jtl.write_text(
        "timeStamp,elapsed,label,responseCode,responseMessage,threadName,dataType,success,failureMessage,bytes,sentBytes,grpThreads,allThreads,URL,Latency,IdleTime,Connect\n"
        "1792044000000,50,HTTP-GET,200,OK,Thread-1,text,true,,100,50,1,1,http://app/api,40,0,10\n"
        "1792044001000,120,HTTP-POST,500,Internal Error,Thread-1,text,false,,100,50,1,1,http://app/pay,110,0,10\n",
        encoding="utf-8"
    )

    import_folder(str(inbox))

    with TestClient(app) as client:
        # Multi-level: ERROR,WARN
        res_levels = client.get("/api/events?levels=ERROR,WARN")
        assert res_levels.status_code == 200
        data = res_levels.json()
        # Should include access.log 404 (WARN), access.log 500 (ERROR), test.jtl 500 (ERROR)
        assert data["total"] == 3
        levels_found = {item["level"] for item in data["items"]}
        assert levels_found == {"WARN", "ERROR"}

        # Multi-level with NONE: ERROR,NONE
        res_err_none = client.get("/api/events?levels=ERROR,NONE")
        assert res_err_none.status_code == 200
        data_err_none = res_err_none.json()
        assert any(item["level"] is None for item in data_err_none["items"])
        assert any(item["level"] == "ERROR" for item in data_err_none["items"])

        # HTTP Code exact: 500
        res_500 = client.get("/api/events?http_codes=500")
        assert res_500.status_code == 200
        data_500 = res_500.json()
        assert data_500["total"] == 2  # access.log 500 + jtl 500

        # HTTP Code class: 4xx
        res_4xx = client.get("/api/events?http_codes=4xx")
        assert res_4xx.status_code == 200
        data_4xx = res_4xx.json()
        assert data_4xx["total"] == 1
        assert "404" in data_4xx["items"][0]["message"]

        # HTTP Code classes combined: 4xx,5xx
        res_err_codes = client.get("/api/events?http_codes=4xx,5xx")
        assert res_err_codes.status_code == 200
        data_err_codes = res_err_codes.json()
        assert data_err_codes["total"] == 3

        # Slice with http_codes=500
        target_t = 1792044001000  # time of 500 in JTL
        slice_res = client.get(f"/api/events/slice?timestamp_ms={target_t}&http_codes=500")
        assert slice_res.status_code == 200
        slice_data = slice_res.json()
        assert len(slice_data["items"]) >= 1
        assert any("500" in (item.get("fields_json") or {}).get("responseCode", "") or "500" in item["message"] for item in slice_data["items"])



# ---------------------------------------------------------------- audit regressions

def test_search_is_case_insensitive_for_cyrillic(setup_test_env):
    inbox = setup_test_env["inbox"]
    (inbox / "ru.log").write_text(
        "2026-10-15 10:00:00.000 ERROR Ошибка подключения к базе\n"
        "2026-10-15 10:00:01.000 INFO Всё хорошо\n",
        encoding="utf-8",
    )
    import_folder(str(inbox))

    with TestClient(app) as client:
        for q in ("Ошибка", "ошибка", "ОШИБКА", "ошибка подключения"):
            assert client.get("/api/events", params={"q": q}).json()["total"] == 1, q
        assert client.get("/api/events", params={"q": "нет такого"}).json()["total"] == 0
        # the ASCII path still escapes LIKE wildcards
        assert client.get("/api/events", params={"q": "%"}).json()["total"] == 0
        assert client.get("/api/events", params={"q": "error"}).json()["total"] == 1


def test_http_class_filter_ignores_bracketed_timestamps(setup_test_env):
    inbox = setup_test_env["inbox"]
    (inbox / "web.log").write_text(
        "[2026-10-15 10:00:00] INFO user logged in\n"
        "2026-10-15 10:00:01.000 INFO GET /health HTTP/1.1 200 OK\n"
        "2026-10-15 10:00:02.000 WARN GET /missing HTTP/1.1 404 Not Found\n",
        encoding="utf-8",
    )
    (inbox / "load.csv").write_text(
        "timeStamp,elapsed,label,responseCode,responseMessage,success\n"
        "1792059800000,10,GET /a,200,OK,true\n"
        "1792059800100,11,GET /b,503,Service Unavailable,false\n",
        encoding="utf-8",
    )
    import_folder(str(inbox))

    with TestClient(app) as client:
        two_xx = client.get("/api/events", params={"http_codes": "2xx"}).json()
        messages = [i["message"] for i in two_xx["items"]]
        assert two_xx["total"] == 2, messages
        assert not any("logged in" in m for m in messages)

        assert client.get("/api/events", params={"http_codes": "5xx"}).json()["total"] == 1
        assert client.get("/api/events", params={"http_codes": "404"}).json()["total"] == 1


def test_level_filter_uses_the_level_index():
    from app.db import connect
    from app.main import build_level_clause

    sql, params = build_level_clause("error")
    with connect() as conn:
        plan = " ".join(
            row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN SELECT COUNT(*) FROM events e WHERE {sql}", params)
        )
    assert "SEARCH" in plan and "idx_events_level_time" in plan, plan


def test_timeline_never_returns_more_buckets_than_requested(setup_test_env):
    with transaction() as conn:
        conn.execute(
            "INSERT INTO sources(id, path, name, size_bytes, mtime_ns, event_count) VALUES (1, '/x', 'x', 1, 1, 1000)"
        )
        conn.executemany(
            "INSERT INTO events(source_id, timestamp_utc_ms, timestamp_ms, message, line_start, line_end) "
            "VALUES (1, ?, ?, 'm', 1, 1)",
            [(i, i) for i in range(1000)],
        )
    with TestClient(app) as client:
        data = client.get("/api/timeline", params={"start_ms": 0, "end_ms": 1000, "buckets": 60}).json()
    assert len(data["buckets"]) <= 60
    assert sum(sum(b["sources"].values()) for b in data["buckets"]) == 1000


def test_context_is_rejected_for_xml_jtl(setup_test_env):
    inbox = setup_test_env["inbox"]
    (inbox / "run.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><testResults version="1.2">'
        '<httpSample t="5" ts="1792059800000" s="true" lb="GET /" rc="200"/></testResults>',
        encoding="utf-8",
    )
    import_folder(str(inbox))
    with TestClient(app) as client:
        event_id = client.get("/api/events").json()["items"][0]["id"]
        res = client.get(f"/api/events/{event_id}/context")
    assert res.status_code == 422


def test_total_is_correct_for_partial_full_and_out_of_range_pages(setup_test_env):
    inbox = setup_test_env["inbox"]
    (inbox / "p.log").write_text(
        "".join(f"2026-10-15 10:00:{i:02d}.000 INFO line {i}\n" for i in range(25)), encoding="utf-8"
    )
    import_folder(str(inbox))

    with TestClient(app) as client:
        def total(**params):
            return client.get("/api/events", params=params).json()["total"]

        assert total(limit=100) == 25               # partial first page (count skipped)
        assert total(limit=10) == 25                # full page -> COUNT query
        assert total(limit=10, offset=20) == 25     # partial later page: offset + rows
        assert total(limit=10, offset=100) == 25    # out of range -> COUNT query
        assert total(limit=10, q="line 7") == 1
        assert total(limit=10, q="no such text", offset=0) == 0


def test_http_filter_no_false_positives(setup_test_env):
    inbox = setup_test_env["inbox"]
    # 1. proxy.log with 201 status and 467 bytes (should NOT match 4xx)
    (inbox / "proxy.log").write_text(
        '2026-10-15 10:00:00.000 INFO 192.168.1.1 - [15/Oct/2026:10:00:00 +0000] "POST /api/item HTTP/1.0" 201 467 "https://example.com" "Mozilla/5.0"\n',
        encoding="utf-8"
    )
    # 2. timescale.log with postgres PID [40] (should NOT match 4xx)
    (inbox / "timescale0.log").write_text(
        '2026-10-15 10:00:00.000 NONE 2026-10-15 10:00:00.000 UTC [40] DETAIL: Last completed transaction was at log time\n',
        encoding="utf-8"
    )
    # 3. real 404 error
    (inbox / "error.log").write_text(
        '2026-10-15 10:00:01.000 WARN GET /missing HTTP/1.1 404 Not Found\n',
        encoding="utf-8"
    )
    import_folder(str(inbox))

    with TestClient(app) as client:
        # 4xx filter must ONLY match error.log
        res_4xx = client.get("/api/events?http_codes=4xx")
        assert res_4xx.status_code == 200
        items_4xx = res_4xx.json()["items"]
        assert len(items_4xx) == 1
        assert items_4xx[0]["source_name"] == "error.log"

        # 5xx filter must match 0 events
        res_5xx = client.get("/api/events?http_codes=5xx")
        assert res_5xx.status_code == 200
        assert res_5xx.json()["total"] == 0

        # NONE_SELECTED must match 0 events
        res_none = client.get("/api/events?http_codes=NONE_SELECTED")
        assert res_none.status_code == 200
        assert res_none.json()["total"] == 0


def test_slice_step_source_id_with_filters(setup_test_env):
    inbox = setup_test_env["inbox"]
    # Source A has events at t=1000 (200), t=3000 (500), t=5000 (200), t=7000 (500)
    (inbox / "srvA.log").write_text(
        '2026-10-15 10:00:01.000 INFO GET /a1 HTTP/1.1 200 OK\n'
        '2026-10-15 10:00:03.000 ERROR GET /a2 HTTP/1.1 500 Error\n'
        '2026-10-15 10:00:05.000 INFO GET /a3 HTTP/1.1 200 OK\n'
        '2026-10-15 10:00:07.000 ERROR GET /a4 HTTP/1.1 500 Error\n',
        encoding="utf-8"
    )
    # Source B has events at t=2000 (200), t=4000 (500), t=6000 (200)
    (inbox / "srvB.log").write_text(
        '2026-10-15 10:00:02.000 INFO GET /b1 HTTP/1.1 200 OK\n'
        '2026-10-15 10:00:04.000 ERROR GET /b2 HTTP/1.1 500 Error\n'
        '2026-10-15 10:00:06.000 INFO GET /b3 HTTP/1.1 200 OK\n',
        encoding="utf-8"
    )
    import_folder(str(inbox))

    with TestClient(app) as client:
        sources = client.get("/api/sources").json()
        src_a = next(s for s in sources if s["name"] == "srvA.log")
        src_b = next(s for s in sources if s["name"] == "srvB.log")

        # Start at t=1000 (t=1000 in srvA)
        t0 = src_a["min_time_ms"]
        # Step forward in srvA with filter http_codes=500
        # The next 500 in srvA is at t0 + 2000 (10:00:03.000). srvB has an event at t0 + 1000 (200) and t0 + 3000 (500),
        # but stepping in srvA must strictly return srvA's next 500!
        res1 = client.get(f"/api/events/slice?timestamp_ms={t0}&step_source_id={src_a['id']}&http_codes=500")
        assert res1.status_code == 200
        d1 = res1.json()
        assert d1["next_timestamp_ms"] == t0 + 2000  # 10:00:03.000 in srvA

        # Now at t=t0+2000, step forward in srvA with http_codes=500
        res2 = client.get(f"/api/events/slice?timestamp_ms={t0 + 2000}&step_source_id={src_a['id']}&http_codes=500")
        assert res2.status_code == 200
        d2 = res2.json()
        assert d2["next_timestamp_ms"] == t0 + 6000  # 10:00:07.000 in srvA
        assert d2["prev_timestamp_ms"] is None  # No earlier 500 in srvA than 10:00:03

        # At t=t0+6000 (10:00:07), no further 500 events in srvA
        res3 = client.get(f"/api/events/slice?timestamp_ms={t0 + 6000}&step_source_id={src_a['id']}&http_codes=500")
        assert res3.status_code == 200
        d3 = res3.json()
        assert d3["next_timestamp_ms"] is None
        assert d3["prev_timestamp_ms"] == t0 + 2000  # can step back to 10:00:03.000 in srvA!

