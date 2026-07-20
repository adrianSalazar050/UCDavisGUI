"""Interval dataset collector for the A1 mini's built-in camera.

Why this exists: the failure detector was trained on 30-70 degree views and is
effectively blind on the A1's wide, low, near-horizontal camera -- zoomed
straight onto a real spaghetti tangle it scores 0.08 against a 0.25 threshold.
Closing that gap needs images from THIS camera, and enough variety of failure
position that the model learns the failure rather than the corner it sat in.

So: it grabs a frame every --interval seconds while you reposition the failure
between shots, and shows a big countdown so you know exactly how long you have.

    python collect_dataset.py --host 192.168.137.249      # code in the env
    python collect_dataset.py --interval 15 --out datasets/a1_run2

The access code comes from BAMBU_ACCESS_CODE, never argv (a process listing is
world-readable; the same rule detect.py follows).

Keys in the preview window:
    space   capture right now, without waiting for the timer
    c       label following frames "clean"     (no failure visible)
    s       label following frames "spaghetti" (failure visible)
    q / Esc stop

The label is written to manifest.csv beside each filename. It does NOT replace
drawing boxes later, but knowing which frames contain a failure at all is most
of the labelling effort, and it is nearly free to record while you are standing
there anyway.

IMPORTANT: run this INSTEAD of the server's detector, not alongside it. Both
want the camera, and detect.py is normally spawned by `python -m server` -- stop
the server (or turn detection off for this printer) before collecting.

Collect BOTH classes. A set containing only failures teaches the model that
every print is a failure; the clean frames are what make the failures mean
something.
"""
from __future__ import annotations

import argparse
import csv
import os
import pathlib
import sys
import threading
import time
from datetime import datetime, timezone

import cv2
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from detect import BambuCameraSource, WebcamSource  # noqa: E402

WINDOW = "A1 dataset collector - space=now  c=clean  s=spaghetti  q=quit"
LABELS = {ord("c"): "clean", ord("s"): "spaghetti"}

PRINTERS_FILE = pathlib.Path(__file__).resolve().parent / "printers.json"


def should_capture(*, manual: bool, shoot: bool, now: float, next_at: float) -> bool:
    """Fire the shutter?

    Manual mode fires ONLY on an explicit request. Guarding on `manual` rather
    than on the interval matters: a zero interval in the timed branch would make
    `now >= next_at` true on every pass and the tool would capture continuously,
    filling the disk in seconds.
    """
    if manual:
        return shoot
    return shoot or now >= next_at


def access_code_from_printers_json(host: str, path=PRINTERS_FILE):
    """The registered access code for `host`, or None.

    printers.json already holds it -- making the operator re-supply it via an
    env var or a prompt is redundant, and every extra input step is another way
    for the tool to do nothing at all. Tolerant by design: any read/parse
    problem just means "not found", and the caller falls back.
    """
    try:
        import json
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    entries = raw if isinstance(raw, list) else raw.get("printers", [])
    if not isinstance(entries, list):
        return None
    for e in entries:
        if isinstance(e, dict) and e.get("host") == host and e.get("access_code"):
            return str(e["access_code"])
    return None


class Grabber:
    """Pulls frames on a background thread so the countdown stays smooth.

    The A1's camera runs at roughly 0.4 fps, so a grab blocks for seconds. If
    the preview grabbed inline, the chronometer would only redraw every few
    seconds -- useless for telling you when to move the failure. Instead this
    keeps `latest` fresh and the UI thread renders freely.
    """

    def __init__(self, source):
        self._source = source
        self._lock = threading.Lock()
        self._latest = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            frame = self._source.grab()
            if frame is None:
                # grab() already retried and reconnected; don't spin hot.
                self._stop.wait(1.0)
                continue
            with self._lock:
                self._latest = frame

    @property
    def latest(self):
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)
        self._source.close()


def overlay(frame, *, remaining, label, saved, flash, manual=False):
    """Draw the chronometer and status onto a copy of the frame."""
    view = frame.copy()
    h, w = view.shape[:2]
    scale = w / 1280.0

    band = int(96 * scale)
    # The capture confirmation has to be unmissable from arm's length, with
    # your hands in the printer: whole-frame border AND the banner goes green.
    if flash:
        cv2.rectangle(view, (0, 0), (w - 1, h - 1), (0, 230, 0), int(26 * scale))
    cv2.rectangle(view, (0, 0), (w, band), (0, 110, 0) if flash else (0, 0, 0), -1)

    secs = max(0.0, remaining)
    if flash:
        cv2.putText(view, "CAPTURED", (int(16 * scale), int(70 * scale)),
                    cv2.FONT_HERSHEY_SIMPLEX, 2.0 * scale, (255, 255, 255),
                    int(4 * scale))
    elif manual:
        cv2.putText(view, "READY", (int(16 * scale), int(70 * scale)),
                    cv2.FONT_HERSHEY_SIMPLEX, 2.2 * scale, (0, 255, 255),
                    int(4 * scale))
        cv2.putText(view, "press SPACE to capture", (int(210 * scale),
                    int(70 * scale)), cv2.FONT_HERSHEY_SIMPLEX, 0.9 * scale,
                    (200, 200, 200), max(1, int(2 * scale)))
    else:
        cv2.putText(view, f"{secs:4.1f}s", (int(16 * scale), int(70 * scale)),
                    cv2.FONT_HERSHEY_SIMPLEX, 2.2 * scale,
                    (0, 255, 255) if secs > 3 else (0, 165, 255), int(4 * scale))
        cv2.putText(view, "to next shot", (int(230 * scale), int(70 * scale)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9 * scale, (200, 200, 200),
                    max(1, int(2 * scale)))

    colour = (0, 220, 0) if label == "clean" else (0, 100, 255)
    cv2.putText(view, f"label: {label}", (int(520 * scale), int(44 * scale)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9 * scale, colour,
                max(1, int(2 * scale)))
    cv2.putText(view, f"saved: {saved}", (int(520 * scale), int(80 * scale)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9 * scale, (200, 200, 200),
                max(1, int(2 * scale)))

    # Key hints on the frame itself. cv2 reads keys through the WINDOW, so they
    # do nothing unless this window has focus -- which is not discoverable, and
    # is the first thing anyone gets wrong. Say so on screen.
    foot = int(52 * scale)
    cv2.rectangle(view, (0, h - foot), (w, h), (0, 0, 0), -1)
    cv2.putText(view, "click this window first   |   c = clean    "
                      "s = spaghetti    SPACE = capture    q = quit",
                (int(16 * scale), h - int(18 * scale)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.62 * scale, (255, 255, 255),
                max(1, int(2 * scale)))
    return view


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default=None, help="printer IP (a1 source)")
    p.add_argument("--source", choices=("a1", "webcam"), default="a1")
    p.add_argument("--camera", type=int, default=0, help="webcam index")
    p.add_argument("--interval", type=float, default=10.0,
                   help="seconds between captures; 0 = MANUAL, capture only "
                        "when you press space (default: %(default)s)")
    p.add_argument("--out", type=pathlib.Path,
                   default=pathlib.Path("datasets") / "a1_camera")
    p.add_argument("--label", default="spaghetti", choices=sorted(set(LABELS.values())),
                   help="starting label (default: %(default)s)")
    p.add_argument("--no-preview", action="store_true",
                   help="headless: no window, no countdown, just capture")
    a = p.parse_args()

    if a.source == "a1":
        if not a.host:
            print("--source a1 requires --host", file=sys.stderr)
            return 1
        # Env var, then printers.json, then ask. Three chances to succeed
        # without the operator having to do anything, because every input step
        # is another way for this to silently exit and look broken.
        code = os.environ.get("BAMBU_ACCESS_CODE")
        where = "BAMBU_ACCESS_CODE"
        if not code:
            code = access_code_from_printers_json(a.host)
            where = "printers.json"
        if not code and sys.stdin.isatty():
            import getpass   # off screen, out of shell history
            code = getpass.getpass("LAN access code (not echoed): ").strip()
            where = "prompt"
        if not code:
            print(f"No access code for {a.host}.\n"
                  f"  Looked in: BAMBU_ACCESS_CODE, {PRINTERS_FILE}\n"
                  f"  Add the printer in the dashboard, or set it:\n"
                  f'    PowerShell:  $env:BAMBU_ACCESS_CODE = "12345678"\n'
                  f"    bash:        export BAMBU_ACCESS_CODE=12345678",
                  file=sys.stderr)
            return 1
        print(f"access code: from {where}")
        print(f"connecting to {a.host} ... (first frame takes a few seconds)",
              flush=True)
        source = BambuCameraSource(a.host, code)
    else:
        source = WebcamSource(a.camera)

    images = a.out / "images"
    images.mkdir(parents=True, exist_ok=True)
    manifest = a.out / "manifest.csv"
    new = not manifest.exists()
    mf = open(manifest, "a", newline="", encoding="utf-8")
    writer = csv.writer(mf)
    if new:
        writer.writerow(["filename", "iso_time", "unix_time", "label"])
        mf.flush()

    # Resume rather than overwrite: a second session must not clobber the first.
    saved = len(list(images.glob("*.jpg")))
    label = a.label
    print(f"collecting every {a.interval:g}s into {images} "
          f"({saved} already there), starting label={label!r}")

    manual = a.interval <= 0
    if manual:
        print("MANUAL mode: nothing is captured until you press space.")
    grab = Grabber(source).start()
    next_at = time.monotonic() + (a.interval if not manual else 0.0)
    shoot = False
    flash_until = 0.0
    rc = 0
    try:
        while True:
            frame = grab.latest
            now = time.monotonic()

            fire = frame is not None and should_capture(
                manual=manual, shoot=shoot, now=now, next_at=next_at)
            if fire:
                shoot = False
                next_at = now + a.interval
                saved += 1
                name = f"{saved:05d}_{label}.jpg"
                cv2.imwrite(str(images / name), frame,
                            [cv2.IMWRITE_JPEG_QUALITY, 95])
                writer.writerow([name,
                                 datetime.now(timezone.utc).isoformat(
                                     timespec="milliseconds"),
                                 f"{time.time():.3f}", label])
                mf.flush()
                flash_until = now + 0.35
                print(f"  [{saved:5d}] {name}")

            if a.no_preview:
                time.sleep(0.05)
                continue

            if frame is None:
                # Show a window IMMEDIATELY, before the first frame lands. The
                # A1 camera takes a few seconds to connect, and silently
                # showing nothing is indistinguishable from "the tool is
                # broken" -- which is exactly how it read the first time.
                waiting = np.zeros((360, 640, 3), np.uint8)
                cv2.putText(waiting, "connecting to camera...", (40, 170),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
                cv2.putText(waiting, f"{a.host or 'webcam'}   (q to quit)",
                            (40, 210), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                            (170, 170, 170), 1)
                cv2.imshow(WINDOW, waiting)
                if cv2.waitKey(100) & 0xFF in (ord("q"), 27):
                    break
                continue

            view = overlay(frame, remaining=next_at - now, label=label,
                           saved=saved, flash=now < flash_until, manual=manual)
            h, w = view.shape[:2]
            cv2.imshow(WINDOW, cv2.resize(view, (1120, int(1120 * h / w))))

            key = cv2.waitKey(30) & 0xFF
            if key in (ord("q"), 27):
                break
            if key in (ord(" "), 13):    # space or enter
                shoot = True
            elif key in LABELS:
                label = LABELS[key]
                print(f"  label -> {label}")
    except KeyboardInterrupt:
        pass
    finally:
        grab.stop()
        mf.close()
        cv2.destroyAllWindows()

    print(f"\n{saved} images in {images}")
    print(f"manifest: {manifest}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
