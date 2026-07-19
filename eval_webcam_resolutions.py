"""Evaluate a trained failure-detector checkpoint against the original test
set and any resolution-degraded simulated-webcam copies produced by
simulate_webcam_resolutions.py.

    python eval_webcam_resolutions.py
    python eval_webcam_resolutions.py --weights runs/train/failure_detector/weights/best.pt --device cpu
"""
import argparse

import torch
from ultralytics import YOLO

from train_failure_detector import DATASET_DIR, REPO_ROOT, resolve_data_yaml

WEBCAM_SIM_DIR = DATASET_DIR / "webcam_sim"


def discover_tiers():
    tiers = []
    if WEBCAM_SIM_DIR.exists():
        for tier_dir in sorted(WEBCAM_SIM_DIR.iterdir()):
            data_yaml = tier_dir / "data.yaml"
            if data_yaml.exists():
                tiers.append((tier_dir.name, data_yaml))
    return tiers


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights",
                         default=str(REPO_ROOT / "runs" / "train" / "failure_detector" / "weights" / "best.pt"))
    parser.add_argument("--device", default="0" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--workers", type=int, default=0)
    return parser.parse_args()


def main():
    args = parse_args()
    model = YOLO(args.weights)

    datasets = [("baseline_test", resolve_data_yaml())] + discover_tiers()
    if len(datasets) == 1:
        raise SystemExit(
            f"No simulated-webcam tiers found under {WEBCAM_SIM_DIR}. "
            "Run simulate_webcam_resolutions.py first."
        )

    rows = []
    for name, data_yaml in datasets:
        metrics = model.val(
            data=str(data_yaml),
            split="test",
            workers=args.workers,
            device=args.device,
            project=str(REPO_ROOT / "runs" / "eval"),
            name=name,
        )
        rows.append((name, metrics.box.map50, metrics.box.map, metrics.box.mp, metrics.box.mr))

    header = f"{'dataset':16s} {'mAP50':>8s} {'mAP50-95':>10s} {'precision':>10s} {'recall':>8s}"
    print("\n" + header)
    print("-" * len(header))
    for name, map50, map5095, precision, recall in rows:
        print(f"{name:16s} {map50:8.4f} {map5095:10.4f} {precision:10.4f} {recall:8.4f}")


if __name__ == "__main__":
    main()
