"""Frozen-backend entry point for the Electron desktop app.

Run directly (`python desktop/launcher.py`) during development, or frozen by
PyInstaller (see bambu-backend.spec) for the shipped app. Either way it boots
the SAME FastAPI app the source server does -- it just resolves paths for a
packaged, no-source-checkout environment and narrows the wiring to the desktop
scope (dashboard + SD upload; no detector).

Why a separate entry instead of `python -m server`:
  * server/__main__.py computes frontend/dist and the YOLO weights path relative
    to its own __file__, which is wrong once frozen, and it always builds a
    DetectorSupervisor. Here we resolve dist from the bundle and pass
    detection=None, so the torch/detector path is never touched.

Writable state (printers.json, queues.json, runs/) goes to a per-user data dir,
NEVER beside the executable: an AppImage is mounted read-only, so a write there
would fail. Electron passes the dir via BAMBU_DATA_DIR; a per-OS default is used
when run standalone.
"""
from __future__ import annotations

import logging
import os
import pathlib
import sys

import uvicorn

from server.main import create_app
from server.queue import PrintQueue, QueueStore
from server.registry import PrinterRegistry
from server.store import PrinterStore
from server.__main__ import real_factory  # reuse the tested factory, don't fork it
from server import slicer as slicer_mod
from server.slicejobs import SliceCoordinator

log = logging.getLogger("desktop.launcher")


def resource_path(rel: str) -> pathlib.Path:
    """Resolve a bundled data file. Frozen: relative to PyInstaller's _MEIPASS.
    Source: relative to the repo root (this file's parent's parent)."""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = pathlib.Path(__file__).resolve().parent.parent
    return pathlib.Path(base) / rel


def data_dir() -> pathlib.Path:
    """Per-user writable state directory. BAMBU_DATA_DIR (set by Electron) wins;
    otherwise a conventional per-OS location."""
    env = os.environ.get("BAMBU_DATA_DIR")
    if env:
        return pathlib.Path(env)
    if sys.platform == "win32":
        root = os.environ.get("APPDATA") or (pathlib.Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        root = pathlib.Path.home() / "Library" / "Application Support"
    else:  # linux / other
        root = os.environ.get("XDG_CONFIG_HOME") or (pathlib.Path.home() / ".config")
    return pathlib.Path(root) / "BambuMonitor"


def build_slicer(registry, queue, work_dir: pathlib.Path):
    """Auto-detect Bambu Studio. Not bundled -- enabled only if the user already
    has it installed, disabled (gracefully) otherwise. Mirrors the three-outcome
    logic in server/__main__.py."""
    exe = slicer_mod.find_slicer()
    if exe is None:
        log.info("no Bambu Studio found; slicing disabled. Set BAMBU_STUDIO_EXE "
                 "to its path (e.g. an AppImage) to enable it")
        return None
    index = slicer_mod.ProfileIndex.load(slicer_mod.profiles_root(exe))
    if not index:
        log.warning("found %s but no vendor profiles beside it; slicing disabled", exe)
        return None
    log.info("slicing enabled: %s (%d profiles)", exe, len(index))
    return SliceCoordinator(registry, queue, exe, index, work_dir=work_dir)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%H:%M:%S")

    dd = data_dir()
    dd.mkdir(parents=True, exist_ok=True)
    runs_dir = dd / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    log.info("data directory: %s", dd)

    registry = PrinterRegistry(PrinterStore(dd / "printers.json"), real_factory)
    registry.load()

    queue = PrintQueue(QueueStore(dd / "queues.json"))
    slicer = build_slicer(registry, queue, runs_dir / "_slice")

    dist = resource_path("frontend/dist")
    if not (dist / "index.html").exists():
        log.warning("frontend build not found at %s -- the window will 404", dist)

    # detection=None: the entire YOLO/torch/detector path is out of scope for the
    # desktop bundle, so no detect.py subprocess is ever spawned.
    app = create_app(registry, runs_dir, dist,
                     detection=None, queue=queue, slicer=slicer)

    host = os.environ.get("BAMBU_HOST", "127.0.0.1")
    port = int(os.environ.get("BAMBU_PORT", "8000"))
    log.info("serving on http://%s:%d", host, port)
    try:
        uvicorn.run(app, host=host, port=port)
    finally:
        registry.stop_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
