import json
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from .config import ALLOWED_ROOT
from .db import connect, init_db
from .importer import import_folder

app = FastAPI(title="LogScope MVP", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8765", "http://localhost:8765"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

@app.on_event("startup")
def startup():
    ALLOWED_ROOT.mkdir(parents=True, exist_ok=True)
    init_db()

@app.get("/", response_class=HTMLResponse)
def index():
    html_path = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
    return html_path.read_text(encoding="utf-8")

@app.get("/api/health")
def health():
    return {"status": "ok", "version": app.version}

@app.get("/api/sources")
def sources():
    with connect() as conn:
        rows = conn.execute("""SELECT s.*, MIN(e.timestamp_ms) AS min_time_ms,
            MAX(e.timestamp_ms) AS max_time_ms
            FROM sources s LEFT JOIN events e ON e.source_id=s.id
            GROUP BY s.id ORDER BY s.name""").fetchall()
    return [dict(row) for row in rows]

@app.post("/api/import/path")
def import_path(payload: dict):
    folder = payload.get("path")
    if not isinstance(folder, str) or not folder.strip():
        raise HTTPException(400, "Нужно указать путь к папке")
    try:
        return import_folder(folder)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except OSError as exc:
        raise HTTPException(400, f"Ошибка чтения: {exc}")

@app.get("/api/events")
def events(
    start_ms: int | None = None,
    end_ms: int | None = None,
    source_id: int | None = None,
    level: str | None = None,
    q: str | None = Query(default=None, max_length=500),
    request_id: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
):
    clauses, params = [], []
    if start_ms is not None:
        clauses.append("timestamp_ms >= ?"); params.append(start_ms)
    if end_ms is not None:
        clauses.append("timestamp_ms <= ?"); params.append(end_ms)
    if source_id is not None:
        clauses.append("source_id = ?"); params.append(source_id)
    if level:
        clauses.append("upper(level) = ?"); params.append(level.upper())
    if q:
        clauses.append("message LIKE ? ESCAPE '\\'"); params.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if request_id:
        clauses.append("request_id = ?"); params.append(request_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM events {where}", params).fetchone()[0]
        rows = conn.execute(f"""SELECT e.*, s.name AS source_name
            FROM events e JOIN sources s ON s.id=e.source_id
            {where}
            ORDER BY e.timestamp_ms IS NULL, e.timestamp_ms, e.id
            LIMIT ? OFFSET ?""", params + [limit, offset]).fetchall()
    return {"total": total, "limit": limit, "offset": offset, "items": [dict(r) for r in rows]}

@app.get("/api/events/{event_id}")
def event_detail(event_id: int):
    with connect() as conn:
        row = conn.execute("""SELECT e.*,s.name AS source_name,s.path AS source_path
            FROM events e JOIN sources s ON s.id=e.source_id WHERE e.id=?""", (event_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Событие не найдено")
    result = dict(row)
    result["fields_json"] = json.loads(result["fields_json"] or "{}")
    return result

@app.get("/api/correlations/{request_id}")
def correlations(request_id: str):
    with connect() as conn:
        rows = conn.execute("""SELECT e.*,s.name AS source_name
            FROM events e JOIN sources s ON s.id=e.source_id
            WHERE e.request_id=? ORDER BY e.timestamp_ms,e.id""", (request_id,)).fetchall()
    return {"request_id": request_id, "total": len(rows), "items": [dict(r) for r in rows]}
