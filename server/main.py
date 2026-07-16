"""FastAPI app: /api/status, /api/frame/latest, /ws, static frontend."""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import runs

log = logging.getLogger("server.main")

WS_POLL_S = 0.25      # summary sampled at 4 Hz -> at most ~4 pushes/s
WS_HEARTBEAT_S = 5.0  # push even when unchanged, keeps report_age_s fresh


def create_app(service, runs_dir: pathlib.Path,
               frontend_dist: pathlib.Path | None = None) -> FastAPI:
    """`service` is anything with a summary() -> dict (PrinterService,
    MockPrinter, or a test fake)."""
    app = FastAPI(title="bambu-monitor")

    @app.get("/api/status")
    def status():
        return service.summary()

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
            payload = service.summary()
            await sock.send_text(json.dumps(payload))
            last_sent, last_time = payload, time.monotonic()
            while True:
                await asyncio.sleep(WS_POLL_S)
                now = time.monotonic()
                # summary() must stay non-blocking: it runs on the event loop
                # and a stall here would freeze every connected client.
                payload = service.summary()
                # report_age_s ticks every sample; ignore it when deciding
                # whether the state meaningfully changed.
                changed = ({k: v for k, v in payload.items()
                            if k != "report_age_s"}
                           != {k: v for k, v in last_sent.items()
                               if k != "report_age_s"})
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
