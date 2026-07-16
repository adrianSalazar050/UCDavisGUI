# Basic GUI (PyQt6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A standalone PyQt6 desktop GUI that mirrors the web dashboard's live printer view and adds M104/M220/M221 controls whose verdicts are earned from telemetry rather than assumed.

**Architecture:** `basic_gui/` owns its own `BambuLink` MQTT connection — no FastAPI server, no npm, no import from `server/`. Two `QTimer`s in the GUI thread poll a locked snapshot (250 ms) and the newest frame on disk (2 s); paho's network thread never touches Qt. Verification logic is a pure module with the clock injected, so every verdict rule is testable without a printer, without Qt, and without sleeping.

**Tech Stack:** Python 3.11, PyQt6 6.11, paho-mqtt (via the unmodified `bambu_link.py`), opencv-python + numpy (mock frames), pytest.

**Spec:** `docs/superpowers/specs/2026-07-16-basic-gui-pyqt6-design.md`

**Worktree:** `C:\Users\adria\.config\superpowers\worktrees\GUI_UCDavis\basic-gui` (branch `basic-gui`, off `master`). All paths below are relative to it. Run every command from that directory.

---

## Background the engineer must read first

Two facts drive most of this design. Neither is obvious, and getting either wrong produces a GUI that lies.

**1. The printer never acks.** `BambuLink.send_gcode()` (`bambu_link.py:176-188`) publishes and returns. The printer never reports that it ignored a line. Whether the A1 mini honours these commands **at all is an open research question** — `probe_gcode.py` exists precisely to answer it.

**2. The obvious telemetry fields are the wrong ones.** `nozzle_target_temper` and `spd_mag` look like the fields to check, and they are not:

| Command | Verify against | NOT against | Why |
|---|---|---|---|
| `M104 S<v>` | `nozzle_temper` — actual temp moving toward the command | `nozzle_target_temper` | It exists in the payload but was never shown to reflect an M104. `probe_gcode.py:56-58` says "watch nozzle_temper. If it tracks toward 195, the printer honoured it." |
| `M220 S<v>` | `mc_remaining_time` **rising** | `spd_mag` / `spd_lvl` | Those are Bambu's speed *profile* (silent/standard/sport), a separate mechanism. `probe_gcode.py:59-61` says "watch mc_remaining_time. If the ETA jumps up, it honoured it." |
| `M221 S<v>` | nothing | anything | `probe_gcode.py:62-64`: "LOOK AT THE PART. Telemetry will NOT tell you. This is the one that matters." |

There is deliberately **no `IGNORED` verdict**. Sixty seconds of silence is not proof of refusal.

Use ASCII only in user-facing strings — this runs on a Windows console where non-ASCII risks `UnicodeEncodeError`.

**Shell note:** commands below are written for **Git Bash** (the Bash tool), including `VAR=value cmd` prefixes such as `QT_QPA_PLATFORM=offscreen python -c ...`. That syntax is a parse error in PowerShell; there, use `$env:QT_QPA_PLATFORM = 'offscreen'` on its own line first. `QT_QPA_PLATFORM=offscreen` renders Qt without a visible window — it is only for the headless construction checks in Tasks 7 and 8. Task 10 needs a real window, so do **not** set it there.

---

## File Structure

| File | Responsibility |
|---|---|
| `basic_gui/__init__.py` | Package marker. Empty. |
| `basic_gui/verify.py` | **Pure.** Command G-code lines, `Watch`, `Verdict`, `evaluate()`. No Qt, no I/O, clock injected. |
| `basic_gui/frames.py` | **Pure-ish.** Newest `layer_NNNN.jpg` under a runs dir. Local copy of `server/runs.py` logic (standalone by design). |
| `basic_gui/link.py` | `build_summary()` (pure), `LiveLink` (wraps `BambuLink`), `MockLink` (fake feed + frames). Never imports Qt. |
| `basic_gui/widgets.py` | `StatusPill`, `StatTile`, `CameraPanel`, `ControlRow`. Presentation only — no MQTT, no rules. |
| `basic_gui/window.py` | `MainWindow`: layout, the two `QTimer`s, send/restore wiring. |
| `basic_gui/__main__.py` | CLI, mock-vs-live switch, `QApplication` lifecycle. |
| `basic_gui/tests/test_verify.py` | Every verdict rule. |
| `basic_gui/tests/test_frames.py` | Frame lookup. |
| `basic_gui/tests/test_link.py` | `build_summary` + `MockLink`. |
| `basic_gui/README.md` | How to run it. |
| `requirements.txt` | Modify: add PyQt6. |
| `.gitignore` | Modify: add `runs-mock-gui/`. |

`bambu_link.py`, `capture.py`, `check_registration.py`, `probe_gcode.py` and everything under `server/` are **not modified**.

---

### Task 1: Package scaffold and dependencies

**Files:**
- Create: `basic_gui/__init__.py`, `basic_gui/tests/__init__.py`
- Modify: `requirements.txt`, `.gitignore`

- [ ] **Step 1: Create the package directories and markers**

```bash
mkdir -p basic_gui/tests
```

`basic_gui/__init__.py` — one line:

```python
"""Standalone PyQt6 GUI for the Bambu A1 mini rig."""
```

`basic_gui/tests/__init__.py` — empty file (zero bytes).

- [ ] **Step 2: Add PyQt6 to `requirements.txt`**

Append these two lines to the end of the file, leaving the existing `# runtime` and `# tests` blocks exactly as they are:

```
# basic gui (PyQt6 desktop alternative to the web dashboard)
PyQt6>=6.6
```

The other session may also be editing this file on the `dashboard` branch. A conflict here is expected and trivial — keep both additions.

- [ ] **Step 3: Add the mock runs dir to `.gitignore`**

Modify the existing runs block. It currently reads:

```
runs/
runs-mock/
```

Change it to:

```
runs/
runs-mock/
runs-mock-gui/
```

- [ ] **Step 4: Verify the package imports and PyQt6 is present**

Run: `python -c "import basic_gui, PyQt6.QtCore as c; print('ok', c.PYQT_VERSION_STR)"`
Expected: `ok 6.11.0`

- [ ] **Step 5: Commit**

```bash
git add basic_gui/__init__.py basic_gui/tests/__init__.py requirements.txt .gitignore
git commit -m "chore(basic_gui): package scaffold, PyQt6 dep, ignore mock runs"
```

---

### Task 2: `verify.py` — verdict rules

This is the heart of the feature. Read the Background section above before writing it.

**Files:**
- Create: `basic_gui/verify.py`
- Test: `basic_gui/tests/test_verify.py`

- [ ] **Step 1: Write the failing tests**

Create `basic_gui/tests/test_verify.py`:

```python
import pytest

from basic_gui import verify
from basic_gui.verify import Verdict, Watch


def snap(**kw):
    """A plausible RUNNING summary; override any field via kwargs."""
    base = {"gcode_state": "RUNNING", "nozzle_temper": 220.0,
            "nozzle_target_temper": 220.0, "mc_remaining_time": 58}
    base.update(kw)
    return base


def test_line_for_renders_gcode():
    assert verify.line_for("nozzle", 195) == "M104 S195"
    assert verify.line_for("speed", 50) == "M220 S50"
    assert verify.line_for("flow", 60) == "M221 S60"


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        verify.evaluate(Watch("bogus", 1, snap(), 0.0), snap(), 0.0)


def test_flow_is_always_unverifiable():
    r = verify.evaluate(Watch("flow", 60, snap(), 0.0), snap(), 0.0)
    assert r.verdict is Verdict.UNVERIFIABLE
    assert "part" in r.detail


def test_flow_stays_unverifiable_after_the_window():
    r = verify.evaluate(Watch("flow", 60, snap(), 0.0), snap(), 999.0)
    assert r.verdict is Verdict.UNVERIFIABLE


def test_nozzle_honoured_when_temp_moves_toward_command():
    w = Watch("nozzle", 195, snap(nozzle_temper=220.0), 0.0)
    r = verify.evaluate(w, snap(nozzle_temper=217.0), 10.0)
    assert r.verdict is Verdict.HONOURED


def test_nozzle_pending_while_temp_has_barely_moved():
    w = Watch("nozzle", 195, snap(nozzle_temper=220.0), 0.0)
    r = verify.evaluate(w, snap(nozzle_temper=219.5), 10.0)
    assert r.verdict is Verdict.PENDING


def test_nozzle_no_evidence_once_the_window_elapses():
    w = Watch("nozzle", 195, snap(nozzle_temper=220.0), 0.0)
    r = verify.evaluate(w, snap(nozzle_temper=219.5), 61.0)
    assert r.verdict is Verdict.NO_EVIDENCE
    assert "not proof" in r.detail


def test_nozzle_moving_away_from_the_command_is_not_evidence():
    w = Watch("nozzle", 195, snap(nozzle_temper=220.0), 0.0)
    r = verify.evaluate(w, snap(nozzle_temper=224.0), 10.0)
    assert r.verdict is Verdict.PENDING


def test_nozzle_target_temper_alone_never_proves_honoured():
    """The correction this module exists for.

    A printer echoing back a target it may not act on is not evidence. Only
    nozzle_temper actually moving is (probe_gcode.py:56-58).
    """
    w = Watch("nozzle", 195, snap(nozzle_temper=220.0,
                                  nozzle_target_temper=220.0), 0.0)
    r = verify.evaluate(w, snap(nozzle_temper=220.0,
                                nozzle_target_temper=195.0), 10.0)
    assert r.verdict is Verdict.PENDING


def test_nozzle_command_too_close_to_current_has_nothing_to_observe():
    w = Watch("nozzle", 222, snap(nozzle_temper=220.0), 0.0)
    r = verify.evaluate(w, snap(nozzle_temper=220.0), 10.0)
    assert r.verdict is Verdict.NOTHING_TO_OBSERVE


def test_nozzle_without_a_baseline_temp_has_nothing_to_observe():
    w = Watch("nozzle", 195, snap(nozzle_temper=None), 0.0)
    r = verify.evaluate(w, snap(), 10.0)
    assert r.verdict is Verdict.NOTHING_TO_OBSERVE


def test_speed_up_is_unverifiable():
    w = Watch("speed", 150, snap(), 0.0)
    assert verify.evaluate(w, snap(), 10.0).verdict is Verdict.UNVERIFIABLE


def test_speed_restore_to_100_is_unverifiable():
    w = Watch("speed", 100, snap(), 0.0)
    assert verify.evaluate(w, snap(), 10.0).verdict is Verdict.UNVERIFIABLE


def test_speed_slowdown_honoured_when_eta_rises():
    w = Watch("speed", 50, snap(mc_remaining_time=58), 0.0)
    r = verify.evaluate(w, snap(mc_remaining_time=61), 10.0)
    assert r.verdict is Verdict.HONOURED


def test_speed_slowdown_pending_while_eta_still_falls():
    w = Watch("speed", 50, snap(mc_remaining_time=58), 0.0)
    r = verify.evaluate(w, snap(mc_remaining_time=57), 10.0)
    assert r.verdict is Verdict.PENDING


def test_speed_slowdown_no_evidence_once_the_window_elapses():
    w = Watch("speed", 50, snap(mc_remaining_time=58), 0.0)
    r = verify.evaluate(w, snap(mc_remaining_time=57), 61.0)
    assert r.verdict is Verdict.NO_EVIDENCE


def test_speed_when_not_printing_has_nothing_to_observe():
    w = Watch("speed", 50, snap(gcode_state="IDLE"), 0.0)
    r = verify.evaluate(w, snap(gcode_state="IDLE"), 10.0)
    assert r.verdict is Verdict.NOTHING_TO_OBSERVE


def test_speed_without_a_baseline_eta_has_nothing_to_observe():
    w = Watch("speed", 50, snap(mc_remaining_time=None), 0.0)
    r = verify.evaluate(w, snap(), 10.0)
    assert r.verdict is Verdict.NOTHING_TO_OBSERVE


def test_every_verdict_has_a_tone():
    for v in Verdict:
        assert verify.TONES[v] in {"ok", "warn", "danger", "muted"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest basic_gui/tests/test_verify.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'basic_gui.verify'`

- [ ] **Step 3: Write the implementation**

Create `basic_gui/verify.py`:

```python
"""Verdicts for G-code commands the printer never acknowledges.

BambuLink.send_gcode() publishes and returns; the printer never says whether
it obeyed (bambu_link.py:176-188). Worse, whether the A1 mini honours these
commands AT ALL is the open question probe_gcode.py exists to answer.

So the rules here are exactly the signals probe_gcode.py:55-65 documents, and
nothing else:

  M104 -> nozzle_temper, the ACTUAL temperature tracking toward the command.
          NOT nozzle_target_temper: it appears in the payload but was never
          shown to reflect an M104, so trusting it would invent evidence.
  M220 -> mc_remaining_time RISING. NOT spd_mag/spd_lvl, which are Bambu's
          speed profile -- a different mechanism entirely.
  M221 -> nothing at all. "LOOK AT THE PART. Telemetry will NOT tell you."

There is deliberately no IGNORED verdict. Sixty seconds of silence is not
proof of refusal, and claiming otherwise would be the same error as trusting
nozzle_target_temper.

Pure: no Qt, no I/O, no clock of its own -- `now` is always passed in.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

WINDOW_S = 60.0          # how long we watch before giving up on evidence
TEMP_MIN_DELTA_C = 5.0   # a smaller command leaves nothing to observe
TEMP_MOVE_C = 2.0        # movement toward the command that counts as evidence


class Verdict(str, Enum):
    PENDING = "PENDING"
    HONOURED = "HONOURED"
    NO_EVIDENCE = "NO_EVIDENCE"
    UNVERIFIABLE = "UNVERIFIABLE"
    NOTHING_TO_OBSERVE = "NOTHING_TO_OBSERVE"


TONES = {
    Verdict.PENDING: "muted",
    Verdict.HONOURED: "ok",
    Verdict.NO_EVIDENCE: "warn",
    Verdict.UNVERIFIABLE: "warn",
    Verdict.NOTHING_TO_OBSERVE: "muted",
}

LINES = {
    "nozzle": "M104 S{v:g}",
    "speed": "M220 S{v:g}",
    "flow": "M221 S{v:g}",
}


def line_for(kind: str, value: float) -> str:
    """The raw G-code line for a command. Same lines probe_gcode.py sends."""
    if kind not in LINES:
        raise ValueError(f"unknown command kind: {kind}")
    return LINES[kind].format(v=value)


@dataclass(frozen=True)
class Watch:
    """One in-flight command: what we asked for, and the state when we asked."""
    kind: str          # "nozzle" | "speed" | "flow"
    commanded: float
    baseline: dict     # summary snapshot taken at send time
    sent_at: float     # monotonic-ish seconds, same clock as evaluate()'s `now`


@dataclass(frozen=True)
class Result:
    verdict: Verdict
    detail: str


def evaluate(watch: Watch, latest: dict, now: float) -> Result:
    """Current verdict for `watch`, given the latest summary and the time."""
    if watch.kind == "flow":
        return _flow()
    if watch.kind == "nozzle":
        return _nozzle(watch, latest, now)
    if watch.kind == "speed":
        return _speed(watch, latest, now)
    raise ValueError(f"unknown command kind: {watch.kind}")


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _settle(detail: str, watch: Watch, now: float) -> Result:
    """Shared tail: still watching, or out of time with nothing seen."""
    if now - watch.sent_at >= WINDOW_S:
        return Result(Verdict.NO_EVIDENCE,
                      detail + f" - no evidence in {WINDOW_S:.0f}s "
                               "(not proof it refused)")
    left = WINDOW_S - (now - watch.sent_at)
    return Result(Verdict.PENDING, detail + f" - watching, {left:.0f}s left")


def _flow() -> Result:
    # probe_gcode.py:62-64. There is no field to check. Saying anything else
    # here would be inventing a result.
    return Result(Verdict.UNVERIFIABLE,
                  "no telemetry signal exists - read it off the part "
                  "(extrusion should visibly thin or gap)")


def _nozzle(watch: Watch, latest: dict, now: float) -> Result:
    base = _num(watch.baseline.get("nozzle_temper"))
    if base is None:
        return Result(Verdict.NOTHING_TO_OBSERVE,
                      "no nozzle_temper reported yet - nothing to compare against")
    delta = watch.commanded - base
    if abs(delta) < TEMP_MIN_DELTA_C:
        return Result(Verdict.NOTHING_TO_OBSERVE,
                      f"commanded {watch.commanded:g}C is ~= current {base:.1f}C; "
                      "no movement could tell honoured from ignored")
    cur = _num(latest.get("nozzle_temper"))
    if cur is None:
        return _settle(f"temp {base:.1f} -> ? "
                       f"(commanded {watch.commanded:g}C)", watch, now)
    moved = (cur - base) * (1.0 if delta > 0 else -1.0)
    detail = f"temp {base:.1f} -> {cur:.1f}C (commanded {watch.commanded:g}C)"
    if moved >= TEMP_MOVE_C:
        return Result(Verdict.HONOURED, detail)
    return _settle(detail, watch, now)


def _speed(watch: Watch, latest: dict, now: float) -> Result:
    if watch.commanded >= 100:
        # A honoured speed-up only makes the ETA fall FASTER. Separating that
        # from natural decay needs a decay model we do not have -- and the
        # S100 restore is the same problem. Say so instead of guessing.
        return Result(Verdict.UNVERIFIABLE,
                      "a honoured speed-up only makes the ETA fall faster; "
                      "indistinguishable from normal countdown here")
    base_eta = _num(watch.baseline.get("mc_remaining_time"))
    if watch.baseline.get("gcode_state") != "RUNNING" or base_eta is None:
        return Result(Verdict.NOTHING_TO_OBSERVE,
                      "the ETA only moves during a RUNNING print")
    cur = _num(latest.get("mc_remaining_time"))
    if cur is None:
        return _settle(f"ETA {base_eta:g} -> ? min "
                       f"(commanded {watch.commanded:g}%)", watch, now)
    detail = (f"ETA {base_eta:g} -> {cur:g} min "
              f"(commanded {watch.commanded:g}%)")
    if cur > base_eta:
        # The ETA only ever falls on its own, so a rise is strong evidence.
        return Result(Verdict.HONOURED, detail)
    return _settle(detail, watch, now)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest basic_gui/tests/test_verify.py -q`
Expected: PASS — 19 passed

- [ ] **Step 5: Commit**

```bash
git add basic_gui/verify.py basic_gui/tests/test_verify.py
git commit -m "feat(basic_gui): verdict rules from probe_gcode's real signals

M104 verifies against nozzle_temper trend, M220 against a rising ETA,
M221 not at all. nozzle_target_temper and spd_mag look right and are not.
No IGNORED verdict: 60s of silence is not proof of refusal."
```

---

### Task 3: `frames.py` — newest captured frame

**Files:**
- Create: `basic_gui/frames.py`
- Test: `basic_gui/tests/test_frames.py`

- [ ] **Step 1: Write the failing tests**

Create `basic_gui/tests/test_frames.py`:

```python
import os
import time

from basic_gui.frames import newest_frame


def mkframe(runs, run, layer, age_s=0.0):
    d = runs / run / "frames"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"layer_{layer:04d}.jpg"
    p.write_bytes(b"\xff\xd8not-a-real-jpeg")
    if age_s:
        t = time.time() - age_s
        os.utime(p, (t, t))
    return p


def test_none_when_runs_dir_missing(tmp_path):
    assert newest_frame(tmp_path / "nope") is None


def test_none_when_no_runs_yet(tmp_path):
    assert newest_frame(tmp_path) is None


def test_none_when_frames_dir_is_empty(tmp_path):
    (tmp_path / "run_a" / "frames").mkdir(parents=True)
    assert newest_frame(tmp_path) is None


def test_none_when_run_has_no_frames_subdir(tmp_path):
    (tmp_path / "run_a").mkdir()
    assert newest_frame(tmp_path) is None


def test_highest_layer_wins(tmp_path):
    mkframe(tmp_path, "run_a", 1)
    mkframe(tmp_path, "run_a", 7)
    mkframe(tmp_path, "run_a", 3)
    info = newest_frame(tmp_path)
    assert info["layer"] == 7
    assert info["run"] == "run_a"
    assert info["path"].name == "layer_0007.jpg"


def test_stale_run_is_ignored(tmp_path):
    mkframe(tmp_path, "run_old", 5, age_s=31 * 60)
    assert newest_frame(tmp_path) is None


def test_most_recently_written_run_wins(tmp_path):
    mkframe(tmp_path, "run_old", 99, age_s=600)
    mkframe(tmp_path, "run_new", 2)
    info = newest_frame(tmp_path)
    assert info["run"] == "run_new"
    assert info["layer"] == 2


def test_non_frame_files_are_ignored(tmp_path):
    d = tmp_path / "run_a" / "frames"
    d.mkdir(parents=True)
    (d / "notes.txt").write_text("x")
    (d / "layer_x.jpg").write_bytes(b"x")
    (d / "layer_0001.png").write_bytes(b"x")
    assert newest_frame(tmp_path) is None


def test_now_is_injectable(tmp_path):
    mkframe(tmp_path, "run_a", 1)
    assert newest_frame(tmp_path, now=time.time() + 31 * 60) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest basic_gui/tests/test_frames.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'basic_gui.frames'`

- [ ] **Step 3: Write the implementation**

Create `basic_gui/frames.py`:

```python
"""Locate the newest captured frame under a runs/ directory.

capture.py writes runs/<ts>_<name>/frames/layer_NNNN.jpg. The GUI displays the
newest of those instead of opening the webcam: Windows allows one process per
camera device, and capture.py owns it. Opening it here would steal it.

This duplicates server/runs.py rather than importing it. That is the point --
this package stays standalone, so a refactor of the web stack cannot break it.
"""

from __future__ import annotations

import pathlib
import re
import time

FRAME_RE = re.compile(r"^layer_(\d{1,6})\.jpg$")
ACTIVE_WINDOW_S = 30 * 60  # a run is "active" if it wrote a frame this recently


def newest_frame(runs_dir, now: float | None = None) -> dict | None:
    """{"path": Path, "layer": int, "run": str} for the highest-numbered frame
    of the run that wrote most recently -- provided that write is within
    ACTIVE_WINDOW_S. Otherwise None.
    """
    now = time.time() if now is None else now
    runs_dir = pathlib.Path(runs_dir)
    if not runs_dir.is_dir():
        return None

    best_run, best_mtime = None, -1.0
    for run in runs_dir.iterdir():
        frames = run / "frames"
        if not frames.is_dir():
            continue
        for f in frames.iterdir():
            if not FRAME_RE.match(f.name):
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue  # vanished or locked mid-scan (OneDrive sync); skip
            if mtime > best_mtime:
                best_run, best_mtime = run, mtime

    if best_run is None or now - best_mtime > ACTIVE_WINDOW_S:
        return None

    best: tuple[int, pathlib.Path] | None = None
    try:
        entries = list((best_run / "frames").iterdir())
    except OSError:
        return None  # run dir vanished between discovery and read
    for f in entries:
        m = FRAME_RE.match(f.name)
        if not m:
            continue
        layer = int(m.group(1))
        if best is None or layer > best[0]:
            best = (layer, f)
    if best is None:
        return None
    return {"path": best[1], "layer": best[0], "run": best_run.name}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest basic_gui/tests/test_frames.py -q`
Expected: PASS — 9 passed

- [ ] **Step 5: Commit**

```bash
git add basic_gui/frames.py basic_gui/tests/test_frames.py
git commit -m "feat(basic_gui): newest-frame lookup under runs/"
```

---

### Task 4: `link.py` — `build_summary`

Build the module in two commits: the pure curation first, the feeds after.

**Files:**
- Create: `basic_gui/link.py`
- Test: `basic_gui/tests/test_link.py`

- [ ] **Step 1: Write the failing tests**

Create `basic_gui/tests/test_link.py`:

```python
from basic_gui.link import STALE_S, build_summary


def test_unreported_fields_are_null():
    s = build_summary({}, None, True, "MOCK")
    assert s["layer_num"] is None
    assert s["nozzle_temper"] is None
    assert s["gcode_state"] is None
    assert s["hms"] == []
    assert s["printer"] == "MOCK"


def test_reported_fields_pass_through():
    s = build_summary({"layer_num": 42, "gcode_state": "RUNNING"},
                      1.0, True, "h")
    assert s["layer_num"] == 42
    assert s["gcode_state"] == "RUNNING"


def test_connection_ok_when_reports_are_fresh():
    assert build_summary({}, 1.0, True, "h")["connection"] == "ok"


def test_connection_stale_when_reports_stop():
    assert build_summary({}, STALE_S + 1, True, "h")["connection"] == "stale"


def test_connection_stale_when_no_report_ever_arrived():
    assert build_summary({}, None, True, "h")["connection"] == "stale"


def test_connection_disconnected_beats_freshness():
    assert build_summary({}, 1.0, False, "h")["connection"] == "disconnected"


def test_report_age_is_rounded_and_nullable():
    assert build_summary({}, 1.234, True, "h")["report_age_s"] == 1.2
    assert build_summary({}, None, True, "h")["report_age_s"] is None


def test_hms_codes_are_decoded():
    s = build_summary({"hms": [{"attr": 0x03000100, "code": 0x00010007}]},
                      1.0, True, "h")
    assert s["hms"] == ["0300_0100_0001_0007"]


def test_malformed_hms_entries_cannot_break_the_summary():
    # hms comes from printer-controlled JSON; one bad entry must not take
    # down every future summary() call.
    s = build_summary({"hms": ["junk", {"attr": "x", "code": 1}, None]},
                      1.0, True, "h")
    assert s["hms"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest basic_gui/tests/test_link.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'basic_gui.link'`

- [ ] **Step 3: Write the implementation**

Create `basic_gui/link.py`:

```python
"""Printer feeds for the GUI: LiveLink (real MQTT) and MockLink (fake).

Both expose exactly: start(), stop(), summary() -> dict, send_gcode(line).

Nothing here imports Qt. paho's callbacks fire on its own network thread, and
Qt widgets are GUI-thread-only -- so this module keeps a locked snapshot and
the window polls it on a QTimer instead. That is the whole threading design:
there is no cross-thread widget access to get wrong.
"""

from __future__ import annotations

import logging
import pathlib
import re
import sys
import threading
import time
from datetime import datetime

import cv2
import numpy as np

# bambu_link.py lives at the repo root, one level above this package.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from bambu_link import BambuLink, decode_hms  # noqa: E402

log = logging.getLogger("basic_gui.link")

STALE_S = 15.0  # connected but no report for this long -> "stale"
RETRY_S = 10.0  # MQTT reconnect attempt interval

SUMMARY_FIELDS = (
    "gcode_state", "layer_num", "total_layer_num", "mc_percent",
    "mc_remaining_time", "nozzle_temper", "nozzle_target_temper",
    "bed_temper", "bed_target_temper", "spd_lvl", "spd_mag",
    "print_error", "fail_reason", "subtask_name", "gcode_file",
)


def build_summary(state: dict, report_age: float | None,
                  connected: bool, printer: str) -> dict:
    """Curate merged printer state into the payload the window renders.

    Fields the printer has not reported yet are None -- it sends partial
    updates, so early in a session most fields are simply unknown.
    """
    out = {k: state.get(k) for k in SUMMARY_FIELDS}
    codes = []
    for h in state.get("hms") or []:
        if not isinstance(h, dict):
            continue
        try:
            codes.append(decode_hms(int(h.get("attr", 0)), int(h.get("code", 0))))
        except (TypeError, ValueError):
            continue
    out["hms"] = codes
    if not connected:
        conn = "disconnected"
    elif report_age is None or report_age > STALE_S:
        conn = "stale"
    else:
        conn = "ok"
    out["connection"] = conn
    out["report_age_s"] = None if report_age is None else round(report_age, 1)
    out["printer"] = printer
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest basic_gui/tests/test_link.py -q`
Expected: PASS — 9 passed

- [ ] **Step 5: Commit**

```bash
git add basic_gui/link.py basic_gui/tests/test_link.py
git commit -m "feat(basic_gui): summary curation with connection state"
```

---

### Task 5: `link.py` — `MockLink`

The mock simulates the README's **middle outcome** ("partial CAXTON"): M104 and M220 honoured, M221 silently ignored. That makes every verdict reachable with no hardware.

Its state machine is a pure `_tick(dt)` so tests drive it directly — no threads, no sleeping.

**Files:**
- Modify: `basic_gui/link.py` (append)
- Test: `basic_gui/tests/test_link.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `basic_gui/tests/test_link.py`:

```python
from basic_gui.link import MockLink


def running(tmp_path):
    """A MockLink advanced just past its first tick, so a run has begun."""
    m = MockLink(tmp_path)
    m._tick(0.25)
    return m


def test_mock_starts_idle(tmp_path):
    assert MockLink(tmp_path).summary()["gcode_state"] == "IDLE"


def test_mock_begins_a_run_on_the_first_tick(tmp_path):
    m = running(tmp_path)
    s = m.summary()
    assert s["gcode_state"] == "RUNNING"
    assert s["total_layer_num"] == MockLink.LAYERS
    assert s["printer"] == "MOCK"


def test_mock_advances_layers_and_writes_real_frames(tmp_path):
    m = running(tmp_path)
    for _ in range(int(MockLink.LAYER_PERIOD_S / 0.25) + 1):
        m._tick(0.25)
    assert m.summary()["layer_num"] >= 1
    frames = list(tmp_path.rglob("layer_*.jpg"))
    assert frames
    assert frames[0].stat().st_size > 0


def test_mock_honours_m104_by_ramping_the_actual_temp(tmp_path):
    m = running(tmp_path)
    before = m.summary()["nozzle_temper"]
    m.send_gcode("M104 S195")
    for _ in range(20):  # 20 * 0.25s * 1.5C/s = 7.5C of ramp
        m._tick(0.25)
    assert m.summary()["nozzle_temper"] < before - 2.0


def test_mock_honours_m220_by_raising_the_eta(tmp_path):
    m = running(tmp_path)
    before = m.summary()["mc_remaining_time"]
    m.send_gcode("M220 S50")
    assert m.summary()["mc_remaining_time"] > before


def test_mock_ignores_m221_exactly_as_bambu_probably_does(tmp_path):
    m = running(tmp_path)
    before = m.summary()
    m.send_gcode("M221 S60")
    after = m.summary()
    before.pop("report_age_s"), after.pop("report_age_s")
    assert before == after
    assert m.flow_commands == [60.0]  # received, deliberately not acted on


def test_mock_ignores_gcode_it_does_not_model(tmp_path):
    m = running(tmp_path)
    before = m.summary()
    m.send_gcode("G28")
    after = m.summary()
    before.pop("report_age_s"), after.pop("report_age_s")
    assert before == after


def test_mock_raises_an_hms_code_midprint_then_clears_it(tmp_path):
    m = running(tmp_path)
    seen, cleared = False, False
    for _ in range(int(MockLink.LAYERS * MockLink.LAYER_PERIOD_S / 0.25) + 8):
        m._tick(0.25)
        if m.summary()["hms"]:
            seen = True
        elif seen:
            cleared = True
    assert seen and cleared


def test_mock_finishes_then_returns_to_idle(tmp_path):
    m = running(tmp_path)
    states = set()
    ticks = int((MockLink.LAYERS * MockLink.LAYER_PERIOD_S
                 + MockLink.IDLE_S) / 0.25) + 12
    for _ in range(ticks):
        m._tick(0.25)
        states.add(m.summary()["gcode_state"])
    assert "FINISH" in states
    assert "IDLE" in states
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest basic_gui/tests/test_link.py -q`
Expected: FAIL — `ImportError: cannot import name 'MockLink'`

- [ ] **Step 3: Write the implementation**

Append to `basic_gui/link.py`:

```python
GCODE_RE = re.compile(r"\s*(M104|M220|M221)\s+S(-?\d+(?:\.\d+)?)", re.I)


class MockLink:
    """Endless fake print, for building the GUI with no hardware.

    Lifecycle per cycle: RUNNING (a layer every LAYER_PERIOD_S, an HMS code
    during HMS_LAYERS) -> FINISH -> IDLE_S idle -> new run. Frames are real
    JPEGs in a real run directory, so frames.newest_frame() is exercised too.

    It simulates the README's MIDDLE outcome -- "partial CAXTON":
        M104 honoured, M220 honoured, M221 silently ignored.
    So every verdict in verify.py is reachable without a printer.

    _tick(dt) is the whole state machine and is deterministic, so tests drive
    it directly instead of sleeping through _loop().
    """

    LAYERS = 30
    LAYER_PERIOD_S = 2.0
    IDLE_S = 10.0
    HMS_LAYERS = range(12, 17)
    TICK_S = 0.25
    RAMP_C_PER_S = 1.5  # nozzle ramps deterministically: the verify rules
                        # need a clean signal, and noise here would only
                        # fight verify.TEMP_MOVE_C. The bed wanders instead.

    def __init__(self, runs_dir):
        self.runs_dir = pathlib.Path(runs_dir)
        self.state: dict = {"gcode_state": "IDLE"}
        self.flow_commands: list[float] = []  # M221s received and ignored
        self._feedrate = 1.0
        self._layer = 0
        self._accum = 0.0
        self._idle_left = 0.0
        self._frames: pathlib.Path | None = None
        self._last_report: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    # ---------------- the interface the window uses ----------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def summary(self) -> dict:
        age = (None if self._last_report is None
               else time.time() - self._last_report)
        return build_summary(self.state, age, True, "MOCK")

    def send_gcode(self, line: str) -> None:
        m = GCODE_RE.match(line.strip())
        if not m:
            return  # anything we do not model is silently dropped, like a printer
        cmd, val = m.group(1).upper(), float(m.group(2))
        if cmd == "M104":
            self._touch({"nozzle_target_temper": val})
        elif cmd == "M220":
            self._feedrate = max(val, 1.0) / 100.0
            self._touch({"mc_remaining_time": self._eta()})
        elif cmd == "M221":
            self.flow_commands.append(val)  # received. deliberately ignored.

    # ---------------- state machine ----------------

    def _loop(self) -> None:
        while not self._stop.wait(self.TICK_S):
            self._tick(self.TICK_S)

    def _tick(self, dt: float) -> None:
        st = self.state.get("gcode_state")
        if st in (None, "IDLE"):
            self._begin_run()
            return
        if st == "FINISH":
            self._idle_left -= dt
            if self._idle_left <= 0:
                self._touch({"gcode_state": "IDLE", "layer_num": 0,
                             "mc_percent": 0})
            return
        self._ramp_temp(dt)
        self._accum += dt * self._feedrate
        if self._accum >= self.LAYER_PERIOD_S:
            self._accum = 0.0
            self._layer += 1
            self._advance_layer()

    def _begin_run(self) -> None:
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        self._frames = self.runs_dir / f"{ts}_mock_benchy" / "frames"
        self._frames.mkdir(parents=True, exist_ok=True)
        self._layer, self._accum, self._feedrate = 0, 0.0, 1.0
        self._touch({
            "gcode_state": "RUNNING", "subtask_name": "mock_benchy",
            "gcode_file": "mock.gcode", "total_layer_num": self.LAYERS,
            "layer_num": 0, "mc_percent": 0,
            "nozzle_temper": 220.0, "nozzle_target_temper": 220.0,
            "bed_temper": 60.0, "bed_target_temper": 60.0,
            "spd_lvl": 2, "spd_mag": 100, "print_error": 0, "hms": [],
            "mc_remaining_time": self._eta(),
        })

    def _advance_layer(self) -> None:
        if self._layer > self.LAYERS:
            self._touch({"gcode_state": "FINISH", "hms": []})
            self._idle_left = self.IDLE_S
            return
        self._touch({
            "layer_num": self._layer,
            "mc_percent": int(100 * self._layer / self.LAYERS),
            "mc_remaining_time": self._eta(),
            "bed_temper": 60.0 + float(np.random.randn()) * 0.3,
            "hms": ([{"attr": 0x03000100, "code": 0x00010007}]
                    if self._layer in self.HMS_LAYERS else []),
        })
        cv2.imwrite(str(self._frames / f"layer_{self._layer:04d}.jpg"),
                    self._frame(self._layer))

    def _eta(self) -> int:
        left = (self.LAYERS - self._layer) * self.LAYER_PERIOD_S / self._feedrate
        return int(left / 60) + 1

    def _ramp_temp(self, dt: float) -> None:
        cur = self.state.get("nozzle_temper", 220.0)
        target = self.state.get("nozzle_target_temper", 220.0)
        step = self.RAMP_C_PER_S * dt
        if abs(target - cur) <= step:
            new = float(target)
        else:
            new = cur + step * (1.0 if target > cur else -1.0)
        self._touch({"nozzle_temper": new})

    def _touch(self, patch: dict) -> None:
        self.state.update(patch)
        self._last_report = time.time()

    def _frame(self, layer: int) -> "np.ndarray":
        # Same idea as capture.py's MockCamera: a synthetic print that grows.
        img = np.full((480, 640, 3), 40, np.uint8)
        cv2.rectangle(img, (180, 380), (460, 400), (90, 90, 95), -1)
        ph = min(layer * 8, 300)
        if ph:
            cv2.rectangle(img, (270, 380 - ph), (370, 380), (30, 110, 200), -1)
        img = cv2.add(img, np.random.randint(0, 12, img.shape, dtype=np.uint8))
        cv2.putText(img, f"layer {layer}", (12, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (220, 220, 220), 2)
        return img
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest basic_gui/tests/test_link.py -q`
Expected: PASS — 18 passed

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest basic_gui -q`
Expected: PASS — 46 passed

- [ ] **Step 6: Commit**

```bash
git add basic_gui/link.py basic_gui/tests/test_link.py
git commit -m "feat(basic_gui): MockLink simulating partial-CAXTON printer

Honours M104/M220, silently ignores M221 -- the README's middle outcome,
so every verify.py verdict is reachable with no hardware. _tick(dt) is
deterministic so tests never sleep."
```

---

### Task 6: `link.py` — `LiveLink`

No unit test: it is a thin wrapper whose behavior is network I/O. It is covered by the real-hardware half of the exit criterion, matching how `server/tests/` leaves `PrinterService` untested.

**Files:**
- Modify: `basic_gui/link.py` (append)

- [ ] **Step 1: Write the implementation**

Append to `basic_gui/link.py`:

```python
class LiveLink:
    """The real printer: owns a BambuLink, retries the initial MQTT connect.

    Startup must not die if the printer is off -- we start disconnected and
    retry every RETRY_S. Once paho has connected ONCE its network loop
    auto-reconnects on drops, so we only drive the initial connect.
    """

    def __init__(self, host: str, serial: str, access_code: str):
        self.host = host
        self.link = BambuLink(host, serial, access_code, on_state=self._on_state)
        self._snapshot: dict = {}
        self._last_report: float | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._connect_loop, daemon=True)

    def _on_state(self, state: dict, patch: dict) -> None:
        # Fires on paho's network thread. Never touch Qt from here.
        # `state` is the deep copy BambuLink made under its own lock, so
        # keeping it means summary() never races that thread's deep_merge.
        with self._lock:
            self._snapshot = state
            self._last_report = time.time()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.link.disconnect()
        except Exception as e:  # never let teardown mask the real exit path
            log.debug("disconnect during shutdown: %s", e)
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def _connect_loop(self) -> None:
        # connect() raising means paho's loop never started -> retrying is
        # ours. connect() returning False means loop_start() ran and paho now
        # retries forever on its own; re-driving connect() here would race
        # paho's thread on the same socket, so we log and hand off.
        while not self._stop.is_set():
            try:
                if self.link.connect(timeout=5):
                    return
                log.warning("reached %s but no CONNACK within 5s (wrong access "
                            "code, or Developer Mode off?). paho keeps retrying "
                            "in the background.", self.host)
                return
            except Exception as e:
                log.warning("MQTT connect to %s failed: %s (retry in %ss)",
                            self.host, e, RETRY_S)
            self._stop.wait(RETRY_S)

    def summary(self) -> dict:
        with self._lock:
            snap, last = self._snapshot, self._last_report
        age = None if last is None else time.time() - last
        return build_summary(snap, age, self.link.connected.is_set(), self.host)

    def send_gcode(self, line: str) -> None:
        """Publish a G-code line. There is no ack -- see verify.py."""
        self.link.send_gcode(line)
```

- [ ] **Step 2: Verify it imports and the suite still passes**

Run: `python -c "from basic_gui.link import LiveLink, MockLink, build_summary; print('ok')" && python -m pytest basic_gui -q`
Expected: `ok` then PASS — 46 passed

- [ ] **Step 3: Commit**

```bash
git add basic_gui/link.py
git commit -m "feat(basic_gui): LiveLink with background MQTT connect retry"
```

---

### Task 7: `widgets.py` — presentation

**Files:**
- Create: `basic_gui/widgets.py`

- [ ] **Step 1: Write the implementation**

Create `basic_gui/widgets.py`:

```python
"""Qt widgets. Presentation only -- no MQTT, no verdict rules, no file scans.

Colors follow the web dashboard's "Slate Daylight" idea: status color appears
only inside a pill, never on body text.
"""

from __future__ import annotations

import pathlib

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (QDoubleSpinBox, QFrame, QHBoxLayout, QLabel,
                             QPushButton, QVBoxLayout, QWidget)

TONE_CSS = {
    "ok": "background:#e6f4ea; color:#0b6b34; border:1px solid #b7e0c4;",
    "warn": "background:#fdf3e0; color:#8a5a00; border:1px solid #f0d9a8;",
    "danger": "background:#fdeaea; color:#a11212; border:1px solid #f2bcbc;",
    "muted": "background:#f1f3f5; color:#5a6672; border:1px solid #dde2e7;",
}

CARD_CSS = "QFrame{background:#ffffff;border:1px solid #e3e8ee;border-radius:8px;}"


class StatusPill(QLabel):
    """A small rounded label. The only place status color is allowed."""

    def __init__(self, text: str = "", tone: str = "muted"):
        super().__init__(text)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        css = TONE_CSS.get(tone, TONE_CSS["muted"])
        self.setStyleSheet("QLabel{border-radius:9px;padding:2px 10px;"
                           "font-weight:600;" + css + "}")

    def set(self, text: str, tone: str) -> None:
        self.setText(text)
        self.set_tone(tone)


class StatTile(QFrame):
    """One headline number with a caption."""

    def __init__(self, caption: str):
        super().__init__()
        self.setStyleSheet(CARD_CSS)
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        k = QLabel(caption)
        k.setStyleSheet("color:#6b7784;font-size:11px;border:none;")
        self._v = QLabel("-")
        self._v.setStyleSheet("font-size:17px;font-weight:700;border:none;")
        v.addWidget(k)
        v.addWidget(self._v)

    def set_value(self, text: str) -> None:
        self._v.setText(text)


class CameraPanel(QFrame):
    """Newest captured frame, or a placeholder. Never a broken image."""

    PLACEHOLDER = "No active capture run - start capture.py"
    BOX = (560, 420)

    def __init__(self):
        super().__init__()
        self.setStyleSheet(CARD_CSS)
        v = QVBoxLayout(self)
        self._img = QLabel(self.PLACEHOLDER)
        self._img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img.setMinimumSize(*self.BOX)
        self._img.setStyleSheet("color:#6b7784;background:#f7f9fb;"
                                "border:1px dashed #dde2e7;border-radius:6px;")
        self._cap = QLabel("")
        self._cap.setStyleSheet("color:#6b7784;font-size:11px;border:none;")
        v.addWidget(self._img)
        v.addWidget(self._cap)
        self._key = None

    def set_placeholder(self) -> None:
        if self._key is None:
            return
        self._key = None
        self._img.setPixmap(QPixmap())
        self._img.setText(self.PLACEHOLDER)
        self._cap.setText("")

    def show_frame(self, path: pathlib.Path, layer: int, run: str) -> None:
        try:
            key = (str(path), path.stat().st_mtime)
        except OSError:
            return  # vanished between lookup and load; the next poll retries
        if key == self._key:
            return
        pm = QPixmap(str(path))
        if pm.isNull():
            # capture.py writes non-atomically, so a torn JPEG is possible and
            # accepted. Keep the last good frame; we re-poll in 2s.
            return
        self._key = key
        self._img.setText("")
        self._img.setPixmap(pm.scaled(
            self._img.width(), self._img.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        self._cap.setText(f"Layer {layer} - {run}")


class ControlRow(QWidget):
    """One command: value, Send, Restore, verdict pill, evidence text.

    Emits intent only. The window owns the watches and the rules.
    """

    sendRequested = pyqtSignal(str, float)
    restoreRequested = pyqtSignal(str)

    def __init__(self, kind: str, label: str, unit: str,
                 lo: float, hi: float, default: float):
        super().__init__()
        self.kind = kind
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)

        name = QLabel(label)
        name.setMinimumWidth(60)
        h.addWidget(name)

        self.spin = QDoubleSpinBox()
        self.spin.setRange(lo, hi)      # the guard against a mis-click
        self.spin.setDecimals(0)
        self.spin.setValue(default)
        self.spin.setSuffix(f" {unit}")
        self.spin.setFixedWidth(90)
        h.addWidget(self.spin)

        send = QPushButton("Send")
        send.clicked.connect(
            lambda: self.sendRequested.emit(self.kind, self.spin.value()))
        h.addWidget(send)

        self.restore = QPushButton("Restore")
        self.restore.setEnabled(False)  # nothing to restore until you send
        self.restore.clicked.connect(
            lambda: self.restoreRequested.emit(self.kind))
        h.addWidget(self.restore)

        self.pill = StatusPill("", "muted")
        self.pill.setFixedWidth(150)
        h.addWidget(self.pill)

        self.evidence = QLabel("")
        self.evidence.setStyleSheet("color:#5a6672;")
        h.addWidget(self.evidence, 1)

    def set_result(self, verdict: str, detail: str, tone: str) -> None:
        self.pill.set(verdict, tone)
        self.evidence.setText(detail)
```

- [ ] **Step 2: Verify the widgets construct headlessly**

Run:

```bash
QT_QPA_PLATFORM=offscreen python -c "
from PyQt6.QtWidgets import QApplication
from basic_gui.widgets import StatusPill, StatTile, CameraPanel, ControlRow
app = QApplication([])
StatusPill('ok', 'ok'); StatTile('State'); CameraPanel()
ControlRow('nozzle', 'Nozzle', 'C', 0, 300, 220)
print('widgets ok')
"
```

Expected: `widgets ok`

- [ ] **Step 3: Commit**

```bash
git add basic_gui/widgets.py
git commit -m "feat(basic_gui): stat tile, status pill, camera panel, control row"
```

---

### Task 8: `window.py` — the window

**Files:**
- Create: `basic_gui/window.py`

- [ ] **Step 1: Write the implementation**

Create `basic_gui/window.py`:

```python
"""MainWindow: layout, the two polling timers, and send/restore wiring.

Threading: this is the only module that touches Qt. The link's MQTT callbacks
run on paho's thread and only ever write a locked snapshot; a QTimer here
polls summary() at STATE_MS -- the same 4Hz the web dashboard's WebSocket
samples at (server/main.py:18). Nothing marshals across threads.
"""

from __future__ import annotations

import pathlib
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QGridLayout, QHBoxLayout, QLabel, QMainWindow,
                             QVBoxLayout, QWidget)

from . import frames, verify
from .widgets import CARD_CSS, CameraPanel, ControlRow, StatTile, StatusPill

CONN_TONE = {"ok": "ok", "stale": "warn", "disconnected": "danger"}

TILES = (
    ("state", "State"),
    ("layer", "Layer"),
    ("progress", "Progress"),
    ("left", "Time left"),
    ("nozzle", "Nozzle C"),
    ("bed", "Bed C"),
)


def _fmt(v) -> str:
    return "-" if v is None else f"{v:g}"


def _fmt1(v) -> str:
    return "-" if v is None else f"{v:.1f}"


class MainWindow(QMainWindow):
    STATE_MS = 250    # matches server/main.py WS_POLL_S = 0.25
    FRAME_MS = 2000   # matches the web dashboard's frame poll

    def __init__(self, link, runs_dir):
        super().__init__()
        self.link = link
        self.runs_dir = pathlib.Path(runs_dir)
        self.watches: dict[str, verify.Watch] = {}
        self.nominal: dict[str, float] = {}

        self.setWindowTitle("Bambu Monitor - basic GUI")
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        outer.addLayout(self._build_topbar())
        outer.addLayout(self._build_tiles())
        outer.addLayout(self._build_body())
        outer.addWidget(self._build_controls())

        self._state_timer = QTimer(self)
        self._state_timer.timeout.connect(self._refresh_state)
        self._state_timer.start(self.STATE_MS)

        self._frame_timer = QTimer(self)
        self._frame_timer.timeout.connect(self._refresh_frame)
        self._frame_timer.start(self.FRAME_MS)

        self._refresh_state()
        self._refresh_frame()

    # ---------------- layout ----------------

    def _build_topbar(self):
        h = QHBoxLayout()
        title = QLabel("Bambu Monitor")
        title.setStyleSheet("font-size:19px;font-weight:700;")
        self.conn_pill = StatusPill("...", "muted")
        self.host_label = QLabel("")
        self.host_label.setStyleSheet("color:#6b7784;")
        h.addWidget(title)
        h.addStretch(1)
        h.addWidget(self.conn_pill)
        h.addWidget(self.host_label)
        return h

    def _build_tiles(self):
        h = QHBoxLayout()
        self.tiles = {}
        for key, caption in TILES:
            t = StatTile(caption)
            self.tiles[key] = t
            h.addWidget(t)
        return h

    def _build_body(self):
        h = QHBoxLayout()
        self.camera = CameraPanel()
        h.addWidget(self.camera, 3)

        side = QVBoxLayout()
        side.addWidget(self._build_info())
        side.addWidget(self._build_hms())
        side.addStretch(1)
        h.addLayout(side, 2)
        return h

    def _build_info(self):
        card = QWidget()
        card.setStyleSheet(CARD_CSS)
        g = QGridLayout(card)
        head = QLabel("Print info")
        head.setStyleSheet("font-weight:700;border:none;")
        g.addWidget(head, 0, 0, 1, 2)
        self.info = {}
        rows = (("gcode_file", "gcode"), ("subtask_name", "task"),
                ("speed", "speed"), ("print_error", "print_error"),
                ("fail_reason", "fail_reason"))
        for i, (key, caption) in enumerate(rows, start=1):
            k = QLabel(caption)
            k.setStyleSheet("color:#6b7784;border:none;")
            v = QLabel("-")
            v.setStyleSheet("border:none;")
            v.setWordWrap(True)
            g.addWidget(k, i, 0)
            g.addWidget(v, i, 1)
            self.info[key] = (k, v)
        return card

    def _build_hms(self):
        card = QWidget()
        card.setStyleSheet(CARD_CSS)
        v = QVBoxLayout(card)
        head = QLabel("HMS")
        head.setStyleSheet("font-weight:700;border:none;")
        v.addWidget(head)
        self.hms_box = QVBoxLayout()
        v.addLayout(self.hms_box)
        self.hms_none = QLabel("No errors")
        self.hms_none.setStyleSheet("color:#6b7784;border:none;")
        v.addWidget(self.hms_none)
        self._hms_pills: list[StatusPill] = []
        return card

    def _build_controls(self):
        card = QWidget()
        card.setStyleSheet(CARD_CSS)
        v = QVBoxLayout(card)
        head = QLabel("Controls")
        head.setStyleSheet("font-weight:700;border:none;")
        v.addWidget(head)
        note = QLabel("The printer never acks. Verdicts come from telemetry "
                      "only, using probe_gcode.py's rules.")
        note.setStyleSheet("color:#6b7784;font-size:11px;border:none;")
        v.addWidget(note)

        self.rows = {}
        specs = (("nozzle", "Nozzle", "C", 0, 300, 220),
                 ("speed", "Speed", "%", 30, 200, 100),
                 ("flow", "Flow", "%", 50, 150, 100))
        for kind, label, unit, lo, hi, default in specs:
            row = ControlRow(kind, label, unit, lo, hi, default)
            row.sendRequested.connect(self._on_send)
            row.restoreRequested.connect(self._on_restore)
            self.rows[kind] = row
            v.addWidget(row)
        return card

    # ---------------- polling ----------------

    def _refresh_state(self) -> None:
        s = self.link.summary()

        conn = s["connection"]
        self.conn_pill.set(conn, CONN_TONE.get(conn, "muted"))
        self.host_label.setText(s["printer"] or "")

        self.tiles["state"].set_value(s["gcode_state"] or "-")
        self.tiles["layer"].set_value(
            f"{_fmt(s['layer_num'])}/{_fmt(s['total_layer_num'])}")
        self.tiles["progress"].set_value(f"{_fmt(s['mc_percent'])}%")
        self.tiles["left"].set_value(f"{_fmt(s['mc_remaining_time'])} min")
        self.tiles["nozzle"].set_value(
            f"{_fmt1(s['nozzle_temper'])}/{_fmt1(s['nozzle_target_temper'])}")
        self.tiles["bed"].set_value(
            f"{_fmt1(s['bed_temper'])}/{_fmt1(s['bed_target_temper'])}")

        self.info["gcode_file"][1].setText(s["gcode_file"] or "-")
        self.info["subtask_name"][1].setText(s["subtask_name"] or "-")
        self.info["speed"][1].setText(
            f"{_fmt(s['spd_lvl'])} ({_fmt(s['spd_mag'])}%)")
        # Only show the failure rows when there is a failure to show.
        for key in ("print_error", "fail_reason"):
            val = s[key]
            shown = bool(val)
            for w in self.info[key]:
                w.setVisible(shown)
            self.info[key][1].setText(str(val) if shown else "-")

        self._refresh_hms(s["hms"])

        now = time.time()
        for kind, watch in self.watches.items():
            r = verify.evaluate(watch, s, now)
            self.rows[kind].set_result(r.verdict.value, r.detail,
                                       verify.TONES[r.verdict])

    def _refresh_hms(self, codes) -> None:
        if len(codes) == len(self._hms_pills) and all(
                p.text() == c for p, c in zip(self._hms_pills, codes)):
            return
        while self._hms_pills:
            p = self._hms_pills.pop()
            self.hms_box.removeWidget(p)
            p.deleteLater()
        for c in codes:
            p = StatusPill(c, "danger")
            p.setToolTip("Look up at wiki.bambulab.com/en/x1/troubleshooting/"
                         "how-to-enter-hms-code")
            self.hms_box.addWidget(p)
            self._hms_pills.append(p)
        self.hms_none.setVisible(not codes)

    def _refresh_frame(self) -> None:
        info = frames.newest_frame(self.runs_dir)
        if info is None:
            self.camera.set_placeholder()
        else:
            self.camera.show_frame(info["path"], info["layer"], info["run"])

    # ---------------- commands ----------------

    def _on_send(self, kind: str, value: float) -> None:
        s = self.link.summary()
        if kind not in self.nominal:
            # Capture nominal BEFORE the first perturbation, so Restore has
            # something true to go back to. probe_gcode.py:124 always restores.
            self.nominal[kind] = self._nominal_for(kind, s)
            self.rows[kind].restore.setEnabled(True)
        self.link.send_gcode(verify.line_for(kind, value))
        self.watches[kind] = verify.Watch(kind, value, s, time.time())

    def _nominal_for(self, kind: str, s: dict) -> float:
        if kind == "nozzle":
            t = s.get("nozzle_target_temper")
            return float(t) if isinstance(t, (int, float)) else 220.0
        return 100.0  # speed and flow are percentages; nominal is 100

    def _on_restore(self, kind: str) -> None:
        v = self.nominal.get(kind)
        if v is None:
            return
        self.rows[kind].spin.setValue(v)
        self._on_send(kind, v)

    def closeEvent(self, event):
        self._state_timer.stop()
        self._frame_timer.stop()
        self.link.stop()
        super().closeEvent(event)
```

- [ ] **Step 2: Verify the window constructs and ticks headlessly**

Run:

```bash
QT_QPA_PLATFORM=offscreen python -c "
import tempfile, pathlib
from PyQt6.QtWidgets import QApplication
from basic_gui.link import MockLink
from basic_gui.window import MainWindow
app = QApplication([])
d = pathlib.Path(tempfile.mkdtemp())
link = MockLink(d)
w = MainWindow(link, d)
for _ in range(12):
    link._tick(0.25)
w._refresh_state(); w._refresh_frame()
print('window ok:', w.tiles['state']._v.text())
"
```

Expected: `window ok: RUNNING`

- [ ] **Step 3: Commit**

```bash
git add basic_gui/window.py
git commit -m "feat(basic_gui): main window, 4Hz state poll, 2s frame poll

Restore captures nominal before the first perturbation, the way
probe_gcode.py always restores after one."
```

---

### Task 9: `__main__.py` and README

**Files:**
- Create: `basic_gui/__main__.py`, `basic_gui/README.md`

- [ ] **Step 1: Write the CLI**

Create `basic_gui/__main__.py`:

```python
"""CLI entry point.

    python -m basic_gui --host 192.168.1.42 --serial 0309xxxxxxxx \
                        --access-code 12345678 [--runs-dir runs/]
    python -m basic_gui --mock [--runs-dir runs-mock-gui/]
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys

from PyQt6.QtWidgets import QApplication

from .link import LiveLink, MockLink
from .window import MainWindow

MOCK_RUNS = pathlib.Path("runs-mock-gui")
LIVE_RUNS = pathlib.Path("runs")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m basic_gui",
        description="Standalone PyQt6 monitor for the Bambu A1 mini.")
    p.add_argument("--host", help="printer IP")
    p.add_argument("--serial", help="printer serial")
    p.add_argument("--access-code", help="8-char LAN access code")
    p.add_argument("--mock", action="store_true",
                   help="fake printer; no hardware needed")
    p.add_argument("--runs-dir", type=pathlib.Path,
                   help=f"where capture.py writes frames "
                        f"(default {LIVE_RUNS}/, or {MOCK_RUNS}/ with --mock)")
    return p


def main(argv=None) -> int:
    parser = build_parser()
    a = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                        datefmt="%H:%M:%S")

    if a.mock:
        runs_dir = a.runs_dir or MOCK_RUNS
        link = MockLink(runs_dir)
    else:
        missing = [f"--{n.replace('_', '-')}"
                   for n in ("host", "serial", "access_code")
                   if not getattr(a, n)]
        if missing:
            parser.error(f"{', '.join(missing)} required without --mock "
                         f"(or pass --mock to run with no hardware)")
        runs_dir = a.runs_dir or LIVE_RUNS
        link = LiveLink(a.host, a.serial, a.access_code)

    runs_dir.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv[:1])
    link.start()
    win = MainWindow(link, runs_dir)
    win.resize(1060, 820)
    win.show()
    try:
        return app.exec()
    finally:
        link.stop()


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Verify the CLI's argument validation**

Run: `python -m basic_gui --host 1.2.3.4 2>&1 | tail -1`
Expected: an error naming the missing flags — `... --serial, --access-code required without --mock (or pass --mock to run with no hardware)`

Run: `python -m basic_gui --help`
Expected: usage text listing `--host --serial --access-code --mock --runs-dir`

- [ ] **Step 3: Write the README**

Create `basic_gui/README.md`:

```markdown
# basic_gui

A standalone PyQt6 desktop monitor for the A1 mini. Same live view as the web
dashboard in `server/` + `frontend/`, plus live parameter controls — but with
no server, no npm build, and no import from `server/`. It talks to the printer
over MQTT itself, via the unmodified `bambu_link.py`.

```bash
pip install -r ../requirements.txt

python -m basic_gui --host 192.168.1.42 --serial 0309xxxxxxxx --access-code 12345678
python -m basic_gui --mock          # no hardware, no server
```

Printer setup (LAN-only Mode **and** Developer Mode) is in the root README.
Without both, MQTT connects but never delivers a report.

## What it shows

Connection (ok / stale / disconnected) · state · layer · progress · time left ·
nozzle and bed temps · HMS codes · the newest frame `capture.py` wrote under
`runs/`. It never opens the webcam — Windows allows one process per camera
device and `capture.py` owns it.

## The controls, and why they hedge

`M104` (nozzle), `M220` (speed), `M221` (flow) — the same lines
`probe_gcode.py` sends. **The printer never acks.** Publishing always
"succeeds"; the printer may silently ignore you. Whether the A1 honours these
at all is exactly the open question `probe_gcode.py` exists to answer.

So each row reports what telemetry can actually support:

| Verdict | Meaning |
|---|---|
| `HONOURED` | Evidence observed |
| `PENDING` | Still watching (60 s window) |
| `NO_EVIDENCE` | Window elapsed, nothing seen. **Not** proof it refused |
| `UNVERIFIABLE` | No telemetry signal exists for this command |
| `NOTHING_TO_OBSERVE` | Commanded value ~= current, so no movement could tell honoured from ignored |

Evidence rules come from `probe_gcode.py:55-65`, not from field names:

- **M104** → `nozzle_temper` moving toward the command. *Not*
  `nozzle_target_temper` — the printer echoing a target it may not act on is
  not evidence.
- **M220** → `mc_remaining_time` rising. *Not* `spd_mag` — that is Bambu's
  speed profile, a different mechanism. Speed-*ups* and the S100 restore read
  `UNVERIFIABLE`: a honoured speed-up only makes the ETA fall faster, which is
  indistinguishable from the normal countdown.
- **M221** → nothing. Read it off the part.

**Restore** returns a row to its nominal value, captured before your first
send — `probe_gcode.py` always restores after a perturbation, and so should
you.

## Layout

| File | Job |
|---|---|
| `verify.py` | Pure verdict rules. Clock injected; no Qt, no I/O |
| `frames.py` | Newest `layer_NNNN.jpg` under a runs dir |
| `link.py` | `LiveLink` (real MQTT), `MockLink` (fake feed), `build_summary` |
| `widgets.py` | Presentation widgets |
| `window.py` | Layout + the 250 ms state poll and 2 s frame poll |
| `__main__.py` | CLI |

`--mock` simulates the README's middle outcome — M104/M220 honoured, M221
ignored — so every verdict is reachable with no printer.

## Tests

```bash
python -m pytest basic_gui -q
```

Pure logic only, no network and no Qt, matching `server/tests/`. Widgets are
verified by running `--mock`.
```

- [ ] **Step 4: Run the whole suite**

Run: `python -m pytest basic_gui -q`
Expected: PASS — 46 passed

- [ ] **Step 5: Commit**

```bash
git add basic_gui/__main__.py basic_gui/README.md
git commit -m "feat(basic_gui): CLI entry point and README"
```

---

### Task 10: Verify the exit criterion against `--mock`

The spec's exit criterion is a live check, not a unit test. Run it.

**Files:** none (verification only)

- [ ] **Step 1: Launch the mock GUI**

Run: `python -m basic_gui --mock`
Expected: a window opens; connection pill reads `ok`; printer reads `MOCK`.

- [ ] **Step 2: Watch a full lifecycle (about 80 s)**

Confirm, in order:
1. State goes `RUNNING`, layer ticks 1→30, progress climbs, ETA falls.
2. The camera panel fills with the growing synthetic print and the caption reads `Layer N - <ts>_mock_benchy`.
3. Around layers 12-16 an HMS pill `0300_0100_0001_0007` appears, then clears.
4. State reaches `FINISH`, then `IDLE`, then a new run starts.

- [ ] **Step 3: Exercise every verdict**

| Do this | Expect |
|---|---|
| Nozzle → `195`, Send | pill `PENDING` with a countdown, then `HONOURED` within ~4 s as the mock's temp ramps down |
| Nozzle → `220`, Send (while temp is ~220) | `NOTHING_TO_OBSERVE` |
| Flow → `60`, Send | `UNVERIFIABLE` immediately, detail points at the part |
| Speed → `50`, Send (while RUNNING) | `HONOURED` — the ETA jumps up |
| Speed → `150`, Send | `UNVERIFIABLE` — speed-ups leave no signature |
| Any row → Restore | spinbox returns to nominal and re-sends |

To see `NO_EVIDENCE`, send Nozzle `195` and then immediately Nozzle `220`
(the mock ramps back, so no movement toward 195 accrues); after 60 s the row
settles to `NO_EVIDENCE` — **not** `IGNORED`.

- [ ] **Step 4: Confirm mock frames landed in the right place**

Run: `ls runs-mock-gui/*/frames | head -3 && git status --short`
Expected: JPEGs listed, and `git status` clean — `runs-mock-gui/` is ignored.

- [ ] **Step 5: Close the window**

Expected: the process exits cleanly with no traceback (timers stop, `link.stop()` joins the thread).

- [ ] **Step 6: Commit any fixes found**

If steps 1-5 surfaced bugs, fix them, re-run `python -m pytest basic_gui -q`, and commit. If nothing needed fixing, there is nothing to commit — say so rather than inventing a commit.

---

## Notes for whoever runs this

- **Do not** import from `server/`. Standalone is the point: another session is actively editing that package.
- **Do not** "improve" the verdict rules to check `nozzle_target_temper` or `spd_mag`. They look correct and are not — see the Background section. `test_nozzle_target_temper_alone_never_proves_honoured` guards this.
- **Do not** add an `IGNORED` verdict.
- The real-hardware half of the exit criterion (running against a printer during an actual print alongside `capture.py`) needs the user's hardware and is theirs to run.
