"""Read-only coordination model for a printer/robot work cell.

The dashboard previously exposed printer state and robot state independently.
This module is the deliberately conservative boundary between them: it turns
the two telemetry streams into a cell state and explains why a plate-removal
routine is, or is not, eligible.  It does *not* command either machine yet.
That separation is important: a stale MQTT report, a missing gripper, or an
xArm fault must never become an implicit autonomous recovery action.
"""
from __future__ import annotations

import json
import pathlib
import threading
import time
from typing import Any


PRINTING_STATES = {"PREPARE", "RUNNING", "SLICING", "PAUSE", "PAUSED"}
FINISHED_STATES = {"FINISH"}
FAILED_STATES = {"FAILED"}
FRESH_REPORT_MAX_AGE_S = 10.0


def _truthy_error(value: Any) -> bool:
    """Printer APIs use both 0 and empty strings to mean no error."""
    return value not in (None, "", 0, "0", False, [], {})


class CellCoordinator:
    """Build a fail-closed state model from printer and robot telemetry."""

    def __init__(self, registry, robot=None, state_path=None):
        self.registry = registry
        self.robot = robot
        self.state_path = None if state_path is None else pathlib.Path(state_path)
        self._lock = threading.RLock()
        self._previous = {}
        self._incidents = {}
        self._events = []
        self._load()

    def _load(self):
        if self.state_path is None or not self.state_path.is_file():
            return
        try:
            data = json.loads(self.state_path.read_text())
            self._previous = dict(data.get("previous", {}))
            self._incidents = dict(data.get("incidents", {}))
            self._events = list(data.get("events", []))[-200:]
        except (OSError, ValueError, TypeError):
            self._previous, self._incidents, self._events = {}, {}, []

    def _save(self):
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"schema_version": 1,
            "previous": self._previous, "incidents": self._incidents,
            "events": self._events[-200:]}, indent=2) + "\n")
        temporary.replace(self.state_path)

    def snapshot(self) -> dict:
        try:
            robot = self.robot.snapshot() if self.robot is not None else None
        except Exception as exc:  # a status page must not crash on telemetry
            robot = {"available": False, "error": f"robot telemetry failed: {exc}"}
        with self._lock:
            cells = [self._cell(printer, robot)
                     for printer in self.registry.summaries()]
            self._record_transitions(cells)
            return {
                "schema_version": 1,
                "autonomy_enabled": False,
                "message": ("Cell coordination is monitoring-only. Plate removal "
                            "requires a commissioned gripper and an explicit routine."),
                "cells": cells,
                "recent_events": list(self._events[-20:]),
            }

    def acknowledge_incident(self, serial: str, operator_note: str = "") -> dict:
        """Record human review; never clear a printer or robot fault."""
        serial = str(serial)
        cells = self.snapshot()["cells"]
        cell = next((item for item in cells if item["printer_serial"] == serial), None)
        if cell is None:
            raise KeyError(f"unknown printer {serial}")
        if cell["state"] != "operator_review":
            raise ValueError("only a printer incident can be acknowledged")
        with self._lock:
            incident = {"acknowledged_at": time.time(),
                        "note": str(operator_note).strip()[:500]}
            self._incidents[serial] = incident
            self._events.append({"at": incident["acknowledged_at"],
                                 "printer_serial": serial,
                                 "kind": "incident_acknowledged"})
            self._save()
        return incident

    def _record_transitions(self, cells: list[dict]):
        changed = False
        active_serials = set()
        for cell in cells:
            serial, state = cell["printer_serial"], cell["state"]
            active_serials.add(serial)
            old = self._previous.get(serial)
            if old != state:
                self._previous[serial] = state
                self._events.append({"at": time.time(), "printer_serial": serial,
                                     "kind": "cell_state_changed",
                                     "from": old, "to": state})
                if state == "operator_review" and old != "operator_review":
                    self._incidents.pop(serial, None)
                changed = True
            cell["incident"] = self._incidents.get(serial)
        for serial in list(self._previous):
            if serial not in active_serials:
                self._previous.pop(serial, None)
                self._incidents.pop(serial, None)
                changed = True
        if changed:
            self._events = self._events[-200:]
            self._save()

    def _cell(self, printer: dict, robot: dict | None) -> dict:
        serial = str(printer.get("serial") or "")
        state, reasons = self._printer_state(printer)
        robot_reasons = self._robot_reasons(robot)
        pickup_reasons = reasons + robot_reasons
        return {
            "printer_serial": serial,
            "printer_name": printer.get("name") or serial,
            "state": state,
            "gcode_state": str(printer.get("gcode_state") or "").upper(),
            "ready_for_plate_removal": state == "finished" and not pickup_reasons,
            "blockers": pickup_reasons,
            "recommended_action": self._recommended_action(state, pickup_reasons),
        }

    @staticmethod
    def _printer_state(printer: dict) -> tuple[str, list[str]]:
        connection = str(printer.get("connection") or "").lower()
        report_age = printer.get("report_age_s")
        gcode = str(printer.get("gcode_state") or "").upper()
        if connection != "ok":
            return "printer_unavailable", [f"printer connection is {connection or 'unknown'}"]
        try:
            fresh_report = (report_age is not None and
                            float(report_age) <= FRESH_REPORT_MAX_AGE_S)
        except (TypeError, ValueError):
            fresh_report = False
        if not fresh_report:
            return "printer_unavailable", ["printer status is stale"]
        if _truthy_error(printer.get("print_error")) or printer.get("fail_reason"):
            return "operator_review", ["printer reports an error; operator review is required"]
        if gcode in FAILED_STATES:
            return "operator_review", ["print failed; do not remove the plate automatically"]
        if gcode in PRINTING_STATES:
            return "printing", ["print is still active"]
        if gcode in FINISHED_STATES:
            # Cooling, toolhead clearance, and plate presence will become
            # explicit gates in the executable removal routine.
            return "finished", []
        return "idle", ["printer has not reported a completed print"]

    @staticmethod
    def _robot_reasons(robot: dict | None) -> list[str]:
        if robot is None:
            return ["robot backend is not configured"]
        if not robot.get("available"):
            return [robot.get("error") or "robot backend is unavailable"]
        safety = robot.get("safety") or {}
        if not safety.get("ready", False):
            return ["robot safety preflight is not ready"]
        gripper = robot.get("gripper") or {}
        if gripper.get("disabled") or not gripper.get("kind"):
            return ["a commissioned physical gripper is required"]
        if gripper.get("plate_routines_enabled") is False:
            return ["the physical gripper must be commissioned before plate routines"]
        return []

    @staticmethod
    def _recommended_action(state: str, blockers: list[str]) -> str:
        if state == "printing":
            return "Monitor the print; no robot motion is permitted."
        if state == "operator_review":
            return "Inspect the printer and explicitly resolve the incident."
        if state == "finished" and blockers:
            return "Resolve the listed robot prerequisites before plate removal."
        if state == "finished":
            return "Validate cooling, toolhead clearance, vision and plate presence."
        return "Wait for a fresh completed-print report."
