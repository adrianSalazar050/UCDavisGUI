import pathlib
import sqlite3

import pytest

from server.ledger import SCHEMA_VERSION, TABLES, Ledger


@pytest.fixture
def led(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    yield ledger
    ledger.close()


def test_creates_every_table(led):
    names = {r["name"] for r in led.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(TABLES) <= names


def test_records_schema_version(led):
    assert led.query("SELECT version FROM schema_version")[0]["version"] == \
        SCHEMA_VERSION


def test_reopening_does_not_re_migrate(tmp_path):
    path = tmp_path / "ledger.db"
    first = Ledger(path)
    first.close()
    second = Ledger(path)
    try:
        rows = second.query("SELECT version FROM schema_version")
        assert len(rows) == 1, "migrations ran twice on reopen"
    finally:
        second.close()


def test_wal_mode_is_on(led):
    mode = led.query("PRAGMA journal_mode")[0]["journal_mode"]
    assert mode.lower() == "wal"
