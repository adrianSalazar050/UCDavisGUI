"""Create resolution-degraded copies of a dataset split to simulate lower-
quality webcams, for testing how a trained detector holds up on cheaper
hardware than what the training images came from.

Each image is downscaled to a simulated native sensor size (area
interpolation, like sensor binning) then upscaled back to the original frame
size (linear interpolation, like a camera driver upsampling for a fixed
output resolution). Label .txt files are copied unchanged: YOLO labels are
fractions of image width/height, so they stay valid across resolution
changes as long as the aspect ratio is preserved.

    python simulate_webcam_resolutions.py
    python simulate_webcam_resolutions.py --resolutions 480 320 160 --split test
"""
import argparse
import shutil
from pathlib import Path

import cv2
import yaml

REPO_ROOT = Path(__file__).resolve().parent
DATASET_DIR = REPO_ROOT / "3d-printing-failure-detection.v1i.yolov8"
SOURCE_DATA_YAML = DATASET_DIR / "data.yaml"
OUTPUT_ROOT = DATASET_DIR / "webcam_sim"


def degrade_image(src_path: Path, dst_path: Path, tier: int, target_size: int) -> None:
    img = cv2.imread(str(src_path))
    if img is None:
        raise ValueError(f"Could not read image: {src_path}")
    small = cv2.resize(img, (tier, tier), interpolation=cv2.INTER_AREA)
    restored = cv2.resize(small, (target_size, target_size), interpolation=cv2.INTER_LINEAR)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(dst_path), restored)


def write_tier_data_yaml(tier_dir: Path) -> None:
    config = yaml.safe_load(SOURCE_DATA_YAML.read_text())
    config["train"] = str((DATASET_DIR / "train" / "images").resolve())
    config["val"] = str((DATASET_DIR / "valid" / "images").resolve())
    config["test"] = str((tier_dir / "images").resolve())
    (tier_dir / "data.yaml").write_text(yaml.safe_dump(config))


def build_tier(tier: int, split: str, target_size: int) -> Path:
    src_images_dir = DATASET_DIR / split / "images"
    src_labels_dir = DATASET_DIR / split / "labels"
    tier_dir = OUTPUT_ROOT / f"res_{tier}"
    dst_images_dir = tier_dir / "images"
    dst_labels_dir = tier_dir / "labels"
    dst_images_dir.mkdir(parents=True, exist_ok=True)
    dst_labels_dir.mkdir(parents=True, exist_ok=True)

    image_paths = sorted(p for p in src_images_dir.iterdir() if p.is_file())
    for image_path in image_paths:
        degrade_image(image_path, dst_images_dir / image_path.name, tier, target_size)
        label_path = src_labels_dir / f"{image_path.stem}.txt"
        if label_path.exists():
            shutil.copy2(label_path, dst_labels_dir / label_path.name)

    write_tier_data_yaml(tier_dir)
    print(f"res_{tier}: {len(image_paths)} images -> {dst_images_dir}")
    return tier_dir


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--resolutions", type=int, nargs="+", default=[480, 320, 160],
                         help="simulated native sensor resolutions (square, px)")
    parser.add_argument("--split", default="test", choices=["train", "valid", "test"],
                         help="which split to degrade")
    parser.add_argument("--target-size", type=int, default=640,
                         help="frame size images are upscaled back to after downscaling")
    return parser.parse_args()


def main():
    args = parse_args()
    for tier in args.resolutions:
        build_tier(tier, args.split, args.target_size)


if __name__ == "__main__":
    main()
