"""FastAPI app: /api/printers, /api/frame/latest, /ws, static frontend."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import posixpath
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import (FastAPI, File, HTTPException, UploadFile, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import runs, sdcard, threemf
from .detection import CLASSES  # the 6 valid armed classes
from .printer import PrinterBusy
from .registry import DuplicateSerial
from .sdcard import SdError
from .store import CAMERA_SOURCES, model_mismatch
from .robot import (RobotBusy, RobotCommandError, RobotUnavailable)

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


class AddQueueJob(BaseModel):
    """POST body for queuing an SD-card file. sd_path is the only
    user-supplied field -- id/name/seconds/grams/source are all derived
    server-side from the fetched + parsed .gcode.3mf, never trusted from the
    client."""

    sd_path: str


class ReorderQueueJobs(BaseModel):
    ids: list[str]


class RobotCommandBody(BaseModel):
    action: str
    parameters: dict = {}


def _comparable(printers: list[dict]) -> list[dict]:
    """report_age_s ticks every sample; ignore it when deciding whether the
    state meaningfully changed."""
    return [{k: v for k, v in p.items() if k != "report_age_s"}
            for p in printers]


def _with_detection(printers: list[dict], detection) -> list[dict]:
    """Attach a `detection` object to each summary (None unless it's the
    capture printer). Detection state lives in detection.py, not the service."""
    if detection is None:
        return printers
    for p in printers:
        p["detection"] = detection.snapshot(p.get("serial"))
    return printers


def create_app(registry, runs_dir: pathlib.Path,
               frontend_dist: pathlib.Path | None = None,
               detection=None, queue=None, robot=None) -> FastAPI:
    """`registry` is anything with summaries() -> list[dict], get(serial),
    add(...), remove(serial) (PrinterRegistry, or a test fake). `queue` is
    anything with add(serial, job), remove(serial, id) -> bool,
    reorder(serial, ids), get(serial) -> list, totals(serial) -> dict
    (PrintQueue, or a test fake); None disables the queue routes entirely,
    same "None means inert" convention as `detection`. `robot` optionally
    supplies start(), stop(), snapshot(), submit(), and cancel()."""

    @asynccontextmanager
    async def lifespan(_app):
        if detection is not None:
            detection.start()
        if robot is not None:
            robot.start()
        try:
            yield
        finally:
            if robot is not None:
                robot.stop()
            if detection is not None:
                detection.stop()

    app = FastAPI(title="bambu-monitor", lifespan=lifespan)

    @app.get("/api/printers")
    def list_printers():
        return {"printers": _with_detection(registry.summaries(), detection)}

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
                                     model_id=body.model_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if result is None:
            raise HTTPException(404, "unknown printer")
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
        try:
            svc.start_print(job["sd_path"], plate=job.get("plate") or 1)
        except PrinterBusy as e:
            raise HTTPException(409, str(e))
        except SdError as e:
            raise HTTPException(502, str(e))

        # Read the module globals here, not as verify_start's defaults: a
        # default binds at def time and could not be overridden (tests would
        # burn the full timeout, and it would be un-tunable at runtime).
        started = verify_start(svc, timeout=START_VERIFY_S, poll=START_POLL_S)
        if not started:
            return {"started": False, "job": job,
                    "detail": "the printer did not report a print starting; "
                              "the job is still queued",
                    "jobs": queue.get(serial),
                    "totals": queue.totals(serial)}
        queue.remove(serial, job_id)
        return {"started": True, "job": job,
                "jobs": queue.get(serial), "totals": queue.totals(serial)}

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

    def _require_robot():
        if robot is None:
            raise HTTPException(404, "robot control is not enabled")
        return robot

    @app.get("/api/robot/status")
    def robot_status():
        return _require_robot().snapshot()

    @app.post("/api/robot/commands", status_code=202)
    def robot_command(body: RobotCommandBody):
        controller = _require_robot()
        try:
            return controller.submit(body.action, body.parameters)
        except RobotCommandError as exc:
            raise HTTPException(400, str(exc))
        except RobotBusy as exc:
            raise HTTPException(409, str(exc))
        except RobotUnavailable as exc:
            raise HTTPException(503, str(exc))

    @app.post("/api/robot/commands/{command_id}/cancel")
    def cancel_robot_command(command_id: str):
        controller = _require_robot()
        if not controller.cancel(command_id):
            raise HTTPException(404, "robot command is not active")
        return controller.snapshot()

    def _live_payload():
        printers = _with_detection(registry.summaries(), detection)
        payload = {"printers": printers}
        if robot is not None:
            payload["robot"] = robot.snapshot()
        return payload

    def _live_comparable(payload):
        value = {"printers": _comparable(payload["printers"])}
        if "robot" in payload:
            value["robot"] = payload["robot"]
        return value

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        try:
            await sock.accept()
            payload = _live_payload()
            await sock.send_text(json.dumps(payload))
            last_sent, last_time = payload, time.monotonic()
            while True:
                await asyncio.sleep(WS_POLL_S)
                now = time.monotonic()
                # summaries() must stay non-blocking: it runs on the event loop
                # and a stall here would freeze every connected client.
                payload = _live_payload()
                changed = (
                    _live_comparable(payload) != _live_comparable(last_sent))
                if changed or now - last_time >= WS_HEARTBEAT_S:
                    await sock.send_text(json.dumps(payload))
                    last_sent, last_time = payload, now
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
