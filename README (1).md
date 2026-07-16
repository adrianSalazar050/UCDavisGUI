# bambu_monitor

Data collection for A1 mini failure detection. Three scripts. No model, no GUI,
no classifier — those come after the data exists and the fixture is proven.

```bash
pip install paho-mqtt opencv-python numpy
```

## Printer setup (do this first, it can fail)

On the printer screen: **Settings → LAN-only Mode → on**, power-cycle, then
**Settings → Developer Mode → on**. Both. Developer Mode is what opens MQTT
(8883), the RTSPS live stream (322), and FTPS (990). It only appears once
LAN-only Mode is enabled, it disconnects the printer from Bambu Cloud, and
Bambu does not support it. Note the 8-char access code; it rotates on some
firmware updates.

In the slicer: **Others → Special mode → Timelapse: Smooth**, so the toolhead
parks at the same place every layer.

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

Use an **external USB webcam**. The built-in camera is ~0.5 fps, toolhead-
mounted, fixed focus at 15 cm — the right viewpoint for CAXTON-style nozzle
monitoring and the wrong frame rate for it.

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

**Do not write a detector until this passes.** If subtraction can't tell two
good prints apart, a network agreeing with you is a coincidence.

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

## What is deliberately not here

- **A classifier.** No data yet. Roboflow numbers mean nothing on your printer.
- **A GUI.** Moves print-level FPR by zero. Build it in React/Vite against
  VERA's tokens once there's something worth displaying.
- **The built-in camera / RTSPS.** 0.5 fps. Not worth the TLS fight.

## Exit criterion

Detection is done at **print-level FPR < 1% over ≥30 successful prints** and
**time-to-detection < 5 min over ≥20 induced failures across ≥3 induction
methods**. Then stop. Anything past that is polishing a commodity.

## 4. `server/` + `frontend/` — the dashboard

Live monitoring GUI (state, temps, layer, HMS, newest captured frame).
It never opens the webcam — it serves the newest frame `capture.py` wrote —
so it is always safe to run alongside a capture.

```bash
pip install -r requirements.txt

# once, or after frontend changes:
cd frontend; npm install; npm run build; cd ..

# with the printer:
python -m server --host 192.168.1.42 --serial 0309xxxxxxxx --access-code 12345678

# without any hardware (endless fake print into runs-mock/):
python -m server --mock
```

Then open http://127.0.0.1:8000. Frontend dev loop: `npm run dev` in
`frontend/` (Vite on :5173, proxies to :8000).

Design/spec: `docs/superpowers/specs/2026-07-16-bambu-dashboard-design.md`.
Look & feel follows `FRONTEND-STACK-GUIDE.md` (Slate Daylight tokens).
