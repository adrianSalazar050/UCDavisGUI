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
import time

from .ledger import DETECTOR, badge_id_for
from .printer import BUSY_STATES

log = logging.getLogger("server.runlog")

TICK_S = 1.0

# How long to wait for a printer to report its state after a restart before
# giving up and closing its orphaned run UNKNOWN. See _reconcile_pending.
RECONCILE_DEADLINE_S = 30.0

# gcode_state values that mean the print is over. FAILED is included because
# master.md section 3.1 verified on hardware that a STOPPED print reports
# FAILED -- there is no separate "stopped" state to look for.
TERMINAL_STATES = ("FINISH", "FAILED", "IDLE")


class RunRecorder:
    """Poll summaries, write runs and events. `detection` is optional and is
    only read (for the stopped_by_monitor latch); None means the auto-stop
    attribution simply never fires, which is what the desktop build gets."""

    def __init__(self, registry, ledger, *, detection=None,
                 tick_s: float = TICK_S,
                 reconcile_deadline_s: float = RECONCILE_DEADLINE_S,
                 clock=time.monotonic):
        self.registry = registry
        self.ledger = ledger
        self.detection = detection
        self._tick_s = tick_s
        self._reconcile_deadline_s = reconcile_deadline_s
        self._clock = clock
        self._prev: dict[str, dict] = {}
        # run_id -> serial for runs open when this recorder started. Resolved
        # LAZILY on ticks (see _reconcile_pending), never synchronously at
        # start(): at startup the MQTT links have not delivered a report, so a
        # printer that is physically mid-print still looks idle. Closing then
        # would mislabel a running print UNKNOWN and split it from its
        # attributed queue row -- a real, hardware-confirmed bug.
        self._pending: dict[str, str] = {}
        self._reconcile_until: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    # ---------------- the tick ----------------

    def tick(self) -> None:
        summaries = self.registry.summaries() or []
        by_serial = {s["serial"]: s for s in summaries
                     if isinstance(s, dict) and s.get("serial")}
        self._reconcile_pending(by_serial)
        for summary in summaries:
            serial = summary.get("serial") if isinstance(summary, dict) else None
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

        run = self.ledger.find_open_run(serial)
        if run is None:
            return
        if busy:
            self._progress(run, summary)
        self._hms(serial, run["id"], prev, summary)
        if was_busy and not busy and state in TERMINAL_STATES:
            self._end(serial, run, summary, state)

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

    def _progress(self, run: dict, summary: dict) -> None:
        """Update the run's layer counters IN PLACE. Deliberately not events:
        a 100-layer print would otherwise write 100 rows containing no
        information, and a 1200-layer print 1200."""
        fields = {}
        layer = summary.get("layer_num")
        total = summary.get("total_layer_num")
        if layer is not None and layer != run.get("last_layer"):
            fields["last_layer"] = layer
        if total and total != run.get("total_layers"):
            fields["total_layers"] = total
        if fields:
            self.ledger.update_run(run["id"], **fields)

    def _hms(self, serial: str, run_id: str, prev: dict | None,
             summary: dict) -> None:
        """Diff the ALREADY-DECODED code strings build_summary() produced."""
        now = {c for c in (summary.get("hms") or []) if isinstance(c, str)}
        before = set()
        if prev:
            before = {c for c in (prev.get("hms") or []) if isinstance(c, str)}
        for code in sorted(now - before):
            self.ledger.add_event(printer_serial=serial, run_id=run_id,
                                  kind="hms_raised", source="server",
                                  payload={"code": code})
            self.ledger.add_run_badge(run_id, badge_id_for("hms_error"),
                                      applied_by=DETECTOR,
                                      note=f"HMS {code}")
        for code in sorted(before - now):
            self.ledger.add_event(printer_serial=serial, run_id=run_id,
                                  kind="hms_cleared", source="server",
                                  payload={"code": code})

    def _end(self, serial: str, run: dict, summary: dict,
             state: str) -> None:
        run = self.ledger.get_run(run["id"]) or run
        end_state = state if state in ("FINISH", "FAILED") else "UNKNOWN"
        stopped = self._stopped_by_monitor(serial)
        if end_state == "FAILED" and stopped:
            end_state = "STOPPED_BY_MONITOR"

        grams, basis = self._grams(run, end_state)
        closed = self.ledger.close_run(
            run["id"], end_state=end_state,
            stopped_by_monitor=int(bool(stopped)),
            **({"actual_grams": grams, "actual_grams_basis": basis}
               if basis else {}))
        self.ledger.add_event(printer_serial=serial, run_id=run["id"],
                              kind="state_change", source="server",
                              payload={"to": state,
                                       "end_state": end_state})
        if end_state == "STOPPED_BY_MONITOR":
            self.ledger.add_run_badge(run["id"], badge_id_for("autostop"),
                                      applied_by=DETECTOR)
        copies = run.get("copies_planned") or 1
        self.ledger.create_pieces(run["id"], int(copies),
                                  part_id=run.get("part_id"),
                                  order_line_id=run.get("order_line_id"))

        # Decrement the spool this run drew from, if one was loaded when it
        # started (the start route stamps run.spool_id). Uses the run's own
        # actual_grams estimate + basis -- the printer never reports consumed
        # filament, so this inherits that estimate's honesty (the basis says
        # which kind). No spool, or no gram estimate -> no consumption row.
        #
        # Gated on `closed` (this call is the one that transitioned the run
        # terminal), NOT just on reaching a terminal state: add_consumption is
        # not idempotent, so re-observing FINISH must not decrement twice.
        # close_run's WHERE end_state IS NULL makes the run row idempotent;
        # this makes the spool decrement ride the same single transition.
        spool_id = run.get("spool_id")
        if closed and spool_id and grams:
            try:
                self.ledger.add_consumption(spool_id, run_id=run["id"],
                                            grams=grams, basis=basis)
            except Exception as e:  # noqa: BLE001
                log.error("could not record consumption for run %s: %s",
                          run["id"], e)

    def _stopped_by_monitor(self, serial: str) -> bool:
        if self.detection is None:
            return False
        try:
            snap = self.detection.snapshot(serial) or {}
        except Exception as e:  # noqa: BLE001
            log.warning("detection snapshot failed for %s: %s", serial, e)
            return False
        return bool(snap.get("stopped_by_monitor"))

    @staticmethod
    def _grams(run: dict, end_state: str):
        """-> (grams, basis). The printer does NOT report filament consumed,
        so this is always an estimate and the basis column says which kind.

        The proportional estimate is wrong in detail -- layers are not equal
        mass -- which is exactly why the basis is recorded rather than the
        number being presented bare.
        """
        planned = run.get("planned_grams")
        if not planned:
            return None, None
        if end_state == "FINISH":
            return float(planned), "planned"
        layer = run.get("last_layer") or 0
        total = run.get("total_layers") or 0
        if not total:
            return None, None
        fraction = max(0.0, min(1.0, float(layer) / float(total)))
        return round(float(planned) * fraction, 3), "proportional"

    # ---------------- thread ----------------

    def _loop(self) -> None:
        while not self._stop.wait(self._tick_s):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                log.exception("run recorder tick failed: %s", e)

    def _arm_reconcile(self) -> None:
        """Snapshot the runs open before this session. _reconcile_pending
        resolves them once printers report -- see _pending's comment for why
        this is deferred rather than done here and now."""
        try:
            self._pending = {r["id"]: r["printer_serial"]
                             for r in self.ledger.open_runs()}
            self._reconcile_until = self._clock() + self._reconcile_deadline_s
        except Exception as e:  # noqa: BLE001
            log.exception("arming startup reconciliation failed: %s", e)

    def _reconcile_pending(self, by_serial: dict) -> None:
        """Resolve each run left open by a restart, ONCE its printer reports.

        Only a printer whose summary shows connection == "ok" gives a verdict:
        busy  -> still printing; leave the run open so _begin re-adopts it and
                 its source/attribution survives the restart. Drop pending.
        idle  -> the print ended while we were down; close UNKNOWN.
        A printer not yet reporting (connection not ok) stays pending and is
        retried next tick, until the deadline -- then closed UNKNOWN, because
        an open row that never resolves poisons every 'what is running' query
        forever and UNKNOWN is the honest verdict.
        """
        if not self._pending:
            return
        overdue = (self._reconcile_until is not None
                   and self._clock() >= self._reconcile_until)
        for run_id, serial in list(self._pending.items()):
            summ = by_serial.get(serial)
            if summ is not None and summ.get("connection") == "ok":
                state = (summ.get("gcode_state") or "").upper()
                if state in BUSY_STATES:
                    del self._pending[run_id]   # still live; _begin adopts it
                else:
                    self._close_orphan(run_id, serial,
                                       "the printer was idle after the restart")
                    del self._pending[run_id]
            elif overdue:
                self._close_orphan(
                    run_id, serial,
                    "the printer never reported after the restart")
                del self._pending[run_id]

    def _close_orphan(self, run_id: str, serial: str, reason: str) -> None:
        self.ledger.close_run(run_id, end_state="UNKNOWN")
        self.ledger.add_event(
            printer_serial=serial, run_id=run_id, kind="state_change",
            source="server",
            payload={"end_state": "UNKNOWN",
                     "reason": f"server restarted while this run was open; "
                               f"{reason}"})
        log.warning("closed orphaned run %s for %s as UNKNOWN (%s)",
                    run_id, serial, reason)

    def start(self) -> None:
        self._arm_reconcile()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
