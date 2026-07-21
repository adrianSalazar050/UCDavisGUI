# 3D-Printing Failure Detector — Training & Resolution-Robustness Report

Date: 2026-07-17

## 1. What this is

A YOLOv8 object detector fine-tuned to spot common FDM print failures
(blobs, cracks, over-extrusion, spaghetti, stringing, under-extrusion) in a
camera frame, plus a follow-up experiment measuring how much a lower-quality
webcam would hurt its accuracy.

This is a baseline/prototype model trained on a public dataset, not the
final classifier for the Bambu A1 mini pipeline described in
`README.md`. That document's stance is deliberate: a generic
internet-scraped dataset doesn't necessarily transfer to one specific
printer's camera angle, lighting, and mount. Treat the numbers below as
"does the pipeline work and how robust is it to resolution," not as a
validated false-positive rate for the real printer.

## 2. Dataset

[3d-printing-failure-detection](https://universe.roboflow.com/verano-wxisi/3d-printing-failure-detection-41jmu)
(v1), exported from Roboflow in YOLOv8 format, CC BY 4.0.

| Split | Images |
|---|---|
| train | 4056 |
| valid | 1161 |
| test | 576 |

6 classes: `blobs`, `cracks`, `over_extrusion`, `spaghetti`, `stringing`,
`under_extrusion`. Images are pre-processed by Roboflow to 640x640
(stretched, auto-oriented), no additional augmentation applied by them.

## 3. Training setup

- **Script:** `train_failure_detector.py`
- **Base checkpoint:** `yolov8s.pt` (COCO-pretrained), fine-tuned
- **Hardware:** NVIDIA RTX 4060 Laptop GPU (8GB VRAM)
- **Key hyperparameters:** `imgsz=640`, `batch=-1` (auto-sized), `patience=30`
  (early stopping), `epochs=100` (cap)
- **Result:** stopped early at **epoch 77/100** (no improvement for 30
  epochs)

### Environment issues hit and fixed along the way

1. **CPU-only torch.** The machine has a CUDA-capable GPU but the installed
   `torch` build was CPU-only. Reinstalled `torch`/`torchvision` from the
   `cu124` wheel index. First attempt silently no-opped (pip considered the
   CPU build "already satisfied"); had to explicitly uninstall before
   reinstalling.
2. **Windows page-file crash (`WinError 1455`).** With the default 8
   dataloader workers, each spawned worker process re-imports `torch` and
   reloads the CUDA runtime DLLs (`cufft64_11.dll` etc.), which exhausted the
   page file. Fixed by setting `workers=0` (single-process data loading) in
   both training and evaluation. This is why the script defaults to
   `--workers 0` — raise it only after enlarging the Windows page file.
3. **Interrupted background run.** The training process was killed when the
   session paused (background processes don't survive a session restart).
   It had completed 23 full epochs at that point. Added a `--resume` flag
   that reloads `weights/last.pt` and calls `model.train(resume=True)`,
   which restores the optimizer/scheduler state and all original training
   args from the checkpoint — training continued from epoch 24 with no lost
   progress.

## 4. Training result

Evaluated on the held-out **test** split (576 images, never seen during
training or validation):

| Metric | Value |
|---|---|
| mAP50 | 0.835 |
| mAP50-95 | 0.490 |
| Precision | 0.857 |
| Recall | 0.782 |

Weights: `runs/train/failure_detector/weights/best.pt`

## 5. Webcam resolution robustness test

**Question:** if this model were deployed with a cheaper/lower-resolution
webcam than the images it was trained on, how much accuracy would it lose?

**Method (`simulate_webcam_resolutions.py`):** for each of the 576 test
images, downscale to a simulated native sensor size using area interpolation
(mimics sensor binning on a lower-resolution camera), then upscale back to
640x640 using linear interpolation (mimics the frame a camera driver would
actually hand off at a fixed output size). The result is a realistic
detail-loss degradation rather than just resampling the same information.

Label files are copied over **unchanged**. This is safe because YOLO labels
store box center/width/height as *fractions* of image width/height, not
absolute pixels — those fractions don't change when an image is resized, as
long as it isn't re-cropped.

Three tiers were simulated, from a decent budget webcam down to a genuinely
low-end one:

| Tier | Simulated native resolution |
|---|---|
| res_480 | 480x480 |
| res_320 | 320x320 |
| res_160 | 160x160 |

**Evaluation (`eval_webcam_resolutions.py`):** ran the trained model
(`best.pt`) against the original test set and each degraded tier, using
identical settings each time.

### Results

| Dataset | mAP50 | mAP50-95 | Precision | Recall |
|---|---|---|---|---|
| baseline (original test set) | 0.835 | 0.490 | 0.857 | 0.782 |
| res_480 (simulated) | 0.834 | 0.493 | 0.856 | 0.788 |
| res_320 (simulated) | 0.829 | 0.485 | 0.854 | 0.785 |
| res_160 (simulated) | 0.757 | 0.413 | 0.810 | 0.701 |

### Interpretation

- **Down to a simulated 320x320 native sensor, the model is essentially
  unaffected** — mAP50 stays within ~1% of baseline. A modern budget webcam
  would not meaningfully hurt detection.
- **At 160x160, accuracy drops noticeably:** mAP50 falls ~9.4% relative,
  mAP50-95 (which also penalizes loose boxes) falls ~15.7% relative, and
  recall drops ~10.4% relative — meaning the model starts *missing*
  failures outright, not just drawing sloppier boxes around them.
- Resolution only becomes a real risk with quite old/cheap camera hardware;
  anything VGA-ish or better should be fine.

## 6. Artifacts

| What | Path |
|---|---|
| Training/eval script | `train_failure_detector.py` |
| Degraded-dataset builder | `simulate_webcam_resolutions.py` |
| Resolution comparison script | `eval_webcam_resolutions.py` |
| Live webcam demo | `run_camera_detection.py` |
| Trained weights | `runs/train/failure_detector/weights/best.pt` |
| Degraded test images | `3d-printing-failure-detection.v1i.yolov8/webcam_sim/res_{480,320,160}/` |
| Per-tier eval plots/confusion matrices | `runs/eval/` |

## 7. Reproducing

```bash
pip install -r requirements.txt
# torch/torchvision need a CUDA build for GPU training, see requirements.txt

python train_failure_detector.py                  # fine-tune (~2-4 hours on an 8GB GPU)
python simulate_webcam_resolutions.py              # build the degraded test sets
python eval_webcam_resolutions.py                  # compare baseline vs degraded tiers
python run_camera_detection.py                     # live webcam demo
```

---

## 8. A1 built-in camera: domain adaptation (2026-07-19)

### 8.1 The problem

The model above was trained on a public dataset shot at 30-70 degrees looking
down. The A1 mini's built-in camera is a wide fisheye mounted low and nearly
horizontal. Measured on 29 real frames from that camera plus 49 real clean
frames as negatives:

| Model | mAP50 | mAP50-95 |
|---|---|---|
| `best.pt` on the public test split | 0.835 | 0.490 |
| `best.pt` on **real A1 frames** | **0.0016** | **0.0003** |

Not weak on this camera -- blind.

### 8.2 Method

`collect_dataset.py` gathered 112 real frames (63 spaghetti, 49 clean across 7
bed positions). `synth_dataset.py` cuts the real tangles out and pastes them
onto the real clean frames at varied position, scale, rotation and lighting,
emitting YOLO labels from the paste geometry, plus the clean frames unchanged as
negatives. `best.pt` was then fine-tuned on the result.

### 8.3 The first result was invalid

The first run scored **mAP50 0.7123, 100% recall, 0% false alarms** -- and was
circular. All 49 clean frames used as eval negatives were themselves training
negatives, and all 29 eval positives had their tangle pasted into ~600 training
composites. (An md5 comparison missed this: the files differ byte-wise only
because the two writers use JPEG q92 and q95. Content comparison caught it.)

`split_source.py` now partitions the source frames first, in blocks of
consecutive frames rather than at random -- frames captured seconds apart are
near-duplicates, so a random split leaks as effectively as sharing a frame.

### 8.4 Result on a disjoint split

Training: 517 composites from 10 cutouts and 32 clean backgrounds (train half).
Evaluation: 9 real spaghetti frames + 17 real clean frames (test half), no
overlap with training.

| Model | mAP50 | mAP50-95 | recall @0.25 | false alarm @0.25 |
|---|---|---|---|---|
| `best.pt` (public data) | 0.0000 | 0.0000 | 77.8% | 58.8% |
| fine-tuned on synthetic | **0.4539** | 0.1772 | **100%** | 11.8% |

The baseline's 77.8% "recall" is meaningless alongside a 58.8% false-alarm rate:
it fires on most frames, which is also why its mAP is 0 -- nothing is localised.

### 8.5 The false alarms are label errors

Both flagged clean frames (conf 0.77 and 0.87) visibly contain debris on the
plate. They were captured seconds after the operator changed the label, while
the plate was still being cleared, so the label state was stale. The model was
right and the labels were wrong.

So **11.8% is an upper bound**; the true rate on genuinely clean frames is near
zero. Root cause fixed in `collect_dataset.py`: an 8-second hold-off after any
label change.

### 8.6 What this does NOT establish

* **One physical tangle.** Every failure image in the dataset is the same object.
  This measures "can it find THIS tangle, in frames and positions it has not
  seen", not "can it find spaghetti". Different prints fail differently.
* **Tiny evaluation.** 9 positives and 17 negatives. Wide confidence intervals.
* **Scene-specific.** Copy-paste bakes in the backgrounds it pastes onto. All
  49 came from one printer in one room; a second A1 in a different room measures
  a scene difference of 72 (same-scene frames differ by 1-3), so this checkpoint
  should not be expected to transfer. The *cutouts* transfer; the backgrounds do
  not. Re-running `synth_dataset.py` with clean frames from the new scene and
  the existing cutouts is the cheap path.
* **Not deployable for auto-stop yet.** Even at the measured 11.8%, three
  consecutive frames are needed to fire, which works out to roughly 1 spurious
  stop per hour of printing. That must be verified as near-zero on genuinely
  clean data before arming.
