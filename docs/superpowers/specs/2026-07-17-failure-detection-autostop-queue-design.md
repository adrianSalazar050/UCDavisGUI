# Live Failure Detection + Auto-Stop + Print Queue — Design

**Date:** 2026-07-17
**Status:** Approved by user (conversation, 2026-07-17)
**Branch:** `dashboard`
**Builds on:** `2026-07-16-multi-printer-sd-browser-design.md` (v2). This adds to
the multi-printer app; it changes none of v2's printer/SD behaviour except where
noted (`build_summary` gains a `detection` sub-object; `/api/frame/latest` is
supplemented, not replaced).

## Purpose

Close the loop from the webcam to the printer, then give the bench a planner:

1. **Live failure detection** — a USB webcam feeds the already-trained YOLOv8
   failure detector (`runs/train/failure_detector/weights/best.pt`, 6 classes).
   The dashboard shows the annotated feed and what is being detected right now.
   Camera index and confidence threshold are set in the UI.
2. **Auto-stop** — when *armed*, a qualifying failure that persists for **10
   continuous seconds** stops the print over MQTT.
3. **Print queue** — a per-printer planner: order SD-card jobs, show each file's
   estimated time and filament grams (read from the sliced `.gcode.3mf`), running
   totals, and a projected finish time. **Planner only** — it does not command
   the printer in this cut.

**Build order: Phase 1 (1 + 2) first, Phase 2 (3) second.** They are separable
and get separate implementation plans; this one spec covers both so the shared
surfaces (nav, `printers.json` config, the selected-printer prop) are designed
once.

## Decisions made

| Question | Decision | Why |
|---|---|---|
| Where inference runs | **Separate process** (`detect.py`), server-supervised | Keeps torch/CUDA out of the FastAPI process; matches v2's "server reads frames off disk, never opens the camera" (`runs.py`). |
| Camera ownership | The detector owns the webcam while enabled; **mutually exclusive** with `capture.py` | Windows allows one process per camera device. An explicit `detect_enabled` flag frees the camera for `capture.py` when off. |
| Which printer | Reuse the existing **`capture` printer** gate | One webcam, one rig — the single-capture invariant already enforces "at most one" (`registry._clear_capture`). |
| Detector → server channel | **Disk handoff**: atomic `status.json` + `latest.jpg` | Same pattern and failure-tolerance as `capture.py` → `runs.py`. No new socket, no shared memory. |
| Who decides & actuates the stop | **The server**, never the detector | The MQTT link is single-owner in `PrinterService`; every destructive action stays on the side that holds the arm state. |
| What counts as a failure | **Per-class arming**, chosen in the UI; default `spaghetti` only | The model is a prototype on generic data (`FAILURE_DETECTOR_REPORT.md`) — it can false-positive on this printer. Narrow default. |
| Auto-stop gate | **Arm switch, off by default**, runtime-only (resets to off on restart) | Destructive + hardware-unverified. Arming should never survive a restart. |
| Debounce | **10 s sustained**, a sub-threshold gap resets the timer | The primary defense against the prototype model's false positives. The user's "more than 10 seconds" *is* the debounce. |
| Stop command | New `BambuLink.stop_print()` → `{"print":{"command":"stop"}}`, then verify + retry once | Bambu stop is a print command, not G-code. No ack exists (`bambu_link.send_gcode` docstring), so confirm via `gcode_state`. **Hardware-unverified.** |
| Detection UI shape | **Glanceable Dashboard** (camera + compact Auto-stop card) **+ a new Detection page** (all settings) | User pick (mockup B). Keeps the dashboard scannable; scales if more printers are watched. |
| Queue power | **Planner only** — no printer control | User pick. Pure software, fully testable with no hardware, zero risk. "Print next" is a documented later increment. |
| Queue time/grams source | **Parse the sliced `.gcode.3mf`** over FTPS; manual fallback | The SD listing has only name/size/mtime; the 3MF embeds `prediction` (time) and per-filament `used_g`. |
| Queue UI shape | **Dedicated "Queue" page** | User pick (mockup 1). Room for reorder, totals, and a future "Print next" button. |

## Architecture

```
detect.py                    # NEW (repo root) — camera owner + YOLO loop, headless.
                             #   Writes status.json + latest.jpg. Peer of capture.py /
                             #   run_camera_detection.py; reuses their open_camera pattern.
server/
  detection.py   # NEW — DetectorSupervisor + StatusReader + AutoStopController
  threemf.py     # NEW — parse sliced .gcode.3mf: est. time + filament grams. Pure.
  queue.py       # NEW — per-serial ordered job list + totals. Pure state, no I/O.
  sdcard.py      # + fetch_file(host, code, path) -> bytes  (FTPS download; hardware-gated)
  store.py       # PrinterConfig gains camera_index, conf, armed_classes, detect_enabled
  printer.py     # PrinterService/MockPrinter gain stop_print() (mock → FAILED)
  registry.py    # exposes capture_serial(); owns the DetectorSupervisor lifecycle
  main.py        # + detection & queue endpoints; WS merges detection into capture summary
  __main__.py    # --mock also seeds a synthetic detector (spaghetti on cue) for the loop
  runs.py        # UNCHANGED
frontend/src/
  pages/Detection.jsx        # NEW — index, confidence, per-class arming, health, preview
  pages/Queue.jsx            # NEW — job table, drag-reorder, totals, Add-from-SD modal
  components/dashboard/AutoStopCard.jsx   # NEW — armed state, Arm toggle, detection, countdown
  components/dashboard/CameraCard.jsx     # prefers the detection frame when a detector is running
  components/detection/*     # NEW — ClassArmList, DetectorHealth, ThresholdField
  components/queue/*         # NEW — QueueTable, JobRow, TotalsBar, AddFromSdModal
  app/pageRegistry.jsx       # + Detection, + Queue
  api/printer.js             # + detection + queue fetch wrappers
  styles.css                 # + auto-stop card, class list, queue table classes
```

`bambu_link.py` gains one method; `capture.py`, `probe_gcode.py`,
`check_registration.py`, `runs.py` are **not modified**. `train_failure_detector.py`
and `run_camera_detection.py` are unchanged (the latter's `open_camera`/predict
usage is the reference `detect.py` mirrors).

### Data flow (Phase 1)

```
 USB webcam ─▶ detect.py                         server (owns MQTT per printer)
              • owns camera (while enabled)       • reconcile loop: keep a detector
              • YOLO @ ~3–5 fps      disk           running for the capture printer,
              • writes atomically:  handoff          matching camera_index/conf
                  _detect/status.json  ─────────▶  • StatusReader reads status.json
                  _detect/latest.jpg   ─────────▶  • detection rides the WS summary
                                                   • AutoStopController: armed +
                                                     sustained ≥10 s ─▶ stop_print()
                                                          │
                                                          ▼  verify gcode_state,
                                                     BambuLink.stop_print()   retry once,
                                                     {"print":{"command":"stop"}}  latch
```

The detector writes to `<runs_dir>/_detect/`. The underscore keeps it clear of
`runs.find_active_run`, which only considers `<run>/frames/layer_*.jpg` — so the
existing frame endpoint and the detector never collide.

## Phase 1 details

### `detect.py` (camera owner + inference)

CLI (server passes these; also runnable by hand for debugging):
`--camera INT --conf FLOAT --weights PATH --imgsz 640 --out DIR --fps 4 --mock`.

Loop: open camera (buffer size 1, like `run_camera_detection.py`); each iteration
grab → `model.predict(conf=…, imgsz=…, verbose=False)` → annotate → write
`latest.jpg` and `status.json` **atomically** (temp + `os.replace`, the `store.py`
pattern) so the server never reads a half-written file.

`status.json` schema:
```json
{
  "ts": 1784310000.12, "fps": 4.1, "camera": 0, "conf": 0.25,
  "detections": [{"cls": "spaghetti", "conf": 0.72, "box": [x, y, w, h]}],
  "error": null
}
```
Camera-open failure or a read failure writes `{"error": "cannot open camera 0", …}`
rather than exiting silently, so the Detection page can show *why* it is down.
`--mock` runs the same loop against a synthetic frame source (no camera, no
weights) and emits a scripted detection pattern.

### `server/detection.py`

- **`DetectorSupervisor`** — a reconcile loop (~1 Hz, off the event loop):
  compute the *desired* detector = `(capture_serial, camera_index, conf, weights)`
  iff a capture printer exists **and** `detect_enabled`. If the running subprocess
  doesn't match desired (wrong printer/index/conf, or crashed), stop it and start
  the right one; if desired is none, stop it. Restart on crash with bounded
  backoff. Exactly one detector process, ever (one webcam). Graceful `stop()` on
  shutdown (`registry.stop_all` path).
- **`StatusReader`** — tolerant read of `_detect/status.json` (missing / half /
  stale → treated as "no detections", staleness surfaced as detector-down), the
  way `runs.py` tolerates a vanishing frame.
- **`AutoStopController`** — one small state machine per capture printer:

  | State | Meaning | Leaves when |
  |---|---|---|
  | `disarmed` | detect + warn only | user arms → `armed_idle` |
  | `armed_idle` | armed, no qualifying fault | an armed class ≥ conf appears → `armed_faulting(t0=now)` |
  | `armed_faulting` | fault building | fault clears → `armed_idle`; or `now - t0 ≥ 10s` → `stopping` |
  | `stopping` | stop sent | `gcode_state ∈ {FAILED, IDLE, FINISH}` → `stopped`; or no change within ~5 s → re-send once → `stopped` |
  | `stopped` | latched "stopped by monitor" | user disarms/acks |

  A qualifying fault = any detection whose `cls ∈ armed_classes` and
  `conf ≥ threshold`. Firing auto-disarms (no repeat stops). `armed` is
  **runtime-only** — not persisted; a restart comes up `disarmed`.

### `BambuLink.stop_print()` + services

`bambu_link.py`: publish `{"print": {"sequence_id": …, "command": "stop"}}`.
`PrinterService.stop_print()` delegates to the link. `MockPrinter.stop_print()`
sets `gcode_state = "FAILED"` so the whole arm→10s→stop→verify path runs under
`--mock`. Flagged hardware-unverified (consistent with the branch status): the
exact stop payload and that the printer honours it need the real A1 mini.

### Config & persistence

`PrinterConfig` gains (persisted in `printers.json`, already gitignored):
`camera_index: int = 0`, `conf: float = 0.25`, `armed_classes: list[str] =
["spaghetti"]`, `detect_enabled: bool = False`. Same tolerant `from_dict`
validation as `capture` (wrong type → safe default). **`armed` is not stored.**

### API

- The `/ws` tick **merges a `detection` object into the capture printer's
  summary** (null for the others), assembled by `detection.py` from the
  StatusReader + controller: `{running, fps, camera_index, conf, detect_enabled,
  armed, armed_classes, detections, stopped_by_monitor, error}`. Detection state
  stays out of `PrinterService` — it is joined at the WS edge (`main.py`) keyed on
  `capture_serial()`. No new polling; it rides the existing socket.
- `GET /api/printers/{serial}/detection` — config + live status (same object).
- `PUT /api/printers/{serial}/detection` — `{camera_index, conf, armed_classes,
  detect_enabled}`; the supervisor reconciles on the next tick.
- `POST /api/printers/{serial}/detection/arm` — `{armed: bool}` (runtime only).
- `GET /api/printers/{serial}/detection/frame` — serves `_detect/latest.jpg`
  (404 when no detector), mirroring `/api/frame/latest`'s in-handler read.

### Frontend (Phase 1)

- **Dashboard** — `AutoStopCard` (compact, right stack): armed dot + **Arm**
  toggle, current top detection + confidence, a countdown while `armed_faulting`,
  and a latched "Stopped by monitor" banner. `CameraCard` shows the annotated
  detection frame when `detection.running`, else falls back to the capture layer
  frame (`/api/frame/latest`) exactly as today.
- **Detection page** (new, in `pageRegistry`) — camera **index**, confidence
  **threshold**, **Enable detection** toggle, the **per-class arming** checklist
  (6 classes), and detector **health** (running / fps / `error`) with the live
  preview. Writes via `PUT …/detection`.

## Phase 2 details — Queue

### `server/threemf.py` (pure parser)

A `.gcode.3mf` is a zip. Read `Metadata/slice_info.config` (XML): total estimated
time from the `prediction` metadata, filament grams from summing each filament's
`used_g`. Tolerant: unknown/missing keys → that field is `None` (→ manual entry in
the UI). Unit-tested against small fixture 3MFs; no network.

### `server/sdcard.py` — `fetch_file(host, code, path) -> bytes`

Download one file over the same implicit-TLS FTPS session `list_dir` uses.
Hardware-gated, like the rest of `sdcard.py`. Guard the path with the existing
`normalize_path` traversal check.

### `server/queue.py` + `queues.json`

Per serial: an ordered list of jobs `{id, sd_path, name, est_seconds, grams,
source: "3mf"|"manual"}`. Operations: `add(sd_path)` (server fetches + parses +
caches time/grams; on FTPS/parse failure returns the job with `source:"manual"`
and null metrics), `remove(id)`, `reorder(ids)`, `totals()` (sum seconds + grams;
`projected_finish = now + total`, returned as a hint the UI labels `≈`). Persisted
atomically like `printers.json`; contains only filenames (no secrets). Isolated
from the registry so it is testable against `tmp_path`.

### API + Frontend (Phase 2)

`GET/POST/DELETE/PUT /api/printers/{serial}/queue`. New **Queue** page: the job
table with drag-to-reorder, a totals + projected-finish footer, and an "Add from
SD" modal that reuses `FileTable`. Per-printer (follows the selected printer, like
Dashboard/SD Files).

## Testing

- **Pure units:** `threemf` parse (fixture 3MFs incl. missing-metadata),
  `queue` add/remove/reorder/totals + persistence (`tmp_path`),
  `AutoStopController` fed synthetic status sequences — assert it fires at ~10 s,
  does **not** fire on a flapping/sub-threshold signal, auto-disarms after firing,
  and only re-sends once.
- **Service/API:** `stop_print()` publishes the right payload; `detection` rides
  the summary for the capture printer and is null elsewhere; config round-trips
  through `printers.json`; `PUT …/detection` is reflected in status.
- **`--mock` end-to-end:** synthetic detector emits `spaghetti`; arm → ~10 s →
  `MockPrinter` goes `FAILED` → summary shows `stopped_by_monitor`. The Queue page
  works over `MockPrinter.list_files` + a fixture 3MF. All green with no hardware.
- **Deferred to hardware** (see branch status): a real webcam at the given index;
  the printer actually honouring `stop`; FTPS fetch of a real sliced 3MF.

## Safety & non-goals

- **Safety:** off by default; runtime-only arm; narrow class default; 10 s
  sustained; fire → verify → retry once → latch; the detector can never command
  the printer.
- **Non-goals:** no auto-start / "Print next" (Phase 2 is planner-only); no model
  retraining or new dataset; no multi-camera; detector and `capture.py` stay
  mutually exclusive (not merged).

## Open items / risks

- **Stop payload:** `{"print":{"command":"stop"}}` is the documented Bambu LAN
  stop; verify on the A1 mini and keep the verify+retry as the backstop if a firmware
  variant ignores it.
- **3MF metadata keys** vary by slicer/version — the parser stays tolerant and the
  UI always allows manual override.
- **Projected finish** ignores inter-job bed-clearing and warm-up — always shown
  as `≈`.
