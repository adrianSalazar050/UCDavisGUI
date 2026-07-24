# ONNX inference backend for the failure detector — design

> **STATUS: PROPOSED (2026-07-23).** Agreed in brainstorming after the
> measurements in §2; implementation follows.
>
> Historical record, not maintained. **`master.md` is authoritative wherever
> this file disagrees with it.**

Date: 2026-07-23

---

## 1. The goal

Run the failure detector without PyTorch, so detection works on **any** machine
rather than only one with an NVIDIA GPU and a 4.7 GB CUDA install.

This is a **backend swap, not a model change**. Everything §12 of `master.md`
says about the detector's quality still stands: the public checkpoint is
effectively blind on the A1's built-in camera, the fine-tune is measured on nine
positives from a single tangle, and the exit criterion is unmet. Changing how
inference runs does not make the model better.

## 2. Feasibility, measured 2026-07-23

All of this was run on this machine and is reproducible.

### 2.1 The ONNX path is correct

Exported `runs/detect/runs/train/a1_holdout/weights/best.pt` with
`yolo export format=onnx imgsz=640 opset=12` → 44.7 MB, output shape
`(1, 4+nc, 8400)` (here `(1, 5, 8400)` — one class).

Compared against ultralytics across six real A1 camera frames: **detection
counts matched on every image**, boxes agreed to **≤1 px**, and confidences to
**0.001** once both paths were fed the same input (see §2.2).

### 2.2 The trap: rect vs square inference

Confidences appeared to differ wildly until the cause was found. Ultralytics'
`predict` on a `.pt` uses **rect inference** — it letterboxes to a
stride-aligned, aspect-preserving size (e.g. 640×416 for a 1680×1080 frame). An
exported ONNX graph has a **fixed square** 640×640 input. Same detection, one
image:

| Path | Confidence |
|---|---|
| torch, default rect input | **0.312** |
| torch, forced square 640×640 | **0.520** |
| onnxruntime, square 640×640 | **0.521** |

So ONNX matches torch to 0.001 — *given the same geometry*. The 0.21 gap is the
geometry, not the runtime.

**The consequence is the important part: a confidence threshold tuned on the
torch path does not transfer to the ONNX path.** The deployed threshold is 0.25,
and this detection reads 0.312 on one path and 0.521 on the other — the kind of
difference that changes whether auto-stop fires. Anyone switching backends must
re-tune `conf`, and must not assume old numbers carry over.

### 2.3 Size and speed

| | onnxruntime (CPU) | torch (as installed) |
|---|---|---|
| Installed | **44 MB** | **4,684 MB** (CUDA build) |
| Per frame | 1,160 ms | 132 ms (RTX 4060) |

ONNX on CPU is ~9× *slower* than this machine's GPU torch — but the detector
runs on a 5-second interval, so 1.16 s is ~23% of one core and speed is not the
constraint. The win is that **no GPU and no CUDA are required at all**.

## 3. Design

### 3.1 The seam already exists

`detection_loop(grab, infer, ...)` takes `infer` injected, and
`make_yolo_infer(weights, conf, imgsz, device)` returns a closure
`infer(frame) -> (detections, annotated_frame)`. A parallel
`make_onnx_infer(weights, conf, imgsz)` returning the **same contract** drops in
with no change to the loop.

### 3.2 Pure helpers, so the maths is testable without a model

- `letterbox(frame, size)` → `(padded, ratio, pad_x, pad_y)`
- `decode_yolo_output(raw, conf)` → candidate boxes/scores/class ids
- `scale_boxes_back(xywh, ratio, pad_x, pad_y)` → original-image coordinates

Each is a plain array-in/array-out function, testable against synthetic tensors
with no `.onnx` file and no onnxruntime.

NMS uses `cv2.dnn.NMSBoxes` — OpenCV is already a dependency, so this adds
nothing. Annotation is drawn with cv2, replacing ultralytics' `result.plot()`.

### 3.3 Backend selection

A `--backend {auto,onnx,ultralytics}` flag, default `auto`: weights ending
`.onnx` use onnxruntime, anything else uses ultralytics. Explicit values force
one. The torch path stays exactly as it is — training and the existing dev
workflow are untouched, and this machine keeps its faster GPU path if it wants
it.

### 3.4 No model ships

Decided in brainstorming: the app bundles the runtime and the plumbing but **no
weights**. Detection stays inert until the operator supplies a `.onnx`, so
nothing misleading ships, and `detection_available` (added 2026-07-23) already
makes the UI honest about it.

## 4. Testing

Pure-function tests with no model and no onnxruntime:

- `letterbox`: ratio and padding for landscape, portrait, and already-square
  inputs; output is exactly `size × size`.
- `decode_yolo_output`: a synthetic `(1, 4+nc, 8400)` tensor with known peaks —
  correct class, correct score, sub-threshold candidates dropped.
- `scale_boxes_back`: round-trips coordinates through a known letterbox.
- Backend selection: `.onnx` → onnx, `.pt` → ultralytics, explicit override wins.

Plus an integration check runnable on any box with a model present, comparing
ONNX output against ultralytics — the §2.1 parity run, kept as a script rather
than a unit test since it needs weights.

## 5. Out of scope

- **Improving the model.** §12 stands; this changes the runtime only.
- Bundling weights (§3.4).
- The frozen-desktop detector spawn. The deployment decision (2026-07-23) is one
  LAN server, where the detector is a normal subprocess — so the
  PyInstaller re-exec problem does not arise. If detection is ever wanted inside
  the packaged desktop app, that is a separate spec.
- GPU execution providers for onnxruntime. CPU is fast enough at a 5 s interval.
