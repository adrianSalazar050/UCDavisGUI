"""CLI entry: python -m server [--mock | --host ... --serial ... --access-code ...]"""
from __future__ import annotations

import argparse
import logging
import pathlib

import uvicorn

from .main import create_app
from .printer import MockPrinter, PrinterService


def main() -> int:
    p = argparse.ArgumentParser(
        prog="python -m server",
        description="Dashboard backend for the bambu_monitor rig.")
    p.add_argument("--host", help="printer IP")
    p.add_argument("--serial", help="printer serial")
    p.add_argument("--access-code", help="8-char LAN access code")
    p.add_argument("--mock", action="store_true",
                   help="no printer: synthesise an endless fake print")
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
        runs_dir = a.runs_dir or pathlib.Path("runs-mock")
        service = MockPrinter(runs_dir)
    else:
        missing = [f for f in ("host", "serial", "access_code")
                   if not getattr(a, f)]
        if missing:
            p.error("need --" + ", --".join(
                m.replace("_", "-") for m in missing) + "  (or use --mock)")
        runs_dir = a.runs_dir or pathlib.Path("runs")
        service = PrinterService(a.host, a.serial, a.access_code)

    runs_dir.mkdir(parents=True, exist_ok=True)
    service.start()

    dist = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "dist"
    app = create_app(service, runs_dir, dist)
    try:
        uvicorn.run(app, host="127.0.0.1", port=a.port)
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
