# Basic GUI (PyQt6) — Design

**Date:** 2026-07-16
**Status:** Approved by user (conversation, 2026-07-16)

## Purpose

A standalone desktop GUI for the Bambu A1 mini rig, built with PyQt6. It shows
the same live printer state as the browser dashboard
(`2026-07-16-bambu-dashboard-design.md`) — connection, state, layer, progress,
temps, HMS, newest captured frame — and adds live parameter controls
(M104 / M220 / M221) with telemetry-based verification.

It is a parallel alternative to the web stack, not a client of it. It runs with
no FastAPI server, no npm build, and no network beyond MQTT to the printer:

```bash
python -m basic_gui --host 192.168.1.42 --serial 0309xxxxxxxx --access-code 12345678
python -m basic_gui --mock          # no hardware, no server
```

Out of scope: browsing past runs, registration visualization, classifier
output, auth, theming.

## Decisions already made

| Question | Decision | Why |
|---|---|---|
| Data source | Own `BambuLink` connection, direct MQTT | Standalone. Does not couple to `server/`, which another session is actively editing |
| Reuse `server/printer.py` / `server/runs.py`? | No — small local copies | Coupling to in-flight files was the thing standalone buys us. Frame lookup is ~40 lines; that duplication is the accepted price |
| Scope | Read-only mirror **plus** M104/M220/M221 controls | User chose controls over a pure mirror |
| Command feedback | Verify against telemetry, using `probe_gcode.py`'s rules | The printer never acks; see below |
| Confirm dialog before send | No | User chose verification without a modal. Inputs are clamped instead |
| Camera | Read newest frame off `runs/`; never open the webcam | Same reason as the web dashboard: Windows allows one process per camera device, and `capture.py` owns it |

`bambu_link.py`, `capture.py`, `check_registration.py`, `probe_gcode.py`, and
everything under `server/` are not modified.

## The no-ack problem (this drives the whole control design)

`BambuLink.send_gcode()` publishes and returns. There is no ack — the printer
never says it ignored you (`bambu_link.py:176-188`). Worse, **whether the A1
mini honours these commands at all is an open question**: `probe_gcode.py`
exists precisely to answer it, and its own docstring says Bambu's standing
feature request for flow control "implies the answer is no for M221".

So this GUI's controls are that experiment with buttons on it. The verification
rules must be exactly the ones `probe_gcode.py` documents, or the GUI will
state a conclusion it has not earned.

### Verification signals — from `probe_gcode.py:55-65`, not from field names

| Command | Signal | Explicitly NOT |
|---|---|---|
| `M104 S<v>` | `nozzle_temper` — the **actual** temp tracking toward the commanded value | `nozzle_target_temper`. It exists in the payload but was never proven to write back on an M104; treating it as proof is a guess |
| `M220 S<v>` | `mc_remaining_time` — the **ETA rising** | `spd_mag` / `spd_lvl`. That is Bambu's speed *profile* (a separate mechanism), not M220 feedrate |
| `M221 S<v>` | none | "LOOK AT THE PART. Telemetry will NOT tell you." |

### Verdicts

`IGNORED` is not a verdict this GUI renders. Within a 60 s window, absence of
evidence is not proof of refusal, and claiming otherwise is the same error as
trusting `nozzle_target_temper`.

| Verdict | Meaning |
|---|---|
| `PENDING` | Watching; row shows a countdown |
| `HONOURED` | Evidence observed |
| `NO_EVIDENCE` | Window elapsed, no evidence. **Not** "ignored" |
| `UNVERIFIABLE` | No telemetry signal exists for this command |
| `NOTHING_TO_OBSERVE` | Commanded value ≈ current value, so no movement could distinguish honoured from ignored |

### Rules (`verify.py`, pure functions over snapshots)

Each rule takes a baseline snapshot (captured at send) plus the latest
snapshot, and returns a verdict.

- **M104** — Requires `|commanded − baseline nozzle_temper| ≥ 5 °C`, else
  `NOTHING_TO_OBSERVE`. `HONOURED` when `nozzle_temper` has moved ≥ 2 °C
  **toward** the commanded value within 60 s. `nozzle_target_temper` is
  displayed as before → now, but never decides the verdict.
- **M220, v < 100** (slow down) — `HONOURED` when `mc_remaining_time` rises
  above baseline within 60 s. ETA only falls naturally, so a rise is strong
  evidence. Requires `gcode_state == RUNNING` and a non-null ETA, else
  `NOTHING_TO_OBSERVE`.
- **M220, v ≥ 100** (speed up, and the S100 restore) — `UNVERIFIABLE`. A
  honoured speed-up makes the ETA fall *faster*; distinguishing that from
  natural decay needs a decay model this GUI does not have. Say so rather than
  guess.
- **M221** — `UNVERIFIABLE` immediately, with the pointer to read it off the
  part.

### Restore

`probe_gcode.py` always restores the nominal value after a perturbation
(`probe_gcode.py:124`). A bare Send button has no such discipline. So each
control row captures the **nominal** value before its first send and offers a
**Restore** button that sends it back. Nominal values: nozzle from
`nozzle_target_temper` at first send (fallback 220), speed 100, flow 100.

### Input clamping

Spinboxes clamp: nozzle 0–300 °C, speed 30–200 %, flow 50–150 %. This is the
guard against a mis-click, since the user declined a confirm dialog.

## Architecture

```
basic_gui/
  __init__.py
  __main__.py    CLI + wiring: argparse, QApplication, live-vs-mock switch
  link.py        LiveLink (wraps BambuLink) + MockLink. Both: start/stop/summary/send_gcode
  frames.py      newest-frame lookup under runs/ (local copy, ~40 lines)
  verify.py      PURE: command specs + verdict rules. No Qt, no I/O, no clock of its own
  widgets.py     StatTile, StatusPill, CameraPanel, ControlRow
  window.py      MainWindow: layout + QTimers
  tests/
    test_verify.py
    test_frames.py
    test_link.py
  README.md
```

Each file has one job and is small enough to read in one sitting. `verify.py`
is deliberately pure — clock and snapshots are passed in — so every rule above
is testable with no printer, no Qt, and no sleeping.

### Threading

paho's callbacks fire on its own network thread. Qt widgets are GUI-thread-only
and touching them from another thread is undefined behavior.

So `link.py` never calls into Qt. It keeps a locked snapshot updated from the
MQTT callback (the same pattern as `server/printer.py:89-94`), and a `QTimer`
in the GUI thread polls `summary()` every 250 ms — the same 4 Hz the web
dashboard's WebSocket samples at (`server/main.py:18`). There is no
cross-thread widget access to get wrong, and no signal/slot marshalling.

A second `QTimer` polls the newest frame every 2 s, matching the web spec.

### Data flow

```
printer --MQTT 8883--> BambuLink --(paho thread)--> LiveLink locked snapshot
                                                          |
                                    QTimer 250ms -> summary() -> tiles/pills/controls
runs/<ts>_<name>/frames/layer_NNNN.jpg --> QTimer 2s -> frames.newest_frame() -> CameraPanel
```

### Connection status

Reuses the web dashboard's thresholds so both GUIs agree: `disconnected` when
the link is down; `stale` when connected but no report for > 15 s; else `ok`.

## Window layout

```
+-- Bambu Monitor ------------------------[ ok ]  192.168.1.42 --+
| State     Layer     Progress   Left     Nozzle      Bed        |
| RUNNING   42/137    31%        58 min   219.8/220   60.1/60    |
+---------------------------------+------------------------------+
| [ newest frame ]                | Print info                   |
|                                 |   gcode   benchy.gcode       |
|                                 |   task    Benchy             |
|                                 |   speed   2 (100%)           |
|                                 | HMS                          |
| Layer 42 - 20260716T1432_Benchy |   0300_0100_0001_0007        |
+---------------------------------+------------------------------+
| Controls                                                       |
|  Nozzle [195] C [Send] [Restore] temp 219.8 -> 213.1 HONOURED  |
|  Speed  [ 50] % [Send] [Restore] ETA 58 -> 61 min   HONOURED   |
|  Flow   [ 60] % [Send] [Restore] UNVERIFIABLE - read the part  |
+----------------------------------------------------------------+
```

- **Stat tiles:** State · Layer `n/total` · Progress % · Time left (min) ·
  Nozzle °C `cur/target` · Bed °C `cur/target`. Unknown fields render `—`
  (the printer sends partial updates; early on, most fields are null).
- **CameraPanel:** newest frame scaled to fit, caption `Layer N — <run>`;
  placeholder "No active capture run — start capture.py" when there is none.
  Never a broken image.
- **Print info:** gcode file, subtask, speed level/magnitude, plus
  `print_error` / `fail_reason` rows only when set.
- **HMS:** each decoded code as a danger pill, or a muted "No errors".
- **Controls:** three `ControlRow`s, each with spinbox, Send, Restore, and a
  live evidence label (before → now, verdict, countdown while `PENDING`).

## Mock mode

`MockLink` fakes the same interface with an endless synthetic print — RUNNING
(one layer every ~2 s, wandering temps, an HMS code mid-print) → FINISH → idle
→ repeat — and writes real JPEGs so the frame path is exercised. Same shape as
`server/printer.py`'s `MockPrinter`.

Crucially, it simulates the README's **middle outcome** ("partial CAXTON"):

- `M104` honoured — target moves, `nozzle_temper` ramps toward it ~1.5 °C/s
- `M220` honoured — layer period and `mc_remaining_time` scale by the feedrate
- `M221` silently ignored — recorded, no state change

So all four terminal verdicts (`HONOURED`, `UNVERIFIABLE`, `NO_EVIDENCE`,
`NOTHING_TO_OBSERVE`) are reachable with no hardware, and the verification
rules are exercised end-to-end. `PENDING` is the transient fifth, shown while
the window is still open.

`--runs-dir` defaults to `runs/` live and `runs-mock-gui/` in mock mode, so
fake frames never pollute real captures — and never collide with the web
stack's `runs-mock/`. `runs-mock-gui/` is added to `.gitignore`.

## Testing

`pytest` under `basic_gui/tests/`, pure logic only, no network and no Qt —
matching the convention in `server/tests/`:

- `test_verify.py` — every rule and every verdict: the M104 toward/away/short-
  delta cases, the M220 slowdown rise, the speed-up `UNVERIFIABLE`, M221,
  timeout → `NO_EVIDENCE`, and `NOTHING_TO_OBSERVE`. Clock is injected.
- `test_frames.py` — newest-frame lookup over tmp dirs: highest layer wins,
  staleness cutoff, empty/missing dirs.
- `test_link.py` — `MockLink` summary shape and state transitions; M104/M220
  move the state, M221 does not.

Widgets are verified manually against `--mock`, per the exit criterion below.

## New dependencies

- `PyQt6` — added to the root `requirements.txt` under a `# basic gui` comment.
  The other session may also be editing that file; expect a trivial conflict.

## Exit criterion

With `--mock`: the window shows a full fake print lifecycle (idle → RUNNING
with ticking layers/temps/frames → HMS appearing and clearing → FINISH);
`M104 S195` resolves to `HONOURED`; `M221 S60` reports `UNVERIFIABLE`;
`M220 S50` resolves to `HONOURED` and `M220 S100` reports `UNVERIFIABLE`;
Restore returns each row to nominal; commanding the current nozzle temp gives
`NOTHING_TO_OBSERVE`.

With real hardware: the same, during one actual print alongside `capture.py`
— and whatever the verdicts turn out to be, they are the honest answer to
`probe_gcode.py`'s question rather than a restatement of the command that was
sent.
