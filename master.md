# master.md — bambu-monitor, end to end

The umbrella document for this repo. Read this first; it links out to the
existing specialist docs rather than restating them.

| Doc | What it covers |
|---|---|
| `README.md` | Quick start, the data-collection scripts (`capture.py`, `check_registration.py`, `probe_gcode.py`), and the research framing + exit criterion |
| `CONNECTION.md` | Verified LAN/MQTT connection parameters for the A1 mini, TLS specifics, prerequisites |
| `FAILURE_DETECTOR_REPORT.md` | YOLO training run, test metrics, webcam-resolution robustness study |
| `FRONTEND-STACK-GUIDE.md` | The React/Vite/token conventions the frontend follows (from the VERA project) |
| `docs/superpowers/specs/`, `docs/superpowers/plans/` | Per-feature design specs and implementation plans |

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
filament totals parsed out of `.gcode.3mf` files, and (separately) logs
layer-indexed frames + telemetry for building training datasets.

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
or the other against a given device — see §10.

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
- `send_gcode(line)` / `stop_print()` — fire-and-forget. **There is no ack.**
  `stop_print()` sends the Bambu print command `{"print": {"command": "stop"}}`,
  not G-code. Its docstring notes it is still unverified against real A1-mini
  hardware.
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

Exists because of the domain gap in §11: the shipped model is effectively blind
on this camera, and closing that needs images from *this* camera. Competes for
the camera with `detect.py` — stop the server before collecting.

### 3.2 `server/` package

There is **no `server/summary.py`** — `build_summary()` lives in
`server/printer.py` (`server/tests/test_summary.py` tests it there).

| Module | Owns | Key names |
|---|---|---|
| `store.py` | `printers.json` persistence + the `PrinterConfig` dataclass | `PrinterConfig`, `PrinterStore`, `MemoryStore`, `DETECTION_CLASSES`, `CAMERA_SOURCES` |
| `printer.py` | One live printer's state | `PrinterService`, `MockPrinter`, `build_summary`, `SUMMARY_FIELDS`, `STALE_S` |
| `registry.py` | The set of printers, keyed by serial | `PrinterRegistry`, `DuplicateSerial` |
| `main.py` | The FastAPI app + all routes | `create_app`, `AddPrinter`, `EditPrinter`, `DetectionUpdate`, `ArmBody`, `AddQueueJob`, `ReorderQueueJobs` |
| `detection.py` | Reading detector status, deciding, actuating | `StatusReader`, `AutoStopController`, `DetectorSupervisor`, `DetectionCoordinator`, `MockDetectorRunner` |
| `queue.py` | Per-printer job list + `queues.json` | `PrintQueue`, `QueueStore`, `MemoryQueueStore` |
| `sdcard.py` | Read-only microSD over FTPS | `list_dir`, `fetch_file`, `normalize_path`, `ImplicitFTP_TLS`, `SdError`, `parse_mlsd`, `parse_list_lines` |
| `threemf.py` | Parsing a sliced `.gcode.3mf` | `parse_slice_info`, `SLICE_INFO_PATH` |
| `runs.py` | Finding the newest captured frame | `find_active_run`, `newest_frame`, `ACTIVE_WINDOW_S` |
| `__main__.py` | CLI entry, wiring, `--mock` seeding | `main`, `real_factory`, `mock_factory`, `MOCK_SEED` |

**`store.py`.** `PrinterConfig` fields: `serial`, `host`, `access_code`, `name`
(falls back to host), `capture`, `camera_source` (`"a1"`/`"webcam"`),
`camera_index`, `conf`, `armed_classes`, `detect_enabled`. `from_dict()` does
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
appears in a queue route's signature.

**`main.py` routes.**

| Method + path | Notes |
|---|---|
| `GET /api/printers` | Summaries, each with a `detection` object (null unless capture printer) |
| `POST /api/printers` | 201; 409 on duplicate serial, 400 on bad fields |
| `PUT /api/printers/{serial}` | Edit host/name/capture; blank `access_code` = keep current. Serial is not editable |
| `DELETE /api/printers/{serial}` | 204 |
| `GET /api/printers/{serial}/files?path=/` | FTPS listing. 400 on bad path, 502 on printer failure |
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
prefers MLSD and falls back to LIST only on a 500/502. Both `list_dir` and
`fetch_file` **always** raise `SdError`, whose message never contains the access
code.

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
seconds indefinitely. See §10 for the two other halves of that fix
(`WebcamSource` recovery and reaping the old process before respawning).

**ROI cropping.** `--roi x,y,w,h` (fractions of the frame) restricts inference to
the bed. On the A1's wide, low view most of the frame is the room, and the model
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
route (§5.3) commands the printer. `POST .../queue` with
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

### 5.3 Starting a print

`POST /api/printers/{serial}/queue/{job_id}/start` sends the queue head to the
printer. No upload: jobs already reference microSD files, so this is one MQTT
`project_file` command via `BambuLink.start_print`.

**The URL scheme is `file:///sdcard/<filename>`, verified on a real A1 mini.**
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

## 6. Frontend

React 19 + Vite 6, plain JSX, one global `styles.css` of design tokens, a
hand-rolled UI kit, and **no router** — see `FRONTEND-STACK-GUIDE.md`.

**Pages** (`src/app/pageRegistry.jsx` — add pages here and nowhere else; every
page gets the same `{printers, selected, onSelect}` props):

| Key | Page | What it does |
|---|---|---|
| `overview` | `Overview.jsx` | Printer grid (`PrinterCard`, with inline `EditPrinterForm`) + `AddPrinterForm` |
| `dashboard` | `Dashboard.jsx` | Stat tiles, `CameraCard`, `AutoStopCard`, `PrintInfoCard`, `HmsCard` |
| `detection` | `Detection.jsx` | Enable/disable, camera source, webcam index, conf slider, armed-class checkboxes, detector health + live view |
| `sdfiles` | `SdFiles.jsx` | FTPS microSD browser with breadcrumbs |
| `queue` | `Queue.jsx` | Job table, reorder, remove, "Add from SD" picker, totals bar |

**Data layer.** `src/api/printer.js` is a set of plain `fetch` wrappers —
`addPrinter`, `updatePrinter`, `removePrinter`, `fetchFiles`, `fetchQueue`,
`addQueueJob`, `removeQueueJob`, `reorderQueue`, `fetchLatestFrame`,
`updateDetection`, `armDetection`, `fetchDetectionFrame`. Its `detail(res)`
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

`usePrinters` reconnects with exponential backoff to 10 s, guards against
StrictMode double-mount teardown, and keeps the last-known-good list if a frame
fails to parse. `App.jsx` auto-selects the printer when there is exactly one and
repairs the selection when the selected printer disappears.

`SdBrowser` and `QueuePanel` are both mounted with `key={printer.serial}` so
switching printers **remounts** rather than resetting via an Effect — see the
long comment in `SdFiles.jsx` for why the Effect version fires a wasted FTPS
handshake at the previous printer's path. Both also use a `requestId` ref so an
out-of-order response can never clobber a newer one.

---

## 7. Running everything

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

### Tests

```bash
python -m pytest              # from the repo root
python -m pytest -q server/tests/test_detection.py
```

---

## 8. Data and file layout

```
GUI_UCDavis/
├─ printers.json                 registered printers (GITIGNORED — plaintext access codes)
├─ queues.json                   {serial: [job,...]} (gitignored; user data, no secrets)
├─ runs/                         (gitignored)
│  ├─ _detect/
│  │  ├─ status.json             detect.py → server: ts, fps, camera, conf, detections, error
│  │  └─ latest.jpg              annotated frame, JPEG q85
│  ├─ train/failure_detector/weights/best.pt      trained detector
│  ├─ eval/                      per-tier eval plots + confusion matrices
│  └─ 20260715T143200_Benchy/    one capture.py run
│     ├─ meta.json               started, camera, settle_s, burst, gcode_file,
│     │                          subtask_name, total_layer_num
│     ├─ telemetry.jsonl         one {"t": iso, "patch": {...}} per MQTT report
│     ├─ frames.csv              layer, iso_time, unix_time, path, sharpness,
│     │                          gcode_state, nozzle_temper, bed_temper
│     └─ frames/layer_0001.jpg …
├─ runs-mock/                    same shape, written by --mock
├─ server/  frontend/  docs/
├─ 3d-printing-failure-detection.v1i.yolov8/     Roboflow dataset (gitignored)
│  └─ webcam_sim/res_{480,320,160}/              degraded copies
└─ yolov8s.pt, yolo26n.pt        pretrained base checkpoints (gitignored, *.pt)
```

`runs/_detect` is a *sibling* of the capture run directories under the same
`--runs-dir`, and its name starts with `_` so it can never be mistaken for a run
(`server/runs.py` only considers directories containing a `frames/` subdir).

---

## 9. Testing

**261 tests**, all under `server/tests/`, run with plain `pytest` from the repo
root. They use no sockets, no camera, and no printer.

| File | Tests | Focus |
|---|---|---|
| `test_api.py` | 55 | Every route via FastAPI's `TestClient`, against a fake registry |
| `test_registry.py` | 41 | Add/remove/update, capture invariant, persistence, locking |
| `test_detection.py` | 34 | `StatusReader`, the `AutoStopController` state machine, `DetectorSupervisor` argv/env, coordinator ticks |
| `test_store.py` | 31 | `PrinterConfig` validation, tolerant load, atomic save |
| `test_services.py` | 28 | `PrinterService` / `MockPrinter` |
| `test_sdcard.py` | 28 | Path guard, MLSD/LIST parsers, sorting, `list_dir` control flow with a fake FTP class |
| `test_detect.py` | 22 | `detect.py`'s pure parts: status building, detection mapping, atomic writes, the loop |
| `test_summary.py` | 10 | `build_summary` — including that it can never contain the access code |
| `test_runs.py` | 7 | Active-run discovery and newest-frame selection |
| `test_threemf.py` | 5 | Slice-info parsing and its tolerance of garbage |
| `test_queue.py` | 4 | `PrintQueue` mutation, ordering, totals |
| `test_bambu_link.py` | 2 | `deep_merge`, `decode_hms` |

The design that makes this possible: everything that touches hardware is behind
an injectable seam — `service_factory` in the registry, `spawn`/`clock` in
`DetectorSupervisor`, `connect` in `BambuCameraSource`, `grab`/`infer` in
`detection_loop`, `clock` in the controller and `StatusReader`. Parsers and path
guards are pure functions.

**Not covered:** the destructive `stop_print()` command against real A1-mini
hardware. `BambuLink.stop_print`'s docstring says so explicitly.

---

## 10. Gotchas and design decisions

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
- **The detector is a prototype.** `FAILURE_DETECTOR_REPORT.md` is explicit:
  trained on a public Roboflow dataset, not validated against this printer's
  camera angle, lighting, or mount. `README.md`'s exit criterion — print-level
  FPR < 1% over ≥30 successful prints, time-to-detection < 5 min over ≥20 induced
  failures — has not been met yet.

---

## 11. The camera-angle domain gap (measured, 2026-07-19)

The most important open problem, and the thing that governs the next phase of
work. **The shipped detector does not work on the A1's built-in camera.**

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

Closing it needs training data from this camera. The plan:

1. **Collect real frames** — `collect_dataset.py`, failure repositioned between
   shots so the model learns the failure and not the corner it sat in.
   **Collect clean frames too:** a set of only failures teaches "every print is
   a failure" and yields 100% false positives.
2. **Copy-paste augmentation** — paste failure crops onto real A1 backgrounds,
   labels free from the paste location. Best domain match per unit of effort,
   because it fixes the background and lighting gap directly.
3. **Perspective warping** — supplementary. It teaches shape distortion but
   cannot invent the occlusion a truly horizontal view produces. Remember labels
   must be warped, not copied (see §3.1).
4. **Fine-tune** from `best.pt` and evaluate on **held-out real A1 frames**. The
   public test split will keep flattering the model; it is not the eval that
   matters any more.

A caution on scale: a handful of real failures is not enough to fine-tune on,
and copy-paste over only a few backgrounds overfits to those backgrounds. Step 1
carries more weight than it looks.

**The alternative that sidesteps all of this** is a USB webcam at 30–70°, which
puts the model back in its training domain for near-zero effort —
`camera_source: webcam` is already built and tested, and the resolution study
says a cheap sensor is fine. Synthetic data is the right answer only when the
built-in camera is a hard constraint.
