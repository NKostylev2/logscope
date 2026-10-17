import csv
import datetime as dt
import io
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator

# Common timestamp patterns
ISO_PATTERN = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,9})?(?:Z|[+-]\d{2}:?\d{2}| UTC| GMT)?)"
)
EU_PATTERN = re.compile(
    r"(?P<ts>\d{2}[/.-]\d{2}[/.-]\d{4}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)"
)

# Recognized log levels
LEVEL_RE = re.compile(r"\b(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\b", re.I)

# Request ID / Correlation ID patterns
REQUEST_RE = re.compile(
    r"\b(?:request[_ -]?id|correlation[_ -]?id|trace[_ -]?id|x-request-id|req[_ -]?id)\s*[=:]\s*([A-Za-z0-9._:-]+)",
    re.I,
)

# Stack trace / continuation line indicators
STACK_TRACE_LINE_RE = re.compile(
    r"^(\s+at\s+[\w$./]+|\s+Caused by:|\s+\.\.\.\s+\d+\s+more|\s+org\.|\s+com\.|\s+io\.|\s+net\.|\s+java\.)"
)

# Valid epoch range: 2000-01-01 to 2100-01-01 in UTC milliseconds
MIN_EPOCH_MS = 946_684_800_000
MAX_EPOCH_MS = 4_102_444_800_000


def parse_epoch(val: str | int | float) -> int | None:
    """Safely parse epoch timestamps (seconds, ms, us, ns).
    Rejects arbitrary integers outside the 2000-2100 epoch range.
    """
    if val is None:
        return None
    try:
        if isinstance(val, (int, float)):
            num = float(val)
        else:
            s = str(val).strip()
            if not s:
                return None
            num = float(s)
    except (ValueError, TypeError, OverflowError):
        return None

    # Determine scale based on absolute value
    if MIN_EPOCH_MS // 1000 <= num <= MAX_EPOCH_MS // 1000:
        # Seconds
        ms = int(round(num * 1000))
    elif MIN_EPOCH_MS <= num <= MAX_EPOCH_MS:
        # Milliseconds
        ms = int(round(num))
    elif MIN_EPOCH_MS * 1000 <= num <= MAX_EPOCH_MS * 1000:
        # Microseconds
        ms = int(round(num / 1000))
    elif MIN_EPOCH_MS * 1_000_000 <= num <= MAX_EPOCH_MS * 1_000_000:
        # Nanoseconds
        ms = int(round(num / 1_000_000))
    else:
        return None

    return ms if MIN_EPOCH_MS <= ms <= MAX_EPOCH_MS else None


def parse_timestamp(value: str) -> int | None:
    """Parse a timestamp string into UTC milliseconds.
    Supports ISO 8601, Docker timestamps with nanoseconds, dates with ms,
    and epoch seconds/ms/us/ns.
    Naive timestamps without explicit timezone are treated as local wall-clock
    time (converted to epoch ms via local timezone) rather than silently assuming UTC.
    """
    if not value or not isinstance(value, str):
        return None
    val = value.strip()
    if not val:
        return None

    # Check for pure epoch number
    epoch = parse_epoch(val)
    if epoch is not None:
        return epoch

    # Normalize separators
    val_norm = val.replace(",", ".")
    # Normalize UTC/GMT suffix to +00:00
    if val_norm.endswith(" UTC") or val_norm.endswith(" GMT"):
        val_norm = val_norm[:-4].strip() + "+00:00"

    # Try ISO fromisoformat
    try:
        iso_str = val_norm.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(iso_str)
        if parsed.tzinfo is not None:
            return int(parsed.timestamp() * 1000)
        # Naive: treat as local wall-clock time
        return int(parsed.astimezone().timestamp() * 1000)
    except (ValueError, TypeError):
        pass

    # Try matching regexes
    for pattern, is_eu in [(ISO_PATTERN, False), (EU_PATTERN, True)]:
        match = pattern.search(val_norm)
        if match:
            raw = match.group("ts")
            raw_clean = raw.replace("Z", "+00:00")
            if is_eu:
                try:
                    # European DD/MM/YYYY or DD.MM.YYYY
                    delim = "/" if "/" in raw_clean else ("." if "." in raw_clean[:10] else "-")
                    fmt = f"%d{delim}%m{delim}%Y %H:%M:%S"
                    base_str = raw_clean[:19]
                    parsed = dt.datetime.strptime(base_str, fmt)
                    fraction = raw_clean[20:] if len(raw_clean) > 19 else ""
                    ms = int((fraction + "000")[:3]) if fraction else 0
                    parsed = parsed.replace(microsecond=ms * 1000)
                    return int(parsed.astimezone().timestamp() * 1000)
                except ValueError:
                    continue
            else:
                try:
                    parsed = dt.datetime.fromisoformat(raw_clean)
                    if parsed.tzinfo is not None:
                        return int(parsed.timestamp() * 1000)
                    return int(parsed.astimezone().timestamp() * 1000)
                except ValueError:
                    continue

    return None


def extract_timestamp(text: str) -> tuple[int | None, str | None]:
    """Extract a timestamp from a log line.
    Returns (timestamp_utc_ms, raw_timestamp_substring).
    """
    if not text:
        return None, None

    # First check ISO / RFC 3339 pattern
    match = ISO_PATTERN.search(text)
    if match:
        raw = match.group("ts")
        ts_ms = parse_timestamp(raw)
        if ts_ms is not None:
            return ts_ms, raw

    # Check EU pattern
    match = EU_PATTERN.search(text)
    if match:
        raw = match.group("ts")
        ts_ms = parse_timestamp(raw)
        if ts_ms is not None:
            return ts_ms, raw

    # Check leading epoch timestamp (e.g. at start of line)
    leading_epoch = re.match(r"^(\d{10,19})\b", text)
    if leading_epoch:
        raw = leading_epoch.group(1)
        ts_ms = parse_epoch(raw)
        if ts_ms is not None:
            return ts_ms, raw

    return None, None


def extract_level(message: str) -> str | None:
    """Extract log level. Standardizes WARNING -> WARN.
    Returns None if no recognized level is present (never defaults to INFO).
    """
    match = LEVEL_RE.search(message)
    if not match:
        return None
    val = match.group(1).upper()
    if val == "WARNING":
        return "WARN"
    if val == "CRITICAL":
        return "FATAL"
    return val


def extract_request_id(message: str) -> str | None:
    """Extract request/correlation/trace ID from text if present.
    Returns None if absent.
    """
    match = REQUEST_RE.search(message)
    return match.group(1) if match else None


def open_text_stream(path: Path):
    """Open a file with encoding fallback (utf-8-sig -> cp1251 -> latin-1)."""
    encodings = ["utf-8-sig", "cp1251", "latin-1"]
    for enc in encodings:
        try:
            with path.open("r", encoding=enc) as f:
                f.read(4096)
            return path.open("r", encoding=enc, errors="replace")
        except (UnicodeDecodeError, OSError):
            continue
    return path.open("r", encoding="utf-8-sig", errors="replace")


def parse_text_file(path: Path) -> Iterator[dict]:
    """Streamingly parse plain text / log files with multi-line message merging.
    Continuation lines (Java stack traces, indented lines, lines without timestamps)
    are merged into the preceding event while tracking exact line_start and line_end.
    """
    stream = open_text_stream(path)
    current_event = None
    line_no = 0

    try:
        for raw_line in stream:
            line_no += 1
            line = raw_line.rstrip("\r\n")

            if not line.strip():
                if current_event:
                    current_event["message_lines"].append(line)
                    current_event["line_end"] = line_no
                continue

            ts_ms, raw_ts = extract_timestamp(line)
            is_continuation = False

            if current_event is not None:
                if ts_ms is None:
                    # Line has no timestamp - likely continuation of current event
                    is_continuation = True
                elif STACK_TRACE_LINE_RE.match(line):
                    # Explicit stack trace marker
                    is_continuation = True
                elif len(current_event["message_lines"]) > 100:
                    # Guard against runaway memory if file has huge blocks
                    is_continuation = False

            if is_continuation and current_event is not None:
                current_event["message_lines"].append(line)
                current_event["line_end"] = line_no
                # Also check if request_id appears in continuation lines
                if not current_event["request_id"]:
                    current_event["request_id"] = extract_request_id(line)
            else:
                # Flush existing event
                if current_event is not None:
                    msg = "\n".join(current_event["message_lines"])
                    yield {
                        "timestamp_utc_ms": current_event["timestamp_utc_ms"],
                        "timestamp_ms": current_event["timestamp_utc_ms"],
                        "timestamp_raw": current_event["timestamp_raw"],
                        "level": current_event["level"],
                        "message": msg,
                        "line_start": current_event["line_start"],
                        "line_end": current_event["line_end"],
                        "request_id": current_event["request_id"],
                        "duration_ms": None,
                        "event_type": "log",
                        "fields_json": "{}",
                    }

                # Start new event
                lvl = extract_level(line)
                req_id = extract_request_id(line)
                current_event = {
                    "timestamp_utc_ms": ts_ms,
                    "timestamp_raw": raw_ts,
                    "level": lvl,
                    "message_lines": [line],
                    "line_start": line_no,
                    "line_end": line_no,
                    "request_id": req_id,
                }

        # Flush final event
        if current_event is not None:
            msg = "\n".join(current_event["message_lines"])
            yield {
                "timestamp_utc_ms": current_event["timestamp_utc_ms"],
                "timestamp_ms": current_event["timestamp_utc_ms"],
                "timestamp_raw": current_event["timestamp_raw"],
                "level": current_event["level"],
                "message": msg,
                "line_start": current_event["line_start"],
                "line_end": current_event["line_end"],
                "request_id": current_event["request_id"],
                "duration_ms": None,
                "event_type": "log",
                "fields_json": "{}",
            }
    finally:
        stream.close()


def parse_csv_file(path: Path) -> Iterator[dict]:
    """Streamingly parse CSV files (JMeter CSV JTL or Monitoring CSV) by headers."""
    stream = open_text_stream(path)
    try:
        sample = stream.read(8192)
        stream.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel

        reader = csv.DictReader(stream, dialect=dialect)
        if not reader.fieldnames:
            return

        # Case-insensitive map of headers
        names = {name.lower().strip(): name for name in reader.fieldnames if name}

        # Check if JMeter CSV JTL
        is_jmeter = "timestamp" in names and ("elapsed" in names or "label" in names or "responsecode" in names)
        
        # Locate timestamp header
        time_key = None
        for candidate in ("timestamp", "timestamp_ms", "time", "datetime", "date_time", "start_time", "epoch", "ts"):
            if candidate in names:
                time_key = names[candidate]
                break
        if not time_key:
            for lowered, orig in names.items():
                if "timestamp" in lowered or lowered == "time":
                    time_key = orig
                    break

        line_no = 1
        for row in reader:
            line_no += 1
            raw_ts = (row.get(time_key) or "").strip() if time_key else ""
            ts_ms = parse_timestamp(raw_ts)

            if is_jmeter:
                # JMeter CSV JTL columns
                label = row.get(names.get("label", "label"), "")
                elapsed_str = row.get(names.get("elapsed", "elapsed"), "")
                rc = row.get(names.get("responsecode", "responseCode"), "")
                rm = row.get(names.get("responsemessage", "responseMessage"), "")
                success_val = str(row.get(names.get("success", "success"), "")).strip().lower()
                thread_name = row.get(names.get("threadname", "threadName"), "")
                latency = row.get(names.get("latency", "Latency"), "")
                connect = row.get(names.get("connect", "Connect"), "")
                url = row.get(names.get("url", "URL"), "")
                failure_msg = row.get(names.get("failuremessage", "failureMessage"), "")
                bytes_in = row.get(names.get("bytes", "bytes"), "")
                bytes_out = row.get(names.get("sentbytes", "sentBytes"), "")
                grp_threads = row.get(names.get("grpthreads", "grpThreads"), "")
                all_threads = row.get(names.get("allthreads", "allThreads"), "")

                # Elapsed duration
                duration_ms = None
                if elapsed_str and elapsed_str.isdigit():
                    duration_ms = int(elapsed_str)

                # Determine level: false or rc >= 400 is ERROR, true is INFO
                is_failed = success_val in ("false", "0")
                if not is_failed and rc.isdigit() and int(rc) >= 400:
                    is_failed = True

                if success_val in ("false", "true", "0", "1"):
                    level = "ERROR" if is_failed else "INFO"
                else:
                    level = "ERROR" if is_failed else None

                # Build readable message
                parts = [f"[{rc} {rm}]".strip(), label]
                if duration_ms is not None:
                    parts.append(f"({duration_ms}ms)")
                if latency:
                    parts.append(f"Latency: {latency}ms")
                if failure_msg:
                    parts.append(f"Failure: {failure_msg}")
                message = " ".join(parts).strip() or "JMeter Sample"

                # Fields dict
                fields = {
                    "label": label,
                    "elapsed": duration_ms,
                    "responseCode": rc,
                    "responseMessage": rm,
                    "success": not is_failed if success_val else None,
                    "threadName": thread_name,
                    "Latency": int(latency) if latency and latency.isdigit() else latency,
                    "Connect": int(connect) if connect and connect.isdigit() else connect,
                    "URL": url,
                    "bytes": int(bytes_in) if bytes_in and bytes_in.isdigit() else bytes_in,
                    "sentBytes": int(bytes_out) if bytes_out and bytes_out.isdigit() else bytes_out,
                    "grpThreads": int(grp_threads) if grp_threads and grp_threads.isdigit() else grp_threads,
                    "allThreads": int(all_threads) if all_threads and all_threads.isdigit() else all_threads,
                    "failureMessage": failure_msg,
                }
                # Clean up empty keys
                fields = {k: v for k, v in fields.items() if v not in ("", None)}

                rid = extract_request_id(message) or extract_request_id(url)
                yield {
                    "timestamp_utc_ms": ts_ms,
                    "timestamp_ms": ts_ms,
                    "timestamp_raw": raw_ts or None,
                    "level": level,
                    "message": message,
                    "line_start": line_no,
                    "line_end": line_no,
                    "request_id": rid,
                    "duration_ms": duration_ms,
                    "event_type": "jmeter_sample",
                    "fields_json": json.dumps(fields, ensure_ascii=False),
                }
            else:
                # Generic / Monitoring CSV
                msg_parts = [f"{k}={v}" for k, v in row.items() if k and v is not None and k != time_key]
                message = " | ".join(msg_parts)
                level = extract_level(message)
                rid = extract_request_id(message)

                # Duration if present
                duration_ms = None
                for d_cand in ("duration", "duration_ms", "elapsed"):
                    if d_cand in names:
                        val = row.get(names[d_cand])
                        if val and val.isdigit():
                            duration_ms = int(val)
                            break

                fields = {k: v for k, v in row.items() if k and v is not None}
                yield {
                    "timestamp_utc_ms": ts_ms,
                    "timestamp_ms": ts_ms,
                    "timestamp_raw": raw_ts or None,
                    "level": level,
                    "message": message,
                    "line_start": line_no,
                    "line_end": line_no,
                    "request_id": rid,
                    "duration_ms": duration_ms,
                    "event_type": "metric" if any("cpu" in k or "mem" in k or "usage" in k for k in names) else "csv_row",
                    "fields_json": json.dumps(fields, ensure_ascii=False),
                }
    finally:
        stream.close()


def parse_xml_jtl(path: Path) -> Iterator[dict]:
    """Streamingly parse JMeter XML JTL files using ElementTree.iterparse.
    Maintains O(1) memory by clearing elements as they are yielded.
    Handles <httpSample> and <sample> elements, extracting latency, elapsed,
    response code, success, failure messages, and thread names.
    """
    stream = open_text_stream(path)
    sample_index = 0
    try:
        # iterparse takes a byte stream or file object
        context = ET.iterparse(stream, events=("end",))
        for event, elem in context:
            tag = elem.tag
            if tag in ("httpSample", "sample"):
                sample_index += 1
                attrib = elem.attrib

                # Timestamp attribute: ts or timeStamp
                raw_ts = attrib.get("ts") or attrib.get("timeStamp") or ""
                ts_ms = parse_timestamp(raw_ts)

                # Elapsed time
                elapsed_str = attrib.get("t") or attrib.get("elapsed") or ""
                duration_ms = int(elapsed_str) if elapsed_str.isdigit() else None

                # Response code and message
                rc = attrib.get("rc") or attrib.get("responseCode") or ""
                rm = attrib.get("rm") or attrib.get("responseMessage") or ""

                # Success
                s_val = (attrib.get("s") or attrib.get("success") or "").strip().lower()
                is_failed = s_val in ("false", "0")
                if not is_failed and rc.isdigit() and int(rc) >= 400:
                    is_failed = True

                level = "ERROR" if is_failed else ("INFO" if s_val in ("true", "1") else None)

                # Other attributes
                label = attrib.get("lb") or attrib.get("label") or ""
                thread_name = attrib.get("tn") or attrib.get("threadName") or ""
                latency = attrib.get("lt") or attrib.get("Latency") or ""
                connect = attrib.get("ct") or attrib.get("Connect") or ""
                bytes_in = attrib.get("by") or attrib.get("bytes") or ""
                bytes_out = attrib.get("sby") or attrib.get("sentBytes") or ""
                grp_threads = attrib.get("ng") or attrib.get("grpThreads") or ""
                all_threads = attrib.get("na") or attrib.get("allThreads") or ""

                # Look for child elements (URL, assertion failures)
                url = ""
                failure_msg = ""
                url_elem = elem.find("java.net.URL")
                if url_elem is not None and url_elem.text:
                    url = url_elem.text.strip()
                for assertion in elem.findall("assertionResult"):
                    fail_elem = assertion.find("failureMessage")
                    if fail_elem is not None and fail_elem.text:
                        failure_msg = fail_elem.text.strip()
                        break

                # Build message
                parts = [f"[{rc} {rm}]".strip(), label]
                if duration_ms is not None:
                    parts.append(f"({duration_ms}ms)")
                if latency:
                    parts.append(f"Latency: {latency}ms")
                if failure_msg:
                    parts.append(f"Failure: {failure_msg}")
                message = " ".join(parts).strip() or "JMeter XML Sample"

                fields = {
                    "label": label,
                    "elapsed": duration_ms,
                    "responseCode": rc,
                    "responseMessage": rm,
                    "success": not is_failed if s_val else None,
                    "threadName": thread_name,
                    "Latency": int(latency) if latency.isdigit() else latency,
                    "Connect": int(connect) if connect.isdigit() else connect,
                    "URL": url,
                    "bytes": int(bytes_in) if bytes_in.isdigit() else bytes_in,
                    "sentBytes": int(bytes_out) if bytes_out.isdigit() else bytes_out,
                    "grpThreads": int(grp_threads) if grp_threads.isdigit() else grp_threads,
                    "allThreads": int(all_threads) if all_threads.isdigit() else all_threads,
                    "failureMessage": failure_msg,
                }
                fields = {k: v for k, v in fields.items() if v not in ("", None)}

                rid = extract_request_id(message) or extract_request_id(url)

                yield {
                    "timestamp_utc_ms": ts_ms,
                    "timestamp_ms": ts_ms,
                    "timestamp_raw": raw_ts or None,
                    "level": level,
                    "message": message,
                    "line_start": sample_index,
                    "line_end": sample_index,
                    "request_id": rid,
                    "duration_ms": duration_ms,
                    "event_type": "jmeter_sample",
                    "fields_json": json.dumps(fields, ensure_ascii=False),
                }

                # Clear element and previous siblings to free memory
                elem.clear()
    except (ET.ParseError, OSError):
        # Gracefully exit on corrupt XML or truncation
        return
    finally:
        stream.close()


def iter_file_events(path: Path) -> Iterator[dict]:
    """Identify file type and dispatch to the appropriate streaming parser."""
    suffix = path.suffix.lower()

    if suffix == ".xml":
        yield from parse_xml_jtl(path)
        return

    if suffix == ".csv":
        yield from parse_csv_file(path)
        return

    if suffix == ".jtl":
        # Check first line to distinguish XML vs CSV JTL
        is_xml = False
        try:
            with open_text_stream(path) as f:
                head = f.read(512).strip()
                if head.startswith("<?xml") or "<testResults" in head:
                    is_xml = True
        except OSError:
            pass

        if is_xml:
            yield from parse_xml_jtl(path)
        else:
            yield from parse_csv_file(path)
        return

    # Plain text / log files
    yield from parse_text_file(path)
