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
