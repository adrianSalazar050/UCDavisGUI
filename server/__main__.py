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

import uvicorn

from .main import create_app
from .printer import MockPrinter, PrinterService
from .registry import PrinterRegistry
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
                           name=cfg.name, capture=cfg.capture)


def mock_factory(runs_dir: pathlib.Path):
    """Fake printers for the seeded serials; a REAL PrinterService for anything
    added through the UI -- that is how the add-printer error path ("Unreachable")
    gets exercised without hardware."""
    modes = {serial: mode for serial, _, mode, _ in MOCK_SEED}

    def make(cfg):
        mode = modes.get(cfg.serial)
        if mode is None:
            return real_factory(cfg)
        return MockPrinter(runs_dir, serial=cfg.serial, host=cfg.host,
                            name=cfg.name, capture=cfg.capture, mode=mode)

    return make


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

    dist = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "dist"
    app = create_app(registry, runs_dir, dist)
    # uvicorn re-raises the signal it caught using whatever handler was
    # installed beforehand. SIGBREAK's OS default kills the process outright
    # (skipping `finally`), so map it to KeyboardInterrupt like SIGINT gets.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    try:
        uvicorn.run(app, host="127.0.0.1", port=a.port)
    finally:
        registry.stop_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
