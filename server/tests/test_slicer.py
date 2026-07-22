import json

import pytest

from server.slicer import ProfileIndex, SliceError, flatten_profile


def test_flatten_merges_parent_then_child():
    index = {
        "base": {"name": "base", "layer_height": "0.2", "walls": "2"},
        "kid": {"name": "kid", "inherits": "base", "walls": "3"},
    }
    assert flatten_profile("kid", index) == {
        "name": "kid", "layer_height": "0.2", "walls": "3"}


def test_flatten_drops_the_inherits_key_itself():
    # The slicer rejects a config that still names a parent it can't resolve.
    index = {"base": {"name": "base", "a": "1"},
             "kid": {"name": "kid", "inherits": "base"}}
    assert "inherits" not in flatten_profile("kid", index)


def test_flatten_walks_a_multi_level_chain():
    index = {
        "g": {"name": "g", "a": "1", "b": "1"},
        "p": {"name": "p", "inherits": "g", "b": "2", "c": "2"},
        "k": {"name": "k", "inherits": "p", "c": "3"},
    }
    assert flatten_profile("k", index) == {
        "name": "k", "a": "1", "b": "2", "c": "3"}


def test_flatten_raises_on_an_unknown_name():
    with pytest.raises(SliceError, match="nope"):
        flatten_profile("nope", {})


def test_flatten_raises_on_a_missing_parent():
    with pytest.raises(SliceError, match="missing"):
        flatten_profile("kid", {"kid": {"name": "kid", "inherits": "missing"}})


def test_flatten_raises_on_an_inheritance_cycle():
    # Must not recurse until the stack blows: a vendor tree that ships a cycle
    # would otherwise take the whole server down on a RecursionError.
    index = {"a": {"name": "a", "inherits": "b"},
             "b": {"name": "b", "inherits": "a"}}
    with pytest.raises(SliceError, match="cycle"):
        flatten_profile("a", index)


def test_profile_index_keys_on_the_name_field_not_the_filename(tmp_path):
    # Verified on the real tree: name and filename are not always the same.
    (tmp_path / "machine").mkdir()
    (tmp_path / "machine" / "whatever.json").write_text(
        json.dumps({"name": "Bambu Lab A1 0.4 nozzle", "a": "1"}),
        encoding="utf-8")
    index = ProfileIndex.load(tmp_path)
    assert "Bambu Lab A1 0.4 nozzle" in index
    assert "whatever" not in index


def test_profile_index_skips_unreadable_and_nameless_files(tmp_path):
    (tmp_path / "good.json").write_text(json.dumps({"name": "good"}),
                                        encoding="utf-8")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "nameless.json").write_text(json.dumps({"a": 1}),
                                            encoding="utf-8")
    index = ProfileIndex.load(tmp_path)
    assert list(index) == ["good"]


def test_profile_index_of_a_missing_directory_is_empty(tmp_path):
    assert ProfileIndex.load(tmp_path / "nope") == {}
