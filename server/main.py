"""FastAPI app: /api/printers, /api/frame/latest, /ws, static frontend."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import posixpath
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import (FastAPI, File, Form, HTTPException, Request, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import runs, sdcard, slicejobs, threemf
from .detection import CLASSES  # the 6 valid armed classes
from .printer import PrinterBusy
from .registry import DuplicateSerial
from .sdcard import SdError
from .ledger import END_STATES, PIECE_STATUSES
from .store import CAMERA_SOURCES, model_mismatch

# How long to wait for a started print to show up in gcode_state before
# reporting it as not-started. The A1 goes FAILED/IDLE -> PREPARE within a
# couple of seconds (measured on hardware); 8 s is slack for a busy printer.
START_VERIFY_S = 8.0
START_POLL_S = 0.25
STARTED_STATES = ("PREPARE", "RUNNING", "SLICING")


def verify_start(svc, *, timeout: float = START_VERIFY_S,
                 poll: float = START_POLL_S, clock=time.monotonic,
                 sleep=time.sleep) -> bool:
    """Poll gcode_state until the printer reports a print starting.

    Injectable clock/sleep so tests don't spend real seconds.
    """
    deadline = clock() + timeout
    while clock() < deadline:
        state = (svc.summary().get("gcode_state") or "").upper()
        if state in STARTED_STATES:
            return True
        sleep(poll)
    return False


def _maybe(registry, method: str, serial: str):
    """Call an optional registry accessor, or None if this registry (or a
    test fake) does not have it. printer_bed_type/printer_nozzle exist on
    PrinterRegistry but not on every fake."""
    fn = getattr(registry, method, None)
    if fn is None:
        return None
    try:
        return fn(serial)
    except Exception:  # noqa: BLE001
        return None


def _close_run_quietly(ledger, run_id, end_state: str, serial: str,
                       detail: str) -> None:
    """Close a ledger run without ever letting a ledger problem surface as a
    print-path failure."""
    if ledger is None or run_id is None:
        return
    try:
        ledger.close_run(run_id, end_state=end_state)
        ledger.add_event(printer_serial=serial, run_id=run_id,
                         kind="start_unconfirmed", source="server",
                         payload={"detail": detail})
    except Exception as e:  # noqa: BLE001
        log.error("could not close ledger run %s: %s", run_id, e)

log = logging.getLogger("server.main")

WS_POLL_S = 0.25      # summary sampled at 4 Hz -> at most ~4 pushes/s
WS_HEARTBEAT_S = 5.0  # push even when unchanged, keeps report_age_s fresh


class AddPrinter(BaseModel):
    """Pydantic rejects non-strings at the body-parse layer -> 422.

    That matters: PrinterConfig's type validation lives in from_dict(), NOT in
    its constructor, so `PrinterConfig(serial=None, ...)` still coerces to "".
    The registry keys on serial, so a None reaching it would collapse printers
    onto one entry. This model is what keeps a request body off that path --
    do not bypass it by building a PrinterConfig straight from raw request data.
    """

    host: str
    serial: str
    access_code: str
    name: str = ""
    capture: bool = False
    # None = "work it out from the serial"; "" = a deliberate Unknown, which
    # disables the printer-model check for this printer.
    model_id: str | None = None


class EditPrinter(BaseModel):
    """Edits a registered printer's connection info. `serial` is deliberately
    absent -- it's the identity key and is not editable here. `access_code`
    defaults to "" ("keep the current one"): the client never receives the
    real code back (see AddPrinter/store.py), so it has nothing to round-trip
    into an edit form."""

    host: str
    access_code: str = ""     # blank = keep the current code
    name: str = ""
    capture: bool = False
    # None = not submitted, keep the stored model. "" = the user picked
    # Unknown. Distinct, so an old client omitting the field can't wipe it.
    model_id: str | None = None
    # None = not submitted, keep the stored plate. Unlike model_id there is
    # no "" Unknown sentinel here -- every printer HAS a plate installed, so
    # registry.update() degrades any value outside store.BED_TYPES to
    # DEFAULT_BED_TYPE rather than accepting it verbatim (see
    # PrinterConfig.bed_type for why getting this wrong is a real, measured
    # print failure: 35 C vs the 65 C this lab's Textured PEI Plate needs).
    bed_type: str | None = None
    # None = not submitted, keep the stored diameter. Mirrors bed_type
    # exactly: no "" Unknown sentinel, every printer HAS a nozzle installed,
    # so registry.update() degrades any value outside store.NOZZLES to
    # DEFAULT_NOZZLE rather than accepting it verbatim (see
    # PrinterConfig.nozzle -- a wrong diameter selects the wrong machine
    # profile when slicing).
    nozzle: str | None = None


class DetectionUpdate(BaseModel):
    camera_source: str | None = None
    camera_index: int | None = None
    conf: float | None = None
    armed_classes: list[str] | None = None
    detect_enabled: bool | None = None
    # [x, y, w, h] fractions of the frame, or null to clear it. NOTE: null is a
    # real value here ("use the whole frame"), so this field cannot use the
    # drop-the-Nones convention the others do -- see put_detection.
    roi: list[float] | None = None


class ArmBody(BaseModel):
    armed: bool


class PatchRun(BaseModel):
    """Operator corrections to a recorded run.

    end_state is editable because an operator stopping a print at the
    printer's own screen is indistinguishable from a genuine failure -- both
    report FAILED (master.md section 3.1) -- so the recorder writes the honest
    default and a human fixes it here.
    """

    end_state: str | None = None
    actual_grams: float | None = None
    notes: str | None = None


class PatchPiece(BaseModel):
    status: str | None = None
    inspected_by: str | None = None
    notes: str | None = None


class BulkPieces(BaseModel):
    status: str
    inspected_by: str | None = None
    overrides: list[dict] = []


class BadgeRef(BaseModel):
    code: str
    note: str | None = None


class NewPart(BaseModel):
    part_number: str
    revision: str = "A"
    name: str = ""
    notes: str | None = None


class EditPart(BaseModel):
    part_number: str | None = None
    revision: str | None = None
    name: str | None = None
    notes: str | None = None


class RecipeBody(BaseModel):
    name: str = ""
    preset_tier: str | None = None
    filament_material: str | None = None
    nozzle: str | None = None
    bed_type: str | None = None
    supports: bool | None = None
    copies_per_plate: int | None = None
    expected_seconds: float | None = None
    expected_grams: float | None = None
    is_default: bool | None = None


class AddQueueJob(BaseModel):
    """POST body for queuing an SD-card file. sd_path is the only
    user-supplied field -- id/name/seconds/grams/source are all derived
    server-side from the fetched + parsed .gcode.3mf, never trusted from the
    client."""

    sd_path: str


class ReorderQueueJobs(BaseModel):
    ids: list[str]


def _comparable(printers: list[dict]) -> list[dict]:
    """report_age_s ticks every sample; ignore it when deciding whether the
    state meaningfully changed."""
    return [{k: v for k, v in p.items() if k != "report_age_s"}
            for p in printers]


def _with_detection(printers: list[dict], detection) -> list[dict]:
    """Attach a `detection` object to each summary (None unless it's the
    capture printer), plus `detection_available`: whether this SERVER has a
    detector wired at all. Detection state lives in detection.py, not the
    service.

    The flag exists because `detection=None` is a real deployment, not just a
    test seam: the desktop build passes it (torch/ultralytics are deliberately
    out of that bundle). Without the flag the client cannot tell "you haven't
    marked a capture printer yet" from "this build has no detector" -- both
    just look like a missing detection object. Reported 2026-07-23 from the
    packaged app: the Detection page told the user to mark a capture printer on
    the Overview page, they did, and nothing changed, because marking one
    cannot conjure a detector that was never bundled.
    """
    available = detection is not None
    for p in printers:
        p["detection_available"] = available
        if available:
            p["detection"] = detection.snapshot(p.get("serial"))
    return printers


def _with_bed_type(printers: list[dict], registry) -> list[dict]:
    """Attach each printer's CONFIGURED build plate to its summary.

    Deliberately not part of svc.summary()/PrinterService/MockPrinter, the
    way model_id is: nothing about the live MQTT connection needs bed_type
    (see PrinterRegistry.printer_bed_type), so growing every service
    implementation another constructor argument for it would be pure churn.
    Reading it straight from the registry here, at request time, is the same
    shape as _with_detection above.

    This has to run everywhere summaries() does (GET /api/printers and both
    /ws send sites), not just the PUT response: EditPrinterForm prefills its
    form state from the printer summary it already has, not from the PUT
    response of a save that hasn't happened yet. Without this, the form
    would always show DEFAULT_BED_TYPE on open and silently overwrite a
    deliberately-chosen plate the next time someone saves an unrelated field
    (name, capture, ...) -- exactly the class of bug access_code's own
    keep-blank-means-keep-current convention exists to avoid.
    """
    for p in printers:
        p["bed_type"] = registry.printer_bed_type(p.get("serial"))
    return printers


def _with_nozzle(printers: list[dict], registry) -> list[dict]:
    """Attach each printer's CONFIGURED nozzle diameter to its summary.

    Mirrors _with_bed_type exactly, for the same reason: nothing about the
    live MQTT connection needs nozzle (see PrinterRegistry.printer_nozzle),
    and this has to run everywhere summaries() does (GET /api/printers and
    both /ws send sites), not just the PUT response, so EditPrinterForm can
    prefill from the summary it already has instead of always showing
    DEFAULT_NOZZLE and silently overwriting a deliberately-chosen diameter
    on the next unrelated save.
    """
    for p in printers:
        p["nozzle"] = registry.printer_nozzle(p.get("serial"))
    return printers


class LoginBody(BaseModel):
    password: str = ""


def create_app(registry, runs_dir: pathlib.Path,
               frontend_dist: pathlib.Path | None = None,
               detection=None, queue=None, slicer=None, auth=None,
               ledger=None, recorder=None, partstore=None) -> FastAPI:
    """`registry` is anything with summaries() -> list[dict], get(serial),
    add(...), remove(serial) (PrinterRegistry, or a test fake). `queue` is
    anything with add(serial, job), remove(serial, id) -> bool,
    reorder(serial, ids), get(serial) -> list, totals(serial) -> dict
    (PrintQueue, or a test fake); None disables the queue routes entirely,
    same "None means inert" convention as `detection`. `slicer` is a
    SliceCoordinator (or a test fake); None disables the slice routes
    entirely, same "None means inert" convention.

    `ledger` is a server.ledger.Ledger (or a test fake); None disables every
    traceability route, the same "None means inert" convention as `queue`,
    `detection`, and `slicer`. `partstore` stores model bytes; None disables
    model up/download (part metadata still works).
    """

    @asynccontextmanager
    async def lifespan(_app):
        # Track what actually STARTED, and stop only that, in reverse order.
        # With two lifecycle components, a raise from the second start() must
        # not skip the finally entirely and leave the first one (detection)
        # running while the app fails to boot -- that was possible when both
        # start() calls sat outside the try.
        started = []
        try:
            if detection is not None:
                detection.start()
                started.append(detection)
            if slicer is not None:
                slicer.start()
                started.append(slicer)
            if recorder is not None:
                recorder.start()
                started.append(recorder)
            yield
        finally:
            for component in reversed(started):
                component.stop()

    app = FastAPI(title="bambu-monitor", lifespan=lifespan)

    # --- authentication ---------------------------------------------------
    # auth=None means inert (same convention as queue/detection/slicer): the
    # desktop app and the dev workflow bind loopback, where there is nowhere to
    # type a password and nothing beyond this machine can reach us anyway.
    # __main__ refuses to bind a non-loopback host WITHOUT an auth, so "served
    # to the LAN" and "password required" cannot come apart.
    def _needs_session(path: str) -> bool:
        # Only the API. The static frontend must stay reachable or the login
        # page could never load, and /api/login must stay open or nobody could
        # ever obtain a session.
        return path.startswith("/api/") and path != "/api/login"

    @app.middleware("http")
    async def _require_session(request, call_next):
        if auth is not None and _needs_session(request.url.path):
            if not auth.valid(request.cookies.get(auth.COOKIE)):
                return JSONResponse({"detail": "authentication required"},
                                    status_code=401)
        return await call_next(request)

    @app.post("/api/login")
    def login(body: LoginBody, response: Response):
        if auth is None:
            return {"ok": True}          # nothing to log into
        token = auth.login(body.password)
        if token is None:
            raise HTTPException(401, "wrong password")
        # HttpOnly so page scripts can't read it; SameSite=Lax is enough for a
        # same-origin dashboard. Not Secure -- there is no TLS on the LAN, a
        # limitation recorded in the design spec.
        response.set_cookie(auth.COOKIE, token, httponly=True,
                            samesite="lax", path="/")
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        if auth is not None:
            auth.logout(request.cookies.get(auth.COOKIE))
            response.delete_cookie(auth.COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/printers")
    def list_printers():
        printers = _with_nozzle(_with_bed_type(registry.summaries(), registry),
                                registry)
        return {"printers": _with_detection(printers, detection)}

    @app.post("/api/printers", status_code=201)
    def add_printer(body: AddPrinter):
        try:
            return registry.add(host=body.host, serial=body.serial,
                                access_code=body.access_code,
                                name=body.name, capture=body.capture,
                                model_id=body.model_id)
        except DuplicateSerial:
            raise HTTPException(409, "that serial is already registered")
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.put("/api/printers/{serial}")
    def edit_printer(serial: str, body: EditPrinter):
        try:
            result = registry.update(serial, host=body.host,
                                     access_code=body.access_code,
                                     name=body.name, capture=body.capture,
                                     model_id=body.model_id,
                                     bed_type=body.bed_type,
                                     nozzle=body.nozzle)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if result is None:
            raise HTTPException(404, "unknown printer")
        # svc.summary() doesn't carry bed_type/nozzle (see _with_bed_type/
        # _with_nozzle) -- attach them here too so the response the frontend
        # awaits after a save reflects reality immediately, not just the
        # next /ws tick.
        result["bed_type"] = registry.printer_bed_type(serial)
        result["nozzle"] = registry.printer_nozzle(serial)
        return result

    @app.delete("/api/printers/{serial}", status_code=204)
    def remove_printer(serial: str):
        if not registry.remove(serial):
            raise HTTPException(404, "unknown printer")
        return Response(status_code=204)

    @app.get("/api/printers/{serial}/files")
    def list_files(serial: str, path: str = "/"):
        # Deliberately a SYNC def: FastAPI runs these on a threadpool, so the
        # blocking FTPS handshake cannot stall the event loop and freeze every
        # connected WebSocket.
        svc = registry.get(serial)
        if svc is None:
            raise HTTPException(404, "unknown printer")
        try:
            target = sdcard.normalize_path(path)
        except SdError as e:
            raise HTTPException(400, str(e))  # bad input, not a printer fault
        try:
            return {"path": target, "entries": svc.list_files(target)}
        except SdError as e:
            raise HTTPException(502, str(e))  # the printer/FTPS failed us

    @app.post("/api/printers/{serial}/reconnect")
    def reconnect_printer(serial: str):
        # Rebuilds the MQTT connection with the config already stored. Sends
        # no command to the printer, so it cannot disturb a running print.
        # SYNC def: the rebuild calls stop()/start(), which join a thread.
        summary = registry.reconnect(serial)
        if summary is None:
            raise HTTPException(404, "unknown printer")
        return summary

    @app.post("/api/printers/{serial}/files", status_code=201)
    def upload_file(serial: str, file: UploadFile = File(...)):
        # SYNC def for the same reason as list_files: the FTPS STOR blocks,
        # and must run on the threadpool rather than stalling every WebSocket.
        #
        # The only mutating SD route. It writes to the card ROOT and takes the
        # name from the upload, discarding any directory component the client
        # sent -- file:///sdcard/<filename> (master.md 5.4) has no path
        # component, so a file parked in a subdirectory could be listed but
        # never started.
        if registry.get(serial) is None:
            raise HTTPException(404, "unknown printer")
        name = os.path.basename((file.filename or "").replace("\\", "/")).strip()
        if not name:
            raise HTTPException(400, "no filename")
        lowered = name.lower()
        # Two printable shapes, and they are NOT equivalent:
        #   .gcode.3mf -- the queue can start it (project_file points at
        #                 Metadata/plate_N.gcode inside the zip, master.md 5.4)
        #   .gcode     -- real cards are full of these, and the printer's own
        #                 screen prints them, but there is no verified MQTT
        #                 command to launch one, so the queue cannot.
        # Anything else the printer cannot print at all, so it is refused
        # rather than left on the card as clutter the UI has to explain.
        warning = None
        if lowered.endswith(".gcode.3mf"):
            pass
        elif lowered.endswith(".gcode"):
            warning = (f"{name} uploaded, but raw .gcode can only be started "
                       "from the printer's own screen, not from the queue "
                       "here. Export a sliced .gcode.3mf to queue it.")
        else:
            raise HTTPException(
                400, f"{name}: the printer can only print .gcode.3mf or "
                     ".gcode files. Use 'Export plate sliced file' for a "
                     ".gcode.3mf the queue can start.")
        try:
            target = sdcard.normalize_path("/" + name)
        except SdError as e:
            raise HTTPException(400, str(e))  # bad input, not a printer fault
        data = file.file.read()
        if not data:
            raise HTTPException(400, "empty file")
        # Model check is a WARNING here, never a refusal: parking a file for
        # another printer in the fleet is legitimate. The hard stop is at
        # start (the only genuinely risky step).
        if warning is None:
            mismatch = model_mismatch(registry.printer_model(serial),
                                      threemf.parse_slice_info(data)["printer_model_id"])
            if mismatch is not None:
                warning = f"{name} uploaded, but it is {mismatch}."
        try:
            registry.upload_sd_file(serial, target, data)
        except SdError as e:
            raise HTTPException(502, str(e))  # the printer/FTPS failed us
        return {"path": target, "bytes": len(data), "warning": warning}

    def _require_queue() -> None:
        if queue is None:
            raise HTTPException(404, "queue not enabled on this server")

    def _require_ledger():
        if ledger is None:
            raise HTTPException(404, "traceability is not enabled on this "
                                     "server")

    @app.get("/api/printers/{serial}/queue")
    def get_queue(serial: str):
        _require_queue()
        return {"jobs": queue.get(serial), "totals": queue.totals(serial)}

    @app.post("/api/printers/{serial}/queue", status_code=201)
    def add_queue_job(serial: str, body: AddQueueJob):
        # Planner only: this never commands the printer, it only reads one
        # SD file to learn its estimated time/grams. Deliberately a SYNC def
        # for the same reason as list_files -- fetch_sd_file's FTPS call
        # blocks and must run on the threadpool, not the event loop.
        _require_queue()
        if registry.get(serial) is None:
            raise HTTPException(404, "unknown printer")
        try:
            target = sdcard.normalize_path(body.sd_path)
        except SdError as e:
            raise HTTPException(400, str(e))  # bad input, not a printer fault
        try:
            # registry.fetch_sd_file hides the access code behind the
            # service, same as svc.list_files() above -- this route never
            # sees it. A fetch/FTPS failure here is NOT fatal to the
            # request: the job is still queued, just without parsed
            # metrics, so a momentarily-offline printer doesn't block
            # planning the queue.
            data = registry.fetch_sd_file(serial, target)
            meta = threemf.parse_slice_info(data)
        except SdError:
            meta = {"seconds": None, "grams": None, "filaments": [],
                    "printer_model_id": None}
        seconds, grams = meta.get("seconds"), meta.get("grams")
        job = {
            "id": uuid.uuid4().hex,
            "sd_path": target,
            "name": posixpath.basename(target) or target,
            "seconds": seconds,
            "grams": grams,
            "source": "3mf" if (seconds or grams) else "manual",
            # Recorded at queue time so start can refuse a mismatch without
            # a second FTPS round trip. None = unknown = never blocks.
            "model_id": meta.get("printer_model_id"),
        }
        queue.add(serial, job)
        return job

    @app.delete("/api/printers/{serial}/queue/{job_id}", status_code=204)
    def remove_queue_job(serial: str, job_id: str):
        _require_queue()
        if not queue.remove(serial, job_id):
            raise HTTPException(404, "unknown job")
        return Response(status_code=204)

    @app.put("/api/printers/{serial}/queue")
    def reorder_queue(serial: str, body: ReorderQueueJobs):
        _require_queue()
        queue.reorder(serial, body.ids)
        return {"jobs": queue.get(serial), "totals": queue.totals(serial)}

    @app.post("/api/printers/{serial}/queue/{job_id}/start")
    def start_queue_job(serial: str, job_id: str):
        """Send the job to the printer and dequeue it only once the printer
        confirms it actually started.

        MQTT has no ack, so "published" is not "printing". We publish, then
        watch gcode_state until it leaves idle (verify_start). Confirmed ->
        drop the job, because it is now the running print and the live status
        shows it. Not confirmed -> leave it queued and report that, so a
        command the printer ignored never silently eats a job.

        SYNC def on purpose: it blocks for up to START_VERIFY_S on the
        threadpool rather than stalling the event loop.
        """
        _require_queue()
        svc = registry.get(serial)
        if svc is None:
            raise HTTPException(404, "unknown printer")
        jobs = queue.get(serial)
        if not jobs:
            raise HTTPException(404, "queue is empty")
        if jobs[0].get("id") != job_id:
            # Only the head starts. The client shows one button; a request for
            # any other job means a stale page, and starting the wrong file is
            # not a mistake worth being permissive about.
            raise HTTPException(409, "only the first job in the queue can be "
                                     "started; reorder it to the top first")
        job = jobs[0]
        # The hard stop. Refusing here (rather than at upload) means a file
        # for another printer can still be parked on the card, but cannot be
        # printed by mistake. Unknown on either side never reaches this.
        mismatch = model_mismatch(registry.printer_model(serial),
                                  job.get("model_id"))
        if mismatch is not None:
            raise HTTPException(
                409, f"{job['name']} is {mismatch}. Re-slice it for this "
                     "printer, or remove it from the queue.")
        # Open the ledger row BEFORE publishing. Two reasons, both load-bearing:
        # RunRecorder adopts an already-open row instead of creating one, so
        # this is what stops its 1 s tick racing us into a duplicate
        # unattributed run; and a start the printer ignores still leaves a
        # record, which nothing in this system used to keep.
        run_id = None
        if ledger is not None:
            try:
                run_id = ledger.open_run(
                    printer_serial=serial,
                    printer_name=(svc.summary().get("name") or ""),
                    source="queue",
                    queue_job_id=job.get("id"),
                    sd_path=job.get("sd_path"),
                    subtask_name=job.get("name"),
                    planned_seconds=job.get("seconds"),
                    planned_grams=job.get("grams"),
                    bed_type=_maybe(registry, "printer_bed_type", serial),
                    nozzle=_maybe(registry, "printer_nozzle", serial))
            except Exception as e:  # noqa: BLE001
                # A ledger problem must never cost a print -- master.md
                # section 11's boot invariant, one layer up.
                log.error("could not open a ledger run for %s: %s", serial, e)

        try:
            svc.start_print(job["sd_path"], plate=job.get("plate") or 1)
        except PrinterBusy as e:
            _close_run_quietly(ledger, run_id, "START_UNCONFIRMED",
                               serial, str(e))
            raise HTTPException(409, str(e))
        except SdError as e:
            _close_run_quietly(ledger, run_id, "START_UNCONFIRMED",
                               serial, str(e))
            raise HTTPException(502, str(e))

        # Read the module globals here, not as verify_start's defaults: a
        # default binds at def time and could not be overridden (tests would
        # burn the full timeout, and it would be un-tunable at runtime).
        started = verify_start(svc, timeout=START_VERIFY_S, poll=START_POLL_S)
        if not started:
            _close_run_quietly(
                ledger, run_id, "START_UNCONFIRMED", serial,
                "the printer never reported a print starting")
            return {"started": False, "job": job,
                    "detail": "the printer did not report a print starting; "
                              "the job is still queued",
                    "jobs": queue.get(serial),
                    "totals": queue.totals(serial)}
        queue.remove(serial, job_id)
        return {"started": True, "job": job,
                "jobs": queue.get(serial), "totals": queue.totals(serial)}

    # --- traceability: runs, pieces, badges -------------------------------

    def _run_payload(run_id: str) -> dict:
        run = ledger.get_run(run_id)
        if run is None:
            raise HTTPException(404, "unknown run")
        pieces = []
        for piece in ledger.pieces_for(run_id):
            piece["badges"] = ledger.piece_badges(piece["id"])
            pieces.append(piece)
        return {"run": run, "events": ledger.events_for(run_id),
                "pieces": pieces, "badges": ledger.run_badges(run_id)}

    ZERO_COUNTS = {"total": 0, "good": 0, "scrap": 0, "rework": 0,
                   "pending": 0}

    @app.get("/api/runs")
    def list_runs(serial: str | None = None, limit: int = 50,
                  offset: int = 0):
        _require_ledger()
        limit = max(1, min(int(limit), 500))
        runs = ledger.list_runs(serial=serial, limit=limit,
                                offset=max(0, int(offset)))
        # One grouped query for the whole page, not one per row.
        counts = ledger.piece_counts([r["id"] for r in runs])
        for run in runs:
            run["piece_counts"] = counts.get(run["id"], dict(ZERO_COUNTS))
        return {"runs": runs}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        _require_ledger()
        return _run_payload(run_id)

    @app.get("/api/badges")
    def list_badges():
        _require_ledger()
        return {"badges": ledger.badges()}

    def _badge_id(code: str) -> str:
        for badge in ledger.badges():
            if badge["code"] == code:
                return badge["id"]
        raise HTTPException(400, f"unknown badge {code!r}")

    @app.patch("/api/runs/{run_id}")
    def patch_run(run_id: str, body: PatchRun):
        """Operator corrections, including attributing a run recorded as
        unattributed. Without this every screen-started print would be
        permanent dead weight in the record."""
        _require_ledger()
        run = ledger.get_run(run_id)
        if run is None:
            raise HTTPException(404, "unknown run")
        fields = {}
        if body.end_state is not None:
            if body.end_state not in END_STATES:
                raise HTTPException(
                    400, f"end_state must be one of {', '.join(END_STATES)}")
            fields["end_state"] = body.end_state
        if body.actual_grams is not None:
            # An operator-entered figure is a measurement, not an estimate --
            # the basis column has to say so, or it reads as our arithmetic.
            fields["actual_grams"] = float(body.actual_grams)
            fields["actual_grams_basis"] = "manual"
        if fields:
            ledger.update_run(run_id, **fields)
        if body.notes:
            ledger.add_event(printer_serial=run["printer_serial"],
                             run_id=run_id, kind="operator_note",
                             source="operator", payload={"note": body.notes})
        return _run_payload(run_id)

    @app.patch("/api/pieces/{piece_id}")
    def patch_piece(piece_id: str, body: PatchPiece):
        _require_ledger()
        if body.status is not None and body.status not in PIECE_STATUSES:
            raise HTTPException(
                400, f"status must be one of {', '.join(PIECE_STATUSES)}")
        ok = ledger.set_piece(piece_id, status=body.status,
                              inspected_by=body.inspected_by,
                              notes=body.notes)
        if not ok:
            raise HTTPException(404, "unknown piece")
        return {"ok": True}

    @app.post("/api/runs/{run_id}/pieces/bulk")
    def bulk_pieces(run_id: str, body: BulkPieces):
        """One action for a whole plate. See the ledger's set_pieces_bulk for
        why this is not a convenience."""
        _require_ledger()
        if ledger.get_run(run_id) is None:
            raise HTTPException(404, "unknown run")
        try:
            # set_pieces_bulk validates status AND every override index up
            # front, raising ValueError -- so only ValueError is a client
            # error here. A KeyError/TypeError would be a real internal bug and
            # must NOT be masked as a 400.
            changed = ledger.set_pieces_bulk(
                run_id, body.status, inspected_by=body.inspected_by,
                overrides=body.overrides)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"changed": changed, **_run_payload(run_id)}

    @app.post("/api/runs/{run_id}/badges")
    def add_run_badge(run_id: str, body: BadgeRef):
        _require_ledger()
        if ledger.get_run(run_id) is None:
            raise HTTPException(404, "unknown run")
        ledger.add_run_badge(run_id, _badge_id(body.code),
                             applied_by="operator", note=body.note)
        return {"badges": ledger.run_badges(run_id)}

    @app.delete("/api/runs/{run_id}/badges")
    def remove_run_badge(run_id: str, body: BadgeRef):
        _require_ledger()
        ledger.remove_run_badge(run_id, _badge_id(body.code))
        return {"badges": ledger.run_badges(run_id)}

    @app.post("/api/pieces/{piece_id}/badges")
    def add_piece_badge(piece_id: str, body: BadgeRef):
        _require_ledger()
        # Symmetric with add_run_badge's run check: without this a badge on a
        # typo'd piece_id would INSERT against a ghost piece and return 200,
        # masking the client bug instead of the clean 404 it gets elsewhere.
        if ledger.get_piece(piece_id) is None:
            raise HTTPException(404, "unknown piece")
        ledger.add_piece_badge(piece_id, _badge_id(body.code),
                               applied_by="operator", note=body.note)
        return {"badges": ledger.piece_badges(piece_id)}

    @app.delete("/api/pieces/{piece_id}/badges")
    def remove_piece_badge(piece_id: str, body: BadgeRef):
        _require_ledger()
        ledger.remove_piece_badge(piece_id, _badge_id(body.code))
        return {"badges": ledger.piece_badges(piece_id)}

    # --- traceability: parts catalogue + recipes --------------------------

    def _part_payload(part_id: str) -> dict:
        part = ledger.get_part(part_id)
        if part is None:
            raise HTTPException(404, "unknown part")
        recipes = ledger.recipes_for(part_id)
        default = ledger.default_recipe(part_id)
        return {"part": part, "recipes": recipes,
                "default_recipe_id": default["id"] if default else None}

    @app.get("/api/parts")
    def list_parts():
        _require_ledger()
        parts = ledger.list_parts()
        for p in parts:
            d = ledger.default_recipe(p["id"])
            p["default_recipe_id"] = d["id"] if d else None
        return {"parts": parts}

    @app.post("/api/parts", status_code=201)
    def create_part(body: NewPart):
        _require_ledger()
        try:
            pid = ledger.create_part(part_number=body.part_number,
                                     revision=body.revision, name=body.name,
                                     notes=body.notes)
        except sqlite3.IntegrityError:
            raise HTTPException(
                409, f"{body.part_number} revision {body.revision} already exists")
        return _part_payload(pid)

    @app.get("/api/parts/{part_id}")
    def get_part(part_id: str):
        _require_ledger()
        return _part_payload(part_id)

    @app.put("/api/parts/{part_id}")
    def edit_part(part_id: str, body: EditPart):
        _require_ledger()
        if ledger.get_part(part_id) is None:
            raise HTTPException(404, "unknown part")
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        if fields:
            try:
                ledger.update_part(part_id, **fields)
            except sqlite3.IntegrityError:
                raise HTTPException(
                    409, "that part_number and revision already exist")
        return _part_payload(part_id)

    @app.delete("/api/parts/{part_id}", status_code=204)
    def archive_part(part_id: str):
        _require_ledger()
        ledger.archive_part(part_id)
        if partstore is not None:
            partstore.delete(part_id)
        return Response(status_code=204)

    @app.post("/api/parts/{part_id}/model", status_code=201)
    def upload_part_model(part_id: str, file: UploadFile = File(...)):
        # sync def: read a possibly-large model off the wire on the threadpool,
        # same as the SD-card upload route, so the event loop keeps serving /ws.
        _require_ledger()
        if partstore is None:
            raise HTTPException(404, "model storage is not enabled")
        if ledger.get_part(part_id) is None:
            raise HTTPException(404, "unknown part")
        data = file.file.read()
        if not data:
            raise HTTPException(400, "empty file")
        try:
            meta = partstore.save(part_id, file.filename, data)
        except ValueError as e:
            raise HTTPException(400, str(e))
        ledger.update_part(part_id, model_filename=meta["filename"],
                           model_sha256=meta["sha256"],
                           model_bytes=meta["bytes"])
        return _part_payload(part_id)

    @app.get("/api/parts/{part_id}/model")
    def download_part_model(part_id: str):
        _require_ledger()
        part = ledger.get_part(part_id)
        if part is None or not part.get("model_filename"):
            raise HTTPException(404, "no model stored for this part")
        if partstore is None:
            raise HTTPException(404, "model storage is not enabled")
        try:
            data = partstore.open_bytes(part_id, part["model_filename"])
        except OSError:
            raise HTTPException(404, "model file missing")
        return Response(
            content=data, media_type="application/octet-stream",
            headers={"Content-Disposition":
                     f'attachment; filename="{part["model_filename"]}"'})

    @app.post("/api/parts/{part_id}/recipes", status_code=201)
    def add_part_recipe(part_id: str, body: RecipeBody):
        _require_ledger()
        if ledger.get_part(part_id) is None:
            raise HTTPException(404, "unknown part")
        fields = {k: v for k, v in body.model_dump().items()
                  if v is not None and k not in ("name", "is_default")}
        rid = ledger.add_recipe(part_id, name=body.name, **fields)
        if body.is_default:
            # route through set_default_recipe so 'exactly one default' holds
            ledger.set_default_recipe(part_id, rid)
        return _part_payload(part_id)

    @app.put("/api/parts/{part_id}/recipes/{recipe_id}")
    def edit_part_recipe(part_id: str, recipe_id: str, body: RecipeBody):
        _require_ledger()
        recipe = ledger.get_recipe(recipe_id)
        # The recipe must belong to the part named in the URL. Without this a
        # PUT to /parts/A/recipes/B (B belonging to part B) would default A to
        # a foreign recipe -- see set_default_recipe.
        if recipe is None or recipe["part_id"] != part_id:
            raise HTTPException(404, "unknown recipe for this part")
        make_default = body.is_default
        fields = {k: v for k, v in body.model_dump().items()
                  if v is not None and k not in ("is_default",)}
        # name="" is the default; only write it if the caller actually sent one
        if not body.name:
            fields.pop("name", None)
        if fields:
            ledger.update_recipe(recipe_id, **fields)
        if make_default:
            ledger.set_default_recipe(part_id, recipe_id)
        return _part_payload(part_id)

    @app.delete("/api/parts/{part_id}/recipes/{recipe_id}", status_code=204)
    def archive_part_recipe(part_id: str, recipe_id: str):
        _require_ledger()
        recipe = ledger.get_recipe(recipe_id)
        # Same membership guard as edit: don't let a wrong-part URL archive
        # someone else's recipe.
        if recipe is None or recipe["part_id"] != part_id:
            raise HTTPException(404, "unknown recipe for this part")
        ledger.archive_recipe(recipe_id)
        return Response(status_code=204)

    def _require_slicer() -> None:
        if slicer is None:
            raise HTTPException(
                404, "slicing is not available on this server -- Bambu Studio "
                     "was not found. Set BAMBU_STUDIO_EXE if it is installed "
                     "elsewhere.")

    @app.get("/api/printers/{serial}/slice/options")
    def slice_options(serial: str):
        _require_slicer()
        # No registry.get() check here, deliberately: an unknown serial
        # degrades to empty presets/filaments (same "unknown never blocks"
        # convention as printer_model/printer_nozzle), not a 404 -- the
        # coordinator itself never raises for an unknown printer.
        return slicer.options(serial)

    @app.post("/api/printers/{serial}/slice", status_code=202)
    def start_slice(serial: str, file: UploadFile = File(...),
                    preset: str = Form(...), material: str = Form(...),
                    supports: bool = Form(False)):
        # SYNC def for the same reason as the SD routes: reading a large model
        # off the wire must not stall the event loop and freeze every
        # WebSocket. The slice itself runs on the coordinator's own thread.
        _require_slicer()
        name = os.path.basename((file.filename or "").replace("\\", "/")).strip()
        if not name:
            raise HTTPException(400, "no filename")
        if not name.lower().endswith(slicejobs.MODEL_EXTS):
            raise HTTPException(
                400, f"{name}: not a model file. Upload one of "
                     f"{', '.join(slicejobs.MODEL_EXTS)}.")
        data = file.file.read()
        if not data:
            raise HTTPException(400, "empty file")
        try:
            job_id = slicer.submit(serial, name, data, preset, material,
                                   bool(supports))
        except KeyError:
            raise HTTPException(404, "unknown printer")
        except ValueError as e:
            raise HTTPException(400, str(e))  # bad choice, not a server fault
        return {"job_id": job_id}

    @app.get("/api/slice/jobs")
    def list_slice_jobs(serial: str | None = None):
        _require_slicer()
        return {"jobs": slicer.list(serial)}

    @app.delete("/api/slice/jobs/{job_id}", status_code=204)
    def cancel_slice_job(job_id: str):
        _require_slicer()
        if not slicer.cancel(job_id):
            raise HTTPException(404, "unknown job")
        return Response(status_code=204)

    @app.get("/api/frame/latest")
    def frame_latest():
        info = runs.newest_frame(runs_dir)
        if info is None:
            return JSONResponse({"error": "no active run"}, status_code=404)
        try:
            # Read in-handler (FastAPI runs sync routes in a threadpool) so a
            # frame vanishing between discovery and send stays a clean 404
            # instead of FileResponse's late FileNotFoundError -> 500.
            # capture.py writes non-atomically, so a rare truncated JPEG is
            # possible and accepted; the frontend re-polls within 2 s.
            data = info["path"].read_bytes()
        except OSError:
            return JSONResponse({"error": "no active run"}, status_code=404)
        return Response(
            content=data, media_type="image/jpeg",
            headers={"X-Frame-Layer": str(info["layer"]),
                     "X-Frame-Run": info["run"],
                     "Cache-Control": "no-store"})

    def _require_detection_snapshot(serial):
        if detection is None:
            raise HTTPException(404, "detection not enabled on this server")
        snap = detection.snapshot(serial)
        if snap is None:
            raise HTTPException(404, "not the capture printer")
        return snap

    @app.get("/api/printers/{serial}/detection")
    def get_detection(serial: str):
        return _require_detection_snapshot(serial)

    @app.put("/api/printers/{serial}/detection")
    def put_detection(serial: str, body: DetectionUpdate):
        if detection is None:
            raise HTTPException(404, "detection not enabled on this server")
        if body.armed_classes is not None:
            bad = [c for c in body.armed_classes if c not in CLASSES]
            if bad:
                raise HTTPException(400, f"unknown class(es): {', '.join(bad)}")
        if body.camera_source is not None and body.camera_source not in CAMERA_SOURCES:
            raise HTTPException(400, f"camera_source must be one of {CAMERA_SOURCES}")
        if body.roi is not None and len(body.roi) != 4:
            raise HTTPException(400, "roi must be [x, y, w, h] fractions")
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        # roi only: an explicit null means "clear the ROI", which the filter
        # above would have swallowed. Forward it only when the client actually
        # sent the key, so an omitted roi still means "leave it alone".
        if "roi" in body.model_fields_set:
            fields["roi"] = body.roi
        if not registry.update_detection(serial, **fields):
            raise HTTPException(404, "unknown printer")
        # Config can be set on any printer, but a snapshot only exists for the
        # capture printer -- return it when present, else just confirm the save
        # (avoids a confusing 404 after a successful update).
        snap = detection.snapshot(serial)
        return snap if snap is not None else {"updated": True}

    @app.post("/api/printers/{serial}/detection/arm")
    def arm_detection(serial: str, body: ArmBody):
        snap = _require_detection_snapshot(serial)  # 404s if not capture
        detection.arm(serial, body.armed)
        return detection.snapshot(serial) or snap

    @app.get("/api/printers/{serial}/detection/frame")
    def detection_frame(serial: str):
        if detection is None:
            raise HTTPException(404, "detection not enabled on this server")
        if detection.snapshot(serial) is None:
            raise HTTPException(404, "not the capture printer")
        path = detection.frame_path()
        if path is None:
            return JSONResponse({"error": "no detector frame"}, status_code=404)
        try:
            data = path.read_bytes()
        except OSError:
            return JSONResponse({"error": "no detector frame"}, status_code=404)
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        # Checked HERE, not in the HTTP middleware above: that middleware only
        # sees http-scope requests, so a WebSocket would sail straight past it.
        # This is also why the session is a cookie -- browsers cannot set
        # headers on a WS handshake, but cookies ride it automatically.
        if auth is not None and not auth.valid(sock.cookies.get(auth.COOKIE)):
            await sock.close(code=1008)   # policy violation
            return
        try:
            await sock.accept()
            printers = _with_detection(
                _with_nozzle(_with_bed_type(registry.summaries(), registry),
                            registry), detection)
            await sock.send_text(json.dumps({"printers": printers}))
            last_sent, last_time = printers, time.monotonic()
            while True:
                await asyncio.sleep(WS_POLL_S)
                now = time.monotonic()
                # summaries()/printer_bed_type/printer_nozzle must stay
                # non-blocking: this runs on the event loop and a stall here
                # would freeze every connected client. All are quick,
                # lock-guarded dict reads.
                printers = _with_detection(
                    _with_nozzle(_with_bed_type(registry.summaries(), registry),
                                registry), detection)
                changed = _comparable(printers) != _comparable(last_sent)
                if changed or now - last_time >= WS_HEARTBEAT_S:
                    await sock.send_text(json.dumps({"printers": printers}))
                    last_sent, last_time = printers, now
        except WebSocketDisconnect:
            pass

    if frontend_dist is not None and (frontend_dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True),
                  name="static")
    else:
        @app.get("/")
        def hint():
            return PlainTextResponse(
                "bambu-monitor server is running.\n"
                "Frontend not built yet: run `npm run build` in frontend/,\n"
                "or use the Vite dev server (`npm run dev`) on port 5173.\n")

    return app
