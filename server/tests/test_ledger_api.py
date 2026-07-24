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
