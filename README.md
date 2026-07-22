# bambu_monitor

Failure detection and monitoring for Bambu Lab A1 / A1 mini printers: a
layer-indexed capture logger, a YOLO failure detector that can stop a print by
itself, and a web dashboard for a fleet of printers.

Developed against an A1 mini through 2026-07-19 and an A1 from 2026-07-21. The
server code is model-agnostic, but the **detection ROI** and the **training
data** are not — see [`master.md` §1.1](master.md) for exactly which claims were
verified on which machine.

**New here? Read [`master.md`](master.md)** — it explains the whole system end to
end: architecture, every module, the auto-stop state machine, and how to run it.
This file is the short version plus the research framing behind it.

```bash
pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
python -m server            # http://127.0.0.1:8000
python -m server --mock     # no hardware: three fake printers into runs-mock/
```

## Printer setup (do this first, it can fail)

On the printer screen: **Settings → LAN-only Mode → on**, power-cycle, then
**Settings → Developer Mode → on**. Both. Developer Mode is what opens MQTT
(8883), the camera stream (6000), and FTPS (990). It only appears once LAN-only
Mode is enabled, it disconnects the printer from Bambu Cloud, and Bambu does not
support it. Note the 8-char access code; it rotates on some firmware updates.

In the slicer: **Others → Special mode → Timelapse: Smooth**, so the toolhead
parks at the same place every layer.

Full connection details, TLS specifics, and troubleshooting:
[`CONNECTION.md`](CONNECTION.md).

## The pieces

| | What it does |
|---|---|
| [`master.md`](master.md) | **Start here.** Full system documentation |
| `server/` + `frontend/` | The dashboard: live status, camera, detection, auto-stop, print queue, microSD browser, multi-printer |
| `detect.py` | Headless detector. Owns the camera, writes `status.json` + `latest.jpg` for the server |
| `capture.py` | Layer-indexed capture logger (the training-data collector) |
| `check_registration.py` | The gate that decides whether the fixture is good enough to build a detector on |
| `probe_gcode.py` | The CAXTON self-labelling question |
| `train_failure_detector.py` | Fine-tune YOLOv8 on the public failure dataset |
| `collect_dataset.py`, `collect_backgrounds.py` | Collect real frames from *this* printer's camera (failures by hand, clean backgrounds unattended) |
| `split_source.py`, `synth_dataset.py` | Disjoint train/test split, then copy-paste augmentation into a trainable dataset |
| `build_real_eval.py`, `eval_real.py` | Evaluate on untouched real frames, at the operating point that matters |
| [`FAILURE_DETECTOR_REPORT.md`](FAILURE_DETECTOR_REPORT.md) | Training results, webcam-resolution study, and the A1-camera domain adaptation |
| [`FRONTEND-STACK-GUIDE.md`](FRONTEND-STACK-GUIDE.md) | Look & feel (Slate Daylight tokens) |

Printers are added in the browser (**Overview → Add printer**) by typing IP,
serial, and LAN access code — there are no `--host/--serial/--access-code` flags.
They persist to `printers.json` (gitignored; it holds access codes in plaintext)
and reconnect on restart.

Use an **external USB webcam** for capture. The built-in camera is toolhead-
mounted with fixed focus at 15 cm — the right viewpoint for CAXTON-style nozzle
monitoring, the wrong one for watching a whole print. (It *is* supported as a
detection source, where a 5-second capture interval makes its low frame rate a
non-issue.)

## 1. `capture.py` — the logger

```bash
python capture.py --host 192.168.1.42 --serial 0309xxxxxxxx \
                  --access-code 12345678 --camera 0 --out runs/

python capture.py --mock --out runs/     # no hardware; exercises the pipeline
```

Per print it writes:

```
runs/20260715T1432_Benchy/
  meta.json         gcode_file, total_layer_num, camera, settle
  telemetry.jsonl   every MQTT report, timestamped
  frames.csv        layer, time, path, sharpness, state, temps
  frames/layer_0001.jpg ...
```

On each layer change it waits `--settle` (default 1.5 s) for the toolhead to
finish parking, grabs `--burst` frames, and keeps the sharpest.

## 2. `check_registration.py` — the gate

Run two prints of the same file, both successful, then:

```bash
python check_registration.py runs/<run_A> runs/<run_B>
```

Reports per-layer sub-pixel shift (phase correlation) and MAD.

| Result | Meaning |
|---|---|
| shift < 2 px, MAD low | registered — proceed |
| shift large | bed not parking repeatably → force it with `G1 Y5 F6000` in layer-change custom G-code |
| shift small, MAD high | lighting/exposure/blur → lock camera exposure + WB, raise `--settle` |
| MAD rises with layer | drift or self-occlusion → check the montage |

**Do not trust a detector until this passes.** If subtraction can't tell two good
prints apart, a network agreeing with you is a coincidence.

## 3. `probe_gcode.py` — the CAXTON question

Run **during** a print of a big flat part:

```bash
python probe_gcode.py --host ... --serial ... --access-code ...
```

Sends M104 / M220 / M221, holds, restores. There is no ack — the printer never
says it ignored you. M104 and M220 you read off telemetry; **M221 you read off
the part.**

| Outcome | Consequence |
|---|---|
| all honoured | reproduce CAXTON self-labelling on a closed-ecosystem printer — that's a paper |
| M104/M220 only | partial: temp + speed heads. Flow/Z must come from slicer-side induction, one label per print |
| none | no self-labelling here. Borrow CAXTON's weights instead of rebuilding its dataset |

## 4. Detection and auto-stop

A YOLOv8 detector fine-tuned on a public failure dataset (blobs, cracks,
over-extrusion, spaghetti, stringing, under-extrusion) runs headless in
`detect.py`, capturing one frame every 5 seconds from either a USB webcam or the
printer's built-in camera. The server reads its output, and when *armed* will
stop the print itself after a qualifying detection is sustained for 10 seconds.

```bash
python train_failure_detector.py    # fine-tune (~2-4 h on an 8 GB GPU)
python run_camera_detection.py      # windowed live demo
```

The detector can never command the printer: the coordinator reads detections and
calls `stop_print` itself. Arming is runtime-only and never survives a restart.

See [`master.md` §4](master.md) for the state machine and
[`FAILURE_DETECTOR_REPORT.md`](FAILURE_DETECTOR_REPORT.md) for what the model
actually scores (mAP50 0.835, and essentially unaffected down to a simulated
320×320 sensor).

**The caveat is now a measurement.** Those numbers come from a generic
internet-scraped dataset, and on the A1's own fisheye camera that model scores
**mAP50 0.0016** — blind, not merely weaker. Fine-tuning on copy-paste synthetic
data built from real frames of this printer recovers it to 0.4539 with 100%
recall, but on 9 test positives from a single physical tangle in a single room.
That is enough to say the approach works and nowhere near enough to arm
auto-stop. [`master.md` §12](master.md) has the full story, including how the
first evaluation of it turned out to be circular; the exit criterion below is
still the bar.

## Exit criterion

Detection is done at **print-level FPR < 1% over ≥30 successful prints** and
**time-to-detection < 5 min over ≥20 induced failures across ≥3 induction
methods**. Then stop. Anything past that is polishing a commodity.

## Tests

```bash
python -m pytest -q          # server + root modules; no hardware required
cd frontend && npm test      # ROI drag maths (vitest)
```

Counts are deliberately not quoted here — see [`master.md` §10](master.md) for
what each test file covers, and run the commands for the number.

Design specs and implementation plans live in `docs/superpowers/`.
