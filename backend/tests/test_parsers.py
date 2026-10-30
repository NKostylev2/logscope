import json
import tracemalloc
import pytest
from pathlib import Path
from app.parsers import (
    parse_timestamp,
    parse_epoch,
    extract_timestamp,
    extract_level,
    extract_request_id,
    parse_text_file,
    parse_csv_file,
    parse_xml_jtl,
    iter_file_events,
)


def test_iso_timestamp_variations():
    # ISO with Z
    ts = parse_timestamp("2026-10-15T10:23:41.250Z")
    assert ts is not None

    # ISO with offset
    ts2 = parse_timestamp("2026-10-15T10:23:41.250+03:00")
    assert ts2 is not None

    # ISO with space and comma (JMeter log)
    ts3 = parse_timestamp("2026-10-15 10:23:41,250")
    assert ts3 is not None

    # ISO with nanoseconds (Docker / container logs)
    ts4 = parse_timestamp("2026-10-14T12:13:11.558648158Z")
    assert ts4 is not None

    # PostgreSQL style with UTC suffix
    ts5 = parse_timestamp("2026-10-14 12:13:22.422 UTC")
    assert ts5 is not None


def test_european_timestamp():
    ts = parse_timestamp("15/10/2026 10:23:41.250")
    assert ts is not None

    ts_dot = parse_timestamp("15.10.2026 10:23:41.250")
    assert ts_dot is not None


def test_epoch_scales():
    # Epoch seconds (10 digits)
    sec = 1792059821
    assert parse_epoch(sec) == sec * 1000
    assert parse_timestamp(str(sec)) == sec * 1000

    # Epoch milliseconds (13 digits)
    ms = 1792059821250
    assert parse_epoch(ms) == ms
    assert parse_timestamp(str(ms)) == ms

    # Epoch microseconds (16 digits)
    us = 1792059821250000
    assert parse_epoch(us) == ms

    # Epoch nanoseconds (19 digits - Telegraf / Influx)
    ns = 1792059821250000000
    assert parse_epoch(ns) == ms


def test_arbitrary_numbers_rejected_as_epoch():
    # HTTP status codes
    assert parse_epoch(200) is None
    assert parse_epoch(404) is None
    assert parse_epoch(500) is None
    assert parse_timestamp("404") is None

    # Small numbers, thread IDs, PIDs
    assert parse_epoch(1) is None
    assert parse_epoch(12345) is None

    # Year outside 2000-2100
    assert parse_epoch(100) is None
    assert parse_epoch(99999999999999999999) is None


def test_naive_timestamp_preserves_raw():
    ts, raw = extract_timestamp("2026-10-15 10:23:41.190 INFO Hello")
    assert ts is not None
    assert raw == "2026-10-15 10:23:41.190"


def test_corrupted_or_missing_timestamp():
    assert parse_timestamp("no timestamp in this line") is None
    assert parse_timestamp("") is None
    assert parse_timestamp(None) is None
    assert parse_timestamp("2026-99-99 99:99:99") is None


def test_log_levels():
    assert extract_level("2026-10-15 INFO test") == "INFO"
    assert extract_level("2026-10-15 WARN test") == "WARN"
    assert extract_level("2026-10-15 WARNING test") == "WARN"
    assert extract_level("2026-10-15 ERROR test") == "ERROR"
    assert extract_level("2026-10-15 FATAL test") == "FATAL"
    assert extract_level("2026-10-15 CRITICAL test") == "FATAL"
    assert extract_level("2026-10-15 DEBUG test") == "DEBUG"
    assert extract_level("2026-10-15 TRACE test") == "TRACE"
    # Never default to INFO when absent
    assert extract_level("Just a plain text message without level") is None


def test_request_id_extraction():
    assert extract_request_id("Handling request_id=req-12345 completed") == "req-12345"
    assert extract_request_id("correlation_id: corr-abc-999") == "corr-abc-999"
    assert extract_request_id("trace_id=trace-001") == "trace-001"
    assert extract_request_id("x-request-id=xyz-888") == "xyz-888"
    assert extract_request_id("no identifier here") is None


def test_multiline_text_parser(tmp_path: Path):
    log_content = (
        "2026-10-15 10:23:41.190 INFO request_id=demo-01 Initializing\n"
        "2026-10-15 10:23:42.500 ERROR request_id=demo-01 Unexpected exception occurred:\n"
        "    java.lang.NullPointerException: Cannot invoke method\n"
        "\tat com.example.service.OrderService.process(OrderService.java:42)\n"
        "\tat com.example.controller.OrderController.create(OrderController.java:18)\n"
        "Caused by: java.lang.IllegalArgumentException: Missing field\n"
        "\t... 12 more\n"
        "2026-10-15 10:23:43.000 INFO request_id=demo-02 Recovery completed\n"
    )
    file_path = tmp_path / "app.log"
    file_path.write_text(log_content, encoding="utf-8")

    events = list(parse_text_file(file_path))
    assert len(events) == 3

    # Event 1
    assert events[0]["level"] == "INFO"
    assert events[0]["line_start"] == 1
    assert events[0]["line_end"] == 1
    assert events[0]["request_id"] == "demo-01"

    # Event 2 (multiline stack trace)
    assert events[1]["level"] == "ERROR"
    assert events[1]["line_start"] == 2
    assert events[1]["line_end"] == 7
    assert events[1]["request_id"] == "demo-01"
    assert "java.lang.NullPointerException" in events[1]["message"]
    assert "Caused by: java.lang.IllegalArgumentException" in events[1]["message"]

    # Event 3
    assert events[2]["level"] == "INFO"
    assert events[2]["line_start"] == 8
    assert events[2]["line_end"] == 8
    assert events[2]["request_id"] == "demo-02"


def test_csv_jmeter_parsing(tmp_path: Path):
    csv_content = (
        "timeStamp,elapsed,label,responseCode,responseMessage,threadName,dataType,success,failureMessage,bytes,sentBytes,grpThreads,allThreads,URL,Latency,IdleTime,Connect\n"
        "1792059821102,840,POST /api/orders,200,OK,Thread Group 1-1,text,true,,1234,256,1,1,http://app/orders,612,0,12\n"
        "1792059823000,2410,GET /api/items,500,Internal Server Error,Thread Group 1-2,text,false,Server error 500,500,120,1,1,http://app/items,2200,0,15\n"
    )
    file_path = tmp_path / "results.csv"
    file_path.write_text(csv_content, encoding="utf-8")

    events = list(parse_csv_file(file_path))
    assert len(events) == 2

    # First sampler
    e1 = events[0]
    assert e1["timestamp_utc_ms"] == 1792059821102
    assert e1["duration_ms"] == 840
    assert e1["level"] == "INFO"
    assert e1["event_type"] == "jmeter_sample"
    fields1 = json.loads(e1["fields_json"])
    assert fields1["responseCode"] == "200"
    assert fields1["Latency"] == 612
    assert fields1["URL"] == "http://app/orders"

    # Second sampler (failure)
    e2 = events[1]
    assert e2["timestamp_utc_ms"] == 1792059823000
    assert e2["duration_ms"] == 2410
    assert e2["level"] == "ERROR"
    fields2 = json.loads(e2["fields_json"])
    assert fields2["responseCode"] == "500"
    assert fields2["failureMessage"] == "Server error 500"


def test_monitoring_csv_parsing(tmp_path: Path):
    csv_content = (
        "timestamp,host,cpu_percent,memory_percent,request_id\n"
        "2026-10-15 10:23:41.250,srv-1,94,76,corr-101\n"
        "2026-10-15 10:23:42.250,srv-1,97,77,corr-101\n"
    )
    file_path = tmp_path / "metrics.csv"
    file_path.write_text(csv_content, encoding="utf-8")

    events = list(parse_csv_file(file_path))
    assert len(events) == 2
    assert events[0]["event_type"] == "metric"
    assert events[0]["request_id"] == "corr-101"
    fields = json.loads(events[0]["fields_json"])
    assert fields["cpu_percent"] == "94"
    assert fields["host"] == "srv-1"


def test_csv_delimiters_and_quotes(tmp_path: Path):
    # Semicolon delimited with quotes containing semicolons
    csv_content = (
        'timestamp;level;message\n'
        '2026-10-15 10:23:41.100;INFO;"Process started; with parameter x=1"\n'
        '2026-10-15 10:23:42.200;WARN;"Value high; check system"\n'
    )
    file_path = tmp_path / "semicolon.csv"
    file_path.write_text(csv_content, encoding="utf-8")

    events = list(parse_csv_file(file_path))
    assert len(events) == 2
    assert "Process started; with parameter x=1" in events[0]["message"]
    assert events[0]["level"] == "INFO"
    assert events[1]["level"] == "WARN"


def test_xml_jtl_valid(tmp_path: Path):
    xml_content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<testResults version="1.2">\n'
        '  <httpSample t="840" it="0" lt="612" ct="12" ts="1792059821102" s="true" lb="POST /api/orders" rc="200" rm="OK" tn="Thread Group 1-1" by="1234" sby="256" ng="1" na="1">\n'
        '    <java.net.URL>http://localhost:8080/api/orders?request_id=order-xml-1</java.net.URL>\n'
        '  </httpSample>\n'
        '  <sample t="2410" it="0" lt="2200" ct="15" ts="1792059823000" s="false" lb="GET /api/items" rc="500" rm="Internal Server Error" tn="Thread Group 1-2">\n'
        '    <assertionResult>\n'
        '      <name>Assertion</name>\n'
        '      <failureMessage>Expected 200 but got 500</failureMessage>\n'
        '    </assertionResult>\n'
        '  </sample>\n'
        '</testResults>\n'
    )
    file_path = tmp_path / "results.jtl"
    file_path.write_text(xml_content, encoding="utf-8")

    events = list(iter_file_events(file_path))
    assert len(events) == 2

    # Sample 1
    e1 = events[0]
    assert e1["timestamp_utc_ms"] == 1792059821102
    assert e1["duration_ms"] == 840
    assert e1["level"] == "INFO"
    assert e1["event_type"] == "jmeter_sample"
    assert e1["request_id"] == "order-xml-1"
    f1 = json.loads(e1["fields_json"])
    assert f1["responseCode"] == "200"
    assert f1["Latency"] == 612

    # Sample 2 (Failure)
    e2 = events[1]
    assert e2["timestamp_utc_ms"] == 1792059823000
    assert e2["duration_ms"] == 2410
    assert e2["level"] == "ERROR"
    f2 = json.loads(e2["fields_json"])
    assert f2["responseCode"] == "500"
    assert f2["failureMessage"] == "Expected 200 but got 500"


def test_xml_jtl_empty_and_corrupt(tmp_path: Path):
    empty_file = tmp_path / "empty.xml"
    empty_file.write_text("", encoding="utf-8")
    assert list(parse_xml_jtl(empty_file)) == []

    corrupt_file = tmp_path / "corrupt.xml"
    corrupt_file.write_text("<testResults><httpSample t='10'>broken xml", encoding="utf-8")
    # Must not crash, should return whatever it parsed or empty list
    events = list(parse_xml_jtl(corrupt_file))
    assert isinstance(events, list)


def test_encoding_fallback_cp1251(tmp_path: Path):
    # Russian text in Windows-1251
    text = "2026-10-15 10:23:41.190 INFO Тестовое сообщение на русском языке\n"
    file_path = tmp_path / "win1251.log"
    file_path.write_bytes(text.encode("cp1251"))

    events = list(parse_text_file(file_path))
    assert len(events) == 1
    assert "Тестовое сообщение на русском языке" in events[0]["message"]


# ---------------------------------------------------------------- audit regressions

def test_csv_truncated_last_row_does_not_crash(tmp_path: Path):
    """A live / killed JMeter run leaves a cut-off last row; it used to abort the whole file."""
    f = tmp_path / "live.csv"
    f.write_text(
        "timeStamp,elapsed,label,responseCode,responseMessage,threadName,success\n"
        "1792059800000,120,GET /a,200,OK,TG-1,true\n"
        "1792059800100,130,GET /b,500,Err,TG-1,false\n"
        "1792059800200,14",
        encoding="utf-8",
    )
    events = list(parse_csv_file(f))
    assert len(events) == 3
    assert [e["level"] for e in events[:2]] == ["INFO", "ERROR"]
    assert events[2]["timestamp_utc_ms"] == 1792059800200


def test_csv_line_numbers_follow_physical_lines(tmp_path: Path):
    f = tmp_path / "multi.csv"
    f.write_text(
        "timeStamp,elapsed,label,responseCode,responseMessage,success,failureMessage\n"
        '1792059800000,10,A,200,OK,true,""\n'
        '1792059800100,11,B,500,Err,false,"line1\nline2\nline3"\n'
        '1792059800200,12,C,200,OK,true,""\n',
        encoding="utf-8",
    )
    spans = [(e["line_start"], e["line_end"]) for e in parse_csv_file(f)]
    assert spans == [(2, 2), (3, 5), (6, 6)]


def test_text_parser_bounds_event_size_without_losing_lines(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("app.parsers.MAX_EVENT_LINES", 50)
    f = tmp_path / "no_ts.log"
    f.write_text("".join(f"plain line {i}\n" for i in range(130)), encoding="utf-8")

    events = list(parse_text_file(f))

    assert len(events) == 3  # 50 + 50 + 30
    assert all(e["message"].count("\n") + 1 <= 50 for e in events)
    assert events[0]["line_start"] == 1 and events[-1]["line_end"] == 130
    for prev, nxt in zip(events, events[1:]):
        assert nxt["line_start"] == prev["line_end"] + 1
    assert "\n".join(e["message"] for e in events) == "\n".join(f"plain line {i}" for i in range(130))


def test_split_segments_inherit_timestamp_level_and_request_id(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("app.parsers.MAX_EVENT_LINES", 3)
    f = tmp_path / "trace.log"
    f.write_text(
        "2026-10-15 10:00:00.000 ERROR request_id=abc boom\n" + "    at com.example.Foo(Foo.java:1)\n" * 7,
        encoding="utf-8",
    )
    events = list(parse_text_file(f))
    assert len(events) == 3  # 3 + 3 + 2 lines
    assert events[0]["timestamp_utc_ms"] is not None
    assert {e["timestamp_utc_ms"] for e in events} == {events[0]["timestamp_utc_ms"]}
    assert all(e["level"] == "ERROR" and e["request_id"] == "abc" for e in events)


def test_encoding_is_detected_beyond_the_first_chunk(tmp_path: Path):
    """cp1251 file that starts with a long ASCII banner: the head alone looks like UTF-8."""
    head = b"2026-10-15 10:00:00.000 INFO startup ok\n" * 1000  # ~40 KB of pure ASCII
    tail = "2026-10-15 10:00:01.000 ERROR Ошибка подключения к базе\n".encode("cp1251")
    f = tmp_path / "late_cyrillic.log"
    f.write_bytes(head + tail)

    last = list(parse_text_file(f))[-1]["message"]
    assert "Ошибка подключения к базе" in last
    assert "\ufffd" not in last


def test_xml_jtl_nested_samples_are_all_parsed(tmp_path: Path):
    f = tmp_path / "nested.xml"
    f.write_text(
        '<?xml version="1.0" encoding="UTF-8"?><testResults version="1.2">'
        '<sample t="30" ts="1792059800000" s="true" lb="parent" rc="200">'
        '<httpSample t="10" ts="1792059800001" s="true" lb="child1" rc="200"/>'
        '<httpSample t="20" ts="1792059800011" s="false" lb="child2" rc="500"/>'
        "</sample>"
        '<httpSample t="5" ts="1792059800100" s="true" lb="next" rc="200"/>'
        "</testResults>",
        encoding="utf-8",
    )
    events = list(parse_xml_jtl(f))
    labels = [json.loads(e["fields_json"])["label"] for e in events]
    assert labels == ["child1", "child2", "parent", "next"]  # inner samples close first
    assert events[2]["duration_ms"] == 30


def test_xml_jtl_memory_is_independent_of_sample_count(tmp_path: Path):
    """README promises O(1) memory; the empty cleared elements used to pile up under the root."""
    n = 60_000
    f = tmp_path / "big.xml"
    with f.open("w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<testResults version="1.2">\n')
        for i in range(n):
            fh.write(f'<httpSample t="{i % 500}" lt="5" ts="{1792059800000 + i}" s="true" lb="GET /x" rc="200" rm="OK" tn="TG 1-1" by="10"/>\n')
        fh.write("</testResults>\n")

    tracemalloc.start()
    try:
        count = sum(1 for _ in parse_xml_jtl(f))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert count == n
    assert peak < 2 * 1024 * 1024, f"peak {peak / 1024 / 1024:.1f} MB grows with the number of samples"
