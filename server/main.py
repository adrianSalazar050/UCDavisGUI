"""FastAPI app: /api/printers, /api/frame/latest, /ws, static frontend."""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import runs, sdcard
from .registry import DuplicateSerial
from .sdcard import SdError

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


def _comparable(printers: list[dict]) -> list[dict]:
    """report_age_s ticks every sample; ignore it when deciding whether the
    state meaningfully changed."""
    return [{k: v for k, v in p.items() if k != "report_age_s"}
            for p in printers]


def create_app(registry, runs_dir: pathlib.Path,
               frontend_dist: pathlib.Path | None = None) -> FastAPI:
    """`registry` is anything with summaries() -> list[dict], get(serial),
    add(...), remove(serial) (PrinterRegistry, or a test fake)."""
    app = FastAPI(title="bambu-monitor")

    @app.get("/api/printers")
    def list_printers():
        return {"printers": registry.summaries()}

    @app.post("/api/printers", status_code=201)
    def add_printer(body: AddPrinter):
        try:
            return registry.add(host=body.host, serial=body.serial,
                                access_code=body.access_code,
                                name=body.name, capture=body.capture)
        except DuplicateSerial:
            raise HTTPException(409, "that serial is already registered")
        except ValueError as e:
            raise HTTPException(400, str(e))

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

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        try:
            await sock.accept()
            printers = registry.summaries()
            await sock.send_text(json.dumps({"printers": printers}))
            last_sent, last_time = printers, time.monotonic()
            while True:
                await asyncio.sleep(WS_POLL_S)
                now = time.monotonic()
                # summaries() must stay non-blocking: it runs on the event loop
                # and a stall here would freeze every connected client.
                printers = registry.summaries()
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
