import pytest

from server.cell_profiles import CellProfileStore, ProfileError


def _profile():
    return {
        "id": "printer_pickup", "role": "printer", "marker_id": 5,
        "purpose": "Approach the pickup pose", "risk_level": "medium",
        "requires_operator_confirmation": True,
        "steps": [
            {"kind": "scan", "label": "Confirm printer", "distance_m": 0.15},
            {"kind": "pose", "label": "Pre-pick", "offset": [0, 0, 0.03, 0, 0, 0],
             "speed_scale": 0.1, "acceleration_scale": 0.1},
            {"kind": "gripper", "label": "Close", "action": "close", "settle_s": 0.5},
        ],
    }


def test_profile_keeps_execution_parameters_and_versions(tmp_path):
    store = CellProfileStore(tmp_path / "profiles.json")
    saved = store.save(_profile())
    assert saved["version"] == 1
    assert saved["steps"][1]["speed_scale"] == 0.1
    assert saved["steps"][2]["settle_s"] == 0.5
    assert store.save(_profile())["version"] == 2


def test_profile_rejects_unsafe_contact_and_pose_values(tmp_path):
    store = CellProfileStore(tmp_path / "profiles.json")
    bad = _profile()
    bad["steps"][1]["speed_scale"] = 2
    with pytest.raises(ProfileError, match="speed scale"):
        store.save(bad)
    bad = _profile()
    bad["steps"].append({"kind": "contact", "label": "Edge", "duration_s": 1,
                         "direction": "-z", "travel_m": 0.2, "speed_mm_s": 5})
    with pytest.raises(ProfileError, match="travel"):
        store.save(bad)


def test_profile_delete(tmp_path):
    store = CellProfileStore(tmp_path / "profiles.json")
    store.save(_profile())
    store.delete("printer_pickup")
    assert store.snapshot()["profiles"] == []
