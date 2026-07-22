"""Compare detector checkpoints on the REAL A1 evaluation set.

    python eval_real.py --models runs/train/failure_detector/weights/best.pt \
                                 runs/detect/runs/train/a1_holdout/weights/best.pt \
                        --eval datasets/real_ho

Use the a1_holdout checkpoint against datasets/real_ho. The earlier a1_synth
checkpoint and datasets/{synth,real_eval} come from the UNSPLIT frames and score
against data they were trained on -- kept only as the record of that mistake.

Reports mAP on real camera frames, and -- more usefully for this application --
the operating point that actually matters: at the deployed confidence threshold,
how many real failure frames are caught, and how many clean frames produce a
false alarm. mAP hides both.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


def operating_point(model, eval_dir: pathlib.Path, conf: float, imgsz=640,
                    device=0):
    """-> (recall on failure frames, false-alarm rate on clean frames)."""
    imgs = sorted((eval_dir / "images" / "val").glob("*.jpg"))
    pos = [p for p in imgs if p.name.endswith("_spaghetti.jpg")]
    neg = [p for p in imgs if p.name.endswith("_clean.jpg")]
    hit = 0
    for p in pos:
        r = model.predict(str(p), conf=conf, imgsz=imgsz, device=device,
                          verbose=False)[0]
        if r.boxes is not None and len(r.boxes):
            hit += 1
    fa = 0
    for p in neg:
        r = model.predict(str(p), conf=conf, imgsz=imgsz, device=device,
                          verbose=False)[0]
        if r.boxes is not None and len(r.boxes):
            fa += 1
    return (hit / max(len(pos), 1), fa / max(len(neg), 1), len(pos), len(neg))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--models", nargs="+", required=True)
    p.add_argument("--eval", type=pathlib.Path,
                   default=pathlib.Path("datasets") / "real_eval")
    p.add_argument("--conf", type=float, nargs="+", default=[0.25, 0.4])
    p.add_argument("--device", default=0)
    a = p.parse_args()

    from ultralytics import YOLO
    for mp in a.models:
        if not pathlib.Path(mp).exists():
            print(f"missing: {mp}")
            continue
        m = YOLO(mp)
        r = m.val(data=str(a.eval / "data.yaml"), imgsz=640, device=a.device,
                  workers=0, verbose=False, plots=False, split="val")
        print(f"\n=== {mp}")
        print(f"    mAP50 {r.box.map50:.4f}   mAP50-95 {r.box.map:.4f}")
        for c in a.conf:
            rec, fa, npos, nneg = operating_point(m, a.eval, c, device=a.device)
            print(f"    conf>={c:.2f}: caught {rec*100:5.1f}% of {npos} failure "
                  f"frames | false alarm on {fa*100:5.1f}% of {nneg} clean frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
