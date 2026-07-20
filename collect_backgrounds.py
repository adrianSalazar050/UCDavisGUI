"""Capture clean bed frames automatically, jogging the bed between shots.

Two things the hand-collected set is short of, and both are free to fix without
anyone standing at the printer:

  * clean (negative) frames -- a set that is mostly failures teaches the
    detector that every print is a failure;
  * bed POSITION variety -- collecting while idle leaves the bed parked in one
    spot, whereas during a print it sweeps through the view. Varied empty-bed
    backgrounds are also exactly what copy-paste augmentation pastes onto.

So: home, then step the bed through a range of Y positions, capturing at each.

    python collect_backgrounds.py --host 192.168.137.249
    python collect_backgrounds.py --host ... --shots 3 --positions 20,60,100,140

SAFETY: this moves the machine. It homes first (G28) because a G1 after a failed
print would otherwise move from an unknown position, and it refuses to run
unless the printer is idle. It will NOT capture until you pass --confirmed-clear:
without it, it writes a single preflight.jpg and exits so a human can check the
plate is empty. (An automatic empty-bed check was tried and removed -- measured
on real data it was only 74% accurate, because clean frames differ from each
other more than a failure does. A check that unreliable is worse than none.)
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import sys
import time
from datetime import datetime, timezone

import cv2
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bambu_link import BambuLink  # noqa: E402
from collect_dataset import (access_code_from_printers_json,  # noqa: E402
                             printer_entry)
from detect import BambuCameraSource  # noqa: E402

IDLE_STATES = ("IDLE", "FINISH", "FAILED", "")
DEFAULT_POSITIONS = (20, 50, 80, 110, 140, 170)


def wait_for_state(state: dict, key: str, timeout: float = 20.0):
    end = time.time() + timeout
    while time.time() < end:
        if state.get(key) is not None:
            return state[key]
        time.sleep(0.5)
    return None


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", required=True)
    p.add_argument("--serial", default=None, help="defaults to printers.json")
    p.add_argument("--out", type=pathlib.Path,
                   default=pathlib.Path("datasets") / "a1_camera")
    p.add_argument("--positions", default=",".join(str(v) for v in DEFAULT_POSITIONS),
                   help="bed Y positions in mm (default: %(default)s)")
    p.add_argument("--shots", type=int, default=2, help="frames per position")
    p.add_argument("--settle", type=float, default=3.0,
                   help="seconds to wait after a move before capturing")
    p.add_argument("--confirmed-clear", action="store_true",
                   help="you have LOOKED at the bed and it is empty. Required: "
                        "without it this only takes a preflight frame and exits.")
    a = p.parse_args()

    entry = printer_entry(a.host) or {}
    serial = a.serial or entry.get("serial")
    code = access_code_from_printers_json(a.host)
    if not serial or not code:
        print(f"need serial + access code for {a.host}; not found in printers.json",
              file=sys.stderr)
        return 1

    positions = [int(v) for v in a.positions.split(",") if v.strip()]

    state: dict = {}
    link = BambuLink(a.host, serial, code, on_state=lambda s, _p: state.update(s))
    if not link.connect(timeout=15):
        print("could not connect over MQTT", file=sys.stderr)
        return 1
    wait_for_state(state, "gcode_state")
    gs = (state.get("gcode_state") or "").upper()
    if gs not in IDLE_STATES:
        print(f"printer is {gs}, not idle -- refusing to jog the bed",
              file=sys.stderr)
        link.disconnect()
        return 1

    cam = BambuCameraSource(a.host, code)
    images = a.out / "images"
    images.mkdir(parents=True, exist_ok=True)
    manifest = a.out / "manifest.csv"
    new = not manifest.exists()
    mf = open(manifest, "a", newline="", encoding="utf-8")
    writer = csv.writer(mf)
    if new:
        writer.writerow(["filename", "iso_time", "unix_time", "label"])
    saved = len(list(images.glob("*.jpg")))

    def grab():
        for _ in range(3):
            f = cam.grab()
            if f is not None:
                return f
        return None

    # Preflight instead of an automatic check. An earlier version diffed each
    # frame against the median of known-clean frames and refused if a large blob
    # appeared -- it did not work: measured on this dataset the clean frames
    # differ from that reference MORE than the spaghetti ones do (bed position,
    # lighting and background objects all move between sessions), giving 74%
    # accuracy. A safety check that wrong is worse than none, because it grants
    # false confidence while mislabelling. So a human looks instead.
    if not a.confirmed_clear:
        frame = grab()
        if frame is None:
            print("no frame from the camera", file=sys.stderr)
            cam.close(); link.disconnect(); return 1
        out = a.out / "preflight.jpg"
        cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
        print(f"\nPREFLIGHT: wrote {out}\n"
              "Look at it. If the bed is empty, re-run with --confirmed-clear.\n"
              "Frames labelled 'clean' with a failure on the plate are worse "
              "than no frames at all.")
        cam.close(); link.disconnect(); mf.close()
        return 0

    print("homing (G28) ...", flush=True)
    link.send_gcode("G28")
    time.sleep(25)

    written = 0
    try:
        for y in positions:
            print(f"bed -> Y{y} ...", flush=True)
            link.send_gcode(f"G1 Y{y} F3000")
            time.sleep(a.settle)
            for k in range(a.shots):
                frame = grab()
                if frame is None:
                    print("  no frame; skipping", file=sys.stderr)
                    continue
                saved += 1
                written += 1
                name = f"{saved:05d}_clean.jpg"
                cv2.imwrite(str(images / name), frame,
                            [cv2.IMWRITE_JPEG_QUALITY, 95])
                writer.writerow([name, datetime.now(timezone.utc).isoformat(
                    timespec="milliseconds"), f"{time.time():.3f}", "clean"])
                mf.flush()
                print(f"  [{saved:5d}] {name}")
                time.sleep(1.5)
    finally:
        cam.close()
        mf.close()
        link.disconnect()

    print(f"\n{written} clean frames added across {len(positions)} bed positions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
