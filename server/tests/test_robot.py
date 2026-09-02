import time

import pytest
from fastapi.testclient import TestClient

from server.main import create_app
from server.robot import (MockRobotBackend, RobotBusy, RobotCommandError,
                          RobotManager, RobotUnavailable, RosRobotBackend,
                          normalize_command)


class Registry:
    def summaries(self):
        return []


def wait_for(manager, state, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = manager.snapshot()
        if snap["state"] == state:
            return snap
        time.sleep(0.005)
    raise AssertionError(
        f"robot did not reach {state}: {manager.snapshot()}")


def test_normalize_rejects_unknown_action():
    with pytest.raises(RobotCommandError, match="unknown robot action"):
        normalize_command("fly", {})


def test_normalize_move_pose_and_joint_goals():
    assert normalize_command(
        "move_pose", {"position": [1, 2, 3], "euler": [0, 0.1, 0.2]}) == (
            "move_pose",
            {"position": [1.0, 2.0, 3.0], "euler": [0.0, 0.1, 0.2]})
    assert normalize_command("move_joints", {"joints": list(range(6))}) == (
        "move_joints", {"joints": [0, 1, 2, 3, 4, 5]})


def test_normalize_marker_and_distance_bounds():
    assert normalize_command("pickup", {"marker_id": "2"}) == (
        "pickup", {"marker_id": 2})
    with pytest.raises(RobotCommandError, match="viewing_distance"):
        normalize_command(
            "scan_marker", {"marker_id": 2, "viewing_distance": 1.0})


def test_normalize_jog_pose_and_bounds():
    assert normalize_command("jog_pose", {"axis": "X", "delta": "0.005"}) == (
        "jog_pose", {"axis": "x", "delta": 0.005})
    with pytest.raises(RobotCommandError, match="axis"):
        normalize_command("jog_pose", {"axis": "diagonal", "delta": 0.005})
    with pytest.raises(RobotCommandError, match="at most"):
        normalize_command("jog_pose", {"axis": "z", "delta": 0.10})
    with pytest.raises(RobotCommandError, match="non-zero"):
        normalize_command("jog_pose", {"axis": "yaw", "delta": 0})


def test_normalize_scan_location():
    action, params = normalize_command("scan_location", {
        "position": [0.3, 0.0, 0.2],
        "euler": [0, 0, 1.57],
        "viewing_distance": "0.15",
    })
    assert action == "scan_location"
    assert params == {
        "position": [0.3, 0.0, 0.2],
        "euler": [0.0, 0.0, 1.57],
        "viewing_distance": 0.15,
    }
    with pytest.raises(RobotCommandError, match="viewing_distance"):
        normalize_command("scan_location", {
            "position": [0, 0, 0], "euler": [0, 0, 0],
            "viewing_distance": 0.9,
        })


def test_manager_executes_one_command_and_reports_telemetry():
    manager = RobotManager(lambda: MockRobotBackend(delay_s=0.01))
    manager.start()
    wait_for(manager, "idle")
    command = manager.submit("move_joints", {"joints": [1, 2, 3, 4, 5, 6]})
    assert command["state"] == "queued"
    snap = wait_for(manager, "idle")
    assert snap["last_command"]["id"] == command["id"]
    assert snap["last_command"]["state"] == "succeeded"
    assert snap["joints"] == [1, 2, 3, 4, 5, 6]
    manager.stop()


def test_manager_rejects_concurrent_command():
    manager = RobotManager(lambda: MockRobotBackend(delay_s=0.2))
    manager.start()
    wait_for(manager, "idle")
    manager.submit("home")
    with pytest.raises(RobotBusy):
        manager.submit("home")
    manager.stop()


def test_mock_jog_updates_cartesian_telemetry():
    manager = RobotManager(lambda: MockRobotBackend(delay_s=0.01))
    manager.start()
    wait_for(manager, "idle")
    manager.submit("jog_pose", {"axis": "y", "delta": -0.005})
    snap = wait_for(manager, "idle")
    assert snap["last_command"]["state"] == "succeeded"
    assert snap["eef_pose"]["xyz_rpy"] == [
        0.35, -0.005, 0.30, 0.0, 0.0, 0.0]
    manager.stop()


def test_manager_cancels_active_command():
    manager = RobotManager(lambda: MockRobotBackend(delay_s=0.2))
    manager.start()
    wait_for(manager, "idle")
    command = manager.submit("home")
    assert manager.cancel(command["id"]) is True
    snap = wait_for(manager, "idle")
    assert snap["last_command"]["state"] == "cancelled"
    manager.stop()


class FakeRobot:
    def __init__(self):
        self.events = []
        self.command = None

    def start(self):
        self.events.append("start")

    def stop(self):
        self.events.append("stop")

    def snapshot(self):
        return {
            "available": True,
            "state": "idle",
            "active_command": self.command,
            "last_command": None,
            "error": None,
        }

    def submit(self, action, parameters):
        if action == "busy":
            raise RobotBusy("already moving")
        action, parameters = normalize_command(action, parameters)
        self.command = {
            "id": "cmd-1", "action": action, "parameters": parameters,
            "state": "queued",
        }
        return self.command

    def cancel(self, command_id=None):
        return self.command is not None and self.command["id"] == command_id


def test_robot_routes_are_inert_when_disabled(tmp_path):
    client = TestClient(create_app(Registry(), tmp_path))
    assert client.get("/api/robot/status").status_code == 404
    assert client.post(
        "/api/robot/commands", json={"action": "home"}).status_code == 404


def test_robot_status_submit_cancel_and_websocket(tmp_path):
    robot = FakeRobot()
    client = TestClient(create_app(Registry(), tmp_path, robot=robot))
    with client:
        assert client.get("/api/robot/status").json()["state"] == "idle"
        response = client.post(
            "/api/robot/commands",
            json={"action": "move_joints",
                  "parameters": {"joints": [0, 1, 2, 3, 4, 5]}})
        assert response.status_code == 202
        assert response.json()["id"] == "cmd-1"
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["robot"]["available"] is True
        response = client.post("/api/robot/commands/cmd-1/cancel")
        assert response.status_code == 200
    assert robot.events == ["start", "stop"]


def test_robot_command_validation_is_http_400(tmp_path):
    robot = FakeRobot()
    client = TestClient(create_app(Registry(), tmp_path, robot=robot))
    response = client.post(
        "/api/robot/commands",
        json={"action": "move_pose",
              "parameters": {"position": [1, 2], "euler": [0, 0, 0]}})
    assert response.status_code == 400
    assert "position" in response.json()["detail"]


def test_camera_frame_is_404_not_500_on_a_backend_without_a_camera():
    """camera_frame() is an OPTIONAL half of the backend contract.

    Only RosRobotBackend has a camera; MockRobotBackend deliberately does not.
    The manager used to call backend.camera_frame() unconditionally, so
    GET /api/robot/camera/frame raised AttributeError and became a 500 under
    --robot-mode mock -- the one mode the Robot page is developed against.
    "This backend has no camera" and "this camera has produced no frame yet"
    are the same answer to the browser, and that answer is 404.
    """
    manager = RobotManager(lambda: MockRobotBackend(delay_s=0.0))
    manager.start()
    try:
        wait_for(manager, "idle")
        assert manager.camera_frame() is None
    finally:
        manager.stop()


def test_configure_camera_on_a_backend_without_a_camera_is_not_attributeerror():
    """Same optional-method contract on the write side.

    A manager built WITHOUT camera_config already refuses with
    RobotUnavailable, but one built with it would previously reach through to
    a backend that has no configure_camera at all.
    """
    manager = RobotManager(lambda: MockRobotBackend(delay_s=0.0),
                           camera_config={"mode": "disabled", "index": None})
    manager.start()
    try:
        wait_for(manager, "idle")
        with pytest.raises(RobotUnavailable, match="camera"):
            manager.configure_camera(0)
    finally:
        manager.stop()


def run_command(manager, action, parameters=None):
    """Submit and wait for the worker to finish, -> the command record."""
    command = manager.submit(action, parameters)
    snapshot = wait_for(manager, "idle")
    assert snapshot["last_command"]["id"] == command["id"]
    return snapshot["last_command"]


def test_normalize_vision_commissioning_commands():
    assert normalize_command("calibration_start", {}) == (
        "calibration_start", {"clear": False})
    assert normalize_command("calibration_capture", {"stray": 1}) == (
        "calibration_capture", {})
    # A name-only observation pose is legitimate: the operator may not have
    # picked a marker for that viewpoint yet.
    assert normalize_command("save_observation", {"name": "  printer 1  "}) == (
        "save_observation", {"name": "printer 1", "role": "other",
                             "marker_id": None})
    assert normalize_command(
        "save_observation",
        {"name": "bed", "marker_id": "2", "role": "printer"}) == (
        "save_observation", {"name": "bed", "role": "printer",
                             "marker_id": 2})
    assert normalize_command("confirm_marker", {"marker_id": 0}) == (
        "confirm_marker", {"marker_id": 0, "role": "other"})


def test_normalize_rejects_bad_observation_name_role_and_flag():
    with pytest.raises(RobotCommandError, match="non-empty string"):
        normalize_command("goto_observation", {"name": "   "})
    with pytest.raises(RobotCommandError, match="at most 64 characters"):
        normalize_command("save_observation", {"name": "x" * 65})
    with pytest.raises(RobotCommandError, match="role must be one of"):
        normalize_command("confirm_marker", {"marker_id": 0, "role": "boxx"})
    with pytest.raises(RobotCommandError, match="must be true or false"):
        normalize_command("calibration_start", {"clear": "yes"})


def test_mock_calibration_session_reaches_a_passing_solve():
    manager = RobotManager(lambda: MockRobotBackend(delay_s=0.001))
    manager.start()
    wait_for(manager, "idle")

    assert run_command(manager, "calibration_capture")["state"] == "failed"
    run_command(manager, "calibration_start", {"clear": True})
    assert manager.snapshot()["vision"]["session_active"] is True

    for _ in range(8):
        run_command(manager, "calibration_capture")
    rejected = run_command(manager, "calibration_solve")
    assert rejected["result"]["quality_passed"] is False
    assert manager.snapshot()["vision"]["hand_eye"] is None

    for _ in range(7):
        run_command(manager, "calibration_capture")
    accepted = run_command(manager, "calibration_solve")
    assert accepted["state"] == "succeeded"
    assert accepted["result"]["quality_passed"] is True

    snapshot = manager.snapshot()
    assert snapshot["vision"]["sample_count"] == 15
    assert snapshot["vision"]["hand_eye"]["quality_passed"] is True
    manager.stop()


def test_mock_solve_below_the_minimum_sample_count_fails():
    manager = RobotManager(lambda: MockRobotBackend(delay_s=0.001))
    manager.start()
    wait_for(manager, "idle")
    run_command(manager, "calibration_start", {"clear": True})
    run_command(manager, "calibration_capture")
    failed = run_command(manager, "calibration_solve")
    assert failed["state"] == "failed"
    assert "At least 8 captures" in failed["error"]
    manager.stop()


def test_mock_observation_pose_round_trip():
    manager = RobotManager(lambda: MockRobotBackend(delay_s=0.001))
    manager.start()
    wait_for(manager, "idle")
    run_command(manager, "move_joints", {"joints": [0.1, 0.2, 0, 0, 0, 0]})
    run_command(manager, "save_observation",
                {"name": "printer 1", "marker_id": 0, "role": "printer"})
    run_command(manager, "home")
    assert manager.snapshot()["joints"] == [0.0] * 6

    run_command(manager, "goto_observation", {"name": "printer 1"})
    assert manager.snapshot()["joints"] == [0.1, 0.2, 0, 0, 0, 0]

    missing = run_command(manager, "goto_observation", {"name": "nowhere"})
    assert missing["state"] == "failed"
    assert "no observation pose named" in missing["error"]
    manager.stop()


def test_mock_confirm_marker_refuses_an_unmeasured_marker():
    manager = RobotManager(lambda: MockRobotBackend(delay_s=0.001))
    manager.start()
    wait_for(manager, "idle")
    # Marker 2 is registered from an estimate, never seen by the camera.
    estimated = run_command(manager, "confirm_marker",
                            {"marker_id": 2, "role": "printer"})
    assert estimated["state"] == "failed"
    assert "has not been measured" in estimated["error"]

    measured = run_command(manager, "confirm_marker",
                           {"marker_id": 0, "role": "printer"})
    assert measured["state"] == "succeeded"
    assert manager.snapshot()["vision"]["marker_roles"] == {"0": "printer"}
    manager.stop()


def test_vision_commands_over_http(tmp_path):
    manager = RobotManager(lambda: MockRobotBackend(delay_s=0.001))
    app = create_app(Registry(), tmp_path, robot=manager)
    with TestClient(app) as client:
        wait_for(manager, "idle")
        accepted = client.post("/api/robot/commands", json={
            "action": "save_observation",
            "parameters": {"name": "scrape bay", "role": "scrape"}})
        assert accepted.status_code == 202
        assert accepted.json()["action"] == "save_observation"

        rejected = client.post("/api/robot/commands", json={
            "action": "confirm_marker",
            "parameters": {"marker_id": 0, "role": "not-a-role"}})
        assert rejected.status_code == 400
        assert "role must be one of" in rejected.json()["detail"]

        wait_for(manager, "idle")
        vision = client.get("/api/robot/status").json()["vision"]
        assert vision["available"] is True
        assert [x["name"] for x in vision["observation_poses"]] == ["scrape bay"]


class GripperNode:
    """Stand-in for the automation node, for the gripper telemetry contract."""

    def __init__(self, gripper, disabled=False, status=None):
        self.gripper = gripper
        self.gripper_disabled = disabled
        if status is not None:
            self.gripper_status = status


class NodeHolder:
    def __init__(self, node):
        self.node = node


def test_mock_gripper_reports_the_last_commanded_action():
    manager = RobotManager(lambda: MockRobotBackend(delay_s=0.001))
    manager.start()
    wait_for(manager, "idle")
    # Nothing has been asked for yet, and a latching output that nobody has
    # driven is unknown -- not open.
    assert manager.snapshot()["gripper"]["command"] is None

    run_command(manager, "gripper_close")
    assert manager.snapshot()["gripper"]["command"] == "close"

    run_command(manager, "gripper_open")
    snapshot = manager.snapshot()
    assert snapshot["gripper"]["command"] == "open"
    # The page must never present a commanded action as a measured one.
    assert snapshot["gripper"]["sensed"] is False
    manager.stop()


def test_gripper_snapshot_prefers_the_automation_packages_status():
    node = GripperNode("cgpio", status=lambda: {
        "kind": "cgpio", "output": "CO0", "command": "close",
        "sensed": False, "disabled": False})
    snapshot = RosRobotBackend._gripper_snapshot(NodeHolder(node))
    assert snapshot["output"] == "CO0"
    assert snapshot["command"] == "close"


def test_gripper_snapshot_falls_back_on_an_older_automation_checkout():
    """A missing gripper_status() must not read as "no gripper".

    Dropping the key would grey out controls on an arm whose gripper works
    perfectly well; reporting what is still knowable keeps them live.
    """
    moveit = RosRobotBackend._gripper_snapshot(
        NodeHolder(GripperNode(object())))
    assert moveit == {"kind": "moveit_action", "command": None,
                      "sensed": False, "disabled": False}

    none = RosRobotBackend._gripper_snapshot(
        NodeHolder(GripperNode(None, disabled=True)))
    assert none["kind"] is None
    assert none["disabled"] is True
