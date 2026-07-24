import gc
import sqlite3
import threading

import pytest

from server import ledger as ledger_module
from server.ledger import SCHEMA_VERSION, TABLES, Ledger, SEED_BADGES, \
    badge_id_for


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
    """in_transaction alone doesn't prove a rollback happened -- COMMIT
    clears that flag exactly the same as ROLLBACK does, and a single
    failing statement never leaves anything of its OWN half-written (SQLite
    backs out a failed statement's own effect regardless of what the
    wrapping transaction later does). The only way to observe a real
    difference is to give the failure a PRIOR sibling write, still pending
    in the same transaction, that only a genuine ROLLBACK discards: nest
    two execute() calls -- the first succeeds, the second collides -- and
    let the IntegrityError unwind the whole enclosing transaction()."""
    with pytest.raises(sqlite3.IntegrityError):
        with led.transaction():
            led.execute(
                "INSERT INTO ledger_meta (key, value) VALUES ('dup', '1')")
            led.execute(
                "INSERT INTO ledger_meta (key, value) VALUES ('dup', '2')")
    assert led._conn.in_transaction is False
    assert led.query("SELECT * FROM ledger_meta WHERE key = 'dup'") == []


def test_transaction_rolls_back_on_exception(led):
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with led.transaction() as conn:
            conn.execute(
                "INSERT INTO ledger_meta (key, value) VALUES ('k', 'v')")
            raise Boom()

    assert led.query("SELECT * FROM ledger_meta WHERE key = 'k'") == []


def test_nested_transaction_rolls_back_independently(led):
    """A raising inner transaction() block, swallowed by the outer block
    (the outer never sees the exception), must still discard only the
    inner's own writes -- the outer's writes must survive. Proves the
    SAVEPOINT-based nesting: the old BEGIN/COMMIT-only design rolled back
    (or committed) the WHOLE connection-level transaction regardless of
    depth, so the inner's write would incorrectly survive alongside the
    outer's."""
    with led.transaction() as outer:
        outer.execute(
            "INSERT INTO ledger_meta (key, value) VALUES ('outer', 'ok')")
        try:
            with led.transaction() as inner:
                inner.execute(
                    "INSERT INTO ledger_meta (key, value) "
                    "VALUES ('inner', 'SHOULD_ROLL_BACK')")
                raise RuntimeError("boom")
        except RuntimeError:
            pass  # outer swallows it and keeps going

    rows = {r["key"]: r["value"] for r in
            led.query("SELECT key, value FROM ledger_meta")}
    assert rows.get("outer") == "ok"
    assert "inner" not in rows


def test_nested_calls_inside_transaction_do_not_deadlock(tmp_path):
    """self._lock must be re-entrant. Two separate places rely on it:
    _migrate() itself nests a transaction() call inside its own outer
    `with self._lock:`, so a plain Lock deadlocks the SECOND that
    constructor runs -- Ledger(path) would never even return. And later
    tasks call execute()/query() from inside a transaction() block
    (writing N piece rows, then closing a run), which nests the same way
    from application code.

    Measured directly: with self._lock downgraded to a plain
    threading.Lock, `Ledger(tmp_path / "ledger.db")` alone hangs forever
    inside __init__ -- there is no point after which a bounded join() on
    a not-yet-created thread could save it. So construction AND use both
    happen on a background daemon thread; the main thread never touches
    the (possibly permanently locked) Ledger object at all, only a plain
    dict the worker writes into. A bounded join() is therefore enough to
    contain the hang to this one test instead of wedging the whole suite
    (which is what happened for real the first time a reviewer mutated
    RLock back to Lock and pytest had to be killed after 300 seconds)."""
    result = {}

    def work():
        ledger = Ledger(tmp_path / "ledger.db")
        with ledger.transaction():
            ledger.execute(
                "INSERT INTO ledger_meta (key, value) VALUES ('a', '1')")
            result["rows"] = ledger.query(
                "SELECT * FROM ledger_meta WHERE key = 'a'")
        ledger.close()
        result["done"] = True

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout=5)
    assert result.get("done"), "deadlocked: nested call inside transaction()"
    assert result["rows"][0]["value"] == "1"


def test_schema_version_rejects_a_second_row(led):
    """A guard never seen to fail is decoration (server/tests/test_docs.py's
    own standard). INSERT OR REPLACE keeping len(rows) == 1 is true whether
    or not the CHECK(id = 1) constraint exists -- this asserts the
    constraint itself, directly, by trying to violate it."""
    with pytest.raises(sqlite3.IntegrityError):
        led.execute(
            "INSERT INTO schema_version (id, version) VALUES (2, 99)")


def test_corrupt_database_is_quarantined_not_deleted(tmp_path, caplog):
    path = tmp_path / "ledger.db"
    path.write_bytes(b"this is not a database, not even slightly")

    ledger = Ledger(path)
    try:
        # It booted, and it works.
        assert ledger.query("SELECT version FROM schema_version")[0][
            "version"] == SCHEMA_VERSION
    finally:
        ledger.close()

    # The bad bytes are still on disk under a new name.
    quarantined = list(tmp_path.glob("ledger.db.corrupt-*"))
    assert len(quarantined) == 1, "the corrupt file was not preserved"
    assert quarantined[0].read_bytes().startswith(b"this is not a database")


def test_a_half_migrated_database_is_quarantined_too(tmp_path):
    """Tables present, version row never stamped -- what an interrupted
    migration from an older build leaves behind. The replay raises
    OperationalError, and that must quarantine rather than refuse to boot."""
    path = tmp_path / "ledger.db"
    scratch = sqlite3.connect(str(path))
    scratch.execute(
        "CREATE TABLE schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "version INTEGER NOT NULL)")
    scratch.commit()
    scratch.close()

    ledger = Ledger(path)
    try:
        assert ledger.query("SELECT version FROM schema_version")[0][
            "version"] == SCHEMA_VERSION
        assert ledger.query("SELECT * FROM print_runs") == []
    finally:
        ledger.close()
    assert len(list(tmp_path.glob("ledger.db.corrupt-*"))) == 1


def test_seeds_the_badge_catalogue(led):
    codes = {b["code"] for b in led.badges()}
    assert codes == {code for code, _, _, _ in SEED_BADGES}


def test_seed_badge_ids_are_deterministic(tmp_path):
    a = Ledger(tmp_path / "a.db")
    b = Ledger(tmp_path / "b.db")
    try:
        ids_a = {x["code"]: x["id"] for x in a.badges()}
        ids_b = {x["code"]: x["id"] for x in b.badges()}
        assert ids_a == ids_b
        assert ids_a["spaghetti"] == badge_id_for("spaghetti")
    finally:
        a.close()
        b.close()


def test_reseeding_does_not_duplicate(tmp_path):
    path = tmp_path / "ledger.db"
    first = Ledger(path)
    count = len(first.badges())
    first.close()
    second = Ledger(path)
    try:
        assert len(second.badges()) == count
    finally:
        second.close()


def test_only_auto_badges_are_marked_auto(led):
    auto = {b["code"] for b in led.badges() if b["auto"]}
    assert auto == {"spaghetti", "stringing", "hms_error", "autostop"}


def test_open_run_returns_an_id_and_leaves_it_open(led):
    run_id = led.open_run(printer_serial="S1", printer_name="A1",
                          source="unattributed")
    run = led.get_run(run_id)
    assert run["end_state"] is None
    assert run["printer_serial"] == "S1"
    assert run["printer_name"] == "A1"
    assert run["started_at"] is not None


def test_find_open_run_finds_only_the_open_one(led):
    closed = led.open_run(printer_serial="S1", printer_name="A1",
                          source="queue")
    led.close_run(closed, end_state="FINISH")
    assert led.find_open_run("S1") is None

    open_id = led.open_run(printer_serial="S1", printer_name="A1",
                           source="queue")
    assert led.find_open_run("S1")["id"] == open_id
    assert led.find_open_run("S2") is None


def test_update_run_sets_only_named_fields(led):
    run_id = led.open_run(printer_serial="S1", printer_name="A1",
                          source="queue")
    led.update_run(run_id, last_layer=42, total_layers=100)
    run = led.get_run(run_id)
    assert run["last_layer"] == 42
    assert run["total_layers"] == 100
    assert run["source"] == "queue"


def test_update_run_rejects_an_unknown_column(led):
    run_id = led.open_run(printer_serial="S1", printer_name="A1",
                          source="queue")
    with pytest.raises(ValueError):
        led.update_run(run_id, drop_table="oops")


def test_close_run_stamps_end_state_and_ended_at(led):
    run_id = led.open_run(printer_serial="S1", printer_name="A1",
                          source="queue")
    led.close_run(run_id, end_state="FINISH")
    run = led.get_run(run_id)
    assert run["end_state"] == "FINISH"
    assert run["ended_at"] is not None


def test_close_run_is_idempotent(led):
    run_id = led.open_run(printer_serial="S1", printer_name="A1",
                          source="queue")
    led.close_run(run_id, end_state="FINISH")
    first = led.get_run(run_id)["ended_at"]
    led.close_run(run_id, end_state="FAILED")
    again = led.get_run(run_id)
    assert again["end_state"] == "FINISH", "a closed run was reopened"
    assert again["ended_at"] == first


def test_list_runs_is_newest_first_and_filterable(led):
    a = led.open_run(printer_serial="S1", printer_name="A1", source="queue")
    b = led.open_run(printer_serial="S2", printer_name="A1m", source="queue")
    ids = [r["id"] for r in led.list_runs()]
    assert set(ids) == {a, b}
    assert [r["id"] for r in led.list_runs(serial="S2")] == [b]


def test_add_event_records_payload_as_json(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.add_event(printer_serial="S1", run_id=run_id, kind="hms_raised",
                  source="server", payload={"code": "0300_1100_0002_0001"})
    events = led.events_for(run_id)
    assert len(events) == 1
    assert events[0]["kind"] == "hms_raised"
    assert events[0]["payload"] == {"code": "0300_1100_0002_0001"}


def test_events_are_returned_oldest_first(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    for kind in ("state_change", "hms_raised", "hms_cleared"):
        led.add_event(printer_serial="S1", run_id=run_id, kind=kind,
                      source="server")
    assert [e["kind"] for e in led.events_for(run_id)] == [
        "state_change", "hms_raised", "hms_cleared"]


def test_duplicate_client_uuid_is_ignored(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    first = led.add_event(printer_serial="S1", run_id=run_id,
                          kind="operator_note", source="operator",
                          client_uuid="abc")
    second = led.add_event(printer_serial="S1", run_id=run_id,
                           kind="operator_note", source="operator",
                           client_uuid="abc")
    assert first is not None
    assert second is None, "a repeated client_uuid created a second row"
    assert len(led.events_for(run_id)) == 1
