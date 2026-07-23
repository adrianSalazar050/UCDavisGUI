# master.md — bambu-monitor, end to end

The umbrella document for this repo. Read this first; it links out to the
existing specialist docs rather than restating them.

| Doc | What it covers |
|---|---|
| `README.md` | Quick start, the data-collection scripts (`capture.py`, `check_registration.py`, `probe_gcode.py`), and the research framing + exit criterion |
| `CONNECTION.md` | Verified LAN/MQTT connection parameters (A1 and A1 mini), TLS specifics, prerequisites, troubleshooting |
| `FAILURE_DETECTOR_REPORT.md` | YOLO training run, test metrics, webcam-resolution robustness study, and §8 the A1-camera domain adaptation |
| `docs/superpowers/` | Per-feature design specs and implementation plans — **historical records, not maintained**. [Index + what's stale in them](docs/superpowers/README.md) |
| `FRONTEND-STACK-GUIDE.md` | ⚠ describes a *different* project (VERA/HORUS) whose conventions `frontend/` copied. Read for conventions, not facts |

---

## 1. What this system does

**Elevator pitch.** bambu-monitor turns a Bambu Lab A1/A1-mini (in LAN-only +
Developer Mode) into a monitored, semi-autonomous print farm node: a FastAPI
backend keeps a live MQTT connection to every registered printer, a React
dashboard shows state/temps/layer/HMS and a live camera view, a YOLO failure
detector watches the camera in its own process, and — when the operator arms
it — the server will send a `stop` command to the printer if a failure class
it is armed for is sustained for long enough. Alongside that it browses each
printer's microSD over FTPS, plans a per-printer print queue with time and
filament totals parsed out of `.gcode.3mf` files, slices an uploaded STL into
a startable `.gcode.3mf` by shelling out to Bambu Studio (§6) and queues the
result, and (separately) logs layer-indexed frames + telemetry for building
training datasets.

**Data-flow narrative.**

1. The printer publishes MQTT status reports to `device/<serial>/report` over
   TLS on port 8883. Reports are *partial* — `bambu_link.BambuLink` deep-merges
   each into a running state dict.
2. `server/printer.py::PrinterService` wraps one `BambuLink` per printer,
   timestamps the last report, and exposes `summary()` — a curated, secret-free
   payload (`build_summary`).
3. `server/registry.py::PrinterRegistry` holds `{serial: service}` plus the
   `PrinterConfig` for each, persisting to `printers.json`.
4. `server/main.py` (FastAPI) exposes those summaries over `GET /api/printers`
   and pushes them over `WS /ws` at up to 4 Hz.
5. Separately, `detect.py` runs as its **own process**: it grabs frames (from
   the printer's built-in camera over TCP 6000, or a USB webcam), runs YOLO,
   and writes `runs/_detect/latest.jpg` + `runs/_detect/status.json`.
6. `server/detection.py::DetectionCoordinator` polls that `status.json` on a
   background thread, feeds detections into an `AutoStopController` state
   machine, and — never the detector itself — calls `PrinterService.stop_print()`
   when the machine fires.
7. The frontend polls `/api/printers/{serial}/detection/frame` for the annotated
   JPEG and reads the detection snapshot off the WebSocket payload.
8. `capture.py` is a third, independent process for dataset collection: it
   subscribes to MQTT itself and writes `runs/<ts>_<name>/frames/layer_NNNN.jpg`
   + `telemetry.jsonl` + `frames.csv`. The server *reads* those files
   (`server/runs.py`) to serve `/api/frame/latest`; it never opens a camera.
9. Separately again, `POST /api/printers/{serial}/slice` hands an uploaded STL
   to `server/slicejobs.py::SliceCoordinator`, which runs on its own worker
   thread and shells out to `bambu-studio.exe` — **the only place in this repo
   that launches a subprocess to do work on a model**, as opposed to reading
   one. On success it reuses the existing `sdcard.upload_file` and
   `PrintQueue.add` unchanged, so slicing is additive: it produces exactly the
   kind of `.gcode.3mf` step 3's queue already knew how to plan and start. See
   §6.

---

### 1.1 Which hardware each claim was verified on

Two different printers have been used, and the docs below say "verified" about
both. They are **not** interchangeable, so when something says verified, check
which machine it means.

| | A1 mini | A1 |
|---|---|---|
| Serial | `0300CA633005010` | `03919D531805572` |
| In use | until 2026-07-19 | from 2026-07-21 (current) |
| Bed | 180 mm | 256 mm |
| Camera frame | 1680x1080 | 1536x1080 |
| Bed sits in frame | top half | bottom ~60% |

Verified on the **A1 mini**, and *not* re-checked on the A1: the
`file:///sdcard/<filename>` start-print URL scheme (§5.4), `stop_print()`'s
`PREPARE → FAILED` transition (§3.1), and the entire domain-adaptation dataset
and checkpoint (§12 — all of it shot on the mini, in a different room).

Verified on the **A1** (2026-07-21): MQTT connect + state, FTPS login/LIST, the
camera stream, and the ROI geometry above. The FTPS **upload** is implemented
and tested but has not written to a real card on either machine (§10).

**Not verified on any hardware:** that a CLI-sliced `.gcode.3mf` (§6) is
accepted and printed by a real printer. The container's shape is verified —
this repo's own `threemf.parse_slice_info` reads it, and the whole pipeline
runs end to end over HTTP against a mock printer — but nobody has slid the
result onto a real card and pressed Start. This is deliberate: there were
people working near the printer while this was built, and it would move.

Everything in `server/` is printer-model-agnostic — it never encodes a bed size
or a frame geometry. The model-specific values are the ROI (§4.1) and the
detection dataset (§12).

---

## 2. Architecture: processes, and why they are separate

The hard constraint that shapes everything: **on Windows a camera device can be
opened by exactly one process at a time.** So exactly one process owns the
camera, and it is never the server.

| Process | Owns | Why separate |
|---|---|---|
| `python -m server` (uvicorn) | MQTT links, FTPS calls, queue/printer persistence, HTTP/WS, the auto-stop decision | Must stay responsive; never blocks on a camera or on inference |
| `detect.py` | The camera (webcam index *or* the A1's TCP-6000 stream) + YOLO inference | Single-process-per-device rule; also keeps torch/CUDA out of the web server. Normally spawned and supervised *by* the server (`DetectorSupervisor`), but runnable standalone |
| `capture.py` | A USB webcam + its own MQTT subscription, for dataset logging | Independent research tool, predates the server; writes files the server later reads |

Because both `detect.py` and `capture.py` want a camera, you generally run one
or the other against a given device — see §11.

The interprocess contract is **files, not sockets**: `detect.py` writes
`runs/_detect/{status.json,latest.jpg}` with atomic temp+`os.replace`, and the
server reads them tolerantly (a bad read degrades to "detector down", never an
exception).

```
   ┌──────────────────────────────────────────────────────────┐
   │  Bambu A1 / A1 mini  (LAN-only + Developer Mode)          │
   │   MQTT 8883 (TLS)  ·  FTPS 990 (implicit TLS)  ·  TCP 6000 (camera, TLS)
   └───┬───────────────────┬──────────────────────────┬───────┘
       │ report / request  │ LIST / RETR              │ JPEG frames
       ▼                   ▼                          ▼
 ┌───────────────┐   ┌───────────────┐        ┌──────────────────┐
 │ bambu_link.py │   │ server/sdcard │        │  detect.py       │  ← separate
 │  BambuLink    │   │  ImplicitFTP  │        │  (owns camera,   │    process
 └──────┬────────┘   └──────┬────────┘        │   runs YOLO)     │
        │                   │                 └────────┬─────────┘
        ▼                   ▼                          │ atomic writes
 ┌──────────────────────────────────────┐              ▼
 │ server/printer.py  PrinterService    │      runs/_detect/status.json
 │ server/registry.py PrinterRegistry   │      runs/_detect/latest.jpg
 │ server/queue.py    PrintQueue        │              │
 │ server/detection.py Coordinator ─────┼──────────────┘ (reads)
 │ server/main.py     FastAPI app       │
 └──────────────┬───────────────────────┘
                │  /api/*   +  /ws        ▲
                ▼                         │ stop_print()  (server actuates,
 ┌──────────────────────────────────────┐ │  the detector never does)
 │ frontend/  React + Vite (port 5173   │ │
 │  in dev, or served from dist/ at     │─┘
 │  127.0.0.1:8000 in prod)             │
 └──────────────────────────────────────┘

 capture.py  ── own MQTT sub + own webcam ──▶  runs/<ts>_<name>/{frames/,
                                                telemetry.jsonl, frames.csv,
                                                meta.json}
                                                     │
                              server/runs.py ────────┘ (reads, serves
                                                        /api/frame/latest)
```

---

## 3. Component reference

### 3.1 Root-level modules

#### `bambu_link.py` — the MQTT client
The one place that speaks the printer protocol.

- `BambuLink(host, serial, access_code, on_state=, on_layer=)` — connects to
  8883 with TLS verification **off** (self-signed cert), user `bblp`, password =
  access code. Subscribes to `device/<serial>/report`, publishes to
  `device/<serial>/request`.
- `deep_merge(base, patch)` — the load-bearing one. Reports are partial; lists
  (`hms`, `ams.tray`) are replaced wholesale, dicts merged recursively.
- `push_all()` — sends `pushall` once on connect to get a full state snapshot.
- `send_gcode(line)` / `stop_print()` / `start_print(sd_path, plate=)` —
  fire-and-forget. **There is no ack**, so every caller confirms by watching
  `gcode_state`. `stop_print()` sends the Bambu print command
  `{"print": {"command": "stop"}}`, not G-code; **verified on hardware
  2026-07-19** — sent during `PREPARE`, the printer went `PREPARE → FAILED`, so
  a stopped print reports as FAILED (which is why `AutoStopController.TERMINAL`
  includes it). `start_print` is built by `build_project_file_command` — see
  §5.4 for the verified URL scheme and the two public references that are wrong
  for this printer.
- `decode_hms(attr, code)` — unpacks two 32-bit ints into the
  `AAAA_BBBB_CCCC_DDDD` form the Bambu wiki lookup expects.
- `summary()` — a convenience view (the server uses its own `build_summary`).

#### `capture.py` — layer-indexed dataset logger
Subscribes to MQTT, and on each **layer_num increase** waits `--settle` seconds,
grabs `--burst` frames, and keeps the sharpest.

- `Webcam` / `MockCamera` — `grab()` flushes one stale buffered frame first.
- `Run` — creates `runs/<ts>_<slug>/` with `frames/`, `telemetry.jsonl`,
  `frames.csv` (header written on open), `meta.json`.
- `Recorder.on_state` — opens a new `Run` on transition to `RUNNING`, closes it
  on `FINISH`/`FAILED`/`IDLE`; logs HMS warnings.
- `Recorder.on_layer` → `_capture` in a daemon thread, serialized by a lock.
- `sharpness(frame)` — variance of the Laplacian; only comparable within the
  same scene, which is exactly the use.
- `run_mock(...)` — synthesizes a print for `--mock`.

#### `detect.py` — the camera owner + detector
Headless. Grab → infer → write, forever.

- `BambuCameraSource(host, access_code)` — the A1's camera stream: TLS to TCP
  6000, a 64-byte auth packet (`bblp` + access code, both null-padded to 32),
  then repeating 16-byte headers whose first 4 bytes are a little-endian JPEG
  length. `grab()` reconnects once on a drop before returning `None`.
  The access code arrives via constructor/env, **never argv**.
- `WebcamSource(index)` — the USB path, and the exact counterpart of
  `BambuCameraSource`: same `grab()`/`close()` contract, where `None` means
  *genuinely gone* rather than *dropped one frame*. Recovery is staged
  cheapest-first — read again, and only if that also fails release and reopen the
  device. Reopening is what the user physically sees (USB chime, webcam LED), so
  it is a last resort, never the routine path. The device is opened lazily on the
  first `grab()`, so a camera still held by a previous detector that is shutting
  down is retried instead of being a hard startup failure.
- `open_camera(index, width, height)` — the raw `cv2.VideoCapture` open
  (`CAP_PROP_BUFFERSIZE = 1`), injectable into `WebcamSource` for tests.
- `make_yolo_infer(weights, conf, imgsz, device)` — imports ultralytics/torch
  *lazily* so unit tests can import `detect.py` with no CUDA runtime.
- `detections_from_result(result, names)` → `[{cls, conf, box:[x,y,w,h]}]`
  (xywh = center x, center y, width, height).
- `mock_infer(frame)` — always "sees" spaghetti at conf 0.9, so the whole
  arm → sustain → stop path is exercisable with no camera and no weights.
- `write_status` / `write_frame` / `_atomic_write_bytes` — temp file +
  `os.replace` in the same directory, with retries on Windows sharing violations.
- `_safe_write(...)` — swallows a transient `OSError` and logs; a dropped
  frame is fine, a persistent failure shows up as a *stale* status (the server
  marks the detector down) rather than crash-looping the process.
- `detection_loop(grab, infer, out_dir, *, camera, conf, ..., stop_event)` — the
  loop. It paces itself to one frame per `--interval` seconds (see §4.1).
  A transient failed read is tolerated and retried on the next tick; the loop
  writes an error status and exits only when the source is genuinely dead.

#### `run_camera_detection.py` — windowed demo
Same model, but opens an OpenCV window and draws FPS; `q` quits, `--save`
writes an mp4. Use it to eyeball the detector; it competes for the same camera
as `detect.py`.

#### `check_registration.py` — the fixture gate
Compares two runs of the *same* file layer by layer: sub-pixel translation via
`cv2.phaseCorrelate` (the number that actually matters) plus mean absolute
difference, and writes a residual montage. Exits non-zero with a verdict when
the bed isn't parking repeatably or the frames are too noisy. Deliberately no
ML. See `README.md` §2 for how to read the table.

#### `probe_gcode.py` — the CAXTON question
During a live print, sends `M104` / `M220` / `M221`, holds, restores, and
snapshots telemetry around each. Answers whether the printer honours live
parameter overrides (i.e. whether CAXTON-style self-labelling is possible here).
M104/M220 read off telemetry; M221 you read off the part.

#### `train_failure_detector.py`
Fine-tunes YOLOv8 on the Roboflow `3d-printing-failure-detection` v1 export.
`resolve_data_yaml()` rewrites Roboflow's relative paths as absolute so the
script runs from anywhere. Defaults to `--workers 0` because on Windows each
spawned dataloader worker reloads the CUDA DLLs and can exhaust the page file
(`WinError 1455`). `--resume` reloads `weights/last.pt`. Ends by validating on
the held-out test split. Results: `FAILURE_DETECTOR_REPORT.md`.

#### `simulate_webcam_resolutions.py` / `eval_webcam_resolutions.py`
Build resolution-degraded copies of the test split (area downscale → linear
upscale, labels copied unchanged since YOLO labels are fractional) and evaluate
`best.pt` against each tier. Conclusion in the report: essentially unaffected
down to a simulated 320×320 sensor; noticeably worse at 160×160.

> Note the label handling: copying labels unchanged is correct for a *resize*,
> because YOLO boxes are fractions of the image. It would be **wrong** for a
> perspective warp, where boxes have to be transformed through the same
> homography and re-fitted. Don't copy this script's shortcut into an augmenter.

#### `collect_dataset.py` — in-domain image collection
Grabs a frame every `--interval` seconds (default 10) from the A1's own camera
at full resolution, showing a large countdown so the operator knows how long
they have to reposition a failure between shots. `c`/`s` tag following frames
clean/spaghetti into `manifest.csv`; `space` shoots immediately. Resumes
numbering across sessions rather than overwriting. Finds the access code from
`BAMBU_ACCESS_CODE`, then `printers.json`, then a prompt.

Exists because of the domain gap in §12: the shipped model is effectively blind
on this camera, and closing that needs images from *this* camera. Competes for
the camera with `detect.py` — stop the server before collecting.

After the label-error finding in §12, it holds off **8 seconds** after any
`c`/`s` label change before capturing again, so the operator's hands and a
half-cleared plate can't land in a frame carrying the new label.

#### `collect_backgrounds.py` — clean frames, unattended
Homes the bed (`G28`), then steps it through a range of Y positions
(`--positions`, default 20–170 mm), capturing at each. Solves the two things
hand collection is short of: negatives, and bed-position variety — collecting
while idle parks the bed in one spot, whereas during a print it sweeps the view,
and varied empty-bed frames are exactly what copy-paste augmentation pastes onto.

**It moves the machine.** It refuses to run unless the printer is idle, and it
will not capture until you pass `--confirmed-clear`; without that flag it writes
a single `preflight.jpg` and exits so a human can confirm the plate is empty. An
automatic empty-bed check was built and then *removed* — measured at 74%
accurate, because clean frames differ from each other more than a failure does,
and a check that unreliable is worse than none.

#### `split_source.py` — the disjoint split
Partitions the collected frames into train/test halves **before** anything is
synthesised. This exists because the first evaluation was circular (§12), and it
splits in **blocks of consecutive frames, not at random**: frames captured
seconds apart are near-duplicates, so a random split puts a frame in train and
its twin in test, which leaks just as effectively as sharing the frame outright.

#### `synth_dataset.py` — copy-paste augmentation
Cuts the real tangles out of the failure frames (background differencing against
the nearest clean frame) and pastes them onto real clean frames at varied
position, scale, rotation, and lighting, emitting YOLO labels from the paste
geometry — labels are exact and free, which is the whole appeal over
hand-labelling. Clean frames are emitted unchanged with empty label files; YOLO
treats those as negatives, and they are what stop the model deciding every print
is a failure. Output is a ready-to-train `images/{train,val}` + `labels/…` +
`data.yaml` layout.

Paste displacement is **capped** (`--max-shift`) rather than uniform across the
frame: this camera is a wide fisheye, so a tangle photographed near the centre
does not look like one at the edge, and pasting it there produces an optical
configuration that never occurs — which a model will happily learn as an
artifact. The real fix for uncovered bed regions is a short collection pass
there, not more synthesis.

#### `build_real_eval.py` — the evaluation set that counts
Training runs on synthetic composites, so validating on synthetic data would
only prove the generator is self-consistent. This builds a val set of *untouched*
camera frames: real spaghetti frames with boxes derived by the same background
differencing, plus real clean frames as negatives. The boxes are **derived, not
hand-drawn**, and inherit that method's errors — good localisation, not
gold-standard annotation.

#### `eval_real.py` — checkpoint comparison at the operating point
Runs one or more checkpoints against a real eval set and reports mAP *and* the
number that actually decides deployment: at the deployed confidence threshold,
what fraction of real failure frames are caught and what fraction of clean
frames raise a false alarm. mAP hides both, and for auto-stop those two numbers
*are* the decision.

> The pipeline order matters and is easy to get wrong:
> `collect_dataset.py`/`collect_backgrounds.py` → **`split_source.py`** →
> `synth_dataset.py` (train half only) → fine-tune → `build_real_eval.py` +
> `eval_real.py` (test half only). Skipping the split is what produced the
> invalid 0.7123 in §12.

### 3.2 `server/` package

There is **no `server/summary.py`** — `build_summary()` lives in
`server/printer.py` (`server/tests/test_summary.py` tests it there).

| Module | Owns | Key names |
|---|---|---|
| `store.py` | `printers.json` persistence + the `PrinterConfig` dataclass | `PrinterConfig`, `PrinterStore`, `MemoryStore`, `DETECTION_CLASSES`, `CAMERA_SOURCES`, `MODEL_NAMES`, `guess_model_id`, `model_mismatch`, `NOZZLES`, `DEFAULT_NOZZLE` |
| `printer.py` | One live printer's state | `PrinterService`, `MockPrinter`, `build_summary`, `SUMMARY_FIELDS`, `STALE_S` |
| `registry.py` | The set of printers, keyed by serial | `PrinterRegistry`, `DuplicateSerial`, `.reconnect()`, `.printer_model()`, `.printer_nozzle()` |
| `main.py` | The FastAPI app + all routes | `create_app`, `AddPrinter`, `EditPrinter`, `DetectionUpdate`, `ArmBody`, `AddQueueJob`, `ReorderQueueJobs` |
| `detection.py` | Reading detector status, deciding, actuating | `StatusReader`, `AutoStopController`, `DetectorSupervisor`, `DetectionCoordinator`, `MockDetectorRunner` |
| `queue.py` | Per-printer job list + `queues.json` | `PrintQueue`, `QueueStore`, `MemoryQueueStore` |
| `sdcard.py` | microSD over FTPS (read + upload) | `list_dir`, `fetch_file`, `upload_file`, `normalize_path`, `ImplicitFTP_TLS`, `SdError`, `parse_mlsd`, `parse_list_lines` |
| `threemf.py` | Parsing a sliced `.gcode.3mf` | `parse_slice_info`, `SLICE_INFO_PATH` |
| `slicer.py` | Locating Bambu Studio, resolving vendor profiles, running the CLI (§6) | `find_slicer`, `profiles_root`, `ProfileIndex`, `flatten_profile`, `build_argv`, `run_slice`, `SliceError`, `SlicerNotFound`, `SLICE_TIMEOUT_S`, `OUTPUT_NAME` |
| `slicepresets.py` | Curated quality tiers + filament mapping (§6.3) | `TIERS`, `MACHINE_TOKENS`, `PROCESS_TOKENS`, `MATERIALS`, `machine_profile_name`, `resolve_preset`, `available_presets`, `filament_profile_name`, `available_filaments`, `detect_loaded_filament` |
| `slicejobs.py` | Slice job records, states, the worker thread (§6.4) | `SliceCoordinator`, `output_name`, `MODEL_EXTS`, `MAX_FINISHED_JOBS`, `TICK_S` |
| `runs.py` | Finding the newest captured frame | `find_active_run`, `newest_frame`, `ACTIVE_WINDOW_S` |
| `__main__.py` | CLI entry, wiring, `--mock` seeding | `main`, `real_factory`, `mock_factory`, `MOCK_SEED` |

**`store.py`.** `PrinterConfig` fields: `serial`, `host`, `access_code`, `name`
(falls back to host), `capture`, `camera_source` (`"a1"`/`"webcam"`),
`camera_index`, `conf`, `armed_classes`, `detect_enabled`, `roi` (§4.1;
`normalize_roi()` degrades a malformed value to `None` = whole frame, the same
rule as `detect.parse_roi`), `model_id` (§5.3), and `nozzle` (§6.4; one of
`NOZZLES`, degrading to `DEFAULT_NOZZLE = "0.4"` on anything else — the
printer never reports its installed nozzle any more than it reports its
model, so like `model_id` it can only be configured). `from_dict()` does
the type validation (the constructor does *not* — it only strips). `load()`
never raises: a corrupt file logs a warning and yields no printers, because a
server that refuses to boot leaves you with no UI to fix it from. Reads with
`utf-8-sig` (Windows editors add a BOM). `save()` is atomic + fsynced, and its
temp file is prefixed `printers.json` so `.gitignore`'s `printers.json*` rule
covers it by construction — an interrupted write can't leak an access code.

**`printer.py`.** `PrinterService` retries the initial MQTT connect every
`RETRY_S = 10`; once paho has connected once it handles reconnects itself. It
distinguishes *unreachable* (`connect()` raised) from *no CONNACK* (wrong access
code / Developer Mode off) and stores that as `last_error`. `build_summary()`
curates `SUMMARY_FIELDS` plus `hms`, `connection` (`ok`/`stale`/`disconnected`,
where stale = no report for `STALE_S = 15` s), `report_age_s`, and identity —
and deliberately takes **no** `access_code` parameter so the secret can never
reach a payload by accident. `MockPrinter` fakes the same duck-typed interface
in three modes (`running`/`stale`/`offline`) and writes real JPEGs into a real
run directory.

**`registry.py`.** Two locks on purpose: `_lock` guards the in-memory dicts and
is never held across `start()`/`stop()`/disk I/O (because `summaries()` runs on
the asyncio event loop for every WS tick and must not block); `_persist_lock`
serializes snapshot-then-write so two concurrent `add()`/`remove()` calls can't
race their `save()` and silently drop a printer from `printers.json`. Ordering
is registration order. Enforces "at most one capture printer" via
`_clear_capture()`, which mutates *both* the config and the live service's own
`capture` attribute. Detection accessors: `capture_serial()`,
`detection_config(serial)`, `detection_target()`, `update_detection(...)`.
`fetch_sd_file(serial, path)` delegates to the service so the access code never
appears in a queue route's signature. `printer_nozzle(serial)` mirrors
`printer_model()`: it never returns `""`, falling back to `store.DEFAULT_NOZZLE`
for an unknown serial, because callers substitute it straight into a slicer
profile name and an empty string would silently build a name that matches
nothing (§6.3).

**`main.py` routes.**

| Method + path | Notes |
|---|---|
| `GET /api/printers` | Summaries, each with a `detection` object (null unless capture printer) |
| `POST /api/printers` | 201; 409 on duplicate serial, 400 on bad fields |
| `PUT /api/printers/{serial}` | Edit host/name/capture/model_id; blank `access_code` = keep current. Serial is not editable |
| `POST /api/printers/{serial}/reconnect` | Rebuild the MQTT connection from the stored config. 200 + summary, 404 unknown. Sends no printer command |
| `DELETE /api/printers/{serial}` | 204 |
| `GET /api/printers/{serial}/files?path=/` | FTPS listing. 400 on bad path, 502 on printer failure |
| `POST /api/printers/{serial}/files` | Multipart upload (STOR) to the card **root**. 201 `{path, bytes, warning}`; 400 on a non-printable extension or empty body, 502 on printer failure |
| `GET/POST/PUT/DELETE /api/printers/{serial}/queue[...]` | Queue CRUD + reorder |
| `GET /api/frame/latest` | Newest `capture.py` frame; `X-Frame-Layer`, `X-Frame-Run` headers |
| `GET/PUT /api/printers/{serial}/detection` | Snapshot / config update |
| `POST /api/printers/{serial}/detection/arm` | `{armed: bool}` — runtime only, never persisted |
| `GET /api/printers/{serial}/detection/frame` | Annotated detector JPEG |
| `WS /ws` | Pushes `{printers: [...]}` on change (sampled every `WS_POLL_S = 0.25`s) or every `WS_HEARTBEAT_S = 5`s |

Two deliberate patterns here: the FTPS routes are **sync `def`** so FastAPI runs
them on a threadpool and a blocking TLS handshake can't stall the event loop and
freeze every WebSocket; and the frame routes read bytes *in-handler* so a file
vanishing mid-request is a clean 404 rather than a 500. `_comparable()` strips
`report_age_s` before diffing so a ticking clock doesn't force a push every
250 ms.

**`sdcard.py`.** MQTT exposes no file listing, so the card is read over FTPS on
port 990 with **implicit** TLS. `ftplib.FTP_TLS` only speaks *explicit* TLS, so
`ImplicitFTP_TLS` overrides the `sock` property to wrap the socket at assignment
time, and overrides `ntransfercmd` to reuse the control connection's TLS session
on the data channel (servers like the printer's require session reuse; without
it LIST hangs or fails after a successful login). `normalize_path()` rejects
`..` outright and rejects C0 control characters at the boundary (a `\r`/`\n` in
a path would otherwise raise a bare `ValueError` from inside `ftplib.putline`,
which is not an `ftplib` error subclass and would escape as a 500). `list_dir`
prefers MLSD and falls back to LIST only on a 500/502. `list_dir`, `fetch_file`
and `upload_file` **always** raise `SdError`, whose message never contains the
access code.

`upload_file` (STOR) is the only function in this package that **writes** to the
card, and it inherits the implicit-TLS and session-reuse machinery above for
free — which is most of why adding it was small. It overwrites silently, because
that is what STOR does and the printer offers no rename.

The route above it enforces what the *printer* can do with the result, and the
two accepted shapes are not equivalent:

| Uploaded | Queue can start it? | Why |
|---|---|---|
| `.gcode.3mf` | yes | `project_file` points at `Metadata/plate_N.gcode` inside the zip (§5.4) |
| `.gcode` | **no** — printer's own screen only | no verified MQTT command launches a raw gcode; real cards are full of these (confirmed on hardware 2026-07-21), so refusing them would break how the card is actually used |
| anything else | — | 400: the printer cannot print it at all |

Uploads always land in the card **root**, and the route takes `os.path.basename`
of the client's filename, discarding any directory component. Both halves matter:
`file:///sdcard/<name>` has no path component, so a file in a subdirectory could
be listed but never started, and stripping the directory also means a traversal
attempt lands harmlessly at `/evil.gcode.3mf` instead of escaping.

**`threemf.py`.** A `.gcode.3mf` is a zip; `Metadata/slice_info.config` is XML
with one `<plate>` each carrying `prediction` (seconds) and `weight` (grams)
metadata plus `<filament>` rows. `parse_slice_info(bytes)` sums across plates
and **never raises** — any missing or corrupt part yields `None` for that field
so the queue UI can fall back.

**`runs.py`.** `find_active_run()` picks the run directory with the most
recently modified `layer_*.jpg`, but only if within `ACTIVE_WINDOW_S` (30 min).
`newest_frame()` then returns the highest-numbered frame of that run. Skips
files that vanish or lock mid-scan (OneDrive).

---

## 4. The detection + auto-stop pipeline

### 4.1 `detect.py` → files

Every tick, `detect.py` grabs one frame, runs inference, and writes both
`latest.jpg` (annotated, JPEG q85) and `status.json`:

```json
{"ts": 1784500000.12, "fps": 3.1, "camera": 0, "conf": 0.25,
 "detections": [{"cls": "spaghetti", "conf": 0.91, "box": [320, 180, 64, 40]}],
 "error": null}
```

**Cadence.** The loop is interval-based: `--interval` (default **5.0 seconds**)
sets the target period between frames. After a grab+infer takes `dt`, the loop
waits out the remainder of the interval on the stop event, so a slow inference
never stacks up work and a fast one doesn't burn CPU. `fps` in the status is the
*inference* rate (`1/dt`), not the capture cadence. Set the interval to `0` to
disable throttling (used by tests).

**Dropped frames.** A USB webcam drops the occasional frame — bandwidth
contention, an auto-exposure re-lock, a driver hiccup while YOLO holds the CPU.
That is normal and is *not* a dead camera. A miss is logged and retried after
`READ_RETRY_S` (0.5 s) without writing a status at all, so the last good status
simply ages. Only `MAX_READ_FAILURES` (3) **consecutive** misses produce
`{"error": "camera N read failed (3 consecutive misses)", ...}` and end the loop,
at which point the supervisor's respawn backoff takes over. A single good frame
resets the counter.

This matters more than it looks: ending the loop exits the process, the
supervisor respawns it, and the respawn reopens the camera. Treating one dropped
frame as fatal therefore made the device disconnect and reconnect every few
seconds indefinitely. See §11 for the two other halves of that fix
(`WebcamSource` recovery and reaping the old process before respawning).

**ROI cropping.** `--roi x,y,w,h` (fractions of the frame) restricts inference to
the bed. **Measure it from a frame taken mid-print, not an idle one.** The bed
parks somewhere quite different from where it prints: the first default here was
measured idle and, during an actual print, contained only the printer's front
panel -- no bed at all.

> **The ROI is per-printer-MODEL, not a constant. Do not copy one between
> machines.** Measured 2026-07-21, the two are close to inverted:
>
> | Printer | Frame | Bed sits in | Working ROI |
> |---|---|---|---|
> | A1 **mini** | 1680x1080 | top half | `0,0,1,0.5` |
> | **A1** | 1536x1080 | bottom ~60% | `0.08,0.32,0.88,0.68` (provisional, measured idle) |
>
> Applying the mini's ROI to an A1 crops the bed out of frame **entirely** and
> feeds the detector nothing but the room -- which is the silent-failure mode
> this whole section exists to warn about. The A1 number above still needs
> confirming from a mid-print frame.

On the A1's wide, low view most of the frame is the room, and the model
duly finds "failures" in furniture — measured on a real frame, the full view
produced 5 false positives, *all* on a laptop keyboard, and the bed ROI produced
0. The crop is an inference input only: detections are mapped back with
`offset_detections`, and `compose_frame` pastes the annotated crop into the full
frame with the region outlined, so the operator keeps the whole view and can
tune the box by eye. A malformed ROI degrades to "whole frame" rather than
cropping the print out of view. Stored per printer as `PrinterConfig.roi`.

### 4.2 `StatusReader`

Reads `status.json` tolerantly. Any of `OSError`, `UnicodeDecodeError` (a torn
write caught mid multi-byte character), `JSONDecodeError`, or a non-dict payload
degrades to the *down* dict. Otherwise it computes `age_s = now - ts` and sets
`running = age <= stale_after and not error`.

`stale_after` is **derived from the capture interval**, not fixed:
`max(MIN_STALE_S 3.0, interval_s * STALE_INTERVALS 2.5)` — 12.5 s at the default
5 s interval. It has to span more than one interval: `status.json` is only
rewritten once per capture, so a 3 s window against a 5 s cadence would mark a
perfectly healthy detector "down" between *every* frame — and because the
coordinator feeds `[]` to the controller whenever the status is not `running`
(§4.4), that would silently disable auto-stop. `server/__main__.py` wires the
same `--detect-interval` into both the supervisor and the coordinator so the two
can never drift apart.

### 4.3 `AutoStopController` — the state machine

Pure: no I/O. It is fed `(detections, gcode_state)` and returns `"fire"` or
`None`; the caller actuates.

Configuration: `configure(armed_classes, threshold)`. A detection *qualifies*
when `d["cls"]` is in the armed set **and** `float(d["conf"]) >= threshold`; a
malformed `conf` is treated as `0.0` (fail-safe, never fires).

| State | Enter when | On qualifying fault | On no fault | Exit |
|---|---|---|---|---|
| `disarmed` | initial, or `arm(False)` | ignored (returns `None` immediately) | — | `arm(True)` → `armed_idle` |
| `armed_idle` | `arm(True)` | → `armed_faulting`, `fault_since = now` | stay | — |
| `armed_faulting` | fault first seen | if `now - fault_since >= sustain_s` (**10 s**): disarm, set `stopped_by_monitor`, → `stopping`, **return `"fire"`** | → back to `armed_idle`, timer cleared | — |
| `stopping` | after firing | — | if `gcode_state` in (`FAILED`,`IDLE`,`FINISH`) → `stopped`. Else after `verify_s` (**5 s**), re-`"fire"` once (max 2 sends total), then → `stopped` | — |
| `stopped` | verified, or gave up re-sending | ignored | — | `arm(True)` resets to `armed_idle` |

Key semantics:

- **Sustain** — a fault must be *continuously* qualifying for `sustain_s`.
  Any sub-threshold gap resets the timer to zero by returning to `armed_idle`.
- **Firing auto-disarms** (`_armed = False`) and latches
  `stopped_by_monitor = True`, which the UI shows until the next `arm(True)`.
- **Verify** — because `stop_print()` has no ack, `stopping` watches
  `gcode_state`. It re-sends exactly once after `verify_s`, then latches
  `stopped` rather than spamming the printer.
- `snapshot()` exposes `{armed, state, seconds_to_stop, stopped_by_monitor}`;
  `seconds_to_stop` is non-null only while `armed_faulting`, which is what drives
  the countdown in `AutoStopCard`.

### 4.4 `DetectorSupervisor`

Keeps exactly one `detect.py` subprocess matching the desired *target*
(`{serial, camera_source, camera_index, conf, host, access_code}`). On a target
change it terminates and respawns; on an unexpected exit it respawns after
`backoff_s` (5 s). `build_argv()` passes `--source`, `--conf`, `--weights`,
`--out`, `--interval`, and either `--host` (a1) or `--camera` (webcam).

Stopping **waits** for the old process: `terminate()` only *requests* an exit, and
until the process is actually gone it still holds the USB camera. Respawning
without reaping it means the new detector finds the device busy, dies with
`cannot open camera index N`, and gets respawned again — a flapping loop
indistinguishable from a camera reconnecting over and over. Hence
`proc.wait(timeout=TERMINATE_TIMEOUT_S)` (5 s) after `terminate()`.
`build_env()` puts the access code in `BAMBU_ACCESS_CODE` in the child's
environment for the `a1` source — **never in argv**, where it would be visible
to any process listing.

### 4.5 `DetectionCoordinator`

A daemon thread ticking every `TICK_S = 0.5`s. Each tick:

1. `runner.reconcile(registry.detection_target())`.
2. Drop controllers for any printer that is no longer the capture printer — a
   controller frozen mid-fault would otherwise fire immediately on the first
   frame after the camera is pointed back, with a stale `fault_since` bypassing
   the sustain debounce.
3. If there is no capture printer, or no detection config, stop here.
4. `status = reader.read()`. **Feed the controller `[]` unless
   `status["running"]`** — a dead or stale detector must never leave a stale
   `spaghetti` ticking toward a stop.
5. `ctrl.configure(...)`, `ctrl.update(detections, gcode_state)`.
6. On `"fire"`, call `svc.stop_print()` — in a `try/except`, and always from the
   server. The detector process has no channel to command the printer.

`snapshot(serial)` merges controller + status + config into the object the API
and WebSocket serve. `arm(serial, bool)` is runtime-only and never persisted.
An exception in a tick is logged and the loop continues.

`MockDetectorRunner` replaces the supervisor under `--mock`: instead of spawning
a subprocess it writes synthetic spaghetti status + annotated frames using
`detect.mock_infer` / `detect.write_*`, so the whole arm → 10 s → stop path and
the live camera view work with no camera and no weights.

---

## 5. Print queue and the multi-printer registry

### 5.1 `printers.json`

The persisted printer list. Written atomically, read tolerantly, **gitignored**
(it holds LAN access codes in plaintext — the same trust model `bambu_link.py`
already takes by disabling TLS verification on a LAN). One entry:

```json
{
  "serial": "0300CA633005010",
  "host": "192.168.137.152",
  "access_code": "········",
  "name": "A1 mini",
  "capture": true,
  "camera_source": "a1",
  "camera_index": 0,
  "conf": 0.25,
  "armed_classes": ["spaghetti", "stringing"],
  "detect_enabled": true
}
```

At most one entry may have `capture: true`. `registry.load()` enforces that even
against a hand-edited file, walking entries in file order with "last one wins".

### 5.2 The queue

`PrintQueue` itself is pure planning — no network, no registry. Only the start
route (§5.4) commands the printer. `POST .../queue` with
`{"sd_path": "/Benchy.gcode.3mf"}`:

1. `sdcard.normalize_path` (400 on a bad path),
2. `registry.fetch_sd_file` over FTPS,
3. `threemf.parse_slice_info` for seconds/grams.

An FTPS failure here is **not** fatal: the job is still queued with
`seconds: null, grams: null, source: "manual"`, so a momentarily-offline printer
doesn't block planning. Everything except `sd_path` is derived server-side —
`id` (uuid4 hex), `name` (basename), `seconds`, `grams`, `source`
(`"3mf"` or `"manual"`).

`PrintQueue` mirrors the registry's two-lock design and persists to
`queues.json` (`{serial: [job, ...]}`) after every mutation. `reorder()` keeps
only known ids in the given order, silently dropping stale ones. `totals()` sums
non-null seconds/grams and returns `finish_epoch = now + seconds` — a planner
hint labelled "~" in the UI, and `None` when there is nothing to time.

Passing `queue=None` to `create_app` disables every queue route (they 404),
the same "None means inert" convention as `detection`.

### 5.3 The printer-model check

A `.gcode.3mf` sliced for one model can be uploaded to another, and printing an
A1 file on an A1-mini's smaller bed is a real crash risk. Two halves:

- **The file knows.** `Metadata/slice_info.config` carries
  `<metadata key="printer_model_id" value="N2S"/>`, in the same file
  `parse_slice_info` already opens. `N2S` = A1, verified 2026-07-21.
- **The printer does not.** All 64 keys the A1 publishes over MQTT were dumped
  on 2026-07-21 and none identifies the model. So `PrinterConfig.model_id` is
  *configured*, prefilled by `guess_model_id()` from the serial prefix and
  correctable in the Edit form.

Enforcement escalates with the cost of being wrong:

| Step | Behaviour |
|---|---|
| Upload | 201 + `warning` naming both models — parking a file for another printer in the fleet is legitimate |
| Queue add | job records `model_id`; the row shows a ⚠ badge |
| **Start** | **409**, and the job stays queued |

> **UNKNOWN NEVER BLOCKS.** `model_mismatch()` returns `None` the moment either
> side is empty — every raw `.gcode`, every 3mf from a slicer that omits the
> key, and every printer whose model was never set. Only a *confirmed*
> difference between two known ids refuses anything. This matters because the
> serial-prefix table is mostly unverified community knowledge (see the comment
> on `MODEL_NAMES`): a confidently wrong guess must never be able to cost you a
> print, and the worst it can do is refuse one you can re-enable by setting the
> model to Unknown.

### 5.4 Starting a print

`POST /api/printers/{serial}/queue/{job_id}/start` sends the queue head to the
printer. No upload: jobs already reference microSD files, so this is one MQTT
`project_file` command via `BambuLink.start_print`.

**The URL scheme is `file:///sdcard/<filename>`, verified on a real A1 mini
(2026-07-19). Not yet re-verified on the A1** now in use — same firmware family,
so it very probably behaves identically, but "probably" is not what the rest of
this section means by *verified*. Confirm it before trusting a queued start.
Both public references are wrong for this printer — OpenBambuAPI's `mqtt.md`
documents `file:///mnt/sdcard` (that is the X1) and `davglass/bambu-cli` sends
`ftp:///<path>`. The candidates were tried against the hardware:
`file:///sdcard/` was accepted first (`FAILED → PREPARE`, the printer echoing
the file back as `subtask_name`). `param` is `Metadata/plate_N.gcode`. Filenames
containing spaces work unencoded. Which spelling of `bed_leveling` /
`bed_levelling` the firmware reads is unconfirmed, so **both keys are sent** with
the same value. Do not "correct" any of this to match the public docs.

Because MQTT has no ack, publishing is not printing. The route therefore:

1. guards — `PrinterService.start_print` raises `PrinterBusy` (→ 409) when
   disconnected or already printing, rather than publishing into silence;
2. publishes;
3. polls `gcode_state` for up to `START_VERIFY_S` (8 s) via `verify_start`;
4. **dequeues only on confirmation.** If the printer never reports starting, the
   job stays queued and the response says so — a command the printer ignored
   must never silently eat a job.

Only the queue **head** may start (409 otherwise): one unambiguous button, and
reorder decides what is next. `MockPrinter.start_print` mirrors the guards and
transitions so `--mock` exercises start → verify → dequeue with no hardware.

What this does *not* do is watch the print afterwards. A start that succeeds and
then fails at layer 3 on an HMS reports as started, because that is all the
route claims to verify.

---

## 6. Automatic slicing

Upload an STL, pick a printer, and get a sliced `.gcode.3mf` uploaded to that
printer's microSD and queued — with the filament detected from MQTT, a curated
quality preset, and a tree-support toggle. Three new modules
(`server/slicer.py`, `server/slicepresets.py`, `server/slicejobs.py`) do the
new work; the last step of the pipeline is the **existing**
`sdcard.upload_file` and `PrintQueue.add` from §5, unmodified. This feature
never commands a printer — it stops at producing a file the existing start
route (§5.4) can start.

### 6.1 The engine is Bambu Studio, not OrcaSlicer

Both are installed on the machine this was built on, and both are PrusaSlicer
forks with near-identical CLIs. They are **not** interchangeable. Measured
2026-07-22: OrcaSlicer slices fine but `--export-3mf` never produced a file
across five argument orderings, and it needs `use_relative_e_distances`
patched before it will slice at all. Bambu Studio has neither problem. This
matters beyond convenience: a raw `.gcode` **cannot be started over MQTT** —
§5.4's `project_file` command points at `Metadata/plate_N.gcode` *inside* a
`.gcode.3mf` zip, and the upload route's table (§5.2) already records that raw
`.gcode` is printer-screen-only. Orca's output was a file the printer could
never be told to run. **Do not "simplify" this back to OrcaSlicer** — it looks
like the more open choice and it is the one that doesn't work.

### 6.2 Vendor profiles are `inherits` partials

Bambu Studio ships **1,932** vendor presets under
`resources/profiles/BBL/**/*.json`, none of them self-contained: the A1
machine profile carries 39 keys and an `inherits` pointing at a parent that
carries the other ~70, and passing the raw 39-key file to `--load-settings`
fails validation. `ProfileIndex.load()` indexes every profile in the tree —
by its `name` **field**, not its filename, because the two differ often
enough that keying on the filename silently loses profiles — and
`flatten_profile(name, index)` walks the `inherits` chain recursively (child
keys win over parent keys), raising `SliceError` on an unknown name, a
missing parent, or an inheritance cycle (never a bare `RecursionError`, which
would otherwise take the whole server down on a vendor tree that ships a
cycle). Both are pure functions tested with a fake index and no slicer
installed.

The invocation, verified by hand on this machine:

```
bambu-studio.exe <model.stl>
  --load-settings  "<flat_machine.json>;<flat_process.json>"
  --load-filaments "<flat_filament.json>"
  --slice 0
  --export-3mf     "<name>.gcode.3mf"
  --outputdir      "<per-job temp dir>"
```

`--outputdir` is **mandatory** — without it the output lands nowhere
findable and the slice appears to succeed while producing nothing. The model
path comes first, before any option; that ordering is the one that was
verified to work. `run_slice()` writes the three flattened configs into the
job's own directory, runs this argv with a `SLICE_TIMEOUT_S` (900 s) timeout so
a pathological model can't pin a core forever, and treats **any** nonzero exit
as failure even when a `.gcode.3mf` is on disk — Bambu Studio exits 0 on
success (measured on this machine), so a nonzero code with a file present is
most likely a crash mid-export leaving a *truncated* file, and those bytes
would otherwise get uploaded to a printer's microSD and queued. (OrcaSlicer
exits nonzero on success, which is exactly why that "treat any file as
success" shortcut is wrong — but Orca isn't the engine here; see §6.1.) Exit 0
with **no** file is OrcaSlicer's exact failure mode from §6.1, and `run_slice`
never lets it read as success either.

### 6.3 Quality tiers, and why a preset can't be a literal profile name

A preset is offered as one of three curated **tiers** — `standard` (vendor
"Standard"), `fine` ("Optimal"), `draft` ("Extra Draft") — resolved against
the profile index at request time, not looked up in a fixed table of profile
names. That indirection exists because the profile-naming scheme has three
traps, all measured on this install on 2026-07-22:

| Trap | Reality |
|---|---|
| The model token differs by profile kind | the mini is `A1 mini` in **machine** profile names (`Bambu Lab A1 mini 0.4 nozzle`) but `A1M` in **process**/**filament** names (`0.20mm Standard @BBL A1M`) — one printer, two tokens, and neither is derivable from the other |
| The nozzle suffix is conditional | omitted at 0.4 (`0.20mm Standard @BBL A1`), present otherwise (`0.30mm Standard @BBL A1 0.6 nozzle`) |
| Layer height is not constant across nozzles | "Standard" is 0.20 mm at 0.4 and 0.30 mm at 0.6 — a hardcoded label would show a height the printer isn't using |

So `resolve_preset(tier_id, model_id, nozzle, index)` builds a **fully
anchored** regex (`^\d+\.\d+mm {tier} @BBL {token}{suffix}$`, `re.fullmatch`)
and searches the index for the one name that satisfies it, then reads the
displayed `label` back off whatever matched. Fully anchored, not
`startswith`/`endswith` checked independently, because a name like
`"0.20mm Silent Standard @BBL A1"` would satisfy a naive
layer-height-prefix-plus-tier-suffix pair for tier `"standard"` — and because
`"Silent Standard"` sorts before `"Standard"`, it would silently win over the
real profile. One anchored pattern closes that gap. `resolve_preset` returns
`None` when nothing resolves (not every tier exists for every nozzle);
`available_presets()` filters those out, so an unavailable combination is a
missing option in the UI rather than a slice that fails late. Filament
profiles (`filament_profile_name`) use the same process token as above, not
the machine one, for the same "Generic PLA @BBL A1" / "@BBL A1M" split.

**Filament detection.** `detect_loaded_filament(state)` reads
`ams.tray[].tray_type`, falling back to `vt_tray`, off the live MQTT state
dict — the same deep-merged, partial-at-any-moment shape as everywhere else
in this repo (§3.1), so it is deliberately paranoid about type at every level.
Returns `None`, the normal case, for any spool the printer's RFID can't
identify (most third-party filament) — the UI prefills the dropdown with the
result and leaves it always editable, so an unidentifiable spool never blocks
slicing. When an AMS has more than one tray loaded, it returns the **first**
identifiable one in unit/slot order — there is no "active tray" signal to read
instead — so on a mixed-material AMS this can report a material other than the
one actually feeding the nozzle. Acceptable only because the field stays
editable; never treat it as authoritative.

**Tree supports** are a single key. The A1 process profile already ships
`support_type = 'tree(auto)'`; the "Tree supports" checkbox patches only
`enable_support` (into a **copy** of the process dict — the caller's cached
profile is never mutated). Default is **off**, matching the vendor default.
Measured on an overhanging test model: 485 s / 1.70 g off, 968 s / 2.84 g on —
a support toggle that silently defaulted on would double a print's cost
without the operator asking for it.

### 6.4 Nozzle, and provenance instead of the file's own metadata

Resolving a machine profile needs the installed nozzle, and the printer
doesn't report it over MQTT any more than it reports its model (§5.3) — so,
like `model_id`, it is a **configured** `PrinterConfig` field: `nozzle`, one of
`NOZZLES = ("0.2", "0.4", "0.6", "0.8")`, degrading to `DEFAULT_NOZZLE = "0.4"`
on anything unparseable rather than raising, the same rule `normalize_roi()`
already applies — a wrong nozzle slices for the wrong hardware, so the default
has to be the common case. `registry.printer_nozzle(serial)` never returns
`""` for the same reason `printer_model()` doesn't: callers splice it straight
into a profile name string, and an empty string would silently build a name
that matches nothing.

**The CLI omits `printer_model_id`.** A CLI-sliced `.gcode.3mf`'s
`Metadata/slice_info.config` carries no `printer_model_id` key (measured
2026-07-22), so if the model-mismatch guard (§5.3) read it off the file the
way the upload route does for a human-sliced file, it would silently see
"unknown" and never fire. `slicejobs._do()` sidesteps this by recording
**provenance** instead of asking the file: the queue job's `model_id` is set
from `registry.printer_model(serial)` — the printer we *sliced for*, known at
submit time — not parsed back out of the bytes we just produced.

### 6.5 The job coordinator: one worker, globally

`SliceCoordinator` (`server/slicejobs.py`) owns the job list and a **single**
background worker thread — not one per printer. Slicing pegs a CPU core, and
this same server supervises a YOLO detector process that has to stay
responsive (§2); a per-printer worker would let a multi-printer fleet start
several slices at once and starve detection, which is the one thing on this
box that must not stall. `run`, `parse`, and `clock` are injectable — the same
seam pattern as `DetectorSupervisor`'s `spawn` and the registry's
`service_factory` — so the whole state machine tests with no slicer, no
printer, and no camera.

A job moves `queued → slicing → uploading → done`, or to `failed` from any of
those, or to `cancelled` while still `queued`. Each job gets its own
directory, `runs/_slice/<job_id>/` (§9), removed with `shutil.rmtree` on
**every** exit path including cancellation. **Any failure — slicing, or the
upload after it — latches `failed` and leaves the queue completely
untouched**, the same "a step that didn't happen must never leave a
half-finished job behind" principle as §5.4's dequeue-only-on-confirmation. If
`queue.add` itself raises *after* a successful upload, the file is already on
the card with no queue entry for it — recoverable from the SD Files page, and
the job reports `failed` even though the upload succeeded; this is a known,
accepted gap rather than something the coordinator tries to paper over.

Jobs are **runtime-only, never persisted** — the same reasoning as "arm is
runtime-only" (§4.5): a half-finished slice pointing at a deleted temp
directory must not survive a restart. The *result* is durable, because it
lands on the microSD and in `queues.json`. Left unbounded, a server nobody
clears the job list on would accumulate one record per slice ever submitted;
`MAX_FINISHED_JOBS` (50) evicts the *oldest* **terminal** (`done`/`failed`/
`cancelled`) records once the count is exceeded — a job still `queued`,
`slicing`, or `uploading` is never touched by this regardless of how far past
the cap the total grows.

Two correctness details worth knowing if you touch this file:

- **`stop()` deliberately does not clear `self._thread` when the join times
  out.** `run_slice` allows the CLI up to `SLICE_TIMEOUT_S` (900 s), so a
  `stop()` called mid-slice can easily outlive a short join. If it cleared the
  thread reference anyway, a later `start()`'s liveness check would see `None`
  and spawn a **second** worker on top of one still running a slice —
  defeating the entire single-global-worker design in the first paragraph
  above. `start()` therefore checks `self._thread.is_alive()`, not identity
  with `None`, specifically so it catches this. Same class of bug
  `DetectorSupervisor._stop_proc` guards against for its own subprocess: never
  let go of a handle to something you only *asked* to stop, only to something
  that actually did.
- **`lifespan` stops only what it actually started, in reverse order.** With
  two lifecycle components (`detection`, `slicer`), a raised exception from
  the second `start()` must not skip past a `finally` that unconditionally
  stops both — that would call `.stop()` on a `slicer` that never started. A
  `started` list is appended to only after each `start()` succeeds, and
  `finally` walks it in reverse.

### 6.6 Routes

| Method + path | Notes |
|---|---|
| `GET /api/printers/{serial}/slice/options` | Presets + filaments that actually resolve for this printer, plus the detected filament. 404 when no slicer. **No unknown-printer check** — an unresolvable serial degrades to empty presets/filaments, the same "unknown never blocks" convention as `printer_model`/`printer_nozzle`, not a 404 |
| `POST /api/printers/{serial}/slice` | Multipart STL/3mf/STEP + preset + filament + supports → 202 `{job_id}`. Sync `def`, same reason as the FTPS routes (§3.2): reading a large model off the wire must not stall the event loop. 400 on a non-model extension or an empty body, 404 on an unknown printer |
| `GET /api/slice/jobs?serial=` | Job list, newest first, for polling |
| `DELETE /api/slice/jobs/{job_id}` | Cancels a still-queued job, **or** clears a finished one from the list; 404 if neither applies |

All four 404 when `slicer=None`, the same "None means inert" convention as
`queue=None`/`detection=None` — a machine with no Bambu Studio install still
boots, still monitors, still prints files already on a card.
`find_slicer(env, candidates)` checks `BAMBU_STUDIO_EXE` first (ignoring it if
that path no longer exists, so a stale env var can't shadow a good default
install), then the default Windows install paths, and returns `None` rather
than raising — a supported outcome, not an error.

---

## 7. Frontend

React 19 + Vite 6, plain JSX, one global `styles.css` of design tokens, a
hand-rolled UI kit, and **no router** — see `FRONTEND-STACK-GUIDE.md`.

**Pages** (`src/app/pageRegistry.jsx` — add pages here and nowhere else; every
page gets the same `{printers, selected, onSelect}` props):

| Key | Page | What it does |
|---|---|---|
| `overview` | `Overview.jsx` | Printer grid (`PrinterCard`, with inline `EditPrinterForm`) + `AddPrinterForm` |
| `dashboard` | `Dashboard.jsx` | Stat tiles, `CameraCard`, `AutoStopCard`, `PrintInfoCard`, `HmsCard` |
| `detection` | `Detection.jsx` | Enable/disable, camera source, webcam index, conf slider, armed-class checkboxes, detector health, and the **draggable ROI editor** (`RoiEditor`) over the live view |
| `sdfiles` | `SdFiles.jsx` | FTPS microSD browser with breadcrumbs + upload |
| `slice` | `Slice.jsx` | STL upload, preset radio group, filament dropdown (prefilled from detection), tree-supports checkbox, polled job list (§6) |
| `queue` | `Queue.jsx` | Job table, reorder, remove, "Add from SD" picker, totals bar |

**Data layer.** `src/api/printer.js` is a set of plain `fetch` wrappers —
`addPrinter`, `updatePrinter`, `removePrinter`, `fetchFiles`, `fetchQueue`,
`addQueueJob`, `removeQueueJob`, `reorderQueue`, `uploadFile`, `fetchLatestFrame`,
`updateDetection`, `armDetection`, `fetchDetectionFrame`, `fetchSliceOptions`,
`startSlice`, `fetchSliceJobs`, `cancelSliceJob` (§6.6). Its `detail(res)`
helper flattens FastAPI's `{"detail": ...}` — which is a *list* of validation
objects for a 422 — so no caller can ever surface `[object Object]`. The two
frame fetchers return an object URL the caller must revoke, or `null`.

**How it updates.** Three different mechanisms, on purpose:

| Data | Mechanism | Cadence |
|---|---|---|
| Printer summaries + detection snapshots | WebSocket `/ws` (`usePrinters` hook) | Pushed on change, ≤4 Hz, heartbeat every 5 s |
| Camera frame (`CameraCard`) | Polling `fetchDetectionFrame` (live) or `fetchLatestFrame` | Every 5 s, matching the detector interval. The **last good frame stays on screen** until a new one arrives — a momentarily missing frame never blanks the view; the placeholder appears only before the first frame |
| Queue | Polling `fetchQueue` + refetch after every mutation | 4 s |
| SD listing | On navigation / Refresh only | Never polled — an FTPS handshake is not instant |
| Slice options | On navigation only, like SD listing | Fetched once per printer; describes what's available *right now* |
| Slice jobs | Polling `fetchSliceJobs`, plus an immediate refetch on submit | 2 s |

`usePrinters` reconnects with exponential backoff to 10 s, guards against
StrictMode double-mount teardown, and keeps the last-known-good list if a frame
fails to parse. `App.jsx` auto-selects the printer when there is exactly one and
repairs the selection when the selected printer disappears.

**The ROI editor.** `components/detection/RoiEditor.jsx` draws a draggable box
(8 handles + move) over the live frame; the four `%` inputs and the box are two
views of one value. The drag maths is pure and lives in `roiGeometry.js`
(`clampRoi`, `applyHandleDrag`) — the only frontend code with unit tests.

Two rectangles are visible at once, deliberately:

| Box | Means |
|---|---|
| burned into the JPEG by `detect.py` | what the detector is using **right now** |
| the draggable overlay | what you are about to apply |

They converge on Apply. Before this, the only feedback was the burned-in one,
which cannot change until the config saves, the supervisor respawns the
detector, and a new frame arrives — so you were aiming blind at the setting
that §4.1 says fails *silently* when wrong.

Two details that are easy to get wrong: deltas are measured from the box as it
was at drag **start** (accumulating per-move drifts once clamping engages, so
the box stops following the cursor back), and `pointermove`/`pointerup` are
bound to `window`, not the element, so a drag that leaves the image still ends.

`SdBrowser` and `QueuePanel` are both mounted with `key={printer.serial}` so
switching printers **remounts** rather than resetting via an Effect — see the
long comment in `SdFiles.jsx` for why the Effect version fires a wasted FTPS
handshake at the previous printer's path. Both also use a `requestId` ref so an
out-of-order response can never clobber a newer one.

---

## 8. Running everything

### Prerequisites (on the printer, do these first)

1. **Settings → LAN-only Mode → ON**, then power-cycle.
2. **Settings → Developer Mode → ON.** Both are required. Without Developer Mode
   MQTT connects but never delivers a report, and FTPS/camera are closed.
3. Note the 8-character **LAN access code** off the printer screen. It rotates
   on some firmware updates and is the most common cause of a connection that
   used to work.
4. Ports: MQTT **8883**, FTPS **990** (implicit TLS), camera **6000**.
5. For `capture.py`: in the slicer set **Others → Special mode → Timelapse:
   Smooth**, so the toolhead parks in the same place every layer.

Full details and firmware-version requirements: `CONNECTION.md`.

### Install

```bash
pip install -r requirements.txt
# GPU training needs a CUDA torch build:
#   pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124

cd frontend && npm install && npm run build && cd ..
```

### The server

```bash
python -m server                       # real printers, restored from printers.json
python -m server --mock                # three fake printers, in-memory stores
python -m server --port 8000 --runs-dir runs --printers-file printers.json
```

Then open <http://127.0.0.1:8000>. Printers are added **in the browser**
(Overview → Add printer) — there are no `--host/--serial/--access-code` flags.

`--mock` seeds three printers (running / stale / offline), uses `MemoryStore` +
`MemoryQueueStore` so nothing touches your real `printers.json`/`queues.json`,
writes to `runs-mock/`, and swaps `DetectorSupervisor` for `MockDetectorRunner`.
Anything you add through the UI under `--mock` still gets a **real**
`PrinterService`, which is how the "Unreachable" error path is exercised without
hardware.

Frontend dev loop:

```bash
cd frontend && npm run dev     # Vite on :5173, proxies /api and /ws to :8000
```

### The detector, standalone

```bash
# printer's built-in camera (access code from the environment, never argv)
export BAMBU_ACCESS_CODE=12345678      # PowerShell: $env:BAMBU_ACCESS_CODE="12345678"
python detect.py --source a1 --host 192.168.1.42 \
                 --weights runs/train/failure_detector/weights/best.pt \
                 --out runs/_detect --interval 5

# USB webcam
python detect.py --source webcam --camera 0 --out runs/_detect

# no camera, no weights
python detect.py --mock --out runs/_detect
```

Normally you do **not** run this by hand — the server spawns and supervises it
whenever a printer is marked `capture` with `detect_enabled`.

### Capture, registration, probing

```bash
python capture.py --host 192.168.1.42 --serial 0309xxxxxxxx \
                  --access-code 12345678 --camera 0 --out runs/
python capture.py --mock --out runs/

python check_registration.py runs/<run_A> runs/<run_B>
python probe_gcode.py --host ... --serial ... --access-code ...   # during a print
```

### Training

```bash
python train_failure_detector.py            # ~2-4 h on an 8 GB GPU
python simulate_webcam_resolutions.py
python eval_webcam_resolutions.py
python run_camera_detection.py              # windowed live demo
```

### Domain adaptation to the A1 camera (§12)

Stop the server first — these compete for the camera. Run the split **before**
synthesising anything; that ordering is the whole point.

```bash
# 1. collect. Failures by hand, backgrounds unattended.
python collect_dataset.py --host 192.168.1.42          # c/s to label, space to shoot
python collect_backgrounds.py --host 192.168.1.42      # writes preflight.jpg, then exits
python collect_backgrounds.py --host 192.168.1.42 --confirmed-clear

# 2. split FIRST -> datasets/a1_train, datasets/a1_test
python split_source.py

# 3. synthesise from the train half only
python synth_dataset.py   --src datasets/a1_train --out datasets/synth_ho

# 4. fine-tune from the public-data checkpoint
yolo detect train model=runs/train/failure_detector/weights/best.pt \
     data=datasets/synth_ho/data.yaml epochs=60 imgsz=640 workers=0 name=a1_holdout

# 5. evaluate on the test half only
python build_real_eval.py --src datasets/a1_test --out datasets/real_ho
python eval_real.py --models runs/train/failure_detector/weights/best.pt \
                             runs/detect/runs/train/a1_holdout/weights/best.pt \
                    --eval datasets/real_ho
```

Note the defaults on `build_real_eval.py` (`--out datasets/real_eval`) and
`eval_real.py` (`--eval datasets/real_eval`) point at the **contaminated**
first-run directories, which are kept as the record of §12.2. Pass the `_ho`
paths explicitly; the bare `python build_real_eval.py` rebuilds the wrong thing.

### Tests

```bash
python -m pytest                        # from the repo root
python -m pytest -q server/tests/test_detection.py
cd frontend && npm test                 # ROI drag maths (vitest)
```

### The desktop app (`desktop/`) — for people who won't run a terminal

Everything above assumes a checkout, a Python env, and a command line. `desktop/`
packages the same server into an **installable app**: an Electron shell that
spawns the backend — frozen by PyInstaller, so no Python on the target machine —
and opens a window on it. Build recipes: `desktop/README.md` (Windows) and
`desktop/LINUX-BUILD.md` (Mint/AppImage); design:
`docs/superpowers/specs/2026-07-22-electron-desktop-packaging-design.md`.

```bash
powershell -ExecutionPolicy Bypass -File desktop\build-windows.ps1   # -> .exe
bash desktop/build-linux.sh                                          # -> .AppImage (needs Docker)
```

Three things about it are load-bearing and easy to get wrong if you touch it:

- **It is a narrower app than `python -m server`.** `desktop/launcher.py` passes
  `detection=None`, so the detector never spawns and torch/ultralytics are
  excluded from the bundle (`requirements-desktop.txt` also swaps
  `opencv-python` → `-headless`, which drops the `libGL.so.1` dependency that
  breaks a plain build inside an AppImage). Scope is the dashboard, the queue,
  and SD upload. Slicing still auto-enables *if* Bambu Studio is installed.
- **The port is chosen per launch, never 8000.** `main.js::getFreePort()` binds
  `:0` and hands the number to the backend via `BAMBU_PORT`. This is not
  fussiness: with a fixed 8000 the readiness poll happily succeeds against a
  *dev server already on 8000* and Electron then shows you the wrong backend —
  observed on the dev box, which is why the fixed port was removed.
- **State goes to a per-user directory, never beside the executable**
  (`%APPDATA%\BambuMonitor`, `~/.config/BambuMonitor`), because an AppImage is
  mounted read-only. `launcher.py::data_dir()` resolves it; Electron passes it
  as `BAMBU_DATA_DIR`.

Verified 2026-07-22 on Windows: the frozen backend serves standalone, and the
packaged app spawns it on a free port and reaps it on quit. **The AppImage has
never been built or run** — the dev box has no Docker (§11).

---

## 9. Data and file layout

```
GUI_UCDavis/
├─ printers.json                 registered printers (GITIGNORED — plaintext access codes)
├─ queues.json                   {serial: [job,...]} (gitignored; user data, no secrets)
├─ runs/                         (gitignored)
│  ├─ _detect/
│  │  ├─ status.json             detect.py → server: ts, fps, camera, conf, detections, error
│  │  └─ latest.jpg              annotated frame, JPEG q85
│  ├─ _slice/                    per-job temp dirs, deleted on every exit path (§6.5)
│  │  └─ <job_id>/                machine.json, process.json, filament.json, sliced.gcode.3mf
│  ├─ train/failure_detector/weights/best.pt      public-data detector ("best.pt")
│  ├─ eval/                      per-tier eval plots + confusion matrices
│  ├─ detect/runs/train/
│  │  ├─ a1_synth/               fine-tune on datasets/synth — CIRCULAR EVAL, do not quote
│  │  └─ a1_holdout/weights/best.pt   fine-tune on the disjoint split — the valid one
│  └─ 20260715T143200_Benchy/    one capture.py run
│     ├─ meta.json               started, camera, settle_s, burst, gcode_file,
│     │                          subtask_name, total_layer_num
│     ├─ telemetry.jsonl         one {"t": iso, "patch": {...}} per MQTT report
│     ├─ frames.csv              layer, iso_time, unix_time, path, sharpness,
│     │                          gcode_state, nozzle_temper, bed_temper
│     └─ frames/layer_0001.jpg …
├─ runs-mock/                    same shape, written by --mock
├─ datasets/                     (gitignored) the A1-camera domain-adaptation data
│  ├─ a1_camera/                 all collected source frames + manifest.csv
│  ├─ a1_train/ a1_test/         split_source.py's disjoint block split
│  ├─ synth/     real_eval/      built from the UNSPLIT frames — contaminated, kept
│  │                             only as the record of the invalid first run
│  └─ synth_ho/  real_ho/        built from a1_train / a1_test — the valid pair
├─ server/  frontend/  docs/
├─ 3d-printing-failure-detection.v1i.yolov8/     Roboflow dataset (gitignored)
│  └─ webcam_sim/res_{480,320,160}/              degraded copies
└─ yolov8s.pt, yolo26n.pt        pretrained base checkpoints (gitignored, *.pt)
```

`runs/_detect` is a *sibling* of the capture run directories under the same
`--runs-dir`, and its name starts with `_` so it can never be mistaken for a run
(`server/runs.py` only considers directories containing a `frames/` subdir).
`runs/_slice` is a sibling with the same load-bearing underscore prefix, one
directory per slice job, always removed by the coordinator when the job
finishes or is cancelled (§6.5) — nothing here is meant to outlive its job.

---

## 10. Testing

```bash
python -m pytest              # server + root modules, from the repo root
cd frontend && npm test       # ROI drag maths (vitest)
```

Neither suite uses a socket, a camera, or a printer.

**Per-test counts are deliberately not written down here.** They were restated
in two documents and went stale three times in a single afternoon's work; the
commands above are the only honest source. What each file covers is stable and
worth knowing:

| File | Covers |
|---|---|
| `test_api.py` | Every route via FastAPI's `TestClient`, against a fake registry |
| `test_registry.py` | Add/remove/update/reconnect, capture invariant, persistence, locking, model id |
| `test_store.py` | `PrinterConfig` validation, tolerant load, atomic save, model-id guessing and mismatch |
| `test_detection.py` | `StatusReader`, the `AutoStopController` state machine, `DetectorSupervisor` argv/env, coordinator ticks |
| `test_sdcard.py` | Path guard, MLSD/LIST parsers, sorting, `list_dir`/`upload_file` control flow with a fake FTP class |
| `test_detect.py` | `detect.py`'s pure parts: status building, detection mapping, ROI/offset math, atomic writes, the loop |
| `test_services.py` | `PrinterService` / `MockPrinter`, and the two service factories |
| `test_summary.py` | `build_summary` — including that it can never contain the access code |
| `test_bambu_link.py` | `deep_merge`, `decode_hms`, `build_project_file_command` |
| `test_threemf.py` | Slice-info parsing, `printer_model_id`, tolerance of garbage |
| `test_runs.py` | Active-run discovery and newest-frame selection |
| `test_queue.py` | `PrintQueue` mutation, ordering, totals |
| `test_slicer.py` | Profile flattening + cycle detection, `ProfileIndex`, `find_slicer`, `build_argv`, `run_slice` against an injected fake subprocess |
| `test_slicepresets.py` | Tier resolution (the `A1`/`A1M` token split, the anchored regex, the decoy-name trap), filament detection off a fake MQTT state |
| `test_slicejobs.py` | The full `SliceCoordinator` state machine against a fake registry/queue and an injected fake `run_slice` — success chains to upload+queue, each failure step latches and leaves the queue untouched, the finished-job cap |
| `test_docs.py` | The documentation itself — see below |

**Frontend** (`vitest`, added 2026-07-21): `roiGeometry.test.js` covers the ROI
drag maths. That module is pure on purpose so it *can* be tested; the React
components around it are verified by build and by eye, which is a real gap
rather than an oversight. The tests earned their keep immediately by catching
`clampRoi` corrupting a valid box through floating-point error
(`0.32 → 0.31999999999999995` on every call).

**The docs are tested too** (`test_docs.py`). Every rule in it exists because
the failure happened here:

| Guard | Why |
|---|---|
| every relative markdown link resolves | a doc linked `LoDISA-GUI/COLORS.md`, which lives in a *different repository* — the link had never once worked |
| every `§N` in master.md matches a real heading | renumbering a section silently orphaned four references, including ones in `server/*.py` comments |
| no hardcoded suite sizes in the maintained docs | they went stale five times in one day (261 → 270 → 299 → 316 → 358 → 362), restated in two files |
| every `docs/superpowers/` file declares a STATUS | a spec for a PyQt6 GUI that was **never built** sat there marked "Approved by user", which is exactly what someone picks up and starts implementing |

Each guard was verified by deliberately breaking the thing it protects and
watching it fail — a doc test that has never been seen to fail is decoration.

**What is not covered anywhere:** every React component, the FTPS `STOR`
against real hardware (the note below), and the dataset scripts (§3.1).

The dataset/training scripts (§3.1) are **not** unit-tested — they are one-shot
research tools whose output is checked by looking at it, and their real
correctness gate is the disjoint-split evaluation in §12.

The design that makes this possible: everything that touches hardware is behind
an injectable seam — `service_factory` in the registry, `spawn`/`clock` in
`DetectorSupervisor`, `connect` in `BambuCameraSource`, `grab`/`infer` in
`detection_loop`, `clock` in the controller and `StatusReader`. Parsers and path
guards are pure functions.

**Not covered:** the FTPS **STOR** against real hardware. `sdcard.upload_file`'s
control flow is fully tested against a fake FTP object and the whole route was
exercised end to end under `--mock`, but as of 2026-07-21 no byte has been
written to a real card — the printer was mid-print and a write to the
filesystem it streams gcode from was not worth the risk. The *read* half
(login, implicit TLS, session reuse, LIST) **is** verified live, mid-print, on
2026-07-21, and STOR reuses exactly that connection machinery. Verify it before
relying on it.

**Also not covered: a CLI-sliced `.gcode.3mf` actually starting and printing on
real hardware (§6).** What *is* verified: the CLI produces a `.gcode.3mf`
whose `Metadata/plate_1.gcode` and `slice_info.config` this repo's own
`threemf.parse_slice_info` reads correctly, and the whole pipeline — slice →
upload → queue, with the right provenance `model_id` — runs end to end over
HTTP against a mock printer. What is **not** verified: that the printer
accepts and prints the result. This is the first thing that makes STOR (above)
load-bearing rather than theoretical — a slice job's upload step is the first
real caller of it. Nobody has started one; the printer was in active use near
people while this was built, and running the hardware gate would have moved
it. Per §1.1, treat this as unverified until someone runs it and updates that
table.

---

## 11. Gotchas and design decisions

- **One process per camera (Windows).** This is why `detect.py` is a separate
  process, why the server serves *files* rather than opening a device, and why
  only one printer may be `capture: true`. Running `detect.py`,
  `run_camera_detection.py`, and `capture.py` against the same USB device at
  once will fail — pick one.
- **A dropped frame is not a dead camera.** This one bit us. Every layer of the
  camera path must distinguish *transient* from *fatal*, because the recovery for
  "fatal" is to exit the process, and the supervisor's response to a dead process
  is to respawn it — which reopens the device. Treat one bad read as fatal and
  you get a camera that disconnects and reconnects forever, at the respawn
  backoff period. Three separate places have to cooperate:
  `WebcamSource.grab()` retries before reopening, `detection_loop` tolerates
  `MAX_READ_FAILURES - 1` consecutive misses, and `DetectorSupervisor._stop_proc`
  waits for the old process to release the device. If you add a new frame source,
  give it the same contract: **`None` means gone, not "not right now."**
- **Atomic writes + Windows/OneDrive retries.** Every JSON/JPEG the server or
  detector writes goes through temp-file + `os.replace` + `fsync`. On Windows
  `os.replace` raises `PermissionError` when the destination is momentarily open
  by a reader or held by OneDrive sync, so `detect.py` retries the rename 5×
  at 50 ms. Readers are correspondingly tolerant: `StatusReader` treats a
  `UnicodeDecodeError` (a read that caught a torn multi-byte character) as
  "detector down", and `runs.py` skips files that vanish or lock mid-scan.
  Keeping a repo like this inside a OneDrive-synced folder is why all of that
  exists.
- **The access code never appears in argv.** `DetectorSupervisor.build_env()`
  passes it through the child's environment. `build_summary()` has no
  `access_code` parameter at all. `SdError` messages must never interpolate it.
  `EditPrinter.access_code` defaults to `""` meaning "keep the current one",
  precisely because the client never receives the real code back and has nothing
  to round-trip.
- **Settle delay + burst/sharpness pick in `capture.py`.** With Smooth timelapse
  the toolhead parks in the same place every layer, but MQTT tells you the layer
  changed — not that the park finished. `--settle` (1.5 s) is that fudge. And
  because the A1 is a bed-slinger with a still-settling bed, one unlucky frame is
  a smear: `--burst 3` plus a Laplacian-variance pick costs ~150 ms and removes
  most of it. Tune settle once by eyeballing the first run's frames.
- **Not the built-in camera for capture.** ~0.5 fps, toolhead-mounted, fixed
  focus at 15 cm — the right viewpoint for CAXTON-style nozzle monitoring and the
  wrong frame rate for it, and the wrong viewpoint for everything else. Use a
  fixed-mount USB webcam. (The built-in camera *is* supported as a detection
  source, where a 5-second interval makes its frame rate a non-issue.)
- **The printer sends partial MQTT updates.** Not deep-merging is, per
  `bambu_link.py`'s own docstring, the single most common integration bug —
  `layer_num` appears to vanish every other message.
- **There is no ack for any command.** `stop_print()` and `send_gcode()` always
  "succeed" at the publish layer. This is exactly why the controller has a
  `stopping` state that watches `gcode_state` and re-sends once.
- **A stale detector must never cause a stop.** Hence `status["running"]`
  gating, hence dropping controllers when the capture printer changes.
- **Arm is runtime-only.** It lives in the controller, not in `PrinterConfig`,
  and is deliberately not persisted — a server restart must not silently
  re-arm an auto-stop.
- **`server/queue.py` shadows nothing.** Python 3 imports are absolute, so
  `import queue` elsewhere still gets the stdlib.
- **Implicit vs explicit FTPS, and TLS session reuse.** Both are documented
  traps in `server/sdcard.py`; a login that succeeds and then hangs on LIST is
  the session-reuse one.
- **A corrupt config file must never stop the boot.** Both `PrinterStore` and
  `QueueStore` degrade to empty with a warning — if the server won't start you
  have no UI left to fix it with.
- **`--workers 0` for training on Windows.** Each spawned dataloader worker
  reloads the CUDA DLLs and can exhaust the page file (`WinError 1455`). Raise it
  only after enlarging the page file.
- **The detector is a prototype, and auto-stop is not validated.** The shipped
  public-data model is blind on the A1's built-in camera, and the fine-tuned
  replacement is measured on 9 positives from a single physical tangle in a
  single room. §12 is the whole story; do not arm auto-stop on the strength of
  the headline numbers. `README.md`'s exit criterion — print-level FPR < 1% over
  ≥30 successful prints, time-to-detection < 5 min over ≥20 induced failures —
  has not been met.
- **Reconnect is not "add retry".** `PrinterService` already retries every
  `RETRY_S` (10 s) until it connects, and paho self-heals after the first
  connect. The button exists for the two things that path can't do: try *now*
  instead of waiting out the window, and rebuild a wedged client. It cannot
  help when the IP changed — that needs Edit, which rebuilds on a host change.
  It sends no command, so it can never disturb a running print.
- **Split the data before you derive anything from it.** §12.2. An md5 check
  will not catch a leak, and a random split is not a split when consecutive
  frames are near-duplicates.
- **The engine is Bambu Studio, not OrcaSlicer.** §6.1. Measured, not a
  preference: OrcaSlicer's `--export-3mf` never produced a file across five
  argument orderings, and a raw `.gcode` can't be started over MQTT anyway
  (§5.4). Do not "simplify" `slicer.py` back to Orca — it looks like the more
  open choice and it is the one that doesn't work.
- **Preset names are not stable strings.** §6.3. The model token differs
  between machine profiles (`A1 mini`) and process/filament profiles (`A1M`);
  the nozzle suffix is omitted at 0.4 and present otherwise; layer height for
  the same tier word varies by nozzle. A preset must be resolved against the
  live profile index with a fully anchored regex and its label read back off
  whatever matched — never hardcoded, and never matched with independent
  `startswith`/`endswith` checks, which a decoy profile name can fool.

---

## 12. The camera-angle domain gap (measured 2026-07-19, first fix measured 2026-07-21)

The most important open problem, and the thing that governs the next phase of
work. **The public-data detector does not work on the A1's built-in camera.**
A first domain-adaptation pass (§12.3) fixes that measurably but is **not yet
deployable for auto-stop** — read §12.4 before quoting any of it.

### 12.1 The gap, measured

The public dataset it was trained on is shot at roughly 30–70° looking down at
the print. The A1 mini's built-in camera is a wide fisheye mounted low and
near-horizontal: the bed occupies the lower-left of the frame, the print is
foreshortened into a thin band, and the rest of the view is the room.

Measured against a real frame that happened to contain a genuine spaghetti
failure on the bed:

| Input | Detections |
|---|---|
| Full frame, conf ≥ 0.25 | **5 false positives**, all on a laptop keyboard (x 6–11%, y 48–53%); none on the print |
| Bed ROI, conf ≥ 0.25 | **0** |
| Cropped straight onto the tangle, conf ≥ 0.03 | `spaghetti: 0.08` |

Two separate conclusions, and they are easy to conflate:

1. **ROI cropping fixes the false positives.** 5 → 0. That is what §4.1's `--roi`
   is for, and it works.
2. **It does not fix detection.** On a large, blatant, unambiguous failure the
   model's best confidence is **0.08**, against a 0.25 threshold and the ~0.9 it
   scores in its own domain (`FAILURE_DETECTOR_REPORT.md`: mAP50 0.835). At this
   angle it is effectively blind, and no amount of thresholding or cropping
   changes that.

**Measured end to end (2026-07-19).** `build_real_eval.py` builds a val set of
29 real spaghetti frames (boxes derived by background differencing) plus 49 real
clean frames as negatives. The shipped public-data model scores:

| | mAP50 | mAP50-95 |
|---|---|---|
| public test split (`FAILURE_DETECTOR_REPORT.md`) | 0.835 | 0.490 |
| **real A1 frames** | **0.0016** | **0.0003** |

Not weak on this camera -- blind.

### 12.2 The eval that was circular (worth internalising)

The first fine-tune scored **mAP50 0.7123, 100% recall, 0% false alarms** — and
the number was meaningless. All 49 clean frames used as eval negatives were
*also* training negatives, and all 29 eval positives had their tangle cut out and
pasted into ~600 training composites. The model was being asked about pixels it
had memorised.

Two things made this easy to miss, and both generalise:

- **An md5 check said the files were distinct.** They were — byte-wise — because
  the two writers use JPEG q92 and q95. Identity of *content* is the question;
  hashing the encoded bytes does not answer it.
- **Random splitting would not have saved it either.** Frames were captured
  seconds apart, so neighbours are near-duplicates; a random split puts a frame
  in train and its twin in test. `split_source.py` therefore splits in **blocks
  of consecutive frames**, and splits the *source* frames before anything is
  derived from them.

Cost of honesty: 10 usable cutouts instead of 31, and 9 test positives instead of
29. The re-run on the disjoint split is the only number worth quoting.

### 12.3 What the fix achieved (disjoint split, 2026-07-21)

Training: 517 composites from 10 cutouts and 32 clean backgrounds (train half).
Evaluation: 9 real spaghetti frames + 17 real clean frames (test half), disjoint.

| Model | mAP50 | mAP50-95 | recall @0.25 | false alarm @0.25 |
|---|---|---|---|---|
| `best.pt` (public data) | 0.0000 | 0.0000 | 77.8% | 58.8% |
| fine-tuned on synthetic | **0.4539** | 0.1772 | **100%** | 11.8% |

The baseline's 77.8% "recall" is an artifact of firing on most frames — which is
also why its mAP is 0: nothing is localised. Read the two columns together or
they lie.

Both "false alarms" turned out to be **label errors**: inspection shows visible
debris on the plate, captured seconds after the operator changed the label while
the plate was still being cleared. The model was right and the labels were wrong,
so 11.8% is an *upper* bound. Root cause is fixed in `collect_dataset.py` (8 s
hold-off after a label change), which is the useful half of the finding — a
metric that surprises you is often a data-collection bug.

Full method and numbers: `FAILURE_DETECTOR_REPORT.md` §8.

### 12.4 What this does NOT establish

Read this before the checkpoint gets used for anything.

- **One physical tangle.** Every failure image in the dataset is the same object.
  This measures "can it find *this* tangle in frames and positions it has not
  seen", not "can it find spaghetti". Different prints fail differently.
- **Tiny evaluation.** 9 positives, 17 negatives. The confidence intervals are
  wide enough to drive through.
- **Scene-specific, and the scene has since changed.** Copy-paste bakes in the
  backgrounds it pastes onto, and all 49 came from **the A1 mini, in a different
  room**. A second machine elsewhere on the LAN measured a scene difference of
  72, where same-scene frames differ by 1–3. Since 2026-07-21 the hardware *is*
  that other machine (§1.1), and its camera geometry is close to inverted
  (bed low, not high; 1536x1080, not 1680x1080). **This checkpoint should be
  assumed not to transfer to the current A1 until re-measured.** The *cutouts*
  transfer; the backgrounds do not. Re-running `collect_backgrounds.py` on the
  A1 and then `synth_dataset.py` with the existing cutouts is the cheap path —
  no new failures need to be induced.
- **Not deployable for auto-stop.** Even at 11.8%, three consecutive qualifying
  frames are needed to fire, which works out to roughly one spurious stop per
  hour of printing. That has to be verified as near-zero on genuinely clean data
  before anyone arms it.
- **Perspective warping was never done.** It was step 3 of the original plan and
  remains untried; it teaches shape distortion but cannot invent the occlusion a
  truly horizontal view produces. If you do it, remember labels must be warped
  through the same homography and re-fitted, **not** copied (see §3.1).

`README.md`'s exit criterion — print-level FPR < 1% over ≥30 successful prints,
time-to-detection < 5 min over ≥20 induced failures — remains unmet.

### 12.5 The alternative that sidesteps all of this

A USB webcam at 30–70° puts the model back in its training domain for near-zero
effort — `camera_source: webcam` is already built and tested, and the resolution
study says a cheap sensor is fine. Synthetic data is the right answer only when
the built-in camera is a hard constraint.
