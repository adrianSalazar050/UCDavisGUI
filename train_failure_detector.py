"""Fine-tune YOLOv8 on the 3d-printing-failure-detection dataset (Roboflow export).

    python train_failure_detector.py
    python train_failure_detector.py --model yolov8n.pt --epochs 50 --device cpu
"""
import argparse
from pathlib import Path

import torch
import yaml
from ultralytics import YOLO

REPO_ROOT = Path(__file__).resolve().parent
DATASET_DIR = REPO_ROOT / "3d-printing-failure-detection.v1i.yolov8"
SOURCE_DATA_YAML = DATASET_DIR / "data.yaml"


def resolve_data_yaml() -> Path:
    """Roboflow's data.yaml uses ../train-style paths that only resolve
    correctly when the current working directory happens to be the dataset
    dir. Rewrite train/val/test as absolute paths so training works no
    matter where this script is run from."""
    config = yaml.safe_load(SOURCE_DATA_YAML.read_text())
    for split in ("train", "val", "test"):
        parts = [p for p in Path(config[split]).parts if p not in ("..", ".")]
        config[split] = str((DATASET_DIR / Path(*parts)).resolve())

    resolved_path = DATASET_DIR / "data.resolved.yaml"
    resolved_path.write_text(yaml.safe_dump(config))
    return resolved_path


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="yolov8s.pt", help="pretrained checkpoint to fine-tune from")
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--batch", default=-1, type=lambda v: int(v) if v not in ("-1",) else -1,
                         help="images per batch; -1 lets Ultralytics auto-size for the GPU")
    parser.add_argument("--patience", type=int, default=30, help="early-stopping patience, in epochs")
    parser.add_argument("--device", default="0" if torch.cuda.is_available() else "cpu",
                         help="'0' for the first GPU, 'cpu' to force CPU")
    parser.add_argument("--workers", type=int, default=0,
                         help="dataloader worker processes; 0 avoids a Windows CUDA+multiprocessing "
                              "crash (each spawned worker reloads the CUDA DLLs, which can exceed the "
                              "page file). Raise this only after enlarging the Windows page file.")
    parser.add_argument("--project", default=str(REPO_ROOT / "runs" / "train"))
    parser.add_argument("--name", default="failure_detector")
    parser.add_argument("--resume", action="store_true",
                         help="resume an interrupted run from project/name/weights/last.pt "
                              "(training args are read back from the checkpoint)")
    return parser.parse_args()


def main():
    args = parse_args()
    data_yaml = resolve_data_yaml()

    if args.resume:
        last_pt = Path(args.project) / args.name / "weights" / "last.pt"
        model = YOLO(str(last_pt))
        model.train(resume=True)
    else:
        model = YOLO(args.model)
        model.train(
            data=str(data_yaml),
            epochs=args.epochs,
            imgsz=args.imgsz,
            batch=args.batch,
            patience=args.patience,
            device=args.device,
            workers=args.workers,
            project=args.project,
            name=args.name,
        )

    metrics = model.val(
        data=str(data_yaml),
        split="test",
        workers=args.workers,
        project=args.project,
        name=f"{args.name}_test",
    )
    weights_path = Path(args.project) / args.name / "weights" / "best.pt"
    print(f"\nTest set mAP50-95: {metrics.box.map:.4f}  mAP50: {metrics.box.map50:.4f}")
    print(f"Best weights: {weights_path}")


if __name__ == "__main__":
    main()
