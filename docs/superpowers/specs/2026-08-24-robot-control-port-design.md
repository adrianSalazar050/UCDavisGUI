# Robot control: porting the xArm plate handler onto the current dashboard — design

> **STATUS: SHIPPED (2026-08-24).** Written before implementation, and the
> port landed the same day as designed. Three defects surfaced during
> verification and were fixed beyond the straight port — all three are
> pre-existing on `integration/robot-control`:
>
> 1. `RobotManager.camera_frame()` called an OPTIONAL backend method
>    unconditionally, so `GET /api/robot/camera/frame` was a 500
>    `AttributeError` under `--robot-mode mock` instead of the documented 404.
>    `configure_camera` had the same hole. Both now check `hasattr`; regression
>    tests in `test_robot.py`.
> 2. Jog dispatch guarded on `busy`/`submitting`, which are React *state* and
>    stay stale for a whole render — several clicks in one tick all passed, so
>    the first got a 202 and the rest a 409. Now a ref is claimed
>    synchronously.
> 3. Even with (2), `run()` resolves at **202 accepted, not completed**, and
>    `busy` lags a WebSocket poll. The slot is now held until our command id
>    appears as `last_command` with no `active_command`. Verified: three rapid
>    5 mm clicks apply exactly 15 mm, zero 409s.
>
> See `master.md` §16, and §16.5 for the verified/unverified split.
>
> This is a **port**, not a new feature: the robot control stack already exists
> on the branch `integration/robot-control` (three commits by fer4036,
> `813edb6`, `1cd6a48`, `94c0fa4`) and this document records how it is
> reconciled with `main`/`adrian`, which has moved 116 commits since the fork
> point.
>
> Approved by user 2026-08-24 after a four-question design pass.

---

## 1. What is being ported, and from where

`integration/robot-control` is `feature/robot-backend` plus exactly three
commits. `feature/robot-backend` itself contains nothing robot-specific — its
head, `724db7a`, is simply the merge base with the current branch, so **only
the three commits below carry robot code**:

| Commit | Adds |
|---|---|
| `813edb6` "integrate robot controls" | `server/robot.py` (RobotManager, MockRobotBackend, RosRobotBackend), three API routes, the WS payload change, `frontend/src/pages/Robot.jsx`, `frontend/src/api/robot.js`, CSS, `server/tests/test_robot.py` |
| `1cd6a48` "support physical xArm 6 backend" | `xarm6` added to `--robot-type`; the gripper-hardware guard extended to `gripper_open`/`gripper_close` and made robot-agnostic in its message |
| `94c0fa4` "add robot vision controls" | Camera enumeration + selection, MJPEG frame route, ArUco detector readouts, teach mode, `scan_location` |

Nothing else on those branches is wanted. `feature/robot-backend` contributes
no code to this port.

## 2. The constraint that shapes everything

`RosRobotBackend` imports `rclpy` and `ar4_automation` from a **separate
repository** (`ar4Automating3DPrinter`, located via the `AR4_AUTOMATION_REPO`
environment variable or `--robot-repo`). That is ROS 2 Humble + MoveIt, on
Linux.

The development machine for this repo is Windows, running numpy 2.2.6 and
opencv 4.13. The branch pins `numpy>=1.26,<2` and `opencv-python>=4.9,<4.12`
for a real reason — ROS Humble's `cv_bridge` is built against the NumPy 1.x
ABI and fails at import under NumPy 2.

**Consequence: the ROS path cannot be executed or tested on this machine at
all.** Only `--robot-mode mock` can run here. The port proceeds anyway,
faithfully, because the ROS backend is the whole point of the feature on the
machine that has the arm. Every ROS import is already lazy — it happens inside
`RosRobotBackend.__init__`, never at module import — so `server/robot.py`
imports cleanly on Windows and the mock path is unaffected.

This is recorded the same way `master.md` §1.1 already separates "verified on
the A1 mini" from "verified on the A1": the documentation must say which
claims were actually exercised and which were carried across untested.

## 3. Architecture (unchanged from the branch — it already fits)

The design is kept as-is because it matches conventions this repo already has.

**`RobotManager`** owns one backend and one worker thread, fed by a 1-deep
`queue.Queue`. Motion is serialized: while a command is active, a second
`submit()` raises `RobotBusy` (HTTP 409). The manager holds a lock over its
own state (`_state`, `_active`, `_last`, `_error`) and never holds it across a
backend call.

**Backends are duck-typed**, not a class hierarchy: `execute(action, params)`,
`cancel()`, `telemetry()`, `close()`, plus optional `camera_frame()` and
`configure_camera(index)`. Two implementations:

- `MockRobotBackend` — pure Python, no dependencies, tracks joints and pose in
  memory. This is what the test suite and the GUI development loop use.
- `RosRobotBackend` — adapts the command contract onto `printerAutomation`
  methods (`go_home`, `move_to_configuration`, `move_to_pose`, `scanToMarker`,
  `pickupPlate`, `placePlate`, `transferPlate`, `scrapePlate`,
  `scanLocationForMarkers`, gripper open/close, xArm teach mode). It
  deliberately does **not** reimplement retries, TF checks or marker handling —
  the automation package remains the single source of truth for those.

**`normalize_command(action, parameters)`** is a pure function at the
validation boundary. It rejects an unknown action, a wrong-length vector, a
non-numeric field, an out-of-range `viewing_distance` (0.05–0.50 m) or an
oversized jog (0.05 m translation / 0.2618 rad rotation) **before any MoveIt
goal exists**. Because it is pure, the whole validation surface is testable
with no hardware and no ROS.

**Two-layer safety.** `RosRobotBackend.execute` calls
`node.assert_motion_safe()` before dispatch, and separately
`_require_camera_ready()` before any vision-dependent motion and
`_require_manipulation_hardware()` before any gripper motion. These are
*early, readable* failures for the GUI; the automation package checks again at
every low-level move. The guard exists so a physical pick can never report
success through a no-op gripper.

**"None means inert."** `create_app(robot=None)` disables every robot route,
exactly as `queue`, `detection`, `slicer`, `ledger` and `auth` already work.
The default is `--robot-mode disabled`, so an existing deployment that pulls
this change gets byte-identical behaviour.

## 4. Reconciling with 116 commits of drift

Three places where the branch cannot simply be merged.

### 4.1 `create_app` lifespan

The branch does a naive `robot.start()` on entry and `robot.stop()` in
`finally`. The current lifespan uses a `started` list with reverse-order
teardown, written specifically so that a raise from a later component's
`start()` cannot strand an earlier one. The robot **joins that list** rather
than reintroducing the bug the list was written to fix:

```python
if robot is not None:
    robot.start()
    started.append(robot)
```

`RobotManager` already exposes `start()`/`stop()`, so it satisfies the
existing protocol with no adapter.

### 4.2 The WebSocket payload

The branch replaces `{"printers": [...]}` with `{"printers": [...], "robot":
{...}}` via `_live_payload()` / `_live_comparable()` helpers. Since the fork,
`/ws` has gained an auth-cookie check (WebSockets bypass the HTTP middleware,
so it is checked in the handler) and now composes three decorators:
`_with_detection(_with_nozzle(_with_bed_type(registry.summaries(), registry),
registry), detection)`.

The payload change is re-applied **on top of** that composition, preserving
both. `_live_comparable` keeps `_comparable`'s existing job of ignoring
`report_age_s` so the change-detection heartbeat is not defeated.

The `robot` key is emitted **only when a robot is configured**, so the
frontend can distinguish "no robot on this server" (key absent, `robot` stays
`null`) from "robot present but unavailable" (key present,
`available: false`). Those need different messages in the UI.

### 4.3 The page registry

The branch's entry is `robot: { title: "Robot", group: "Control", component:
Robot }` — written when the registry was a flat four-page list. It now
requires `title`, `description`, `group`, `scope`, `component`, and groups are
ordered by an explicit `GROUP_ORDER` so a misspelled group is visible rather
than silently reshuffling the sidebar.

- `"Control"` is **added to `GROUP_ORDER`, last**, so it renders deliberately
  instead of falling through the "group we don't know about" branch.
- `scope: "fleet"`. One arm serves the whole lab; it must not appear to follow
  the topbar printer switcher. This makes the topbar badge it "Fleet-wide",
  which is the honest label.

## 5. Component-by-component change list

### New files

| File | Purpose |
|---|---|
| `server/robot.py` | RobotManager, MockRobotBackend, RosRobotBackend, `normalize_command`, `list_video_devices` |
| `server/tests/test_robot.py` | Validation, serialization, busy/cancel, route contract |
| `frontend/src/api/robot.js` | Six fetch wrappers, sharing printer.js's `detail()` error-flattening shape |
| `frontend/src/api/robot.test.js` | Vitest coverage of the API client |
| `frontend/src/pages/Robot.jsx` | The page |
| `requirements-robot.txt` | ROS-machine-only pins |

### Modified files

| File | Change |
|---|---|
| `server/main.py` | `robot=None` param; lifespan entry; six routes; WS payload |
| `server/__main__.py` | Four CLI flags, camera env vars, `create_app(robot=...)` |
| `frontend/src/hooks/usePrinters.js` | Return `{printers, robot, wsUp}` |
| `frontend/src/App.jsx` | Pass `robot`/`wsUp` to every page |
| `frontend/src/app/pageRegistry.jsx` | `robot` entry; `"Control"` in `GROUP_ORDER`; update the props-contract comment |
| `frontend/src/styles.css` | The robot CSS block |
| `master.md` | New §16; edits to §3.2, §7.2, §8 |

### Routes

All six sit under `/api/`, so the existing `_require_session` middleware
protects them with no additional work — including the MJPEG frame route.

| Method | Path | Behaviour |
|---|---|---|
| GET | `/api/robot/status` | `snapshot()` — state, active/last command, telemetry |
| POST | `/api/robot/commands` | 202 + command record; 400 invalid, 409 busy, 503 unavailable |
| POST | `/api/robot/commands/{id}/cancel` | 404 if that command is not active |
| GET | `/api/robot/cameras` | V4L2 device list (empty on non-Linux) |
| PUT | `/api/robot/camera` | Select or disable the webcam |
| GET | `/api/robot/camera/frame` | JPEG, `Cache-Control: no-store`; 404 when no frame |

### CLI

```
--robot-mode {disabled,mock,ros}   default: disabled
--robot-type {xarm6,ar4,lite6}     default: xarm6   <-- changed from the branch
--robot-sim                        connect to Gazebo topics
--robot-repo PATH                  default $AR4_AUTOMATION_REPO or ~/ar4Automating3DPrinter
```

The default robot type changes from the branch's `ar4` to `xarm6`, because the
xArm 6 is the arm actually installed in this lab. `ar4` and `lite6` remain
selectable. `XARM_CAMERA_MODE` / `XARM_CAMERA_INDEX` seed the initial camera
configuration; a `webcam` mode with no index degrades to `disabled` rather
than crashing at startup.

## 6. Dependencies

`requirements.txt` is **left untouched**. The ROS pins go in a new
`requirements-robot.txt`, following the precedent this repo already set with
`requirements-desktop.txt`:

```
-r requirements.txt
# ROS 2 Humble's cv_bridge is built against the NumPy 1.x API.
numpy>=1.26,<2
opencv-python>=4.9,<4.12
transforms3d>=0.4.2
```

Tightening the base file instead would force a NumPy downgrade on every
install — including this Windows box and the ultralytics training stack — to
satisfy a constraint that only ever applies on the ROS machine.

## 7. Testing

**Python** (`server/tests/test_robot.py`): `normalize_command` accepts valid
joint/pose/marker/jog/scan commands and rejects unknown actions, wrong-length
vectors, non-numeric values, out-of-range viewing distances, bad jog axes,
zero jogs and oversized jogs. `RobotManager` executes one command and reports
telemetry, refuses a concurrent submit with `RobotBusy`, records the last
command, and cancels an active one. Route-level tests through `TestClient`
cover the status/submit/cancel contract and confirm that `robot=None` yields
404 on every robot route.

**JavaScript** (`frontend/src/api/robot.test.js`): each wrapper hits the right
URL with the right method and body, and a non-ok response surfaces the
FastAPI `detail` string rather than `[object Object]`.

**Manual gate on this machine**: boot `python -m server --mock --robot-mode
mock`, confirm the Robot page renders, the arm reports Ready, Home executes,
joint and pose goals move the mock telemetry, jog coalescing works, and a
second command during an active one is refused.

**Not verified here, and documented as such**: anything requiring `rclpy` —
backend startup, MoveIt planning, ArUco detection, gripper actuation, teach
mode, and plate pickup on the physical xArm 6.

## 8. Out of scope

The ported Robot page is **standalone**. Marker IDs are typed by hand, and
nothing connects a finished print to an automatic plate pickup — no queue
hook, no run-completion trigger, no per-printer marker mapping.

Wiring the arm into the print lifecycle (queue → print finishes → robot clears
the plate → next job starts) is the obvious next feature, and it exists on
neither branch. Inventing it here would be widening the port into a feature
design that has had no requirements pass. It is called out in §16 of
`master.md` as the named next step rather than silently omitted.

## 9. Risks

| Risk | Mitigation |
|---|---|
| ROS backend has drifted against the automation repo's current API | Ported verbatim; any mismatch surfaces at `--robot-mode ros` startup as a clear import/attribute error, not silently |
| `list_video_devices()` reads `/sys/class/video4linux` | Returns `[]` on Windows rather than raising; the camera card then shows only "Disabled" |
| A stuck backend call blocks the worker thread forever | `stop()` joins with a 5 s timeout and the thread is a daemon, so shutdown cannot hang the server |
| WS payload growth on every poll | `_live_comparable` keeps change detection working, so an idle robot does not defeat the existing heartbeat throttling |
