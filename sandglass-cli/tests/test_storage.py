import json
import os

import pytest

from sandglass.storage import StorageService


@pytest.fixture
def storage(tmp_path):
    return StorageService(base_path=str(tmp_path / ".sandglass"))


def test_load_json_missing_file_returns_empty_dict(storage):
    assert storage.load_json(storage.queue_path) == {}


def test_save_json_then_load_json_roundtrips(storage):
    data = {"prompts": [{"id": "001", "title": "hello"}]}
    storage.save_json(storage.queue_path, data)

    assert os.path.exists(storage.queue_path)
    assert storage.load_json(storage.queue_path) == data


def test_save_json_is_atomic_no_leftover_tmp_file(storage):
    storage.save_json(storage.queue_path, {"a": 1})

    directory = os.path.dirname(storage.queue_path)
    leftovers = [f for f in os.listdir(directory) if f.endswith(".tmp")]
    assert leftovers == []


def test_load_json_backs_up_and_resets_corrupt_file(storage):
    os.makedirs(os.path.dirname(storage.queue_path), exist_ok=True)
    with open(storage.queue_path, "w", encoding="utf-8") as fh:
        fh.write("{not valid json")

    result = storage.load_json(storage.queue_path)

    assert result == {}
    directory = os.path.dirname(storage.queue_path)
    backups = [f for f in os.listdir(directory) if f.endswith(".bak")]
    assert len(backups) == 1


def test_ensure_sandglass_dir_creates_base_and_responses(storage):
    storage.ensure_sandglass_dir()

    assert os.path.isdir(storage.base_path)
    assert os.path.isdir(storage.responses_dir)


def test_save_json_writes_valid_utf8_json(storage):
    storage.save_json(storage.queue_path, {"title": "café ☕"})

    with open(storage.queue_path, "r", encoding="utf-8") as fh:
        loaded = json.load(fh)
    assert loaded["title"] == "café ☕"
