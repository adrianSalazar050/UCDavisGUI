import os
import time

from server.runs import ACTIVE_WINDOW_S, find_active_run, newest_frame


def make_frame(runs_dir, run, layer, age_s=0.0):
    frames = runs_dir / run / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    p = frames / f"layer_{layer:04d}.jpg"
    p.write_bytes(b"\xff\xd8fake-jpeg")
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


def test_missing_dir_is_none(tmp_path):
    assert find_active_run(tmp_path / "nope") is None
    assert newest_frame(tmp_path / "nope") is None


def test_empty_dir_is_none(tmp_path):
    assert newest_frame(tmp_path) is None


def test_picks_run_with_most_recent_frame(tmp_path):
    make_frame(tmp_path, "20260101T000000_old", 50, age_s=600)
    make_frame(tmp_path, "20260716T120000_new", 3, age_s=5)
    assert find_active_run(tmp_path).name == "20260716T120000_new"


def test_stale_run_is_not_active(tmp_path):
    make_frame(tmp_path, "20260101T000000_old", 50, age_s=ACTIVE_WINDOW_S + 60)
    assert find_active_run(tmp_path) is None
    assert newest_frame(tmp_path) is None


def test_newest_frame_is_highest_layer_of_active_run(tmp_path):
    # layer 2 written most recently, but layer 10 is the highest layer number
    make_frame(tmp_path, "20260716T120000_a", 10, age_s=30)
    make_frame(tmp_path, "20260716T120000_a", 2, age_s=1)
    info = newest_frame(tmp_path)
    assert info["layer"] == 10
    assert info["run"] == "20260716T120000_a"
    assert info["path"].name == "layer_0010.jpg"


def test_ignores_non_frame_files(tmp_path):
    make_frame(tmp_path, "20260716T120000_a", 1, age_s=1)
    junk = tmp_path / "20260716T120000_a" / "frames" / "thumbs.db"
    junk.write_bytes(b"junk")
    info = newest_frame(tmp_path)
    assert info["layer"] == 1
