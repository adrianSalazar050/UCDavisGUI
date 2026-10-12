from server.cell import CellCoordinator


class Registry:
    def __init__(self, summaries):
        self._summaries = summaries

    def summaries(self):
        return self._summaries


class Robot:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def snapshot(self):
        return self._snapshot


def _printer(**extra):
    state = {
        "serial": "P1", "name": "Printer 1", "connection": "ok",
        "report_age_s": 0.1, "gcode_state": "FINISH", "print_error": 0,
        "fail_reason": "",
    }
    state.update(extra)
    return state


def _robot(**extra):
    state = {
        "available": True,
        "safety": {"ready": True},
        "gripper": {"kind": "vacuum", "disabled": False},
    }
    state.update(extra)
    return state


def test_finished_print_needs_a_ready_robot_and_gripper():
    cell = CellCoordinator(Registry([_printer()]), Robot(_robot())).snapshot()
    result = cell["cells"][0]
    assert result["state"] == "finished"
    assert result["ready_for_plate_removal"] is True
    assert result["blockers"] == []


def test_finished_print_is_blocked_without_commissioned_gripper():
    robot = _robot(gripper={"kind": None, "disabled": False})
    result = CellCoordinator(Registry([_printer()]), Robot(robot)).snapshot()["cells"][0]
    assert result["ready_for_plate_removal"] is False
    assert "commissioned physical gripper" in result["blockers"][0]


def test_failed_print_requires_operator_review_not_auto_removal():
    result = CellCoordinator(
        Registry([_printer(gcode_state="FAILED")]), Robot(_robot())
    ).snapshot()["cells"][0]
    assert result["state"] == "operator_review"
    assert result["ready_for_plate_removal"] is False


def test_stale_printer_status_is_not_eligible_for_removal():
    result = CellCoordinator(
        Registry([_printer(report_age_s=11.0)]), Robot(_robot())
    ).snapshot()["cells"][0]
    assert result["state"] == "printer_unavailable"
    assert result["ready_for_plate_removal"] is False


def test_incident_acknowledgement_is_persisted_without_clearing_fault(tmp_path):
    coordinator = CellCoordinator(
        Registry([_printer(gcode_state="FAILED")]), Robot(_robot()),
        state_path=tmp_path / "cell_state.json")
    coordinator.snapshot()
    incident = coordinator.acknowledge_incident("P1", "Checked printer")
    assert incident["note"] == "Checked printer"
    restored = CellCoordinator(
        Registry([_printer(gcode_state="FAILED")]), Robot(_robot()),
        state_path=tmp_path / "cell_state.json").snapshot()["cells"][0]
    assert restored["state"] == "operator_review"
    assert restored["incident"]["note"] == "Checked printer"
