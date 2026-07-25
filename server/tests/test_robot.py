import time

import pytest
from fastapi.testclient import TestClient

from server.main import create_app
from server.robot import (MockRobotBackend, RobotBusy, RobotCommandError,
                          RobotManager, normalize_command)


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
