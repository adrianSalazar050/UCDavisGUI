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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from detect import BambuCameraSource, WebcamSource  # noqa: E402

WINDOW = "A1 dataset collector - space=now  c=clean  s=spaghetti  q=quit"
LABELS = {ord("c"): "clean", ord("s"): "spaghetti"}


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


def overlay(frame, *, remaining, label, saved, flash):
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
    return view


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default=None, help="printer IP (a1 source)")
    p.add_argument("--source", choices=("a1", "webcam"), default="a1")
    p.add_argument("--camera", type=int, default=0, help="webcam index")
    p.add_argument("--interval", type=float, default=10.0,
                   help="seconds between captures (default: %(default)s)")
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
        code = os.environ.get("BAMBU_ACCESS_CODE")
        if not code:
            print("--source a1 requires BAMBU_ACCESS_CODE in the environment",
                  file=sys.stderr)
            return 1
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

    grab = Grabber(source).start()
    next_at = time.monotonic() + a.interval
    flash_until = 0.0
    rc = 0
    try:
        while True:
            frame = grab.latest
            now = time.monotonic()

            fire = frame is not None and now >= next_at
            if fire:
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
                time.sleep(0.05)
                continue

            view = overlay(frame, remaining=next_at - now, label=label,
                           saved=saved, flash=now < flash_until)
            h, w = view.shape[:2]
            cv2.imshow(WINDOW, cv2.resize(view, (1120, int(1120 * h / w))))

            key = cv2.waitKey(30) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == ord(" "):
                next_at = 0.0            # fire on the next pass
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
