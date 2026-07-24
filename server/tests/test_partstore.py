import hashlib
import pathlib

import pytest

from server.partstore import PartStore


@pytest.fixture
def store(tmp_path):
    return PartStore(tmp_path)


def test_save_round_trips_bytes_and_reports_sha256(store):
    data = b"solid cube\nfacet normal 0 0 0\n" * 100
    meta = store.save("pid1", "cube.stl", data)
    assert meta["bytes"] == len(data)
    assert meta["sha256"] == hashlib.sha256(data).hexdigest()
    assert meta["filename"] == "cube.stl"
    assert store.open_bytes("pid1", "cube.stl") == data


def test_a_non_model_extension_is_rejected(store):
    with pytest.raises(ValueError):
        store.save("pid1", "notes.txt", b"nope")


def test_a_traversing_filename_lands_inside_the_part_dir(store, tmp_path):
    # A malicious filename must not escape the store root.
    meta = store.save("pid1", "../../evil.stl", b"data")
    assert meta["filename"] == "evil.stl"
    written = tmp_path / "parts" / "pid1" / "evil.stl"
    assert written.exists()
    # nothing escaped above the root
    assert not (tmp_path.parent / "evil.stl").exists()


def test_a_backslash_filename_is_also_basenamed(store, tmp_path):
    meta = store.save("pid1", "sub\\dir\\part.3mf", b"data")
    assert meta["filename"] in ("part.3mf",)   # directory stripped
    assert (tmp_path / "parts" / "pid1" / "part.3mf").exists()


def test_resave_leaves_exactly_one_file(store, tmp_path):
    store.save("pid1", "old.stl", b"a")
    store.save("pid1", "new.3mf", b"b")
    files = list((tmp_path / "parts" / "pid1").iterdir())
    assert [f.name for f in files] == ["new.3mf"]


def test_delete_removes_the_part_dir(store, tmp_path):
    store.save("pid1", "cube.stl", b"a")
    store.delete("pid1")
    assert not (tmp_path / "parts" / "pid1").exists()
    # delete of a nonexistent part is a no-op, not an error
    store.delete("pid-missing")
