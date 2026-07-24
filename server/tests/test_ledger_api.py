import pathlib

import pytest
from fastapi.testclient import TestClient

from server.ledger import Ledger, badge_id_for
from server.main import create_app


class FakeRegistry:
    def summaries(self):
        return []

    def get(self, serial):
        return None


@pytest.fixture
def led(tmp_path):
    ledger = Ledger(tmp_path / "ledger.db")
    yield ledger
    ledger.close()


@pytest.fixture
def client(tmp_path, led):
    app = create_app(FakeRegistry(), tmp_path, ledger=led)
    return TestClient(app)


@pytest.fixture
def no_ledger_client(tmp_path):
    app = create_app(FakeRegistry(), tmp_path)
    return TestClient(app)


def test_routes_404_without_a_ledger(no_ledger_client):
    assert no_ledger_client.get("/api/runs").status_code == 404
    assert no_ledger_client.get("/api/badges").status_code == 404


def test_lists_runs_newest_first(client, led):
    a = led.open_run(printer_serial="S1", printer_name="A1", source="queue")
    b = led.open_run(printer_serial="S2", printer_name="A1m", source="queue")
    res = client.get("/api/runs")
    assert res.status_code == 200
    assert {r["id"] for r in res.json()["runs"]} == {a, b}


def test_filters_runs_by_serial(client, led):
    led.open_run(printer_serial="S1", source="queue")
    b = led.open_run(printer_serial="S2", source="queue")
    res = client.get("/api/runs", params={"serial": "S2"})
    assert [r["id"] for r in res.json()["runs"]] == [b]


def test_run_detail_includes_events_pieces_and_badges(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.add_event(printer_serial="S1", run_id=run_id, kind="state_change",
                  source="server", payload={"to": "RUNNING"})
    led.create_pieces(run_id, 2)
    led.add_run_badge(run_id, badge_id_for("spaghetti"),
                      applied_by="detector")
    body = client.get(f"/api/runs/{run_id}").json()
    assert body["run"]["id"] == run_id
    assert len(body["events"]) == 1
    assert len(body["pieces"]) == 2
    assert [b["code"] for b in body["badges"]] == ["spaghetti"]
    assert body["pieces"][0]["badges"] == []


def test_unknown_run_is_404(client):
    assert client.get("/api/runs/nope").status_code == 404


def test_the_run_list_carries_a_piece_rollup(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 3)
    led.set_piece(led.pieces_for(run_id)[0]["id"], status="good")
    row = client.get("/api/runs").json()["runs"][0]
    assert row["piece_counts"] == {"total": 3, "good": 1, "scrap": 0,
                                   "rework": 0, "pending": 2}


def test_a_run_with_no_pieces_still_has_a_zeroed_rollup(client, led):
    led.open_run(printer_serial="S1", source="queue")
    row = client.get("/api/runs").json()["runs"][0]
    assert row["piece_counts"]["total"] == 0


def test_badge_catalogue_is_served(client):
    codes = {b["code"] for b in client.get("/api/badges").json()["badges"]}
    assert "spaghetti" in codes and "warped" in codes


def test_patch_corrects_the_end_state(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.close_run(run_id, end_state="FAILED")
    res = client.patch(f"/api/runs/{run_id}",
                       json={"end_state": "STOPPED_BY_OPERATOR"})
    assert res.status_code == 200
    assert led.get_run(run_id)["end_state"] == "STOPPED_BY_OPERATOR"


def test_patch_overrides_actual_grams_and_marks_the_basis_manual(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    client.patch(f"/api/runs/{run_id}", json={"actual_grams": 18.4})
    run = led.get_run(run_id)
    assert run["actual_grams"] == pytest.approx(18.4)
    assert run["actual_grams_basis"] == "manual"


def test_patch_rejects_an_unknown_end_state(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    res = client.patch(f"/api/runs/{run_id}", json={"end_state": "GREAT"})
    assert res.status_code == 400


def test_patch_of_an_unknown_run_is_404(client):
    assert client.patch("/api/runs/nope",
                        json={"end_state": "FINISH"}).status_code == 404


def test_piece_verdict_is_recorded(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 1)
    piece = led.pieces_for(run_id)[0]
    res = client.patch(f"/api/pieces/{piece['id']}",
                       json={"status": "scrap", "inspected_by": "adrian"})
    assert res.status_code == 200
    assert led.pieces_for(run_id)[0]["status"] == "scrap"


def test_piece_verdict_rejects_an_unknown_status(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 1)
    piece = led.pieces_for(run_id)[0]
    res = client.patch(f"/api/pieces/{piece['id']}",
                       json={"status": "lovely"})
    assert res.status_code == 400


def test_bulk_sets_a_whole_plate_with_one_exception(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 8)
    res = client.post(f"/api/runs/{run_id}/pieces/bulk",
                      json={"status": "good", "inspected_by": "adrian",
                            "overrides": [{"index_in_run": 3,
                                           "status": "scrap"}]})
    assert res.status_code == 200
    by_index = {p["index_in_run"]: p["status"] for p in led.pieces_for(run_id)}
    assert by_index[3] == "scrap"
    assert by_index[1] == by_index[8] == "good"


def test_operator_may_apply_a_human_badge_to_a_piece(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    led.create_pieces(run_id, 1)
    piece = led.pieces_for(run_id)[0]
    res = client.post(f"/api/pieces/{piece['id']}/badges",
                      json={"code": "warped"})
    assert res.status_code == 200
    assert [b["code"] for b in led.piece_badges(piece["id"])] == ["warped"]
    assert client.request(
        "DELETE", f"/api/pieces/{piece['id']}/badges",
        json={"code": "warped"}).status_code == 200


def test_a_badge_route_rejects_an_unknown_code(client, led):
    run_id = led.open_run(printer_serial="S1", source="queue")
    res = client.post(f"/api/runs/{run_id}/badges", json={"code": "banana"})
    assert res.status_code == 400
