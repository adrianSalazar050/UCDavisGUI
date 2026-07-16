"""
Do two successful prints of the SAME file produce the same picture at the
same layer?

This is the gate. If the answer is no, every downstream idea -- reference
differencing, silhouette-vs-expected, anomaly scoring -- is dead, and you will
spend months blaming a model for a fixture problem. Run this before writing
any detector.

    python check_registration.py runs/2026...._benchy runs/2026...._benchy

Output: per-layer mean absolute difference + a residual montage.

How to read it:
  flat, low MAD across layers          -> registered. Proceed.
  MAD grows with layer number          -> the camera or printer is drifting,
                                          or the part occludes itself. Look at
                                          the montage.
  MAD spikes at scattered layers       -> the settle delay is too short and you
                                          are catching the toolhead in motion.
                                          Raise --settle in capture.py.
  MAD high everywhere, residual shows
  a shifted copy of the part           -> the bed is NOT parking at a repeatable
                                          Y. Force it: add to the slicer's
                                          layer-change custom G-code
                                              G1 Y5 F6000
                                          so the bed is at a known Y at every
                                          capture. This is the fix, not a
                                          better model.

There is no ML here on purpose. If a subtraction can't tell two good prints
apart, a neural network's agreement with you is a coincidence.
"""

from __future__ import annotations

import argparse
import csv
import pathlib

import cv2
import numpy as np


def load_layers(run: pathlib.Path) -> dict[int, pathlib.Path]:
    idx: dict[int, pathlib.Path] = {}
    csv_path = run / "frames.csv"
    if not csv_path.exists():
        raise SystemExit(f"{run}: no frames.csv")
    with open(csv_path) as f:
        for row in csv.DictReader(f):
            p = run / row["path"]
            if p.exists():
                idx[int(row["layer"])] = p
    return idx


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_a", type=pathlib.Path)
    ap.add_argument("run_b", type=pathlib.Path)
    ap.add_argument("--montage", type=pathlib.Path,
                    default=pathlib.Path("registration_residual.jpg"))
    ap.add_argument("--warn-shift", type=float, default=2.0,
                    help="mean inter-run shift in px above which the fixture "
                         "is not repeatable enough to difference against")
    ap.add_argument("--warn-mad", type=float, default=6.0,
                    help="MAD above this on 0-255 grey is suspicious")
    a = ap.parse_args()

    A, B = load_layers(a.run_a), load_layers(a.run_b)
    common = sorted(set(A) & set(B))
    if not common:
        raise SystemExit("no layers in common -- different prints?")

    print(f"{len(common)} common layers (A has {len(A)}, B has {len(B)})\n")
    print(f"{'layer':>6} {'dx_px':>7} {'dy_px':>7} {'|d|':>6} {'MAD':>7}")

    rows, tiles = [], []
    for n in common:
        ia = cv2.imread(str(A[n]), cv2.IMREAD_GRAYSCALE)
        ib = cv2.imread(str(B[n]), cv2.IMREAD_GRAYSCALE)
        if ia is None or ib is None:
            continue
        if ia.shape != ib.shape:
            raise SystemExit(f"layer {n}: frame sizes differ {ia.shape} vs "
                             f"{ib.shape} -- camera settings changed between "
                             f"runs. Fix that first.")
        # Sub-pixel translation between the two frames. This is the quantity
        # that actually matters: it IS "did the bed park in the same place".
        # MAD alone is dominated by background and is nearly blind to shift.
        (dx, dy), _ = cv2.phaseCorrelate(np.float32(ia), np.float32(ib),
                                         cv2.createHanningWindow(ia.shape[::-1],
                                                                 cv2.CV_32F))
        mag = float(np.hypot(dx, dy))
        d = cv2.absdiff(ia, ib)
        mad = float(d.mean())
        rows.append((n, dx, dy, mag, mad))
        flag = "  <-- shifted" if mag > a.warn_shift else ""
        print(f"{n:>6} {dx:>7.2f} {dy:>7.2f} {mag:>6.2f} {mad:>7.2f}{flag}")
        if len(tiles) < 8:
            tiles.append(cv2.applyColorMap(
                cv2.resize(d, (240, 180)), cv2.COLORMAP_INFERNO))

    mags = np.array([r[3] for r in rows])
    mads = np.array([r[4] for r in rows])
    ns = np.array([r[0] for r in rows], float)

    print(f"\nshift  mean {mags.mean():5.2f} px   max {mags.max():5.2f} px   "
          f"over {a.warn_shift} px: {(mags > a.warn_shift).sum()}/{len(mags)}")
    print(f"MAD    mean {mads.mean():5.2f}       max {mads.max():5.2f}")

    if len(ns) > 3:
        r = float(np.corrcoef(ns, mads)[0, 1])
        print(f"corr(layer, MAD) = {r:+.2f}  " + (
            "-> residual grows with height: drift or self-occlusion"
            if r > 0.5 else "-> no systematic drift with height"))

    if tiles:
        cv2.imwrite(str(a.montage), np.hstack(tiles))
        print(f"\nresidual montage -> {a.montage}")

    if mags.mean() > a.warn_shift:
        print(f"\nVerdict: NOT registered -- mean shift {mags.mean():.2f} px.")
        print("The bed is not parking repeatably. Force it in layer-change "
              "custom G-code (e.g. G1 Y5 F6000) before writing any detector.")
        return 1
    if mads.mean() > a.warn_mad:
        print(f"\nVerdict: aligned but noisy -- shift is fine "
              f"({mags.mean():.2f} px) yet MAD is {mads.mean():.2f}.")
        print("Lighting, exposure drift, or motion blur. Lock camera exposure "
              "and white balance, and raise --settle in capture.py.")
        return 1
    print("\nVerdict: registered. Proceed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
