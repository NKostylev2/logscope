import os
import threading
import time
import pytest
from pathlib import Path
from app.config import ALLOWED_ROOT
from app.db import connect, init_db
from app.importer import (
    import_folder,
    import_single_file,
    resolve_import_path,
    ImportManager,
)


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path: Path, monkeypatch):
    test_db = tmp_path / "test_logscope.sqlite3"
    monkeypatch.setattr("app.db.DB_PATH", test_db)
    monkeypatch.setattr("app.config.DB_PATH", test_db)
    init_db()
    yield test_db


def test_first_and_second_import_no_duplicates(tmp_path: Path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr("app.config.ALLOWED_ROOT", inbox)
    monkeypatch.setattr("app.importer.ALLOWED_ROOT", inbox)

    log1 = inbox / "srv1.log"
    log1.write_text(
        "2026-10-15 10:00:00.000 INFO Server boot\n"
        "2026-10-15 10:00:01.000 WARN High load\n",
        encoding="utf-8"
    )

    # First import
    res1 = import_folder(str(inbox))
    assert res1["total_discovered"] == 1
    assert res1["files"][0]["status"] == "imported"
    assert res1["files"][0]["event_count"] == 2

    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 2

    # Second import without changes
    res2 = import_folder(str(inbox))
    assert res2["files"][0]["status"] == "unchanged"

    # Event count must remain exactly 2, no duplicates!
    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 2


def test_file_modification_updates_events(tmp_path: Path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr("app.config.ALLOWED_ROOT", inbox)
    monkeypatch.setattr("app.importer.ALLOWED_ROOT", inbox)

    log1 = inbox / "app.log"
    log1.write_text("2026-10-15 10:00:00.000 INFO Line 1\n", encoding="utf-8")
    import_folder(str(inbox))

    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1

    # Modify file
    time.sleep(0.01)  # Ensure mtime changes
    log1.write_text(
        "2026-10-15 10:00:00.000 INFO Line 1\n"
        "2026-10-15 10:00:02.000 INFO Line 2\n"
        "2026-10-15 10:00:03.000 ERROR Line 3\n",
        encoding="utf-8"
    )

    res = import_folder(str(inbox))
    assert res["files"][0]["status"] == "imported"
    assert res["files"][0]["event_count"] == 3

    # Clean update: old record replaced, total is 3
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3


def test_resilience_to_corrupt_file(tmp_path: Path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr("app.config.ALLOWED_ROOT", inbox)
    monkeypatch.setattr("app.importer.ALLOWED_ROOT", inbox)

    # Valid file
    good = inbox / "good.log"
    good.write_text("2026-10-15 10:00:00.000 INFO OK\n", encoding="utf-8")

    # Another valid file
    good2 = inbox / "good2.log"
    good2.write_text("2026-10-15 10:00:01.000 INFO OK2\n", encoding="utf-8")

    res = import_folder(str(inbox))
    statuses = [f["status"] for f in res["files"]]
    assert statuses == ["imported", "imported"]

    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


def test_deleted_file_cleanup(tmp_path: Path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr("app.config.ALLOWED_ROOT", inbox)
    monkeypatch.setattr("app.importer.ALLOWED_ROOT", inbox)

    temp_log = inbox / "temp.log"
    temp_log.write_text("2026-10-15 10:00:00.000 INFO Temp\n", encoding="utf-8")

    import_folder(str(inbox))
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1

    # Delete file from disk
    temp_log.unlink()

    # Re-run import
    res = import_folder(str(inbox))
    assert res["deleted_sources"] == 1

    # Database records should be removed cleanly
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_path_traversal_protection(tmp_path: Path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr("app.config.ALLOWED_ROOT", inbox)
    monkeypatch.setattr("app.importer.ALLOWED_ROOT", inbox)

    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="разрешённой папки"):
        resolve_import_path(str(outside))

    with pytest.raises(ValueError, match="разрешённой папки"):
        resolve_import_path(str(inbox / ".." / "outside"))


def test_cancellation(tmp_path: Path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr("app.config.ALLOWED_ROOT", inbox)
    monkeypatch.setattr("app.importer.ALLOWED_ROOT", inbox)

    cancel_ev = threading.Event()
    cancel_ev.set()  # Pre-cancelled

    res = import_folder(str(inbox), cancel_event=cancel_ev)
    assert res["files"] == []


def test_background_import_manager(tmp_path: Path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr("app.config.ALLOWED_ROOT", inbox)
    monkeypatch.setattr("app.importer.ALLOWED_ROOT", inbox)

    log1 = inbox / "app.log"
    log1.write_text("2026-10-15 10:00:00.000 INFO Line 1\n", encoding="utf-8")

    mgr = ImportManager()
    mgr.start(str(inbox))

    # Wait for completion
    for _ in range(50):
        status = mgr.get_status()
        if status["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.05)

    final_status = mgr.get_status()
    assert final_status["status"] == "completed"
    assert final_status["stats"]["imported"] == 1
    assert final_status["total_events_imported"] == 1
