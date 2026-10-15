from app.parsers import parse_timestamp
def test_iso_timestamp_milliseconds():
    assert parse_timestamp("2026-10-15T10:23:41.250") is not None
def test_epoch_milliseconds():
    assert parse_timestamp("1792059821250") == 1792059821250
def test_unknown_timestamp():
    assert parse_timestamp("no timestamp here") is None
