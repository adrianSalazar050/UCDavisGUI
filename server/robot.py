"""Serialized robot command execution with optional ROS 2 backing.

The web server never talks to MoveIt directly.  RobotManager accepts one
high-level command at a time and runs it on a dedicated worker thread.  The
ROS backend imports and reuses printerAutomation from ar4Automating3DPrinter,
so its retries, TF checks, marker handling, gripper sequencing, teach-mode
controller handover, and hand-eye calibration remain the single source of
truth.
"""
from __future__ import annotations

import copy
import pathlib
import queue
import sys
import threading
import time
import uuid
from typing import Any, Callable


ACTIONS = {
    "home",
    "move_joints",
    "move_pose",
    "jog_pose",
    "scan_marker",
    "pickup",
    "place",
    "transfer",
    "scrape",
    "gripper_open",
    "gripper_close",
    "scan_location",
    "teach_enable",
    "teach_disable",
    # Vision commissioning (ar4_automation.vision_commissioning). These read
    # the camera and the current TF; only goto_observation moves the arm.
    "calibration_start",
    "calibration_stop",
    "calibration_capture",
    "calibration_discard",
    "calibration_solve",
    "intrinsic_start",
    "intrinsic_stop",
    "intrinsic_capture",
    "intrinsic_discard",
    "intrinsic_solve",
    "save_observation",
    "goto_observation",
    "confirm_marker",
}

# Vision actions that read the camera and the current TF but never move the
# arm, so they must NOT go through assert_motion_safe(). Teach mode hands
# trajectory ownership away from ros2_control, which makes the
# trajectory-controller interlock legitimately red -- and hand-guiding the
# wrist is exactly when samples and observation poses get taken.
VISION_STATE_ACTIONS = {
    "calibration_start",
    "calibration_stop",
    "calibration_capture",
    "calibration_discard",
    "calibration_solve",
    "intrinsic_start",
    "intrinsic_stop",
    "intrinsic_capture",
    "intrinsic_discard",
    "intrinsic_solve",
    "save_observation",
    "confirm_marker",
}

# Hand-eye calibration writes calibration/<robot>_hand_eye.json, which the
# automation package loads on the next physical start. A Gazebo camera is
# mounted perfectly by construction, so solving there would only overwrite a
# real measurement with a meaningless one.
PHYSICAL_ONLY_ACTIONS = {
    "teach_enable",
    "teach_disable",
    "calibration_start",
    "calibration_stop",
    "calibration_capture",
    "calibration_discard",
    "calibration_solve",
    "intrinsic_start",
    "intrinsic_stop",
    "intrinsic_capture",
    "intrinsic_discard",
    "intrinsic_solve",
}

# Free-form in the automation package; constrained here so the browser cannot
# scatter typos through a file an operator has to read back later.
MARKER_ROLES = {"printer", "box", "scrape", "other"}

# Configurable digital outputs on the xArm control box. Both the AC and the DC
# box expose "8xCO+8xDO" (UFACTORY technical specifications), and it is the CO
# block a gripper is wired into -- so a selectable output is CO0..CO7.
# Widening this to reach the DO block is a one-line change here, but only make
# it against a measured pin-out: driving the wrong output on a live cell is not
# a mistake the software can detect.
XARM_CO_COUNT = 8


class RobotBusy(RuntimeError):
    pass


class RobotUnavailable(RuntimeError):
    pass


class RobotCommandError(RuntimeError):
    pass


def _number_list(value: Any, name: str, length: int) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise RobotCommandError(f"{name} must contain exactly {length} numbers")
    try:
        return [float(v) for v in value]
    except (TypeError, ValueError):
        raise RobotCommandError(f"{name} must contain only numbers") from None


def _integer(params: dict, name: str) -> int:
    value = params.get(name)
    if isinstance(value, bool):
        raise RobotCommandError(f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RobotCommandError(f"{name} must be an integer") from None


def _name(params: dict, name: str = "name") -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RobotCommandError(f"{name} must be a non-empty string")
    value = value.strip()
    if len(value) > 64:
        raise RobotCommandError(f"{name} must be at most 64 characters")
    return value


def _role(params: dict) -> str:
    value = params.get("role", "other")
    if value not in MARKER_ROLES:
        raise RobotCommandError(
            "role must be one of " + ", ".join(sorted(MARKER_ROLES)))
    return value


def _flag(params: dict, name: str) -> bool:
    value = params.get(name, False)
    if not isinstance(value, bool):
        raise RobotCommandError(f"{name} must be true or false")
    return value


def normalize_command(action: str, parameters: dict | None) -> tuple[str, dict]:
    """Validate and normalize the browser-facing command contract."""
    if action not in ACTIONS:
        raise RobotCommandError(
            f"unknown robot action '{action}'; expected one of "
            + ", ".join(sorted(ACTIONS)))
    p = dict(parameters or {})

    if action == "move_joints":
        return action, {"joints": _number_list(p.get("joints"), "joints", 6)}
    if action == "move_pose":
        return action, {
            "position": _number_list(p.get("position"), "position", 3),
            "euler": _number_list(p.get("euler"), "euler", 3),
        }
    if action == "jog_pose":
        axis = str(p.get("axis", "")).lower()
        if axis not in {"x", "y", "z", "roll", "pitch", "yaw"}:
            raise RobotCommandError(
                "axis must be x, y, z, roll, pitch, or yaw")
        try:
            delta = float(p.get("delta"))
        except (TypeError, ValueError):
            raise RobotCommandError("delta must be a number") from None
        limit = 0.05 if axis in {"x", "y", "z"} else 0.2618
        if delta == 0 or abs(delta) > limit:
            unit = "metres" if axis in {"x", "y", "z"} else "radians"
            raise RobotCommandError(
                f"delta for {axis} must be non-zero and at most "
                f"{limit:g} {unit}")
        return action, {"axis": axis, "delta": delta}
    if action == "scan_location":
        try:
            viewing_distance = float(p.get("viewing_distance", 0.15))
        except (TypeError, ValueError):
            raise RobotCommandError("viewing_distance must be a number") from None
        if not 0.05 <= viewing_distance <= 0.50:
            raise RobotCommandError(
                "viewing_distance must be between 0.05 and 0.50 metres")
        return action, {
            "position": _number_list(p.get("position"), "position", 3),
            "euler": _number_list(p.get("euler"), "euler", 3),
            "viewing_distance": viewing_distance,
        }
    if action in {"scan_marker", "pickup", "place"}:
        out = {"marker_id": _integer(p, "marker_id")}
        if action == "scan_marker":
            try:
                out["viewing_distance"] = float(p.get("viewing_distance", 0.20))
            except (TypeError, ValueError):
                raise RobotCommandError(
                    "viewing_distance must be a number") from None
            if not 0.05 <= out["viewing_distance"] <= 0.50:
                raise RobotCommandError(
                    "viewing_distance must be between 0.05 and 0.50 metres")
        return action, out
    if action == "transfer":
        return action, {
            "source_id": _integer(p, "source_id"),
            "dest_id": _integer(p, "dest_id"),
            "rescan_id": _integer(p, "rescan_id"),
        }
    if action == "scrape":
        return action, {
            "source_id": _integer(p, "source_id"),
            "scrape_id": _integer(p, "scrape_id"),
        }
    if action == "calibration_start":
        return action, {"clear": _flag(p, "clear")}
    if action == "intrinsic_start":
        return action, {"clear": _flag(p, "clear")}
    if action == "save_observation":
        # marker_id is optional: an observation pose may just be a good place
        # to look from, with no marker chosen for it yet.
        raw = p.get("marker_id")
        return action, {
            "name": _name(p),
            "role": _role(p),
            "marker_id": None if raw is None or raw == ""
                         else _integer(p, "marker_id"),
        }
    if action == "goto_observation":
        return action, {"name": _name(p)}
    if action == "confirm_marker":
        return action, {"marker_id": _integer(p, "marker_id"),
                        "role": _role(p)}
    return action, {}


def _jsonable(value: Any) -> Any:
    """Convert numpy/ROS-adjacent values into JSON-safe builtins."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "tolist"):
        return _jsonable(value.tolist())
    return str(value)


class RobotManager:
    """Own one backend and serialize all potentially dangerous motion."""

    def __init__(self, backend_factory: Callable[[], Any], camera_config=None,
                 gripper_config=None):
        self._backend_factory = backend_factory
        self._backend = None
        self._lock = threading.Lock()
        self._commands: queue.Queue = queue.Queue(maxsize=1)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "stopped"
        self._error: str | None = None
        self._active: dict | None = None
        self._last: dict | None = None
        self._cancel_requested = False
        self.camera_config = camera_config
        self.gripper_config = gripper_config

    def configure_gripper(self, output: int) -> None:
        """Point the gripper at a different controller output (CO0..CO7)."""
        if isinstance(output, bool) or not isinstance(output, int):
            raise RobotCommandError("gripper output must be an integer")
        if not 0 <= output < XARM_CO_COUNT:
            raise RobotCommandError(
                f"gripper output must be between CO0 and CO{XARM_CO_COUNT - 1}")
        with self._lock:
            backend = self._backend
            if self._active is not None:
                raise RobotBusy(
                    "cannot change the gripper output while a command is active")
        if backend is None:
            raise RobotUnavailable("robot backend is not ready")
        if not hasattr(backend, "configure_gripper"):
            raise RobotUnavailable(
                "this robot backend has no selectable gripper output")
        backend.configure_gripper(output)
        if self.gripper_config is not None:
            self.gripper_config["output"] = output

    def configure_camera(self, index: int | None) -> None:
        if self.camera_config is None:
            raise RobotUnavailable("camera reconfiguration is unavailable")
        if index is not None and (isinstance(index, bool) or index < 0):
            raise RobotCommandError("camera index must be a non-negative integer")
        with self._lock:
            backend = self._backend
            if self._active is not None:
                raise RobotBusy("cannot change camera while a command is active")
        if backend is None:
            raise RobotUnavailable("robot backend is not ready")
        if not hasattr(backend, "configure_camera"):
            raise RobotUnavailable(
                "this robot backend has no camera to configure")
        backend.configure_camera(index)
        self.camera_config["mode"] = "disabled" if index is None else "webcam"
        self.camera_config["index"] = index

    def camera_frame(self) -> bytes | None:
        """-> the latest JPEG, or None when there isn't one.

        camera_frame() is an OPTIONAL half of the backend contract: only
        RosRobotBackend has a camera. Calling through unconditionally made
        GET /api/robot/camera/frame raise AttributeError -- a 500 -- under
        --robot-mode mock, which is the mode the Robot page is developed
        against. "No camera on this backend" and "no frame captured yet" are
        the same answer to the browser, and the route turns None into the 404
        that says it.
        """
        with self._lock:
            backend = self._backend
        if backend is None or not hasattr(backend, "camera_frame"):
            return None
        return backend.camera_frame()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._state = "starting"
            self._error = None
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="robot-command-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.cancel()
        except Exception:
            pass
        try:
            self._commands.put_nowait(None)
        except queue.Full:
            pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
        backend = self._backend
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
        with self._lock:
            self._state = "stopped"

    def submit(self, action: str, parameters: dict | None = None) -> dict:
        action, parameters = normalize_command(action, parameters)
        with self._lock:
            if self._state == "starting":
                raise RobotUnavailable("robot is still starting")
            if self._state in {"error", "stopped"} or self._backend is None:
                raise RobotUnavailable(self._error or "robot is unavailable")
            if self._active is not None:
                raise RobotBusy(
                    f"robot is already running command {self._active['id']}")
            command = {
                "id": uuid.uuid4().hex,
                "action": action,
                "parameters": parameters,
                "state": "queued",
                "submitted_at": time.time(),
                "started_at": None,
                "finished_at": None,
                "result": None,
                "error": None,
            }
            self._active = command
            self._cancel_requested = False
            self._state = "queued"
        self._commands.put_nowait(command)
        return copy.deepcopy(command)

    def cancel(self, command_id: str | None = None) -> bool:
        with self._lock:
            command = self._active
            if command is None:
                return False
            if command_id is not None and command["id"] != command_id:
                return False
            self._cancel_requested = True
            command["state"] = "cancelling"
            backend = self._backend
        if backend is not None:
            backend.cancel()
        return True

    def snapshot(self) -> dict:
        with self._lock:
            state = self._state
            error = self._error
            active = copy.deepcopy(self._active)
            last = copy.deepcopy(self._last)
            backend = self._backend
        telemetry = {}
        if backend is not None:
            try:
                telemetry = backend.telemetry()
            except Exception as exc:
                telemetry = {"telemetry_error": str(exc)}
        return {
            "available": backend is not None and state not in {"error", "stopped"},
            "state": state,
            "active_command": active,
            "last_command": last,
            "error": error,
            **_jsonable(telemetry),
        }

    def _run(self) -> None:
        try:
            backend = self._backend_factory()
        except Exception as exc:
            with self._lock:
                self._state = "error"
                self._error = f"robot startup failed: {exc}"
            return
        with self._lock:
            self._backend = backend
            self._state = "idle"

        while not self._stop.is_set():
            try:
                command = self._commands.get(timeout=0.25)
            except queue.Empty:
                continue
            if command is None:
                break
            with self._lock:
                command["state"] = "executing"
                command["started_at"] = time.time()
                self._state = "executing"
            try:
                result = backend.execute(
                    command["action"], command["parameters"])
                if result is False:
                    raise RobotCommandError(
                        f"{command['action']} reported failure")
                with self._lock:
                    command["result"] = _jsonable(result)
                    command["state"] = (
                        "cancelled" if self._cancel_requested else "succeeded")
            except Exception as exc:
                with self._lock:
                    command["state"] = (
                        "cancelled" if self._cancel_requested else "failed")
                    command["error"] = str(exc)
            finally:
                with self._lock:
                    command["finished_at"] = time.time()
                    self._last = copy.deepcopy(command)
                    self._active = None
                    self._cancel_requested = False
                    self._state = "idle"
                self._commands.task_done()


# Enough of a workspace for the Robot page's marker and commissioning panels
# to be reviewable under --robot-mode mock, which on a machine with no ROS is
# the only mode that runs at all (master.md section 16.7).
MOCK_MARKERS = [
    {"id": 0, "dict_name": "DICT_4X4_50", "estimated": False,
     "marker_size": 0.03, "distanceFromCamera": 0.243,
     "positionInWorld": [0.322, -0.181, 0.210],
     "orientInWorld": {"roll": 0.4, "pitch": -1.1, "yaw": 179.6}},
    {"id": 2, "dict_name": "DICT_6X6_50", "estimated": True,
     "marker_size": 0.025, "distanceFromCamera": 0.0,
     "positionInWorld": [0.601, 0.104, 0.210],
     "orientInWorld": {"roll": 0.0, "pitch": 0.0, "yaw": 270.0}},
]


class MockRobotBackend:
    """Hardware-free backend for API/frontend development."""

    def __init__(self, delay_s: float = 0.02):
        self.delay_s = delay_s
        self.joints = [0.0] * 6
        self.pose = [0.35, 0.0, 0.30, 0.0, 0.0, 0.0]
        self.cancelled = False
        self.teaching = False
        self.gripper_command = None
        self.gripper_output = 0
        self.vision = {
            "session_active": False,
            "sample_count": 0,
            "recommended_sample_count": 15,
            "last_detection": {"valid": False, "corner_count": 0,
                               "error": "no camera on the mock backend"},
            "observation_poses": [],
            "marker_roles": {},
            "hand_eye": None,
            "last_solve": None,
            "intrinsic": {
                "session_active": False, "sample_count": 0,
                "recommended_sample_count": 20,
                "last_detection": {"valid": False, "corner_count": 0,
                                   "error": "no camera on the mock backend"},
                "last_solve": None,
            },
            "state_path": "<mock>/data/vision_commissioning.json",
            "calibration_path": "<mock>/calibration/mock_hand_eye.json",
        }

    def execute(self, action: str, parameters: dict) -> dict:
        self.cancelled = False
        deadline = time.monotonic() + self.delay_s
        while time.monotonic() < deadline:
            if self.cancelled:
                return {"cancelled": True}
            time.sleep(0.005)
        if action == "move_joints":
            self.joints = list(parameters["joints"])
        elif action == "home":
            self.joints = [0.0] * 6
        elif action == "move_pose":
            self.pose = list(parameters["position"]) + list(parameters["euler"])
        elif action == "jog_pose":
            axes = ("x", "y", "z", "roll", "pitch", "yaw")
            self.pose[axes.index(parameters["axis"])] += parameters["delta"]
        elif action in {"teach_enable", "teach_disable"}:
            self.teaching = action == "teach_enable"
        elif action in {"gripper_open", "gripper_close"}:
            self.gripper_command = action.removeprefix("gripper_")
        elif action in VISION_STATE_ACTIONS or action == "goto_observation":
            return self._vision_command(action, parameters)
        return {"mock": True, "action": action}

    def _vision_command(self, action: str, p: dict) -> dict:
        """Mirror VisionCommissioning closely enough to drive the page."""
        state = self.vision
        if action == "calibration_start":
            if p["clear"]:
                state["sample_count"] = 0
            state["session_active"] = True
            state["last_detection"] = {"valid": True, "corner_count": 42,
                                       "error": None}
        elif action == "calibration_stop":
            state["session_active"] = False
        elif action == "calibration_capture":
            if not state["session_active"]:
                raise RobotCommandError("start a calibration session first")
            state["sample_count"] += 1
        elif action == "calibration_discard":
            if not state["sample_count"]:
                raise RobotCommandError("there are no calibration samples")
            state["sample_count"] -= 1
        elif action == "calibration_solve":
            if state["sample_count"] < 8:
                raise RobotCommandError(
                    "At least 8 captures are required (15-25 recommended)")
            passed = state["sample_count"] >= 15
            state["last_solve"] = {
                "method": "PARK", "sample_count": state["sample_count"],
                "quality_passed": passed,
                "translation_m": [0.041, -0.013, 0.072],
                "rpy_rad": [0.02, -1.55, 0.01],
                "metrics": {"translation_rmse_m": 0.004 if passed else 0.031,
                            "rotation_rmse_deg": 1.2 if passed else 6.4},
            }
            if passed:
                state["hand_eye"] = dict(state["last_solve"])
            return dict(state["last_solve"])
        elif action == "intrinsic_start":
            if p["clear"]:
                state["intrinsic"]["sample_count"] = 0
            state["intrinsic"]["session_active"] = True
            state["intrinsic"]["last_detection"] = {
                "valid": True, "corner_count": 42, "error": None}
        elif action == "intrinsic_stop":
            state["intrinsic"]["session_active"] = False
        elif action == "intrinsic_capture":
            if not state["intrinsic"]["session_active"]:
                raise RobotCommandError("start an intrinsic calibration session first")
            state["intrinsic"]["sample_count"] += 1
        elif action == "intrinsic_discard":
            if not state["intrinsic"]["sample_count"]:
                raise RobotCommandError("there are no intrinsic calibration captures")
            state["intrinsic"]["sample_count"] -= 1
        elif action == "intrinsic_solve":
            intrinsic = state["intrinsic"]
            if intrinsic["sample_count"] < 12:
                raise RobotCommandError("at least 12 intrinsic captures are required")
            passed = intrinsic["sample_count"] >= 20
            intrinsic["last_solve"] = {
                "sample_count": intrinsic["sample_count"], "rms_px": 0.42 if passed else 1.37,
                "max_rms_px": 1.0, "quality_passed": passed,
            }
            return dict(intrinsic["last_solve"])
        elif action == "save_observation":
            item = {"name": p["name"], "role": p["role"],
                    "marker_id": p["marker_id"], "robot": "mock",
                    "timestamp": time.time(),
                    "joint_positions": list(self.joints),
                    "eef_pose": {"good_position": self.pose[:3],
                                 "good_euler": self.pose[3:],
                                 "frame": "good"}}
            state["observation_poses"] = [
                x for x in state["observation_poses"]
                if x.get("name") != p["name"]] + [item]
            return item
        elif action == "goto_observation":
            item = next((x for x in state["observation_poses"]
                         if x.get("name") == p["name"]), None)
            if item is None:
                raise RobotCommandError(
                    f"no observation pose named '{p['name']}' has been saved")
            self.joints = list(item["joint_positions"])
            return {"mock": True, "action": action, "name": p["name"]}
        elif action == "confirm_marker":
            marker = next((m for m in MOCK_MARKERS
                           if m["id"] == p["marker_id"]), None)
            if marker is None or marker["estimated"]:
                raise RobotCommandError(
                    f"ArUco {p['marker_id']} has not been measured by the camera")
            state["marker_roles"][str(p["marker_id"])] = p["role"]
            return {"marker_id": p["marker_id"], "role": p["role"],
                    "saved": True}
        return {"mock": True, "action": action,
                "sample_count": state["sample_count"]}

    def cancel(self) -> None:
        self.cancelled = True

    def telemetry(self) -> dict:
        return {
            "robot": "mock",
            "joints": list(self.joints),
            "eef_pose": {"xyz_rpy": list(self.pose), "frame": "good"},
            "markers": copy.deepcopy(MOCK_MARKERS),
            "teaching": self.teaching,
            "vision": {"available": True, **copy.deepcopy(self.vision)},
            "gripper": {"kind": "cgpio", "disabled": False, "sensed": False,
                        "output": f"CO{self.gripper_output}",
                        "ionum": self.gripper_output,
                        "output_count": XARM_CO_COUNT,
                        "command": self.gripper_command},
        }

    def configure_gripper(self, output: int) -> None:
        if self.gripper_command == "close":
            raise RobotCommandError(
                "open the gripper before changing its output; the current one "
                "stays latched and would be left holding")
        self.gripper_output = int(output)
        self.gripper_command = None

    def close(self) -> None:
        pass


class RosRobotBackend:
    """Adapter from the command contract to printerAutomation methods."""

    def __init__(self, repo_path: pathlib.Path, robot: str = "ar4",
                 sim: bool = False, camera_mode=None, camera_index=None,
                 gripper_output=None):
        repo_path = repo_path.expanduser().resolve()
        if not (repo_path / "ar4_automation").is_dir():
            raise RuntimeError(
                f"{repo_path} does not contain ar4_automation")
        if str(repo_path) not in sys.path:
            sys.path.insert(0, str(repo_path))

        import rclpy
        from rclpy.signals import SignalHandlerOptions
        from ar4_automation.runner_common import sim_printer_specs, start_node
        from ar4_automation.simulated3DPrinter import Simulated3DPrinter

        self._rclpy = rclpy
        if not rclpy.ok():
            # Uvicorn owns SIGINT/SIGTERM in the main thread. This backend is
            # constructed by RobotManager's worker, where Python forbids
            # installing signal handlers.
            rclpy.init(
                args=None,
                signal_handler_options=SignalHandlerOptions.NO)
        overrides = {}
        if not sim and robot == "xarm6" and camera_mode is not None:
            if camera_mode == "disabled":
                overrides["stream_source"] = "ros"
            elif camera_mode == "webcam":
                overrides.update(stream_source="webcam",
                                 camera_index=int(camera_index))
        self.node = start_node(
            sim=sim, robot=robot, joint_state_timeout=2.0, **overrides)
        self.robot = robot
        self.sim = sim
        if gripper_output is not None:
            # Apply the seeded output before anything can command the gripper,
            # so the first close of the session already goes to the right pin.
            self.configure_gripper(int(gripper_output))
        # Commissioning is optional: it pulls cv2.aruco's ChArUco surface and
        # the calibration/ package, and neither is worth losing plate handling
        # over. A failure here is reported through telemetry, not raised.
        self.vision = None
        self.vision_error = None
        try:
            from ar4_automation.vision_commissioning import VisionCommissioning
            self.vision = VisionCommissioning(self.node, robot, repo_path)
        except Exception as exc:
            self.vision_error = str(exc)
            self.node.get_logger().error(
                f"vision commissioning unavailable: {exc}")
        self.sim_setup_state = None
        self.sim_setup_error = None
        self.sim_printers = []
        if sim:
            specs = sim_printer_specs(robot, count=3)
            self.node.register_printers(specs)
            self.node.marker_offset_config.update({
                0: "box_offset", 1: "box_offset", 2: "printer_offset",
            })
            # Register deterministic marker geometry immediately, before the
            # visual Gazebo models finish spawning. Automation goals can then
            # plan from estimates without keeping the GUI in "starting".
            for spec in specs:
                printer = Simulated3DPrinter(
                    node=self.node,
                    pos=spec["pos"],
                    orient=spec["orient"],
                    door_marker_texture=spec["door_marker_texture"],
                )
                bad_pos, bad_euler = printer.get_door_marker_pose_in_base()
                self.node.register_estimated_marker(
                    marker_id=spec["marker_id"],
                    bad_pos=bad_pos,
                    bad_euler=bad_euler,
                )
                self.sim_printers.append(printer)
            self.sim_setup_state = "spawning"

            def prepare_simulation():
                try:
                    for printer in self.sim_printers:
                        printer.spawn_fast()
                    self.sim_setup_state = "ready"
                except Exception as exc:
                    self.sim_setup_error = str(exc)
                    self.sim_setup_state = "error"
                    self.node.get_logger().error(
                        f"Background printer setup failed: {exc}")

            threading.Thread(
                target=prepare_simulation,
                name="gazebo-printer-setup",
                daemon=True,
            ).start()

    def execute(self, action: str, p: dict) -> Any:
        node = self.node
        if self.sim and action in PHYSICAL_ONLY_ACTIONS:
            raise RobotCommandError(f"{action} is physical-only")
        if action == "teach_enable":
            # enter/exit_teach_mode hand trajectory ownership between MoveIt's
            # ros2_control controller and UFACTORY manual mode.  Setting the
            # controller mode alone (the old set_xarm_mode(2) call) left the
            # trajectory controller active and fighting the hand guiding it.
            return self._teach(node, "enter_teach_mode")
        if action == "teach_disable":
            return self._teach(node, "exit_teach_mode")
        if action in VISION_STATE_ACTIONS:
            return self._vision_state(action, p)
        # The automation package owns the authoritative interlocks.  Checking
        # here gives the GUI an immediate, readable failure before any MoveIt
        # goal is submitted; every low-level move checks again.
        node.assert_motion_safe()
        if action == "goto_observation":
            item = self._observation(p["name"])
            joints = list(item.get("joint_positions") or [])
            if len(joints) == 6:
                # Replaying the recorded joint vector avoids the IK ambiguity
                # a Cartesian round-trip would introduce: the operator saved
                # this exact arm configuration for a reason.
                node.validate_joint_target(joints)
                return node.move_to_configuration(joints)
            pose = item.get("eef_pose") or {}
            position = pose.get("good_position")
            euler = pose.get("good_euler")
            if not position or not euler:
                raise RobotCommandError(
                    f"observation pose '{p['name']}' has neither a joint nor a "
                    "Cartesian target to replay")
            return node.move_to_pose(list(position), list(euler))
        if action == "scan_location":
            self._require_camera_ready(action)
            return node.scanLocationForMarkers(
                estimated_pos=p["position"], estimated_orient=p["euler"],
                viewing_distance=p["viewing_distance"])
        if action == "home":
            return node.go_home()
        if action == "move_joints":
            node.validate_joint_target(p["joints"], manual=True)
            return node.move_to_configuration(p["joints"])
        if action == "move_pose":
            return node.move_to_pose(p["position"], p["euler"])
        if action == "jog_pose":
            profile_name = (
                "simulation_motion" if self.sim
                else "physical_motion")
            profile = node.robot_config.get(profile_name, {})
            angular = p["axis"] in {"roll", "pitch", "yaw"}
            max_delta = profile.get(
                "max_jog_rotation" if angular else "max_jog_translation")
            if max_delta is not None and abs(p["delta"]) > max_delta:
                unit = "rad" if angular else "m"
                raise RobotCommandError(
                    f"jog delta {abs(p['delta']):.4f} {unit} exceeds "
                    f"{profile_name} safety limit {max_delta:.4f} {unit}")
            from scipy.spatial.transform import Rotation
            position, quaternion = node._eef_pose_truth()
            if position is None:
                raise RobotCommandError(
                    "cannot jog until a current end-effector TF is available")
            euler = Rotation.from_quat(quaternion).as_euler(
                "XYZ", degrees=False)
            good_position, good_euler = node.to_good_frame(position, euler)
            axes = ("x", "y", "z", "roll", "pitch", "yaw")
            target = list(good_position) + list(good_euler)
            target[axes.index(p["axis"])] += p["delta"]
            # Interactive jog must fail quickly instead of monopolizing the
            # command worker through several long planning retries.
            return node.move_to_pose(
                target[:3], target[3:], max_retries=0, timeout=6.0)
        if action == "scan_marker":
            self._require_camera_ready(action)
            move_ok, spotted = node.scanToMarker(
                marker_id=p["marker_id"],
                viewing_distance=p["viewing_distance"])
            if not move_ok:
                return False
            return {"move_ok": bool(move_ok), "marker_spotted": bool(spotted)}
        if action == "pickup":
            self._require_camera_ready(action)
            self._require_manipulation_hardware(action)
            return node.pickupPlate(markerID=p["marker_id"])
        if action == "place":
            self._require_camera_ready(action)
            self._require_manipulation_hardware(action)
            return node.placePlate(markerID=p["marker_id"])
        if action == "transfer":
            self._require_camera_ready(action)
            self._require_manipulation_hardware(action)
            return node.transferPlate(
                source_id=p["source_id"], dest_id=p["dest_id"],
                rescan_id=p["rescan_id"])
        if action == "scrape":
            self._require_camera_ready(action)
            self._require_manipulation_hardware(action)
            return node.scrapePlate(
                source_id=p["source_id"], scrape_id=p["scrape_id"])
        if action == "gripper_open":
            self._require_manipulation_hardware(action)
            node.open_gripper()
            return True
        if action == "gripper_close":
            self._require_manipulation_hardware(action)
            node.close_gripper()
            return True
        raise RobotCommandError(f"unsupported action {action}")

    def _teach(self, node, method: str) -> Any:
        handler = getattr(node, method, None)
        if handler is None:
            raise RobotCommandError(
                f"{method} is missing from the automation package; update the "
                "ar4Automating3DPrinter checkout to a revision that releases "
                "the trajectory controller with teach mode")
        return handler()

    def _vision(self, action: str):
        if self.vision is None:
            detail = f": {self.vision_error}" if self.vision_error else ""
            raise RobotCommandError(
                f"{action} needs the automation package's vision "
                f"commissioning module{detail}")
        return self.vision

    def _observation(self, name: str) -> dict:
        try:
            return self._vision("goto_observation").observation(name)
        except KeyError:
            raise RobotCommandError(
                f"no observation pose named '{name}' has been saved") from None

    def _vision_state(self, action: str, p: dict) -> Any:
        """Camera/TF reads and calibration bookkeeping -- never motion."""
        vision = self._vision(action)
        if action == "calibration_start":
            return vision.start(clear=p["clear"])
        if action == "calibration_stop":
            return vision.stop()
        if action == "calibration_capture":
            return vision.capture_sample()
        if action == "calibration_discard":
            return vision.discard_last()
        if action == "calibration_solve":
            # A rejected solve is still a successful command: its metrics are
            # the feedback that tells the operator which poses to add.
            return vision.solve()
        if action == "intrinsic_start":
            return vision.start_intrinsic(clear=p["clear"])
        if action == "intrinsic_stop":
            return vision.stop_intrinsic()
        if action == "intrinsic_capture":
            return vision.capture_intrinsic()
        if action == "intrinsic_discard":
            return vision.discard_intrinsic()
        if action == "intrinsic_solve":
            return vision.solve_intrinsic()
        if action == "save_observation":
            return vision.save_observation(
                p["name"], marker_id=p["marker_id"], role=p["role"])
        if action == "confirm_marker":
            return vision.confirm_marker(p["marker_id"], role=p["role"])
        raise RobotCommandError(f"unsupported action {action}")

    def _require_manipulation_hardware(self, action: str) -> None:
        """Never report a physical pick/place success with a no-op gripper."""
        if not self.sim and self.node.gripper is None:
            raise RobotCommandError(
                f"{action} requires a configured physical {self.robot} gripper; "
                "motion was blocked before the first waypoint")

    def _require_camera_ready(self, action: str) -> None:
        """Block vision-dependent physical motion on stale camera input."""
        if self.sim:
            return
        camera = self.node.stream.diagnostics()
        problems = []
        if camera.get("color_age_s") is None:
            problems.append("no color frames received")
        elif camera["color_age_s"] > 1.0:
            problems.append(
                f"color frame is stale ({camera['color_age_s']:.2f}s)")
        if not camera.get("calibrated"):
            problems.append("camera_info calibration not received")
        if not camera.get("camera_frame"):
            problems.append("camera optical frame is unknown")
        if camera.get("last_error"):
            problems.append(camera["last_error"])
        if problems:
            raise RobotCommandError(
                f"{action} blocked by camera preflight: " + "; ".join(problems))

    def cancel(self) -> None:
        from std_msgs.msg import String
        msg = String()
        msg.data = "stop"
        self.node._cancellation_pub.publish(msg)

    def telemetry(self) -> dict:
        joints = self.node._last_joint_msg
        return {
            "robot": self.robot,
            "sim": self.sim,
            "sim_setup_state": self.sim_setup_state,
            "sim_setup_error": self.sim_setup_error,
            "joints": list(joints) if joints is not None else None,
            "eef_pose": {
                "xyz_rpy": _jsonable(self.node.pose),
                "quaternion": _jsonable(self.node.quat),
                "frame": self.node.frame,
            },
            "markers": _jsonable(self.node.marker_poses),
            "camera": _jsonable(self.node.stream.diagnostics()),
            "safety": _jsonable(self.node.safety_snapshot()),
            "vision": self._vision_snapshot(),
            "gripper": self._gripper_snapshot(),
        }

    def _gripper_snapshot(self) -> dict:
        """What the page can honestly say about the gripper.

        `command` is the last action ASKED FOR, never a sensed position: no
        supported gripper reports back. A latched CO output outlives this
        process and an e-stop drops it silently, so `None` means unknown --
        not open.
        """
        node = self.node
        status = getattr(node, "gripper_status", None)
        if callable(status):
            snapshot = _jsonable(status())
        else:
            # Automation checkout predating gripper_status(); report what is
            # still knowable rather than dropping the key and making the page
            # think this backend has no gripper at all.
            kind = getattr(node, "gripper", None)
            if kind is not None and not isinstance(kind, str):
                kind = "moveit_action"
            snapshot = {"kind": kind, "command": None, "sensed": False,
                        "disabled": bool(getattr(node, "gripper_disabled",
                                                 False))}
        # Read the selected output out of the same dict _call_cgpio_gripper
        # reads at call time, rather than parsing it back out of the "CO0"
        # label: the number here is then always the pin that will be driven.
        cfg = node.robot_config.get("gripper")
        if isinstance(cfg, dict) and cfg.get("type") == "cgpio":
            snapshot["ionum"] = int(cfg.get("ionum", 0))
            snapshot["output_count"] = XARM_CO_COUNT
        return snapshot

    def _vision_snapshot(self) -> dict:
        if self.vision is None:
            return {"available": False, "error": self.vision_error}
        try:
            return {"available": True, **_jsonable(self.vision.snapshot())}
        except Exception as exc:
            return {"available": False, "error": str(exc)}

    def camera_frame(self) -> bytes | None:
        import cv2
        stream = self.node.stream
        with stream.lock:
            if stream.frame is None:
                return None
            frame = stream.frame.copy()
        if self.vision is not None:
            # Refreshes ChArUco corner health while a calibration session is
            # open, and is a no-op otherwise.  It returns the ArUco-annotated
            # frame unchanged: the operator needs those axes visible exactly
            # when they are registering a workspace marker.
            #
            # Guarded because this runs on the frame route: a commissioning
            # problem belongs in the calibration card's detection status, not
            # in a 500 that blanks the live camera the operator is aiming with.
            try:
                previewed = self.vision.preview(frame)
                if previewed is not None:
                    frame = previewed
            except Exception as exc:
                self.node.get_logger().warn(
                    f"calibration preview failed: {exc}")
        ok, encoded = cv2.imencode('.jpg', frame,
                                   [cv2.IMWRITE_JPEG_QUALITY, 75])
        return encoded.tobytes() if ok else None

    def configure_gripper(self, output: int) -> None:
        """Select which controller output drives the gripper.

        `_call_cgpio_gripper` reads `robot_config['gripper']` fresh on every
        call, so writing the index back into that dict takes effect on the next
        open/close with no restart and no second copy of the setting.

        Refused while the gripper is commanded closed: the old output stays
        latched high after the switch, so re-pointing mid-grip would strand a
        live pin holding a plate that nothing is tracking any more.
        """
        cfg = self.node.robot_config.get("gripper")
        if not isinstance(cfg, dict) or cfg.get("type") != "cgpio":
            raise RobotCommandError(
                f"{self.robot} does not drive its gripper from a controller "
                "output, so there is nothing to select")
        if getattr(self.node, "gripper_command", None) == "close":
            raise RobotCommandError(
                "open the gripper before changing its output; the current one "
                "stays latched and would be left holding")
        cfg["ionum"] = int(output)
        # The new pin's state is genuinely unknown -- it was never driven by
        # this process. Saying "open" here would be a guess presented as fact.
        self.node.gripper_command = None
        self.node.get_logger().info(f"Gripper output set to CO{output}")

    def configure_camera(self, index: int | None) -> None:
        if self.sim:
            raise RobotCommandError("Gazebo camera selection is fixed by ROS topics")
        if index is None:
            self.node.stream.disable_webcam()
        else:
            self.node.stream.configure_webcam(index)

    def close(self) -> None:
        try:
            self.node.destroy_node()
        finally:
            if self._rclpy.ok():
                self._rclpy.shutdown()


def list_video_devices() -> list[dict]:
    """Enumerate Linux V4L2 nodes without opening/locking the cameras."""
    root = pathlib.Path('/sys/class/video4linux')
    devices = []
    if not root.is_dir():
        return devices
    for path in sorted(root.glob('video*'), key=lambda p: int(p.name[5:])):
        try:
            name = (path / 'name').read_text().strip()
            index = int(path.name[5:])
        except (OSError, ValueError):
            continue
        devices.append({"index": index, "name": name,
                        "path": f"/dev/{path.name}"})
    return devices
