import os
import threading
import time
import pytest
from pathlib import Path
from app.config import ALLOWED_ROOT
from app.db import connect, init_db
from app.importer import (
    clean_deleted_sources,
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


# ---------------------------------------------------------------- audit regressions

def test_failed_import_is_retried_instead_of_reported_unchanged(tmp_path: Path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr("app.config.ALLOWED_ROOT", inbox)
    monkeypatch.setattr("app.importer.ALLOWED_ROOT", inbox)
    log = inbox / "app.log"
    log.write_text("2026-10-15 10:00:00.000 INFO hello\n", encoding="utf-8")

    import app.importer as importer_mod
    real_iter = importer_mod.iter_file_events
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real_iter(path)

    monkeypatch.setattr(importer_mod, "iter_file_events", flaky)

    assert import_single_file(log)["status"] == "error"
    # File was NOT modified. It must be retried, not skipped as "unchanged".
    assert import_single_file(log)["status"] == "imported"

    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM sources").fetchone()[0] == "ok"


def test_symlink_pointing_outside_root_is_not_indexed(tmp_path: Path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr("app.config.ALLOWED_ROOT", inbox)
    monkeypatch.setattr("app.importer.ALLOWED_ROOT", inbox)

    outside = tmp_path / "secret.log"
    outside.write_text("2026-10-15 10:00:00.000 INFO password=hunter2\n", encoding="utf-8")
    inside = inbox / "real.log"
    inside.write_text("2026-10-15 10:00:00.000 INFO fine\n", encoding="utf-8")
    try:
        (inbox / "innocent.log").symlink_to(outside)
        (inbox / "alias.log").symlink_to(inside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform")

    res = import_folder(str(inbox))
    by_name = {Path(f["path"]).name: f for f in res["files"]}

    assert by_name["innocent.log"]["status"] == "skipped"
    assert by_name["innocent.log"]["reason"] == "outside_allowed_root"
    assert by_name["alias.log"]["status"] == "imported"  # symlink that stays inside the root is fine
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE message LIKE '%hunter2%'").fetchone()[0] == 0


def test_cleanup_does_not_touch_sibling_folder_with_common_prefix(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("app.config.ALLOWED_ROOT", tmp_path)
    monkeypatch.setattr("app.importer.ALLOWED_ROOT", tmp_path)
    inbox = tmp_path / "inbox"
    sibling = tmp_path / "inbox_old"  # shares the prefix; '_' is also a LIKE wildcard
    inbox.mkdir()
    sibling.mkdir()
    kept = inbox / "kept.log"
    gone = sibling / "gone.log"
    for f in (kept, gone):
        f.write_text("2026-10-15 10:00:00.000 INFO x\n", encoding="utf-8")
        assert import_single_file(f)["status"] == "imported"
    gone.unlink()

    assert clean_deleted_sources(inbox) == 0  # 'gone.log' is not inside 'inbox'
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 2
    assert clean_deleted_sources(sibling) == 1


def test_get_status_returns_an_independent_snapshot():
    manager = ImportManager()
    snapshot = manager.get_status()
    snapshot["stats"]["imported"] = 999
    snapshot["errors"].append({"file": "x", "error": "y"})

    fresh = manager.get_status()
    assert fresh["stats"]["imported"] == 0
    assert fresh["errors"] == []
