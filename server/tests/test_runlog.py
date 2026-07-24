import pytest

from server.ledger import Ledger
from server.runlog import RunRecorder


class FakeRegistry:
    """Emits a scripted list of summaries, one list per tick. Matches the
    shape build_summary() produces (server/printer.py) closely enough for the
    recorder: serial, name, gcode_state, layer_num, total_layer_num, hms,
    subtask_name."""

    def __init__(self, script):
        self._script = list(script)
        self._i = 0

    def summaries(self):
        if self._i < len(self._script):
            out = self._script[self._i]
            self._i += 1
            return out
        return self._script[-1] if self._script else []


def summary(serial="S1", name="A1", state="IDLE", layer=None, total=None,
            hms=(), subtask=None):
    return {"serial": serial, "name": name, "gcode_state": state,
            "layer_num": layer, "total_layer_num": total, "hms": list(hms),
            "subtask_name": subtask}


@pytest.fixture
def led(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    yield ledger
    ledger.close()


def run_ticks(led, script, **kwargs):
    rec = RunRecorder(FakeRegistry(script), led, **kwargs)
    for _ in script:
        rec.tick()
    return rec


def test_opens_an_unattributed_run_when_a_print_appears(led):
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING", layer=1, total=100)]])
    runs = led.list_runs()
    assert len(runs) == 1
    assert runs[0]["source"] == "unattributed"
    assert runs[0]["end_state"] is None


def test_prepare_counts_as_the_start_of_a_print(led):
    run_ticks(led, [[summary(state="IDLE")], [summary(state="PREPARE")]])
    assert len(led.list_runs()) == 1


def test_does_not_open_a_second_run_while_one_is_open(led):
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="PREPARE")],
                    [summary(state="RUNNING")],
                    [summary(state="RUNNING")]])
    assert len(led.list_runs()) == 1


def test_adopts_a_run_the_start_route_already_opened(led):
    existing = led.open_run(printer_serial="S1", printer_name="A1",
                            source="queue", sd_path="/Benchy.gcode.3mf")
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING", layer=1, total=100)]])
    runs = led.list_runs()
    assert len(runs) == 1, "the recorder opened a duplicate run"
    assert runs[0]["id"] == existing
    assert runs[0]["source"] == "queue"


def test_records_the_printer_name_as_a_snapshot(led):
    run_ticks(led, [[summary(state="IDLE", name="Bench A1")],
                    [summary(state="RUNNING", name="Bench A1")]])
    assert led.list_runs()[0]["printer_name"] == "Bench A1"


def test_writes_a_state_change_event_on_open(led):
    run_ticks(led, [[summary(state="IDLE")], [summary(state="RUNNING")]])
    run_id = led.list_runs()[0]["id"]
    kinds = [e["kind"] for e in led.events_for(run_id)]
    assert "state_change" in kinds


def test_layer_progress_updates_the_run_and_writes_no_events(led):
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING", layer=1, total=100)],
                    [summary(state="RUNNING", layer=2, total=100)],
                    [summary(state="RUNNING", layer=3, total=100)]])
    run = led.list_runs()[0]
    assert run["last_layer"] == 3
    assert run["total_layers"] == 100
    kinds = [e["kind"] for e in led.events_for(run["id"])]
    assert kinds.count("state_change") == 1
    assert not [k for k in kinds if k.startswith("layer")], \
        "layer progress must not append events -- a 1200-layer print would " \
        "write 1200 rows of nothing"


def test_a_new_hms_code_raises_an_event(led):
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING")],
                    [summary(state="RUNNING", hms=["0300_1100_0002_0001"])]])
    run_id = led.list_runs()[0]["id"]
    raised = [e for e in led.events_for(run_id) if e["kind"] == "hms_raised"]
    assert len(raised) == 1
    assert raised[0]["payload"]["code"] == "0300_1100_0002_0001"


def test_an_hms_code_that_persists_does_not_re_raise(led):
    code = "0300_1100_0002_0001"
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING")],
                    [summary(state="RUNNING", hms=[code])],
                    [summary(state="RUNNING", hms=[code])],
                    [summary(state="RUNNING", hms=[code])]])
    run_id = led.list_runs()[0]["id"]
    raised = [e for e in led.events_for(run_id) if e["kind"] == "hms_raised"]
    assert len(raised) == 1


def test_a_cleared_hms_code_writes_a_cleared_event(led):
    code = "0300_1100_0002_0001"
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING")],
                    [summary(state="RUNNING", hms=[code])],
                    [summary(state="RUNNING", hms=[])]])
    run_id = led.list_runs()[0]["id"]
    kinds = [e["kind"] for e in led.events_for(run_id)]
    assert kinds.count("hms_raised") == 1
    assert kinds.count("hms_cleared") == 1


def test_an_hms_badge_is_applied_to_the_run(led):
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING")],
                    [summary(state="RUNNING", hms=["0300_1100_0002_0001"])]])
    run_id = led.list_runs()[0]["id"]
    assert [b["code"] for b in led.run_badges(run_id)] == ["hms_error"]


class FakeDetection:
    def __init__(self, stopped_by_monitor=False):
        self._snap = {"stopped_by_monitor": stopped_by_monitor}

    def snapshot(self, serial):
        return dict(self._snap)


def test_finish_closes_the_run_and_creates_pieces(led):
    existing = led.open_run(printer_serial="S1", printer_name="A1",
                            source="queue", copies_planned=4,
                            planned_grams=20.0)
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING", layer=100, total=100)],
                    [summary(state="FINISH", layer=100, total=100)]])
    run = led.get_run(existing)
    assert run["end_state"] == "FINISH"
    assert run["ended_at"] is not None
    assert len(led.pieces_for(existing)) == 4


def test_finish_records_planned_grams_as_the_actual(led):
    existing = led.open_run(printer_serial="S1", source="queue",
                            planned_grams=20.0)
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING", layer=100, total=100)],
                    [summary(state="FINISH")]])
    run = led.get_run(existing)
    assert run["actual_grams"] == pytest.approx(20.0)
    assert run["actual_grams_basis"] == "planned"


def test_a_failure_prorates_grams_by_layer_and_says_so(led):
    existing = led.open_run(printer_serial="S1", source="queue",
                            planned_grams=20.0)
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING", layer=25, total=100)],
                    [summary(state="FAILED", layer=25, total=100)]])
    run = led.get_run(existing)
    assert run["end_state"] == "FAILED"
    assert run["actual_grams"] == pytest.approx(5.0)
    assert run["actual_grams_basis"] == "proportional"


def test_a_monitor_stop_is_distinguished_from_a_plain_failure(led):
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING")],
                    [summary(state="FAILED")]],
              detection=FakeDetection(stopped_by_monitor=True))
    run = led.list_runs()[0]
    assert run["end_state"] == "STOPPED_BY_MONITOR"
    assert run["stopped_by_monitor"] == 1
    assert "autostop" in {b["code"] for b in led.run_badges(run["id"])}


def test_going_idle_without_finishing_closes_as_unknown(led):
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING")],
                    [summary(state="IDLE")]])
    assert led.list_runs()[0]["end_state"] == "UNKNOWN"


def test_grams_stay_null_when_nothing_was_planned(led):
    run_ticks(led, [[summary(state="IDLE")],
                    [summary(state="RUNNING", layer=5, total=100)],
                    [summary(state="FAILED")]])
    run = led.list_runs()[0]
    assert run["actual_grams"] is None
    assert run["actual_grams_basis"] is None


def test_reconcile_startup_closes_a_run_left_open_by_a_restart(led):
    stale = led.open_run(printer_serial="S1", printer_name="A1",
                         source="queue")
    rec = RunRecorder(FakeRegistry([[summary(state="IDLE")]]), led)
    rec.reconcile_startup()
    run = led.get_run(stale)
    assert run["end_state"] == "UNKNOWN"
    kinds = [e["kind"] for e in led.events_for(stale)]
    assert "state_change" in kinds


def test_reconcile_startup_leaves_a_genuinely_running_print_alone(led):
    live = led.open_run(printer_serial="S1", printer_name="A1",
                        source="queue")
    rec = RunRecorder(FakeRegistry([[summary(state="RUNNING")]]), led)
    rec.reconcile_startup()
    assert led.get_run(live)["end_state"] is None


def test_a_ledger_that_raises_never_escapes_the_tick(led):
    class Exploding:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise RuntimeError("ledger on fire")
            return boom

    rec = RunRecorder(FakeRegistry([[summary(state="RUNNING")]]), Exploding())
    rec.tick()   # must not raise
