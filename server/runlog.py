"""RunRecorder: turn gcode_state transitions into ledger rows.

The one component that observes prints. It polls registry.summaries() and
writes to the ledger; it opens no socket, subscribes to no MQTT topic, and
sends no command, so it cannot disturb a print no matter how wrong it is.

Everything it writes is derived from the difference between two consecutive
summaries. `printer.BUSY_STATES` is reused as the "a print is happening here"
predicate rather than restating that list, so the two can never drift.

Note on HMS: build_summary() (server/printer.py) has ALREADY decoded the raw
attr/code integers into 'AAAA_BBBB_CCCC_DDDD' strings by the time a summary
reaches here. Diff those strings; do not call decode_hms again.
"""
from __future__ import annotations

import logging
import threading

from .ledger import badge_id_for
from .printer import BUSY_STATES

log = logging.getLogger("server.runlog")

TICK_S = 1.0


class RunRecorder:
    """Poll summaries, write runs and events. `detection` is optional and is
    only read (for the stopped_by_monitor latch); None means the auto-stop
    attribution simply never fires, which is what the desktop build gets."""

    def __init__(self, registry, ledger, *, detection=None,
                 tick_s: float = TICK_S):
        self.registry = registry
        self.ledger = ledger
        self.detection = detection
        self._tick_s = tick_s
        self._prev: dict[str, dict] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    # ---------------- the tick ----------------

    def tick(self) -> None:
        for summary in self.registry.summaries() or []:
            serial = summary.get("serial")
            if not serial:
                continue
            try:
                self._one(serial, summary)
            except Exception as e:  # noqa: BLE001
                # One printer's bad summary must not stop the others being
                # recorded, and must never propagate into the print path.
                log.exception("run recording failed for %s: %s", serial, e)
            self._prev[serial] = summary

    def _one(self, serial: str, summary: dict) -> None:
        state = (summary.get("gcode_state") or "").upper()
        prev = self._prev.get(serial)
        prev_state = (prev.get("gcode_state") or "").upper() if prev else None
        busy = state in BUSY_STATES
        was_busy = prev_state in BUSY_STATES if prev_state else False

        if busy and not was_busy:
            self._begin(serial, summary, state)

    def _begin(self, serial: str, summary: dict, state: str) -> None:
        """Open a run, or adopt the one the start route already opened.

        Adoption is why the start route creates its row BEFORE publishing: it
        is the only place that knows the queue job, and if this tick got there
        first the attributed row and an unattributed one would both exist.
        """
        run = self.ledger.find_open_run(serial)
        if run is None:
            run_id = self.ledger.open_run(
                printer_serial=serial,
                printer_name=summary.get("name") or "",
                source="unattributed",
                subtask_name=summary.get("subtask_name"),
                total_layers=summary.get("total_layer_num"),
                last_layer=summary.get("layer_num"))
        else:
            # Adopted. Fill in only what the start route could not know --
            # the printer reports subtask_name and total_layer_num itself,
            # and it had not done so yet when the route opened the row.
            run_id = run["id"]
            fields = {}
            if not run.get("printer_name"):
                fields["printer_name"] = summary.get("name") or ""
            if summary.get("subtask_name"):
                fields["subtask_name"] = summary.get("subtask_name")
            if summary.get("total_layer_num"):
                fields["total_layers"] = summary.get("total_layer_num")
            if fields:
                self.ledger.update_run(run_id, **fields)
        self.ledger.add_event(printer_serial=serial, run_id=run_id,
                              kind="state_change", source="server",
                              payload={"to": state})

    # ---------------- thread ----------------

    def _loop(self) -> None:
        while not self._stop.wait(self._tick_s):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                log.exception("run recorder tick failed: %s", e)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
