import gc
import sqlite3
import threading

import pytest

from server import ledger as ledger_module
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


def test_interrupted_migration_does_not_brick_the_database(tmp_path,
                                                             monkeypatch):
    """A failure partway through a version's DDL (simulating a process
    crash) must not leave partially-created tables committed -- otherwise
    the next boot sees no schema_version row, replays the same migration,
    and dies forever on "table already exists" (master.md section 11: the
    server must always be able to boot)."""
    path = tmp_path / "ledger.db"
    broken = dict(ledger_module.MIGRATIONS)
    broken[1] = list(ledger_module.MIGRATIONS[1][:2]) + ["THIS IS NOT SQL"]
    monkeypatch.setattr(ledger_module, "MIGRATIONS", broken)

    with pytest.raises(sqlite3.OperationalError):
        Ledger(path)
    # Drop any reference to the failed instance/connection before reopening
    # the same file, so a lingering handle can't fool the next open.
    gc.collect()

    monkeypatch.undo()
    real = Ledger(path)
    try:
        rows = real.query("SELECT version FROM schema_version")
        assert len(rows) == 1
        assert rows[0]["version"] == SCHEMA_VERSION
    finally:
        real.close()


def test_failed_execute_leaves_no_open_transaction(led):
    """A prepare-time error (bad SQL, missing table) never opens a
    transaction in the first place, so it can't reproduce this bug. A
    runtime constraint violation is the real scenario the reviewer measured
    -- a later task turns a duplicate client_uuid's IntegrityError into a
    None return, so this needs to actually happen and be recovered from."""
    led.execute("INSERT INTO ledger_meta (key, value) VALUES ('dup', '1')")
    with pytest.raises(sqlite3.IntegrityError):
        led.execute("INSERT INTO ledger_meta (key, value) VALUES ('dup', '2')")
    assert led._conn.in_transaction is False


def test_transaction_rolls_back_on_exception(led):
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with led.transaction() as conn:
            conn.execute(
                "INSERT INTO ledger_meta (key, value) VALUES ('k', 'v')")
            raise Boom()

    assert led.query("SELECT * FROM ledger_meta WHERE key = 'k'") == []


def test_nested_calls_inside_transaction_do_not_deadlock(led):
    """self._lock must be re-entrant: later tasks call self.execute()/
    self.query() from inside a transaction() block (writing N piece rows,
    then closing a run). A plain threading.Lock here would deadlock the
    moment that happens -- run it on a background thread with a bounded
    join so a regression fails the test instead of hanging the suite."""
    result = {}

    def work():
        with led.transaction():
            led.execute(
                "INSERT INTO ledger_meta (key, value) VALUES ('a', '1')")
            result["rows"] = led.query(
                "SELECT * FROM ledger_meta WHERE key = 'a'")

    t = threading.Thread(target=work)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "deadlocked: nested call inside transaction()"
    assert result.get("rows") and result["rows"][0]["value"] == "1"
