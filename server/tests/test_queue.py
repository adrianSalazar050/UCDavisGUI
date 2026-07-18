import time

from server.queue import QueueStore, PrintQueue


def job(id, name, seconds=600, grams=10.0, source="3mf"):
    return {"id": id, "sd_path": "/" + name, "name": name,
            "seconds": seconds, "grams": grams, "source": source}


def test_add_remove_reorder_and_get(tmp_path):
    q = PrintQueue(QueueStore(tmp_path / "queues.json"))
    q.add("S1", job("a", "A.3mf"))
    q.add("S1", job("b", "B.3mf"))
    assert [j["id"] for j in q.get("S1")] == ["a", "b"]
    q.reorder("S1", ["b", "a"])
    assert [j["id"] for j in q.get("S1")] == ["b", "a"]
    assert q.remove("S1", "b") is True
    assert [j["id"] for j in q.get("S1")] == ["a"]
    assert q.remove("S1", "nope") is False


def test_totals(tmp_path):
    q = PrintQueue(QueueStore(tmp_path / "queues.json"))
    q.add("S1", job("a", "A.3mf", seconds=600, grams=10.0))
    q.add("S1", job("b", "B.3mf", seconds=1200, grams=5.5))
    t = q.totals("S1", now=1000.0)
    assert t["seconds"] == 1800 and t["grams"] == 15.5
    assert t["finish_epoch"] == 1000.0 + 1800   # planner hint, labeled ~ in UI


def test_totals_ignore_none_metrics(tmp_path):
    q = PrintQueue(QueueStore(tmp_path / "queues.json"))
    q.add("S1", job("a", "A.3mf", seconds=None, grams=None, source="manual"))
    q.add("S1", job("b", "B.3mf", seconds=600, grams=10.0))
    t = q.totals("S1", now=0.0)
    assert t["seconds"] == 600 and t["grams"] == 10.0


def test_persistence_round_trip(tmp_path):
    p = tmp_path / "queues.json"
    PrintQueue(QueueStore(p)).add("S1", job("a", "A.3mf"))
    assert [j["id"] for j in PrintQueue(QueueStore(p)).get("S1")] == ["a"]
