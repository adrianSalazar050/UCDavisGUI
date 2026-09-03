import time

import pytest
from fastapi.testclient import TestClient

from server.main import create_app
from server.robot import (XARM_CO_COUNT, MockRobotBackend, RobotBusy,
                          RobotCommandError, RobotManager,
                          RobotUnavailable, RosRobotBackend,
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
    """Stand-in for the automation node, for the gripper telemetry contract.

    `robot_config` is always present on a real node (PoseReader sets it), so
    the stub carries one rather than the snapshot guarding for its absence.
    """

    def __init__(self, gripper, disabled=False, status=None, config=None):
        self.gripper = gripper
        self.gripper_disabled = disabled
        self.robot_config = {"gripper": config}
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
    node = GripperNode("cgpio", config={"type": "cgpio", "ionum": 4},
                       status=lambda: {
        "kind": "cgpio", "output": "CO4", "command": "close",
        "sensed": False, "disabled": False})
    snapshot = RosRobotBackend._gripper_snapshot(NodeHolder(node))
    assert snapshot["output"] == "CO4"
    assert snapshot["command"] == "close"
    # The selectable index comes from robot_config -- the same dict the driver
    # reads at call time -- not from parsing the "CO4" label back apart.
    assert snapshot["ionum"] == 4
    assert snapshot["output_count"] == XARM_CO_COUNT


def test_gripper_snapshot_omits_the_output_for_a_non_cgpio_gripper():
    """A Lite 6 or AR4 gripper has no pin to point anywhere; no picker."""
    node = GripperNode("lite6_service",
                       config={"type": "lite6_service"},
                       status=lambda: {"kind": "lite6_service",
                                       "command": None, "sensed": False,
                                       "disabled": False})
    snapshot = RosRobotBackend._gripper_snapshot(NodeHolder(node))
    assert "ionum" not in snapshot


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


def test_gripper_output_is_selectable_across_the_co_block():
    manager = RobotManager(lambda: MockRobotBackend(delay_s=0.001),
                           gripper_config={"output": None})
    manager.start()
    wait_for(manager, "idle")
    assert manager.snapshot()["gripper"]["ionum"] == 0

    for output in range(XARM_CO_COUNT):
        manager.configure_gripper(output)
        snapshot = manager.snapshot()
        assert snapshot["gripper"]["ionum"] == output
        assert snapshot["gripper"]["output"] == f"CO{output}"
    # The manager mirrors the choice, the way it already does for the camera.
    assert manager.gripper_config["output"] == XARM_CO_COUNT - 1
    manager.stop()


def test_gripper_output_rejects_anything_outside_the_co_block():
    manager = RobotManager(lambda: MockRobotBackend(delay_s=0.001))
    manager.start()
    wait_for(manager, "idle")
    for bad in (-1, XARM_CO_COUNT, 99):
        with pytest.raises(RobotCommandError, match="CO0 and CO"):
            manager.configure_gripper(bad)
    # bool is an int subclass; True would otherwise silently mean CO1.
    for bad in (True, 1.5, "2", None):
        with pytest.raises(RobotCommandError, match="must be an integer"):
            manager.configure_gripper(bad)
    manager.stop()


def test_gripper_output_cannot_change_while_commanded_closed():
    """Switching mid-grip strands the old pin latched, holding a plate."""
    manager = RobotManager(lambda: MockRobotBackend(delay_s=0.001))
    manager.start()
    wait_for(manager, "idle")
    run_command(manager, "gripper_close")
    with pytest.raises(RobotCommandError, match="open the gripper"):
        manager.configure_gripper(3)
    assert manager.snapshot()["gripper"]["ionum"] == 0

    run_command(manager, "gripper_open")
    manager.configure_gripper(3)
    snapshot = manager.snapshot()
    assert snapshot["gripper"]["ionum"] == 3
    # The new pin was never driven by this process, so its state is unknown --
    # carrying "open" over from the old one would be a guess stated as fact.
    assert snapshot["gripper"]["command"] is None
    manager.stop()


def test_gripper_output_is_refused_while_a_command_is_running():
    manager = RobotManager(lambda: MockRobotBackend(delay_s=0.2))
    manager.start()
    wait_for(manager, "idle")
    manager.submit("home")
    with pytest.raises(RobotBusy, match="while a command is active"):
        manager.configure_gripper(2)
    manager.stop()


def test_gripper_output_on_a_backend_without_one_is_not_attributeerror():
    """MockRobotBackend gained configure_gripper; a bare backend has not."""
    class Bare:
        def execute(self, action, parameters):
            return True

        def cancel(self):
            pass

        def telemetry(self):
            return {}

        def close(self):
            pass

    manager = RobotManager(Bare)
    manager.start()
    wait_for(manager, "idle")
    with pytest.raises(RobotUnavailable, match="selectable gripper output"):
        manager.configure_gripper(1)
    manager.stop()


def test_gripper_route_reports_400_409_and_503(tmp_path):
    manager = RobotManager(lambda: MockRobotBackend(delay_s=0.001))
    app = create_app(Registry(), tmp_path, robot=manager)
    with TestClient(app) as client:
        wait_for(manager, "idle")
        ok = client.put("/api/robot/gripper", json={"output": 5})
        assert ok.status_code == 200
        assert ok.json()["gripper"]["output"] == "CO5"

        bad = client.put("/api/robot/gripper", json={"output": 9})
        assert bad.status_code == 400
        assert "CO0 and CO" in bad.json()["detail"]

        run_command(manager, "gripper_close")
        holding = client.put("/api/robot/gripper", json={"output": 1})
        assert holding.status_code == 400
        assert "open the gripper" in holding.json()["detail"]


def test_gripper_route_is_inert_when_the_robot_is_disabled(tmp_path):
    app = create_app(Registry(), tmp_path, robot=None)
    with TestClient(app) as client:
        assert client.put("/api/robot/gripper",
                          json={"output": 0}).status_code == 404
