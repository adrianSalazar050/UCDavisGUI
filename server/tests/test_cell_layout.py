from server.cell_layout import CellLayoutStore, LayoutError, default_layout


def test_layout_round_trips_with_a_keepout_volume(tmp_path):
    store = CellLayoutStore(tmp_path / "layout.json")
    layout = default_layout()
    layout["frame"]["axes_confirmed"] = True
    layout["zones"] = [{
        "id": "printer-base", "name": "Printer base", "kind": "keepout",
        "points": [[-0.2, 0.2], [-0.2, 0.6], [0.2, 0.6], [0.2, 0.2]],
        "z_min": 0.025, "z_max": 0.45, "notes": "Rigid printer volume",
    }]
    saved = store.save(layout)
    assert saved["frame"]["axes_confirmed"] is True
    assert saved["zones"][0]["id"] == "printer-base"
    assert CellLayoutStore(tmp_path / "layout.json").snapshot()["zones"] == saved["zones"]


def test_layout_rejects_flat_or_inverted_volumes(tmp_path):
    store = CellLayoutStore(tmp_path / "layout.json")
    layout = default_layout()
    layout["zones"] = [{
        "id": "bad", "name": "Bad", "kind": "keepout",
        "points": [[0, 0], [0, 0.2], [0, 0.4]], "z_min": 0.2, "z_max": 0.1,
    }]
    try:
        store.save(layout)
    except LayoutError as exc:
        assert "polygon has no area" in str(exc)
    else:
        raise AssertionError("invalid layout was accepted")
