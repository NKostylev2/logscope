import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from .config import ALLOWED_ROOT
from .db import init_db, transaction
from .importer import import_folder, import_manager, is_within_root
from .parsers import is_xml_source, open_text_stream


@asynccontextmanager
async def lifespan(app: FastAPI):
    ALLOWED_ROOT.mkdir(parents=True, exist_ok=True)
    init_db()
    yield


app = FastAPI(title="LogScope MVP", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8765", "http://localhost:8765"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/", response_class=HTMLResponse)
def index():
    html_path = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
    return html_path.read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    return {"status": "ok", "version": app.version}


@app.get("/api/sources")
def sources():
    with transaction() as conn:
        rows = conn.execute("SELECT * FROM sources ORDER BY name").fetchall()
    return [dict(row) for row in rows]


@app.get("/api/sources/{source_id}")
def source_detail(source_id: int):
    with transaction() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Источник не найден")
    return dict(row)


@app.post("/api/import/path")
def import_path(payload: dict):
    folder = payload.get("path")
    sync = bool(payload.get("sync", False))

    if not isinstance(folder, str) or not folder.strip():
        raise HTTPException(400, "Нужно указать путь к папке")

    if sync:
        try:
            return import_folder(folder)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except OSError as exc:
            raise HTTPException(400, f"Ошибка чтения: {exc}")
    else:
        # Background import
        try:
            return import_manager.start(folder)
        except ValueError as exc:
            raise HTTPException(400, str(exc))


@app.post("/api/import/start")
def import_start(payload: dict):
    folder = payload.get("path")
    if not isinstance(folder, str) or not folder.strip():
        raise HTTPException(400, "Нужно указать путь к папке")
    try:
        return import_manager.start(folder)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/import/status")
def import_status():
    return import_manager.get_status()


@app.post("/api/import/cancel")
def import_cancel():
    return import_manager.cancel()


def build_level_clause(levels: Optional[str] = None, level: Optional[str] = None) -> tuple[Optional[str], list]:
    # Levels are stored upper-case by the parsers, so the column is compared as is:
    # wrapping it in UPPER() would turn the idx_events_level_time seek into a full scan.
    raw = levels if (levels is not None and levels.strip() != "") else level
    if not raw or not raw.strip():
        return None, []

    tokens = [x.strip() for x in raw.split(",") if x.strip()]
    if not tokens:
        return None, []

    named_levels = set()
    has_none = False

    for t in tokens:
        up = t.upper()
        if up in ("NONE", "UNKNOWN", "NULL"):
            has_none = True
        elif up == "ERROR":
            named_levels.update(["ERROR", "FATAL", "CRITICAL"])
        elif up == "WARN":
            named_levels.update(["WARN", "WARNING"])
        elif up == "INFO":
            named_levels.add("INFO")
        elif up == "DEBUG":
            named_levels.update(["DEBUG", "TRACE"])
        else:
            named_levels.add(up)

    if named_levels and has_none:
        ph = ",".join("?" for _ in named_levels)
        return f"(e.level IN ({ph}) OR e.level IS NULL)", sorted(named_levels)
    elif named_levels:
        ph = ",".join("?" for _ in named_levels)
        return f"e.level IN ({ph})", sorted(named_levels)
    elif has_none:
        return "e.level IS NULL", []
    return None, []


def build_text_clause(q: Optional[str]) -> tuple[Optional[str], list]:
    """Substring search in the message.

    ASCII queries keep using the native (fast) LIKE. SQLite folds case for ASCII only,
    so a query that contains any other character goes through ucontains(), which uses
    str.casefold(): 'ошибка' now finds 'Ошибка' and 'ОШИБКА'.
    """
    if not q:
        return None, []
    if q.isascii():
        safe_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return "e.message LIKE ? ESCAPE '\\'", [f"%{safe_q}%"]
    return "ucontains(e.message, ?)", [q.casefold()]


def build_http_codes_clause(http_codes: Optional[str] = None) -> tuple[Optional[str], list]:
    if not http_codes or not http_codes.strip():
        return None, []

    tokens = [x.strip().lower() for x in http_codes.split(",") if x.strip()]
    if not tokens:
        return None, []

    if "none_selected" in tokens:
        return "1=0", []

    all_classes = {"2xx", "3xx", "4xx", "5xx"}
    if all_classes.issubset(set(tokens)) and not any(t.isdigit() for t in tokens):
        return None, []

    sub_clauses = []
    params = []

    for token in tokens:
        if token in ("2xx", "3xx", "4xx", "5xx"):
            digit = int(token[0])
            sub_clauses.append("(e.http_code >= ? AND e.http_code <= ?)")
            params.extend([digit * 100, digit * 100 + 99])
        elif token.isdigit():
            code_num = int(token)
            sub_clauses.append("e.http_code = ?")
            params.append(code_num)

    if not sub_clauses:
        return "1=0", []

    combined = "(" + " OR ".join(sub_clauses) + ")"
    return combined, params


@app.get("/api/events")
def events(
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    source_id: Optional[int] = None,
    source_ids: Optional[str] = None,
    level: Optional[str] = None,
    levels: Optional[str] = None,
    http_codes: Optional[str] = None,
    q: Optional[str] = Query(default=None, max_length=500),
    request_id: Optional[str] = Query(default=None, max_length=256),
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
):
    clauses, params = [], []

    # Timestamp boundaries: start_ms <= timestamp < end_ms
    if start_ms is not None:
        clauses.append("e.timestamp_utc_ms >= ?")
        params.append(start_ms)
    if end_ms is not None:
        clauses.append("e.timestamp_utc_ms < ?")
        params.append(end_ms)

    # Multi-source selection support
    if source_ids:
        try:
            s_ids = [int(x.strip()) for x in source_ids.split(",") if x.strip()]
            if s_ids:
                placeholders = ",".join("?" for _ in s_ids)
                clauses.append(f"e.source_id IN ({placeholders})")
                params.extend(s_ids)
        except ValueError:
            raise HTTPException(400, "Некорректный формат source_ids (ожидаются числа через запятую)")
    elif source_id is not None:
        clauses.append("e.source_id = ?")
        params.append(source_id)

    # Level filter (single or multi-level with checkboxes)
    lvl_sql, lvl_params = build_level_clause(levels, level)
    if lvl_sql:
        clauses.append(lvl_sql)
        params.extend(lvl_params)

    # HTTP status code filter
    http_sql, http_params = build_http_codes_clause(http_codes)
    if http_sql:
        clauses.append(http_sql)
        params.extend(http_params)

    # Text search
    text_sql, text_params = build_text_clause(q)
    if text_sql:
        clauses.append(text_sql)
        params.extend(text_params)

    # Request ID filter
    if request_id:
        clauses.append("e.request_id = ?")
        params.append(request_id.strip())

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with transaction() as conn:
        # Deterministic sorting
        rows = conn.execute(f"""
            SELECT e.*, s.name AS source_name
            FROM events e
            JOIN sources s ON s.id = e.source_id
            {where}
            ORDER BY e.timestamp_utc_ms IS NULL, e.timestamp_utc_ms, e.source_id, e.line_start, e.id
            LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()

        if len(rows) < limit and (offset == 0 or rows):
            # The page is not full, so the total is already known. This skips a second
            # full scan, which matters for selective searches on large indexes.
            total = offset + len(rows)
        else:
            total = conn.execute(f"SELECT COUNT(*) FROM events e {where}", params).fetchone()[0]

    items = []
    for r in rows:
        d = dict(r)
        d["timestamp_ms"] = d.get("timestamp_utc_ms")
        items.append(d)

    return {"total": total, "limit": limit, "offset": offset, "items": items}


@app.get("/api/events/slice")
def events_slice(
    timestamp_ms: int = Query(..., description="Целевая временная метка (UTC ms)"),
    source_ids: Optional[str] = Query(default=None, description="Список ID источников через запятую"),
    step_source_id: Optional[int] = Query(default=None, description="ID выбранного источника для шага по событиям"),
    max_drift_ms: Optional[int] = Query(default=None, description="Максимальное отклонение в мс"),
    level: Optional[str] = Query(default=None, description="Фильтр по уровню логирования"),
    levels: Optional[str] = Query(default=None, description="Список уровней логирования через запятую"),
    http_codes: Optional[str] = Query(default=None, description="Список кодов или классов HTTP ответов через запятую"),
    q: Optional[str] = Query(default=None, max_length=500, description="Полнотекстовый поиск"),
    request_id: Optional[str] = Query(default=None, max_length=256, description="Request / Trace ID"),
):
    """Синхронный срез состояния: возвращает ровно по одной (наиболее близкой)
    записи для каждого источника на момент timestamp_ms с учётом активных фильтров.
    """
    extra_clauses = []
    extra_params = []

    lvl_sql, lvl_params = build_level_clause(levels, level)
    if lvl_sql:
        extra_clauses.append(lvl_sql)
        extra_params.extend(lvl_params)

    http_sql, http_params = build_http_codes_clause(http_codes)
    if http_sql:
        extra_clauses.append(http_sql)
        extra_params.extend(http_params)

    text_sql, text_params = build_text_clause(q)
    if text_sql:
        extra_clauses.append(text_sql)
        extra_params.extend(text_params)

    if request_id:
        extra_clauses.append("e.request_id = ?")
        extra_params.append(request_id.strip())

    filter_sql = (" AND " + " AND ".join(extra_clauses)) if extra_clauses else ""
    has_active_filters = bool(extra_clauses)

    with transaction() as conn:
        # Determine source list
        s_ids = None
        if source_ids:
            try:
                s_ids = [int(x.strip()) for x in source_ids.split(",") if x.strip()]
                if s_ids:
                    placeholders = ",".join("?" for _ in s_ids)
                    src_rows = conn.execute(
                        f"SELECT id, name, path FROM sources WHERE id IN ({placeholders}) ORDER BY name",
                        s_ids
                    ).fetchall()
                else:
                    src_rows = []
            except ValueError:
                raise HTTPException(400, "Некорректный формат source_ids")
        else:
            src_rows = conn.execute(
                "SELECT id, name, path FROM sources WHERE event_count > 0 ORDER BY name"
            ).fetchall()

        items = []
        for src in src_rows:
            sid = src["id"]
            # 1. Nearest event <= target timestamp matching filters
            before = conn.execute(f"""
                SELECT e.*, s.name AS source_name, s.path AS source_path
                FROM events e
                JOIN sources s ON s.id = e.source_id
                WHERE e.source_id = ? AND e.timestamp_utc_ms <= ? {filter_sql}
                ORDER BY e.timestamp_utc_ms DESC, e.id DESC
                LIMIT 1
            """, [sid, timestamp_ms] + extra_params).fetchone()

            # 2. Nearest event > target timestamp matching filters
            after = conn.execute(f"""
                SELECT e.*, s.name AS source_name, s.path AS source_path
                FROM events e
                JOIN sources s ON s.id = e.source_id
                WHERE e.source_id = ? AND e.timestamp_utc_ms > ? {filter_sql}
                ORDER BY e.timestamp_utc_ms ASC, e.id ASC
                LIMIT 1
            """, [sid, timestamp_ms] + extra_params).fetchone()

            candidate = None
            if before and after:
                diff_b = abs(before["timestamp_utc_ms"] - timestamp_ms)
                diff_a = abs(after["timestamp_utc_ms"] - timestamp_ms)
                candidate = before if diff_b <= diff_a else after
            elif before:
                candidate = before
            elif after:
                candidate = after

            if candidate:
                d = dict(candidate)
                d["event_id"] = d.get("id")
                d["timestamp_ms"] = d.get("timestamp_utc_ms")
                delta_ms = d["timestamp_utc_ms"] - timestamp_ms
                if max_drift_ms is not None and abs(delta_ms) > max_drift_ms:
                    continue
                d["delta_ms"] = delta_ms
                d["is_exact"] = (delta_ms == 0)
                try:
                    d["fields_json"] = json.loads(d.get("fields_json") or "{}")
                except Exception:
                    d["fields_json"] = {}
                items.append(d)
            elif not has_active_filters:
                # Only show empty sources if no filters are applied
                items.append({
                    "source_id": sid,
                    "source_name": src["name"],
                    "source_path": src["path"],
                    "event_id": None,
                    "timestamp_utc_ms": None,
                    "timestamp_ms": None,
                    "delta_ms": None,
                    "is_exact": False,
                    "level": None,
                    "message": "Нет событий с временной меткой в данном источнике",
                    "line_start": None,
                    "line_end": None,
                    "request_id": None,
                    "duration_ms": None,
                    "event_type": None,
                    "fields_json": {},
                })

        # Calculate previous and next distinct timestamps matching filters and sources for stepping
        step_where = []
        step_params_base = []
        if step_source_id is not None:
            step_where.append("e.source_id = ?")
            step_params_base.append(step_source_id)
        elif s_ids and len(s_ids) == 1:
            step_where.append("e.source_id = ?")
            step_params_base.append(s_ids[0])
        elif s_ids:
            step_where.append(f"e.source_id IN ({','.join('?' for _ in s_ids)})")
            step_params_base.extend(s_ids)
        if extra_clauses:
            step_where.append(" AND ".join(extra_clauses))
            step_params_base.extend(extra_params)

        step_extra_sql = (" AND " + " AND ".join(step_where)) if step_where else ""

        try:
            prev_row = conn.execute(f"""
                SELECT MAX(e.timestamp_utc_ms) AS prev_ts
                FROM events e
                WHERE e.timestamp_utc_ms < ? {step_extra_sql}
            """, [timestamp_ms] + step_params_base).fetchone()
            next_row = conn.execute(f"""
                SELECT MIN(e.timestamp_utc_ms) AS next_ts
                FROM events e
                WHERE e.timestamp_utc_ms > ? {step_extra_sql}
            """, [timestamp_ms] + step_params_base).fetchone()

            prev_ts = prev_row["prev_ts"] if prev_row else None
            next_ts = next_row["next_ts"] if next_row else None
        except Exception:
            prev_ts = None
            next_ts = None

    # Sort items: exact matches first, then by absolute delta_ms ascending, then by source name
    items.sort(key=lambda x: (
        0 if x.get("is_exact") else (1 if x.get("delta_ms") is not None else 2),
        abs(x["delta_ms"]) if x.get("delta_ms") is not None else 9999999999,
        x.get("source_name", "")
    ))

    return {
        "target_timestamp_ms": timestamp_ms,
        "step_source_id": step_source_id,
        "prev_timestamp_ms": prev_ts,
        "next_timestamp_ms": next_ts,
        "total_sources": len(items),
        "items": items,
    }


@app.get("/api/events/{event_id}")
def event_detail(event_id: int):
    with transaction() as conn:
        row = conn.execute("""
            SELECT e.*, s.name AS source_name, s.path AS source_path
            FROM events e
            JOIN sources s ON s.id = e.source_id
            WHERE e.id = ?
        """, (event_id,)).fetchone()

    if row is None:
        raise HTTPException(404, "Событие не найдено")

    result = dict(row)
    result["timestamp_ms"] = result.get("timestamp_utc_ms")
    try:
        result["fields_json"] = json.loads(result["fields_json"] or "{}")
    except Exception:
        result["fields_json"] = {}
    return result


@app.get("/api/events/{event_id}/context")
def event_context(event_id: int, lines: int = Query(default=10, ge=1, le=50)):
    """Retrieve surrounding lines from the original source file for context."""
    with transaction() as conn:
        row = conn.execute("""
            SELECT e.id, e.line_start, e.line_end, s.path AS source_path, s.name AS source_name
            FROM events e
            JOIN sources s ON s.id = e.source_id
            WHERE e.id = ?
        """, (event_id,)).fetchone()

    if row is None:
        raise HTTPException(404, "Событие не найдено")

    source_path = Path(row["source_path"])
    root = ALLOWED_ROOT.resolve()

    if not is_within_root(source_path, root):
        raise HTTPException(403, "Доступ к файлу запрещён (выход за разрешённый каталог)")

    if not source_path.exists() or not source_path.is_file():
        raise HTTPException(404, "Исходный файл не найден на диске")

    if is_xml_source(source_path):
        # For XML JTL line_start/line_end hold the sample ordinal, not a file line,
        # so a line-based context would show unrelated text.
        raise HTTPException(422, "Построчный контекст недоступен для XML JTL")

    target_start = row["line_start"]
    target_end = row["line_end"]
    context_start = max(1, target_start - lines)
    context_end = target_end + lines

    result_lines = []
    current_line = 0

    try:
        with open_text_stream(source_path) as stream:
            for raw_line in stream:
                current_line += 1
                if current_line < context_start:
                    continue
                if current_line > context_end:
                    break
                result_lines.append({
                    "line_no": current_line,
                    "text": raw_line.rstrip("\r\n"),
                    "is_target": target_start <= current_line <= target_end,
                })
    except OSError as exc:
        raise HTTPException(500, f"Ошибка чтения файла: {exc}")

    return {
        "event_id": event_id,
        "source_name": row["source_name"],
        "source_path": str(source_path),
        "target_start": target_start,
        "target_end": target_end,
        "context_start": context_start,
        "context_end": current_line,
        "lines": result_lines,
    }


@app.get("/api/correlations/{request_id}")
def correlations(request_id: str):
    """Retrieve all events related to the given Request ID across all sources."""
    rid = request_id.strip()
    if not rid:
        raise HTTPException(400, "Request ID не может быть пустым")

    with transaction() as conn:
        rows = conn.execute("""
            SELECT e.*, s.name AS source_name
            FROM events e
            JOIN sources s ON s.id = e.source_id
            WHERE e.request_id = ?
            ORDER BY e.timestamp_utc_ms IS NULL, e.timestamp_utc_ms, e.source_id, e.line_start, e.id
        """, (rid,)).fetchall()

    items = []
    sources_involved = set()
    timestamps = []

    for r in rows:
        d = dict(r)
        d["timestamp_ms"] = d.get("timestamp_utc_ms")
        try:
            d["fields_json"] = json.loads(d.get("fields_json") or "{}")
        except Exception:
            pass
        items.append(d)
        sources_involved.add(d["source_name"])
        if d["timestamp_utc_ms"] is not None:
            timestamps.append(d["timestamp_utc_ms"])

    time_span_ms = (max(timestamps) - min(timestamps)) if len(timestamps) >= 2 else 0

    return {
        "request_id": rid,
        "total": len(items),
        "time_span_ms": time_span_ms,
        "sources": sorted(list(sources_involved)),
        "items": items,
    }


@app.get("/api/timeline")
def timeline(
    start_ms: int = Query(..., description="Начало временного диапазона в мс"),
    end_ms: int = Query(..., description="Конец временного диапазона в мс"),
    source_ids: Optional[str] = None,
    buckets: int = Query(default=60, ge=10, le=200),
):
    """Generate time-bucketed event counts per source for efficient UI timeline rendering."""
    if end_ms <= start_ms:
        raise HTTPException(400, "end_ms должен быть больше start_ms")

    # Ceiling division: with floor, the tail of the range spilled into extra buckets
    # (e.g. 63 buckets for buckets=60).
    bucket_width = max(1, -(-(end_ms - start_ms) // buckets))
    clauses = ["e.timestamp_utc_ms >= ?", "e.timestamp_utc_ms < ?"]
    params = [start_ms, end_ms]

    if source_ids:
        try:
            s_ids = [int(x.strip()) for x in source_ids.split(",") if x.strip()]
            if s_ids:
                placeholders = ",".join("?" for _ in s_ids)
                clauses.append(f"e.source_id IN ({placeholders})")
                params.extend(s_ids)
        except ValueError:
            raise HTTPException(400, "Некорректный формат source_ids")

    where = "WHERE " + " AND ".join(clauses)

    query = f"""
        SELECT
            ((e.timestamp_utc_ms - ?) / ?) AS bucket_idx,
            e.source_id,
            s.name AS source_name,
            COUNT(*) AS event_count
        FROM events e
        JOIN sources s ON s.id = e.source_id
        {where}
        GROUP BY bucket_idx, e.source_id
        ORDER BY bucket_idx
    """
    with transaction() as conn:
        rows = conn.execute(query, [start_ms, bucket_width] + params).fetchall()

    timeline_buckets = []
    bucket_map = {}
    for r in rows:
        idx = r["bucket_idx"]
        if idx not in bucket_map:
            t_ms = start_ms + idx * bucket_width
            bucket_map[idx] = {"time_ms": t_ms, "sources": {}}
        bucket_map[idx]["sources"][r["source_name"]] = r["event_count"]

    return {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "bucket_width_ms": bucket_width,
        "buckets": [bucket_map[k] for k in sorted(bucket_map.keys())],
    }
