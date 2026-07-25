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


def test_create_pieces_makes_one_row_per_copy(led):
    run_id = led.open_run(printer_serial="S1", source="queue",
                          copies_planned=4)
    assert led.create_pieces(run_id, 4) == 4
    pieces = led.pieces_for(run_id)
    assert [p["index_in_run"] for p in pieces] == [1, 2, 3, 4]
    assert {p["status"] for p in pieces} == {"pending_inspection"}


def test_create_pieces_is_idempotent(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 3)
    assert led.create_pieces(run_id, 3) == 0
    assert len(led.pieces_for(run_id)) == 3


def test_set_piece_records_the_inspector(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 1)
    piece = led.pieces_for(run_id)[0]
    assert led.set_piece(piece["id"], status="scrap", inspected_by="adrian")
    updated = led.pieces_for(run_id)[0]
    assert updated["status"] == "scrap"
    assert updated["inspected_by"] == "adrian"
    assert updated["inspected_at"] is not None


def test_set_piece_rejects_an_unknown_status(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 1)
    piece = led.pieces_for(run_id)[0]
    with pytest.raises(ValueError):
        led.set_piece(piece["id"], status="excellent")


def test_bulk_applies_a_status_with_per_index_overrides(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 8)
    changed = led.set_pieces_bulk(run_id, "good", inspected_by="adrian",
                                  overrides=[{"index_in_run": 3,
                                              "status": "scrap"}])
    assert changed == 8
    by_index = {p["index_in_run"]: p["status"]
                for p in led.pieces_for(run_id)}
    assert by_index[3] == "scrap"
    assert by_index[1] == by_index[8] == "good"


def test_piece_counts_rolls_up_by_run(led):
    a = led.open_run(printer_serial="S1", source="queue")
    b = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(a, 3)
    led.create_pieces(b, 1)
    for piece in led.pieces_for(a)[:2]:
        led.set_piece(piece["id"], status="good")
    counts = led.piece_counts([a, b])
    assert counts[a] == {"total": 3, "good": 2, "scrap": 0, "rework": 0,
                         "pending": 1}
    assert counts[b]["pending"] == 1
    assert led.piece_counts([]) == {}


def test_run_badges_add_and_remove(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.add_run_badge(run_id, badge_id_for("spaghetti"),
                      applied_by="detector")
    assert [b["code"] for b in led.run_badges(run_id)] == ["spaghetti"]
    # Applying the same badge twice is not an error and not a second row.
    led.add_run_badge(run_id, badge_id_for("spaghetti"),
                      applied_by="detector")
    assert len(led.run_badges(run_id)) == 1
    assert led.remove_run_badge(run_id, badge_id_for("spaghetti"))
    assert led.run_badges(run_id) == []


def test_a_non_auto_badge_cannot_be_applied_by_the_detector(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    with pytest.raises(ValueError):
        led.add_run_badge(run_id, badge_id_for("warped"),
                          applied_by="detector")


def test_piece_badges_add_and_remove(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 1)
    piece = led.pieces_for(run_id)[0]
    led.add_piece_badge(piece["id"], badge_id_for("warped"),
                        applied_by="operator")
    assert [b["code"] for b in led.piece_badges(piece["id"])] == ["warped"]
    assert led.remove_piece_badge(piece["id"], badge_id_for("warped"))


def test_an_unknown_applier_cannot_apply_a_non_auto_badge(led):
    """Fail-closed: only an explicit human source may apply a non-auto badge.
    A machine identity that is not exactly 'detector' -- a typo, or a new
    applier added later -- must be RESTRICTED, not silently allowed."""
    run_id = led.open_run(printer_serial="S1", source="queue")
    for applier in ("robot", "Detector", "system", "monitor"):
        with pytest.raises(ValueError):
            led.add_run_badge(run_id, badge_id_for("warped"),
                              applied_by=applier)
    # And the explicit human still may.
    led.add_run_badge(run_id, badge_id_for("warped"), applied_by="operator")
    assert [b["code"] for b in led.run_badges(run_id)] == ["warped"]


def test_bulk_rejects_a_bad_override_index_before_any_write(led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 3)
    with pytest.raises(ValueError):
        led.set_pieces_bulk(run_id, "good",
                            overrides=[{"index_in_run": "not-a-number",
                                        "status": "scrap"}])
    # The blanket 'good' write must have rolled back with the bad override.
    assert {p["status"] for p in led.pieces_for(run_id)} == {
        "pending_inspection"}


# ---------------- schema v2: parts + part_recipes ----------------

def test_schema_upgrades_v1_to_v2_without_data_loss(tmp_path):
    """A ledger created at v1 (Phase 1) must gain the parts tables on reopen
    and keep its existing rows."""
    import sqlite3 as _sq
    path = tmp_path / "ledger.db"
    con = _sq.connect(str(path)); con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY "
                "CHECK (id = 1), version INTEGER NOT NULL)")
    con.execute("INSERT INTO schema_version (id, version) VALUES (1, 1)")
    con.execute("CREATE TABLE print_runs (id TEXT PRIMARY KEY, "
                "printer_serial TEXT, end_state TEXT)")
    con.execute("INSERT INTO print_runs (id, printer_serial) VALUES ('r1','S1')")
    # A real Phase-1 ledger also has `badges` (Ledger.__init__ seeds it on
    # every open) -- included here so this synthetic v1 file matches what a
    # genuine v1 database looks like, instead of crashing _seed_badges().
    con.execute("""CREATE TABLE badges (
             id TEXT PRIMARY KEY,
             code TEXT NOT NULL UNIQUE,
             label TEXT NOT NULL,
             severity TEXT NOT NULL,
             auto INTEGER NOT NULL DEFAULT 0,
             archived INTEGER NOT NULL DEFAULT 0,
             created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL)""")
    con.commit(); con.close()

    led = Ledger(path)
    try:
        assert led.query("SELECT version FROM schema_version")[0]["version"] == SCHEMA_VERSION
        names = {r["name"] for r in led.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"parts", "part_recipes"} <= names
        assert led.query("SELECT id FROM print_runs")[0]["id"] == "r1"
    finally:
        led.close()


# ---------------- parts ----------------

def test_create_and_find_part(led):
    pid = led.create_part(part_number="BRK-100", revision="A", name="Bracket")
    assert led.find_part("BRK-100", "A")["id"] == pid
    assert led.get_part(pid)["name"] == "Bracket"
    assert led.find_part("BRK-100", "B") is None


def test_a_new_revision_is_allowed_but_a_duplicate_is_not(led):
    import sqlite3 as _sq
    led.create_part(part_number="BRK-100", revision="A")
    led.create_part(part_number="BRK-100", revision="B")
    with pytest.raises(_sq.IntegrityError):
        led.create_part(part_number="BRK-100", revision="A")


def test_update_part_rejects_unknown_column(led):
    pid = led.create_part(part_number="X", revision="A")
    with pytest.raises(ValueError):
        led.update_part(pid, drop="oops")


def test_archive_hides_a_part(led):
    pid = led.create_part(part_number="X", revision="A")
    led.archive_part(pid)
    assert led.list_parts() == []
    assert len(led.list_parts(include_archived=True)) == 1


def test_recipes_belong_to_a_part(led):
    pid = led.create_part(part_number="X", revision="A")
    r = led.add_recipe(pid, name="Standard PLA", preset_tier="standard")
    assert [x["id"] for x in led.recipes_for(pid)] == [r]
    assert led.get_recipe(r)["name"] == "Standard PLA"
    assert led.get_recipe(r)["preset_tier"] == "standard"


def test_exactly_one_default_recipe_per_part(led):
    pid = led.create_part(part_number="X", revision="A")
    r1 = led.add_recipe(pid, name="Standard PLA")
    r2 = led.add_recipe(pid, name="Fine PLA")
    led.set_default_recipe(pid, r1)
    led.set_default_recipe(pid, r2)   # must clear r1
    defaults = [x for x in led.recipes_for(pid) if x["is_default"]]
    assert [d["id"] for d in defaults] == [r2]
    assert led.default_recipe(pid)["id"] == r2


def test_update_recipe_rejects_unknown_column(led):
    pid = led.create_part(part_number="X", revision="A")
    r = led.add_recipe(pid)
    with pytest.raises(ValueError):
        led.update_recipe(r, bogus=1)


def test_archive_hides_a_recipe(led):
    pid = led.create_part(part_number="X", revision="A")
    r = led.add_recipe(pid)
    led.archive_recipe(r)
    assert led.recipes_for(pid) == []
    assert len(led.recipes_for(pid, include_archived=True)) == 1
    # an archived default is not returned by default_recipe either
    led.set_default_recipe(pid, r)
    assert led.default_recipe(pid) is None


def test_update_recipe_cannot_set_is_default(led):
    """The single-default invariant is structural: is_default is not writable
    via update_recipe -- the only path is set_default_recipe."""
    pid = led.create_part(part_number="X", revision="A")
    r = led.add_recipe(pid)
    with pytest.raises(ValueError):
        led.update_recipe(r, is_default=1)


def test_set_default_recipe_refuses_a_foreign_recipe(led):
    """A recipe from another part must never become this part's default."""
    a = led.create_part(part_number="A", revision="A")
    b = led.create_part(part_number="B", revision="A")
    ra = led.add_recipe(a, name="A-recipe")
    rb = led.add_recipe(b, name="B-recipe")
    led.set_default_recipe(a, ra)
    with pytest.raises(ValueError):
        led.set_default_recipe(a, rb)          # rb belongs to part b
    # part a's own default is untouched
    assert led.default_recipe(a)["id"] == ra


# ---------------- schema v3: filament spools ----------------

def test_schema_upgrades_v2_to_v3(tmp_path):
    import sqlite3 as _sq
    path = tmp_path / "ledger.db"
    con = _sq.connect(str(path)); con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY "
                "CHECK (id = 1), version INTEGER NOT NULL)")
    con.execute("INSERT INTO schema_version (id, version) VALUES (1, 2)")
    con.execute("CREATE TABLE parts (id TEXT PRIMARY KEY, part_number TEXT)")
    con.execute("INSERT INTO parts (id, part_number) VALUES ('p1','BRK')")
    # A real v2 ledger also has `badges` (Ledger.__init__ seeds it on every
    # open) -- included here so this synthetic v2 file matches what a
    # genuine v2 database looks like, instead of crashing _seed_badges().
    con.execute("""CREATE TABLE badges (
             id TEXT PRIMARY KEY,
             code TEXT NOT NULL UNIQUE,
             label TEXT NOT NULL,
             severity TEXT NOT NULL,
             auto INTEGER NOT NULL DEFAULT 0,
             archived INTEGER NOT NULL DEFAULT 0,
             created_at TEXT NOT NULL,
             updated_at TEXT NOT NULL)""")
    con.commit(); con.close()

    led = Ledger(path)
    try:
        assert led.query("SELECT version FROM schema_version")[0]["version"] == SCHEMA_VERSION
        names = {r["name"] for r in led.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"filament_spools", "filament_consumption"} <= names
        assert led.query("SELECT id FROM parts")[0]["id"] == "p1"
    finally:
        led.close()


def test_fresh_database_is_at_v3(led):
    assert led.query("SELECT version FROM schema_version")[0]["version"] == 3


# ---------------- filament spools ----------------

def test_spool_remaining_grams_is_initial_minus_consumption(led):
    sid = led.create_spool(spool_code="S-1", material="PLA", initial_grams=1000.0)
    led.add_consumption(sid, run_id=None, grams=120.0, basis="planned")
    led.add_consumption(sid, run_id=None, grams=80.0, basis="proportional")
    assert led.remaining_grams(sid) == 800.0
    row = [s for s in led.list_spools() if s["id"] == sid][0]
    assert row["remaining_grams"] == 800.0


def test_remaining_is_none_without_an_initial_weight(led):
    sid = led.create_spool(spool_code="S-2", material="PLA")
    assert led.remaining_grams(sid) is None
    row = [s for s in led.list_spools() if s["id"] == sid][0]
    assert row["remaining_grams"] is None


def test_exactly_one_loaded_spool_per_printer(led):
    a = led.create_spool(spool_code="A", material="PLA")
    b = led.create_spool(spool_code="B", material="PETG")
    led.set_loaded_spool("PRN1", a)
    led.set_loaded_spool("PRN1", b)
    assert led.loaded_spool("PRN1")["id"] == b
    loaded = [s for s in led.list_spools() if s["status"] == "in_use"
              and s["printer_serial"] == "PRN1"]
    assert [s["id"] for s in loaded] == [b]


def test_a_duplicate_spool_code_is_rejected(led):
    import sqlite3 as _sq
    led.create_spool(spool_code="DUP", material="PLA")
    with pytest.raises(_sq.IntegrityError):
        led.create_spool(spool_code="DUP", material="PETG")


def test_update_spool_rejects_unknown_column(led):
    sid = led.create_spool(spool_code="X", material="PLA")
    with pytest.raises(ValueError):
        led.update_spool(sid, bogus=1)


# ---------------- filament consumption + low-stock ----------------

def test_low_stock_lists_only_spools_below_the_threshold(led):
    low = led.create_spool(spool_code="LOW", material="PLA", initial_grams=100.0)
    led.add_consumption(low, run_id=None, grams=90.0, basis="planned")
    led.create_spool(spool_code="FULL", material="PLA", initial_grams=1000.0)
    led.create_spool(spool_code="UNK", material="PLA")   # no initial weight
    got = {s["spool_code"] for s in led.low_stock(50.0)}
    assert got == {"LOW"}


def test_consumption_is_linked_to_a_run(led):
    sid = led.create_spool(spool_code="S", material="PLA", initial_grams=500.0)
    led.add_consumption(sid, run_id="r1", grams=42.0, basis="planned")
    rows = led.consumption_for_run("r1")
    assert len(rows) == 1 and rows[0]["grams"] == 42.0
