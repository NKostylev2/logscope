import copy
import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Optional
from .config import ALLOWED_ROOT, MAX_FILE_BYTES, SUPPORTED_EXTENSIONS, PROJECT_DIR
from .db import transaction
from .parsers import iter_file_events


def is_within_root(path: Path, root: Path) -> bool:
    """Strictly verify that path is inside root directory, preventing directory traversal."""
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
        return resolved_path.is_relative_to(resolved_root)
    except (ValueError, AttributeError):
        # Fallback for Python versions where is_relative_to might raise or not exist
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False


def compute_quick_fingerprint(path: Path) -> str:
    """Compute a fast content fingerprint without reading gigabytes of data.
    Hashes file size + mtime + first 64KB + last 64KB.
    Provides fast O(1) change detection for arbitrarily large files.
    """
    try:
        stat = path.stat()
        size = stat.st_size
        hasher = hashlib.sha256()
        hasher.update(f"{size}:{stat.st_mtime_ns}".encode("utf-8"))

        chunk_size = 65536
        with path.open("rb") as f:
            head = f.read(chunk_size)
            hasher.update(head)
            if size > chunk_size * 2:
                f.seek(size - chunk_size)
                tail = f.read(chunk_size)
                hasher.update(tail)

        return hasher.hexdigest()
    except OSError:
        return ""


def resolve_import_path(folder_str: str) -> Path:
    """Resolve and validate folder path against ALLOWED_ROOT."""
    root = ALLOWED_ROOT.resolve()
    raw_path = Path(folder_str)

    if raw_path.is_absolute():
        candidate = raw_path.resolve()
    else:
        # Check relative to PROJECT_DIR first, then ALLOWED_ROOT
        cand1 = (PROJECT_DIR / raw_path).resolve()
        cand2 = (root / raw_path).resolve()
        candidate = cand1 if cand1.exists() else cand2

    if not is_within_root(candidate, root):
        raise ValueError(f"Путь должен находиться внутри разрешённой папки: {root}")
    if not candidate.exists() or not candidate.is_dir():
        raise ValueError(f"Указанный путь не существует или не является папкой: {candidate}")

    return candidate


def import_single_file(path: Path, cancel_event: Optional[threading.Event] = None) -> dict:
    """Process a single file within its own isolated database transaction.
    Returns status dict: {'path', 'status', 'source_id', 'event_count', 'reason'}.
    """
    if cancel_event and cancel_event.is_set():
        return {"path": str(path), "status": "cancelled", "reason": "user_cancelled"}

    # The folder was validated, but a file inside it may be a symlink that points
    # outside ALLOWED_ROOT. Never index (and later serve) content from there.
    if not is_within_root(path, ALLOWED_ROOT):
        return {"path": str(path), "status": "skipped", "reason": "outside_allowed_root"}

    try:
        stat = path.stat()
    except OSError as exc:
        return {"path": str(path), "status": "error", "reason": f"File access error: {exc}"}

    if stat.st_size > MAX_FILE_BYTES:
        return {"path": str(path), "status": "skipped", "reason": f"file_too_large (>{MAX_FILE_BYTES} bytes)"}

    quick_hash = compute_quick_fingerprint(path)
    path_str = str(path)

    with transaction() as conn:
        existing = conn.execute(
            "SELECT id, size_bytes, mtime_ns, sha256, status FROM sources WHERE path=?",
            (path_str,)
        ).fetchone()

        # Check if file is completely unchanged. A source whose previous import failed
        # (status 'error' / 'indexing') carries the real size+mtime, so without the
        # status check it would be reported as "unchanged" and never retried.
        if (
            existing
            and existing["status"] == "ok"
            and existing["size_bytes"] == stat.st_size
            and existing["mtime_ns"] == stat.st_mtime_ns
            and (existing["sha256"] == quick_hash if existing["sha256"] else True)
        ):
            return {
                "path": path_str,
                "status": "unchanged",
                "source_id": existing["id"],
                "reason": "mtime_and_hash_matched",
            }

        # If modified or new, use a transaction for this file
        try:
            conn.execute("BEGIN TRANSACTION")

            if existing:
                # Remove prior events for this source before re-indexing
                conn.execute("DELETE FROM events WHERE source_id=?", (existing["id"],))
                conn.execute("DELETE FROM sources WHERE id=?", (existing["id"],))

            cur = conn.execute(
                """INSERT INTO sources (path, name, size_bytes, mtime_ns, sha256, status)
                   VALUES (?, ?, ?, ?, ?, 'indexing')""",
                (path_str, path.name, stat.st_size, stat.st_mtime_ns, quick_hash)
            )
            source_id = cur.lastrowid

            count = 0
            parse_errors = 0
            unparsed_ts = 0
            min_ts = None
            max_ts = None
            batch = []

            for event in iter_file_events(path):
                if cancel_event and cancel_event.is_set():
                    conn.execute("ROLLBACK")
                    return {"path": path_str, "status": "cancelled", "reason": "user_cancelled"}

                ts_utc = event.get("timestamp_utc_ms")
                if ts_utc is None:
                    unparsed_ts += 1
                else:
                    if min_ts is None or ts_utc < min_ts:
                        min_ts = ts_utc
                    if max_ts is None or ts_utc > max_ts:
                        max_ts = ts_utc

                batch.append((
                    source_id,
                    ts_utc,
                    ts_utc,  # timestamp_ms alias
                    event.get("timestamp_raw"),
                    event.get("level"),
                    event.get("message", ""),
                    event.get("line_start", 1),
                    event.get("line_end", 1),
                    event.get("request_id"),
                    event.get("duration_ms"),
                    event.get("event_type", "log"),
                    event.get("fields_json", "{}"),
                    event.get("http_code"),
                ))
                count += 1

                if len(batch) >= 1000:
                    conn.executemany(
                        """INSERT INTO events
                           (source_id, timestamp_utc_ms, timestamp_ms, timestamp_raw, level, message,
                            line_start, line_end, request_id, duration_ms, event_type, fields_json, http_code)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        batch
                    )
                    batch.clear()

            if batch:
                conn.executemany(
                    """INSERT INTO events
                       (source_id, timestamp_utc_ms, timestamp_ms, timestamp_raw, level, message,
                        line_start, line_end, request_id, duration_ms, event_type, fields_json, http_code)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    batch
                )

            conn.execute(
                """UPDATE sources
                   SET event_count=?, min_time_ms=?, max_time_ms=?, parse_errors=?, unparsed_timestamps=?, status='ok'
                   WHERE id=?""",
                (count, min_ts, max_ts, parse_errors, unparsed_ts, source_id)
            )
            conn.execute("COMMIT")

            return {
                "path": path_str,
                "status": "imported",
                "source_id": source_id,
                "event_count": count,
                "unparsed_timestamps": unparsed_ts,
            }

        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
                conn.execute(
                    """INSERT OR REPLACE INTO sources
                       (path, name, size_bytes, mtime_ns, status, error_message)
                       VALUES (?, ?, ?, ?, 'error', ?)""",
                    (path_str, path.name, stat.st_size, stat.st_mtime_ns, str(exc))
                )
            except Exception:
                pass
            return {"path": path_str, "status": "error", "reason": str(exc)}


def clean_deleted_sources(folder: Path) -> int:
    """Find and remove sources registered in DB that no longer exist on disk."""
    folder_resolved = folder.resolve()
    deleted_count = 0
    with transaction() as conn:
        # Containment is checked per path component. The former `path LIKE '<folder>%'`
        # also matched sibling folders ('inbox_old' for 'inbox') and treated '_' / '%'
        # in the folder name as wildcards.
        rows = conn.execute("SELECT id, path FROM sources").fetchall()
        for row in rows:
            source_path = Path(row["path"])
            if not source_path.is_relative_to(folder_resolved):
                continue
            if not source_path.exists():
                conn.execute("DELETE FROM events WHERE source_id=?", (row["id"],))
                conn.execute("DELETE FROM sources WHERE id=?", (row["id"],))
                deleted_count += 1
    return deleted_count


def import_folder(folder_str: str, cancel_event: Optional[threading.Event] = None) -> dict:
    """Synchronous import of an entire folder."""
    candidate = resolve_import_path(folder_str)

    # Collect files
    supported_files = [
        p for p in sorted(candidate.rglob("*"))
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]

    deleted_sources = clean_deleted_sources(candidate)
    results = []

    for path in supported_files:
        if cancel_event and cancel_event.is_set():
            break
        res = import_single_file(path, cancel_event)
        results.append(res)

    return {
        "root": str(candidate),
        "total_discovered": len(supported_files),
        "deleted_sources": deleted_sources,
        "files": results,
    }


MAX_RECORDED_ERRORS = 200


class ImportManager:
    """Thread-safe background import manager with progress tracking and cancellation."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self.state = {
            "status": "idle",  # idle, running, completed, cancelled, failed
            "folder": None,
            "total_files": 0,
            "processed_files": 0,
            "current_file": None,
            "total_events_imported": 0,
            "progress_percent": 0.0,
            "started_at": None,
            "completed_at": None,
            "stats": {"imported": 0, "unchanged": 0, "error": 0, "skipped": 0, "cancelled": 0, "deleted": 0},
            "errors": [],
            "message": "Ожидание команды импорта",
        }

    def get_status(self) -> dict:
        with self._lock:
            # Deep copy: the nested `stats` / `errors` objects are mutated by the worker
            # thread, and the caller serializes the result after the lock is released.
            return copy.deepcopy(self.state)

    def cancel(self) -> dict:
        with self._lock:
            if self.state["status"] == "running":
                self._cancel_event.set()
                self.state["message"] = "Отмена импорта запрошена..."
                return {"status": "cancelling", "message": "Запрос на отмену отправлен"}
            return {"status": self.state["status"], "message": "Импорт сейчас не выполняется"}

    def start(self, folder_str: str) -> dict:
        with self._lock:
            if self.state["status"] == "running":
                raise ValueError("Импорт уже выполняется. Дождитесь завершения или отмените его.")

            candidate = resolve_import_path(folder_str)
            self._cancel_event.clear()
            self.state = {
                "status": "running",
                "folder": str(candidate),
                "total_files": 0,
                "processed_files": 0,
                "current_file": None,
                "total_events_imported": 0,
                "progress_percent": 0.0,
                "started_at": time.time(),
                "completed_at": None,
                "stats": {"imported": 0, "unchanged": 0, "error": 0, "skipped": 0, "cancelled": 0, "deleted": 0},
                "errors": [],
                "message": "Сканирование файлов...",
            }

            self._thread = threading.Thread(
                target=self._run_import,
                args=(candidate,),
                daemon=True,
                name="logscope-importer",
            )
            self._thread.start()

            return {
                "status": "started",
                "folder": str(candidate),
                "message": "Импорт запущен в фоновом режиме",
            }

    def _run_import(self, folder: Path):
        try:
            # 1. Clean missing files
            deleted = clean_deleted_sources(folder)
            with self._lock:
                self.state["stats"]["deleted"] = deleted

            # 2. Discover files
            files = [
                p for p in sorted(folder.rglob("*"))
                if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
            ]

            total_files = len(files)
            with self._lock:
                self.state["total_files"] = total_files
                self.state["message"] = f"Найдено {total_files} файлов для обработки"

            total_events = 0

            # 3. Process files iteratively
            for idx, path in enumerate(files, 1):
                if self._cancel_event.is_set():
                    with self._lock:
                        self.state["status"] = "cancelled"
                        self.state["completed_at"] = time.time()
                        self.state["message"] = "Импорт отменён пользователем"
                    return

                with self._lock:
                    self.state["current_file"] = path.name
                    self.state["processed_files"] = idx - 1
                    self.state["progress_percent"] = round(((idx - 1) / max(total_files, 1)) * 100, 1)

                res = import_single_file(path, self._cancel_event)

                with self._lock:
                    status = res.get("status", "error")
                    self.state["stats"][status] = self.state["stats"].get(status, 0) + 1
                    if status == "imported":
                        total_events += res.get("event_count", 0)
                        self.state["total_events_imported"] = total_events
                    elif status == "error" and len(self.state["errors"]) < MAX_RECORDED_ERRORS:
                        # The full count stays in stats["error"]; the list is capped.
                        self.state["errors"].append({"file": path.name, "error": res.get("reason", "unknown")})

            if self._cancel_event.is_set():
                # Cancel arrived while the last file was being imported.
                with self._lock:
                    self.state["status"] = "cancelled"
                    self.state["completed_at"] = time.time()
                    self.state["message"] = "Импорт отменён пользователем"
                return

            with self._lock:
                self.state["status"] = "completed"
                self.state["processed_files"] = total_files
                self.state["progress_percent"] = 100.0
                self.state["completed_at"] = time.time()
                self.state["current_file"] = None
                self.state["message"] = (
                    f"Импорт завершён: {self.state['stats']['imported']} импортировано, "
                    f"{self.state['stats']['unchanged']} без изменений, "
                    f"{self.state['stats']['error']} ошибок."
                )
        except Exception as exc:
            with self._lock:
                self.state["status"] = "failed"
                self.state["completed_at"] = time.time()
                self.state["message"] = f"Сбой задачи импорта: {exc}"
                self.state["errors"].append({"file": "system", "error": str(exc)})


# Global manager instance
import_manager = ImportManager()
