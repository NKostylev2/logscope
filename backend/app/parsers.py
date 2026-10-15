import csv
import datetime as dt
import io
import json
import re
from pathlib import Path
from typing import Iterator

TIMESTAMP_PATTERNS = [
    # 2026-10-15 10:23:41.250
    (re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)"), "%Y-%m-%d %H:%M:%S"),
    # 15/10/2026 10:23:41.250
    (re.compile(r"(?P<ts>\d{2}/\d{2}/\d{4}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)"), "%d/%m/%Y %H:%M:%S"),
]
LEVEL_RE = re.compile(r"\b(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\b", re.I)
REQUEST_RE = re.compile(r"\b(?:request[_ -]?id|correlation[_ -]?id|trace[_ -]?id)\s*[=:]\s*([A-Za-z0-9._:-]+)", re.I)

def parse_timestamp(value: str):
    value = value.strip().replace(",", ".")
    # ISO timestamp, optionally timezone-aware.
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return int(parsed.timestamp() * 1000)
        # Naive timestamps are deliberately treated as local wall-clock time.
        return int(parsed.timestamp() * 1000)
    except ValueError:
        pass
    for regex, fmt in TIMESTAMP_PATTERNS:
        match = regex.search(value)
        if match:
            raw = match.group("ts").replace(",", ".")
            try:
                parsed = dt.datetime.strptime(raw[:19], fmt)
                fraction = raw[20:] if len(raw) > 19 else ""
                ms = int((fraction + "000")[:3]) if fraction else 0
                parsed = parsed.replace(microsecond=ms * 1000)
                return int(parsed.timestamp() * 1000)
            except ValueError:
                continue
    # JTL timestamps may be epoch milliseconds.
    if value.isdigit() and len(value) >= 13:
        try:
            return int(value[:13])
        except ValueError:
            return None
    return None

def _level(message: str):
    match = LEVEL_RE.search(message)
    if not match:
        return None
    value = match.group(1).upper()
    return "WARN" if value == "WARNING" else value

def _request_id(message: str):
    match = REQUEST_RE.search(message)
    return match.group(1) if match else None

def parse_text_file(path: Path) -> Iterator[dict]:
    # Keep the first-slice parser intentionally simple: one non-empty line = one event.
    # A future multiline parser will group stack traces under their leading timestamp.
    with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
        for line_no, line in enumerate(stream, 1):
            message = line.rstrip("\r\n")
            if not message.strip():
                continue
            yield {
                "timestamp_ms": parse_timestamp(message),
                "timestamp_raw": _extract_timestamp(message),
                "level": _level(message),
                "message": message,
                "line_start": line_no,
                "line_end": line_no,
                "request_id": _request_id(message),
                "fields_json": "{}",
            }

def _extract_timestamp(value: str):
    for regex, _ in TIMESTAMP_PATTERNS:
        match = regex.search(value)
        if match:
            return match.group("ts")
    # ISO-like values
    match = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})?", value)
    return match.group(0) if match else None

def parse_csv_file(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
        sample = stream.read(8192)
        stream.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(stream, dialect=dialect)
        if not reader.fieldnames:
            return
        names = {name.lower().strip(): name for name in reader.fieldnames if name}
        time_key = next((names[k] for k in (
            "timestamp", "timestamp_ms", "time", "datetime", "date_time", "start_time", "time_stamp"
        ) if k in names), None)
        # JMeter CSV JTL commonly uses timeStamp.
        if time_key is None:
            time_key = next((original for lowered, original in names.items() if "timestamp" in lowered or lowered == "time"), None)
        line_no = 1
        for row in reader:
            line_no += 1
            raw_ts = (row.get(time_key) or "").strip() if time_key else ""
            timestamp_ms = parse_timestamp(raw_ts)
            if timestamp_ms is None and raw_ts.isdigit() and len(raw_ts) >= 13:
                timestamp_ms = int(raw_ts[:13])
            message = " | ".join(f"{k}={v}" for k, v in row.items() if k and v is not None)
            level = _level(message)
            rid = _request_id(message)
            fields = {}
            for key in ("label", "responseCode", "responseMessage", "success", "threadName", "elapsed", "Latency", "Connect", "URL", "url"):
                if key in row:
                    fields[key] = row[key]
            yield {
                "timestamp_ms": timestamp_ms,
                "timestamp_raw": raw_ts or None,
                "level": level,
                "message": message,
                "line_start": line_no,
                "line_end": line_no,
                "request_id": rid,
                "fields_json": json.dumps(fields, ensure_ascii=False),
            }

def iter_file_events(path: Path):
    if path.suffix.lower() == ".csv" or (path.suffix.lower() == ".jtl" and _looks_csv(path)):
        yield from parse_csv_file(path)
    else:
        yield from parse_text_file(path)

def _looks_csv(path: Path):
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as f:
            head = f.readline().lower()
        return "timestamp" in head and "," in head
    except OSError:
        return False
