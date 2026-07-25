"""CLI entry: python -m server [--mock] [--printers-file printers.json]

Printers are no longer configured on the command line -- they are added in the
browser and restored from printers.json. The server starts fine with none
registered; the UI shows the add form.
"""
from __future__ import annotations

import argparse
import logging
import pathlib
import signal
import socket

import uvicorn

from .detection import (DEFAULT_INTERVAL_S, DetectionCoordinator,
                        DetectorSupervisor, MockDetectorRunner)
from .main import create_app
from .printer import MockPrinter, PrinterService
from .queue import MemoryQueueStore, PrintQueue, QueueStore
from .registry import PrinterRegistry
from .store import MemoryStore, PrinterStore

log = logging.getLogger("server.__main__")

# Interfaces the dashboard answers on. 0.0.0.0 covers wifi, hotspot and the
# tailnet, so a phone can reach it; "127.0.0.1" restricts it to this machine,
# and a tailscale address (`tailscale ip -4`) to tailnet devices only.
HOST = "0.0.0.0"

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


def _outbound_ip() -> str:
    """This machine's address on the network it routes through. Taken off a UDP
    socket's local end rather than gethostbyname(), which tends to answer
    127.0.1.1; connect() on a datagram socket sends nothing."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:                      # offline, no route to pick
            return "this machine's IP"


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
    p.add_argument("--detect-interval", type=float, default=DEFAULT_INTERVAL_S,
                   help="seconds between detector captures (default: "
                        "%(default)s)")
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

    dist = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "dist"
    app = create_app(registry, runs_dir, dist, detection=coordinator, queue=queue)
    # uvicorn re-raises the signal it caught using whatever handler was
    # installed beforehand. SIGBREAK's OS default kills the process outright
    # (skipping `finally`), so map it to KeyboardInterrupt like SIGINT gets.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    try:
        # uvicorn only logs the bind address, and 0.0.0.0 isn't one anybody can
        # type into a phone.
        if HOST == "0.0.0.0":
            log.info("open the dashboard from another device at http://%s:%d",
                     _outbound_ip(), a.port)
        uvicorn.run(app, host=HOST, port=a.port)
    finally:
        registry.stop_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
