"""CLI entry: python -m server [--lan] [--mock] [--printers-file printers.json]

Printers are no longer configured on the command line -- they are added in the
browser and restored from printers.json. The server starts fine with none
registered; the UI shows the add form.

`--lan` serves the dashboard to the whole network. It is a convenience over
`--host 0.0.0.0` plus BAMBU_PASSWORD, and deliberately owns no part of the
fail-closed decision: it only RESOLVES a host and a password, then hands both
to build_auth, which refuses to start if the pair is unsafe. See master.md
§2.1 and §8.
"""
from __future__ import annotations

import argparse
import logging
import os
import pathlib
import signal
import socket
import sys

import uvicorn

from .auth import Auth, is_loopback
from .detection import (DEFAULT_INTERVAL_S, DetectionCoordinator,
                        DetectorSupervisor, MockDetectorRunner)
from .ledger import Ledger
from .main import create_app
from .partstore import PartStore
from .printer import MockPrinter, PrinterService
from .runlog import RunRecorder
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

# --host's two resolved values. DEFAULT_HOST is this machine only; LAN_HOST is
# what --lan means. The flag default is None, not DEFAULT_HOST, so an explicit
# `--host 127.0.0.1` is distinguishable from "not given" -- see resolve_host.
DEFAULT_HOST = "127.0.0.1"
LAN_HOST = "0.0.0.0"

# Where --lan looks for the shared password. Purely a convention: nothing else
# in the codebase knows this filename, it is gitignored beside printers.json
# for the same reason (both hold a secret in plaintext), and BAMBU_PASSWORD
# still overrides it.
PASSWORD_FILE = pathlib.Path(".bambu-password")


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
        f"variable, put one line in {PASSWORD_FILE} and pass --lan, or bind "
        "127.0.0.1 (the default) to keep it to this machine.")


def resolve_host(host_arg: str | None, lan: bool) -> str:
    """-> the interface to bind.

    An explicit --host always wins, so `--lan --host 192.168.1.5` narrows the
    bind rather than being silently overridden. --lan alone means 0.0.0.0.
    Neither means loopback, the safe default.
    """
    if host_arg:
        return host_arg
    return LAN_HOST if lan else DEFAULT_HOST


def read_password_file(path: pathlib.Path) -> str | None:
    """-> the password in `path`, or None if it is missing, unreadable, blank,
    or whitespace-only.

    First line only, stripped: the file is written by hand and by `echo`, so a
    trailing newline is the normal case rather than an error. utf-8-sig for the
    same reason store.py uses it -- Windows editors add a BOM, and a BOM
    silently glued to the front of a password is a wrong password with no
    visible cause.

    Returns None rather than raising on a bad file **on purpose**: the decision
    about what a missing password means belongs to build_auth and must live in
    exactly one place, or the fail-closed rule ends up with two implementations
    that can disagree.
    """
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    return lines[0].strip() if lines and lines[0].strip() else None


def resolve_password(env_password: str | None, *, lan: bool,
                     password_file: pathlib.Path = PASSWORD_FILE) -> str | None:
    """-> the password to hand to build_auth.

    The environment always wins, so --lan changes nothing about the documented
    BAMBU_PASSWORD path (and nothing about the desktop build, which never
    passes --lan). --lan only ADDS a fallback: read the conventional file.

    Crucially it can still return None -- a missing or blank password file
    yields None and build_auth then refuses to start, exactly as it would
    have. --lan is a convenience for typing, never a way around the
    fail-closed rule; `test_lan_cannot_open_a_hole` pins that down.
    """
    if env_password:
        return env_password
    if lan:
        return read_password_file(password_file)
    return None


def _routed_ipv4() -> str | None:
    """-> the address of the interface that would carry traffic off this box.

    A UDP `connect` sends nothing -- it only makes the OS pick a route and bind
    a local address, which is then readable via getsockname(). 8.8.8.8 is a
    routing hint, never contacted, so this works with no internet.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def local_ipv4s() -> list[str]:
    """-> this machine's non-loopback, non-link-local IPv4 addresses.

    Two sources, because neither alone is complete. `gethostbyname_ex` gives a
    list but on Windows returns only what resolves for the hostname -- measured
    on the dev box, it missed one of five real addresses. The routed address
    fills that gap for the interface that actually matters. Deduped, order
    preserved.

    Deliberately unfiltered beyond loopback/link-local: a dev box typically
    also carries virtual-adapter addresses (VirtualBox/VMware/Hyper-V) that go
    nowhere, and there is no reliable way from here to tell those from the real
    one. Showing all of them and letting the operator pick beats guessing wrong
    -- which is also why the printer-subnet hint exists.
    """
    found = []
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    routed = _routed_ipv4()
    if routed:
        found.append(routed)
    out = []
    for a in found:
        if a not in out and not a.startswith(("127.", "169.254.")):
            out.append(a)
    return out


def _same_subnet(a: str, b: str) -> bool:
    """Crude /24 comparison. Enough for the only claim being made: that this
    address is on the same network segment as a printer we already talk to."""
    return a.rsplit(".", 1)[0] == b.rsplit(".", 1)[0]


def lan_url_lines(addresses, port: int, printer_hosts=()) -> list[str]:
    """-> one display line per address, marking any that share a /24 with a
    registered printer.

    That mark is a HINT about which of several addresses is the useful one, not
    a promise of reachability. It cannot tell you whether the network permits
    client-to-client traffic at all -- campus and guest networks routinely
    block exactly that, and it is invisible from this side, which is why the
    banner says to test with a phone.
    """
    lines = []
    for addr in addresses:
        hint = ("   <- same subnet as a registered printer"
                if any(_same_subnet(addr, h) for h in printer_hosts) else "")
        lines.append(f"http://{addr}:{port}{hint}")
    return lines


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
    p.add_argument("--host", default=None,
                   help=f"interface to bind (default: {DEFAULT_HOST}, this "
                        "machine only). Use 0.0.0.0 to serve the dashboard to "
                        "the LAN -- that REQUIRES the BAMBU_PASSWORD "
                        "environment variable to be set. An explicit value "
                        "here overrides --lan")
    p.add_argument("--lan", action="store_true",
                   help=f"serve to the whole LAN: binds {LAN_HOST}, reads the "
                        f"shared password from {PASSWORD_FILE}, and prints the "
                        "URLs to hand out. Still refuses to start if that file "
                        "is missing or blank -- it is a shortcut for typing, "
                        "not a way around the password requirement")
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

    # The ledger lives beside printers.json/queues.json, so it follows
    # BAMBU_DATA_DIR on the desktop build with no special handling. --mock
    # gets its own file rather than an in-memory store: unlike the queue
    # there is no MemoryLedger, and pointing it at runs-mock/ keeps mock runs
    # out of the real history just as effectively.
    ledger = Ledger((runs_dir / "ledger.db") if a.mock
                    else (runs_dir.parent / "ledger.db"))
    recorder = RunRecorder(registry, ledger, detection=coordinator)

    # Model files live beside ledger.db, in the same directory --mock keeps
    # separate from the real one, so a mock part's uploaded STL never lands
    # next to real parts on disk.
    parts_dir = runs_dir if a.mock else runs_dir.parent
    partstore = PartStore(parts_dir)

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

    host = resolve_host(a.host, a.lan)
    env_password = os.environ.get("BAMBU_PASSWORD")
    password = resolve_password(env_password, lan=a.lan)
    auth = build_auth(host, password)

    if not is_loopback(host):
        source = "the BAMBU_PASSWORD environment variable" if env_password \
            else str(PASSWORD_FILE)
        print(f"\n  Bambu Monitor -- serving to the LAN on {host}:{a.port}")
        print(f"  password loaded from {source}\n")
        # summaries()["printer"] is the printer's host -- see PrinterService
        # .summary(). Using it here avoids a registry accessor that would exist
        # only for this banner.
        urls = lan_url_lines(local_ipv4s(), a.port,
                             [s["printer"] for s in registry.summaries()])
        if urls:
            print("  hand out one of these:")
            for line in urls:
                print(f"    {line}")
        else:
            print("  (could not enumerate this machine's addresses -- find its "
                  "LAN IP by hand)")
        # Said every time, because a green result HERE does not prove anyone
        # else can connect: client isolation is common on campus and guest
        # networks, blocks device-to-device traffic outright, and is invisible
        # from the serving machine.
        print("\n  Test with one phone before telling everyone the URL.")
        print("  Ctrl+C to stop.\n")

    dist = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "dist"
    app = create_app(registry, runs_dir, dist, detection=coordinator,
                     queue=queue, slicer=slicer, auth=auth, ledger=ledger,
                     recorder=recorder, partstore=partstore)
    # uvicorn re-raises the signal it caught using whatever handler was
    # installed beforehand. SIGBREAK's OS default kills the process outright
    # (skipping `finally`), so map it to KeyboardInterrupt like SIGINT gets.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    try:
        uvicorn.run(app, host=host, port=a.port)
    finally:
        registry.stop_all()
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
