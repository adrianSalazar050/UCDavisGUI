"""Serialized robot command execution with optional ROS 2 backing.

The web server never talks to MoveIt directly.  RobotManager accepts one
high-level command at a time and runs it on a dedicated worker thread.  The
ROS backend imports and reuses printerAutomation from ar4Automating3DPrinter,
so its retries, TF checks, marker handling, and gripper sequencing remain the
single source of truth.
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
}


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

    def __init__(self, backend_factory: Callable[[], Any]):
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


class MockRobotBackend:
    """Hardware-free backend for API/frontend development."""

    def __init__(self, delay_s: float = 0.02):
        self.delay_s = delay_s
        self.joints = [0.0] * 6
        self.pose = [0.35, 0.0, 0.30, 0.0, 0.0, 0.0]
        self.cancelled = False

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
        return {"mock": True, "action": action}

    def cancel(self) -> None:
        self.cancelled = True

    def telemetry(self) -> dict:
        return {
            "robot": "mock",
            "joints": list(self.joints),
            "eef_pose": {"xyz_rpy": list(self.pose), "frame": "good"},
            "markers": [],
        }

    def close(self) -> None:
        pass


class RosRobotBackend:
    """Adapter from the command contract to printerAutomation methods."""

    def __init__(self, repo_path: pathlib.Path, robot: str = "ar4",
                 sim: bool = False):
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
        self.node = start_node(
            sim=sim, robot=robot, joint_state_timeout=2.0)
        self.robot = robot
        self.sim = sim
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
        # The automation package owns the authoritative interlocks.  Checking
        # here gives the GUI an immediate, readable failure before any MoveIt
        # goal is submitted; every low-level move checks again.
        node.assert_motion_safe()
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
            move_ok, spotted = node.scanToMarker(
                marker_id=p["marker_id"],
                viewing_distance=p["viewing_distance"])
            if not move_ok:
                return False
            return {"move_ok": bool(move_ok), "marker_spotted": bool(spotted)}
        if action == "pickup":
            self._require_manipulation_hardware(action)
            return node.pickupPlate(markerID=p["marker_id"])
        if action == "place":
            self._require_manipulation_hardware(action)
            return node.placePlate(markerID=p["marker_id"])
        if action == "transfer":
            self._require_manipulation_hardware(action)
            return node.transferPlate(
                source_id=p["source_id"], dest_id=p["dest_id"],
                rescan_id=p["rescan_id"])
        if action == "scrape":
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

    def _require_manipulation_hardware(self, action: str) -> None:
        """Never report a physical pick/place success with a no-op gripper."""
        if not self.sim and self.node.gripper is None:
            raise RobotCommandError(
                f"{action} requires a configured physical {self.robot} gripper; "
                "motion was blocked before the first waypoint")

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
            "safety": _jsonable(self.node.safety_snapshot()),
        }

    def close(self) -> None:
        try:
            self.node.destroy_node()
        finally:
            if self._rclpy.ok():
                self._rclpy.shutdown()
