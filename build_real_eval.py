"""Build a YOLO validation set from REAL collected frames.

Training happens on synthetic composites, so validating on synthetic data would
only prove the generator is self-consistent. This makes a val set of untouched
camera frames: real spaghetti frames with boxes derived by differencing against
the nearest clean background, plus real clean frames as negatives.

    python build_real_eval.py                       # -> datasets/real_eval

LIMITS, because these numbers will be quoted later:

  * The boxes are DERIVED, not hand-drawn. They come from the same background
    differencing the cutout extractor uses, so they inherit its errors -- a
    tangle overlapping dark background may be clipped. Treat them as good
    localisation, not gold-standard annotation.
  * There is only ONE physical tangle in the whole dataset. A model trained on
    composites cut from it and evaluated on frames containing it is being tested
    on an object it has already seen. Scores here therefore measure "can it find
    THIS failure on this camera", NOT "can it find spaghetti in general". The
    latter needs failures from different prints.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import cv2
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from synth_dataset import (BED_ROI, best_background, is_plausible_failure,  # noqa: E402
                           load_pairs)


def box_for(spag_path, clean_paths, *, thresh=38, min_area=3000):
    img = cv2.imread(str(spag_path))
    bg = cv2.imread(str(best_background(spag_path, clean_paths)))
    if img is None or bg is None or img.shape != bg.shape:
        return None, None
    h, w = img.shape[:2]
    y1 = int((BED_ROI[1] + BED_ROI[3]) * h)
    diff = cv2.absdiff(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY),
                       cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY))
    m = (diff > thresh).astype(np.uint8)
    m[y1:] = 0
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    if n < 2:
        return None, None
    i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if stats[i, cv2.CC_STAT_AREA] < min_area:
        return None, None
    x, y, bw, bh = (stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_TOP],
                    stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT])
    alpha = ((lab == i).astype(np.uint8) * 255)[y:y+bh, x:x+bw]
    if not is_plausible_failure(img[y:y+bh, x:x+bw], alpha):
        return None, None      # same filter as training: hands are not failures
    return img, (x, y, x + bw, y + bh)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--src", type=pathlib.Path,
                   default=pathlib.Path("datasets") / "a1_camera")
    p.add_argument("--out", type=pathlib.Path,
                   default=pathlib.Path("datasets") / "real_eval")
    a = p.parse_args()

    clean, spag = load_pairs(a.src / "images")
    for sub in ("images/val", "labels/val"):
        (a.out / sub).mkdir(parents=True, exist_ok=True)

    pos = skipped = 0
    for sp in spag:
        img, box = box_for(sp, clean)
        if box is None:
            skipped += 1
            continue
        h, w = img.shape[:2]
        x0, y0, x1, y1 = box
        cv2.imwrite(str(a.out / "images/val" / sp.name), img,
                    [cv2.IMWRITE_JPEG_QUALITY, 95])
        (a.out / "labels/val" / (sp.stem + ".txt")).write_text(
            f"0 {((x0+x1)/2)/w:.6f} {((y0+y1)/2)/h:.6f} "
            f"{(x1-x0)/w:.6f} {(y1-y0)/h:.6f}\n", encoding="utf-8")
        pos += 1

    for cp in clean:
        img = cv2.imread(str(cp))
        if img is None:
            continue
        cv2.imwrite(str(a.out / "images/val" / cp.name), img,
                    [cv2.IMWRITE_JPEG_QUALITY, 95])
        (a.out / "labels/val" / (cp.stem + ".txt")).write_text("", encoding="utf-8")

    (a.out / "data.yaml").write_text(
        f"path: {a.out.resolve().as_posix()}\n"
        f"train: images/val\nval: images/val\nnc: 1\nnames: ['spaghetti']\n",
        encoding="utf-8")
    print(f"real eval set: {pos} positives (+{skipped} skipped as unusable), "
          f"{len(clean)} negatives -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
