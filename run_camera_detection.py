"""Live 3D-print failure detection from a webcam feed.

Opens a USB webcam, captures one frame every --interval seconds, runs the
trained failure-detector on it, and shows the annotated result in a window. The
last result stays on screen until the next capture replaces it. Press 'q' to
quit.

Capturing on an interval rather than every frame is deliberate: a print failure
takes tens of seconds to develop, so 5 s is ample, and it keeps USB bandwidth
and GPU load near zero. Pass --interval 0 for the old flat-out live feed.

    python run_camera_detection.py
    python run_camera_detection.py --camera 1 --conf 0.4
    python run_camera_detection.py --interval 2          # faster cadence
    python run_camera_detection.py --save runs/camera_demo.mp4
"""
import argparse
import sys
import time
from pathlib import Path

import cv2
import torch
from ultralytics import YOLO

sys.path.insert(0, str(Path(__file__).resolve().parent))
from detect import DEFAULT_INTERVAL_S, MAX_READ_FAILURES, WebcamSource  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_WEIGHTS = REPO_ROOT / "runs" / "train" / "failure_detector" / "weights" / "best.pt"
WINDOW_NAME = "3D print failure detection (q to quit)"


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--camera", type=int, default=0, help="USB camera index")
    parser.add_argument("--weights", default=str(DEFAULT_WEIGHTS),
                         help="trained checkpoint to run")
    parser.add_argument("--conf", type=float, default=0.25, help="confidence threshold")
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--device", default="0" if torch.cuda.is_available() else "cpu",
                         help="'0' for the first GPU, 'cpu' to force CPU")
    parser.add_argument("--width", type=int, default=1280, help="requested capture width")
    parser.add_argument("--height", type=int, default=720, help="requested capture height")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                         help="seconds between captures; 0 = every frame "
                              "(default: %(default)s)")
    parser.add_argument("--save", type=Path, default=None,
                         help="optional path to also save the annotated feed as an .mp4")
    return parser.parse_args()


def main():
    args = parse_args()
    if not Path(args.weights).exists():
        raise SystemExit(f"weights not found: {args.weights}\n"
                          "Train a model first (train_failure_detector.py) or pass --weights.")

    model = YOLO(args.weights)
    cam = WebcamSource(args.camera, width=args.width, height=args.height)
    writer = None
    shown = None        # last annotated result; stays on screen between captures
    failures = 0
    next_capture = 0.0

    try:
        while True:
            now = time.time()
            if now >= next_capture:
                next_capture = now + args.interval
                frame = cam.grab()
                if frame is None:
                    # A dropped frame is normal on USB. Only a run of them means
                    # the camera is actually gone -- keep showing the last good
                    # result meanwhile instead of tearing the window down.
                    failures += 1
                    print(f"dropped frame {failures}/{MAX_READ_FAILURES}")
                    if failures >= MAX_READ_FAILURES:
                        print("camera read failed repeatedly, stopping")
                        break
                else:
                    failures = 0
                    start = time.time()
                    results = model.predict(frame, conf=args.conf, imgsz=args.imgsz,
                                             device=args.device, verbose=False)
                    shown = results[0].plot()
                    dt = time.time() - start
                    cv2.putText(shown, f"{dt * 1000:.0f} ms  every {args.interval:g}s",
                                (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

                    if args.save is not None:
                        if writer is None:
                            h, w = shown.shape[:2]
                            args.save.parent.mkdir(parents=True, exist_ok=True)
                            writer = cv2.VideoWriter(str(args.save),
                                                      cv2.VideoWriter_fourcc(*"mp4v"),
                                                      20.0, (w, h))
                        writer.write(shown)

            if shown is not None:
                cv2.imshow(WINDOW_NAME, shown)
            # Short wait regardless of the capture interval: this is what keeps
            # the window repainting and 'q' responsive while we idle between
            # captures. Never sleep the interval outright.
            if cv2.waitKey(30) & 0xFF == ord("q"):
                break
    finally:
        cam.close()
        if writer is not None:
            writer.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
