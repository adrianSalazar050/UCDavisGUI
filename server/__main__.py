"""CLI entry: python -m server [--mock] [--printers-file printers.json]

Printers are no longer configured on the command line -- they are added in the
browser and restored from printers.json. The server starts fine with none
registered; the UI shows the add form.
"""
from __future__ import annotations

import argparse
import logging
import os
import pathlib
import signal
import sys

import uvicorn

from .auth import Auth, is_loopback
from .detection import (DEFAULT_INTERVAL_S, DetectionCoordinator,
                        DetectorSupervisor, MockDetectorRunner)
from .main import create_app
from .printer import MockPrinter, PrinterService
from .queue import MemoryQueueStore, PrintQueue, QueueStore
from .registry import PrinterRegistry
from . import slicer as slicer_mod
from .slicejobs import SliceCoordinator
from .store import MemoryStore, PrinterStore

log = logging.getLogger("server.__main__")

# serial, name, mode, capture -- one of each state so the Overview grid is
# fully exercisable with no hardware.
MOCK_SEED = [
    ("MOCK0000000001", "mock-bench", "running", True),
    ("MOCK0000000002", "mock-window", "stale", False),
    ("MOCK0000000003", "mock-spare", "offline", False),
]

# Not a real LAN access code -- --mock's seeded printers are served by
# MockPrinter, which never reads it. It exists only to satisfy
# PrinterConfig/registry.add()'s non-empty requirement. MemoryStore never
# writes to disk, and build_summary() never echoes access_code back out of
# the API, so this sentinel is neither persisted nor shown to the user.
MOCK_ACCESS_CODE = "00000000"

DEFAULT_PRINTERS_FILE = pathlib.Path("printers.json")


def real_factory(cfg):
    return PrinterService(cfg.host, cfg.serial, cfg.access_code,
                           name=cfg.name, capture=cfg.capture,
                           model_id=cfg.model_id)


def mock_factory(runs_dir: pathlib.Path):
    """Fake printers for the seeded serials; a REAL PrinterService for anything
    added through the UI -- that is how the add-printer error path ("Unreachable")
    gets exercised without hardware."""
    modes = {serial: mode for serial, _, mode, _ in MOCK_SEED}

    def make(cfg):
        mode = modes.get(cfg.serial)
        if mode is None:
            return real_factory(cfg)
        # Every field here must mirror real_factory: the registry rebuilds a
        # service from the config on any host/access-code edit, so a field
        # this forgets is silently reset on every such edit.
        return MockPrinter(runs_dir, serial=cfg.serial, host=cfg.host,
                            name=cfg.name, capture=cfg.capture, mode=mode,
                            model_id=cfg.model_id)

    return make


def build_auth(host: str, password: str | None):
    """-> an Auth, or None when no authentication is needed.

    THE FAIL-CLOSED RULE. Binding anywhere but loopback puts printer control --
    stop a print, upload a file, start a job -- on the network. Doing that
    without a password must not be possible by forgetting a flag, so this
    EXITS rather than starting up unprotected.

    Loopback needs no password: nothing off this machine can reach it, and the
    desktop app (which spawns its own backend on a random local port) would
    have nowhere to type one. A password on loopback is still honoured if you
    want one.
    """
    if password:
        return Auth(password)
    if is_loopback(host):
        return None
    raise SystemExit(
        f"refusing to bind {host} without a password: that would expose "
        "printer control to the network. Set the BAMBU_PASSWORD environment "
        "variable, or bind 127.0.0.1 (the default) to keep it to this machine.")


def main() -> int:
    p = argparse.ArgumentParser(
        prog="python -m server",
        description="Dashboard backend for the bambu_monitor rig.")
    p.add_argument("--mock", action="store_true",
                   help="no printers: seed three fake ones (running/stale/"
                        "offline) and never touch printers.json")
    p.add_argument("--printers-file", type=pathlib.Path,
                   default=DEFAULT_PRINTERS_FILE,
                   help="registered-printer list (default printers.json)")
    p.add_argument("--runs-dir", type=pathlib.Path, default=None,
                   help="capture output dir (default runs/, or runs-mock/ "
                        "with --mock)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1",
                   help="interface to bind (default: %(default)s, this machine "
                        "only). Use 0.0.0.0 to serve the dashboard to the LAN "
                        "-- that REQUIRES the BAMBU_PASSWORD environment "
                        "variable to be set")
    p.add_argument("--detect-interval", type=float, default=DEFAULT_INTERVAL_S,
                   help="seconds between detector captures (default: "
                        "%(default)s)")
    p.add_argument("--no-slicer", action="store_true",
                   help="disable slicing even if Bambu Studio is installed")
    a = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%H:%M:%S")

    if a.mock:
        if a.printers_file != DEFAULT_PRINTERS_FILE:
            log.warning("--printers-file %s is ignored under --mock: mock "
                        "mode uses an in-memory store and never touches "
                        "disk", a.printers_file)
        runs_dir = a.runs_dir or pathlib.Path("runs-mock")
        runs_dir.mkdir(parents=True, exist_ok=True)
        registry = PrinterRegistry(MemoryStore(), mock_factory(runs_dir))
        for serial, name, _mode, capture in MOCK_SEED:
            registry.add(host=name, serial=serial,
                         access_code=MOCK_ACCESS_CODE, name=name,
                         capture=capture)
    else:
        runs_dir = a.runs_dir or pathlib.Path("runs")
        runs_dir.mkdir(parents=True, exist_ok=True)
        registry = PrinterRegistry(PrinterStore(a.printers_file), real_factory)
        registry.load()

    detect_out = runs_dir / "_detect"
    weights = pathlib.Path(__file__).resolve().parent.parent / "runs" / "train" \
        / "failure_detector" / "weights" / "best.pt"
    # One interval drives both halves: the supervisor tells detect.py how often
    # to capture, and the coordinator sizes its staleness window to match. Wiring
    # them from the same value keeps a healthy detector from ever reading as down.
    runner = MockDetectorRunner(detect_out) if a.mock \
        else DetectorSupervisor(detect_out, weights, interval_s=a.detect_interval)
    coordinator = DetectionCoordinator(registry, runs_dir, runner,
                                       interval_s=a.detect_interval)

    # --mock uses an in-memory queue store so the seeded fake printers' jobs
    # never land in the user's real queues.json (mirrors printers.json's
    # MemoryStore split). Real runs persist to queues.json beside runs_dir.
    queue_store = MemoryQueueStore() if a.mock \
        else QueueStore(runs_dir.parent / "queues.json")
    queue = PrintQueue(queue_store)

    # Three distinct outcomes, each logged clearly: disabled by the flag,
    # nothing installed, or installed but with a profile tree that didn't
    # index (a corrupt or unexpected install). Only the last case actually
    # builds a coordinator -- an empty index would resolve every preset to
    # None, so "installed" alone is not enough to call slicing enabled.
    slicer = None
    if a.no_slicer:
        log.info("slicing disabled (--no-slicer)")
    else:
        exe = slicer_mod.find_slicer()
        if exe is None:
            log.info("no Bambu Studio found; slicing disabled "
                     "(set BAMBU_STUDIO_EXE to point at it)")
        else:
            index = slicer_mod.ProfileIndex.load(slicer_mod.profiles_root(exe))
            if not index:
                log.warning("found %s but no vendor profiles beside it; "
                            "slicing disabled", exe)
            else:
                log.info("slicing enabled: %s (%d profiles)", exe, len(index))
                # runs_dir, not a.runs_dir: the CLI flag defaults to None and
                # is resolved into runs_dir above (runs/ or runs-mock/) --
                # a.runs_dir itself is still None on that default path.
                slicer = SliceCoordinator(
                    registry, queue, exe, index, work_dir=runs_dir / "_slice")

    auth = build_auth(a.host, os.environ.get("BAMBU_PASSWORD"))

    dist = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "dist"
    app = create_app(registry, runs_dir, dist, detection=coordinator,
                     queue=queue, slicer=slicer, auth=auth)
    # uvicorn re-raises the signal it caught using whatever handler was
    # installed beforehand. SIGBREAK's OS default kills the process outright
    # (skipping `finally`), so map it to KeyboardInterrupt like SIGINT gets.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    try:
        uvicorn.run(app, host=a.host, port=a.port)
    finally:
        registry.stop_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
