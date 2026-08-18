"""Unit tests for model_source.resolve_model_source.

No ray/vllm/GPU dependencies — exercises the resolver against fsspec's
in-memory filesystem and monkeypatched stand-ins for the hf:// path and
the missing-driver error. Run with:

    pytest -q runtimes/vllm/tests
"""
import sys
import types

import fsspec
import pytest
from fsspec.implementations.memory import MemoryFileSystem
from model_source import resolve_model_source


@pytest.fixture()
def memory_fs():
    """A clean fsspec in-memory filesystem, wiped after each test."""
    fs = fsspec.filesystem("memory")
    fs.store.clear()
    yield fs
    fs.store.clear()


def _seed_memory_model(fs, root="models/demo"):
    files = {
        f"/{root}/config.json": b'{"architectures": ["DemoModel"]}',
        f"/{root}/tokenizer/tokenizer.json": b"tok-data",
        f"/{root}/weights/model-00001.safetensors": b"\x00" * 64,
    }
    for path, data in files.items():
        fs.pipe(path, data)
    return files


def test_local_path_passthrough(tmp_path):
    path = str(tmp_path / "some" / "model-dir")
    assert resolve_model_source(path, cache_root=str(tmp_path / "cache")) == path


def test_non_string_source_rejected():
    with pytest.raises(ValueError, match="must be a string"):
        resolve_model_source({"bucket_uri": "gs://x"})  # type: ignore[arg-type]


def test_memory_mirror_round_trip(tmp_path, memory_fs):
    files = _seed_memory_model(memory_fs)
    dest = resolve_model_source("memory://models/demo", cache_root=str(tmp_path))

    assert dest == str(tmp_path / "memory" / "models" / "demo")
    for remote_path, data in files.items():
        rel = remote_path[len("/models/demo/"):]
        local = tmp_path / "memory" / "models" / "demo" / rel
        assert local.is_file(), f"missing mirrored file {rel}"
        assert local.read_bytes() == data


def test_idempotent_re_resolve_skips_copy(tmp_path, memory_fs, monkeypatch):
    _seed_memory_model(memory_fs)
    resolve_model_source("memory://models/demo", cache_root=str(tmp_path))

    calls = []
    real_get_file = MemoryFileSystem.get_file

    def counting_get_file(self, rpath, lpath, **kwargs):
        calls.append(rpath)
        return real_get_file(self, rpath, lpath, **kwargs)

    monkeypatch.setattr(MemoryFileSystem, "get_file", counting_get_file)

    dest = resolve_model_source("memory://models/demo", cache_root=str(tmp_path))
    assert dest == str(tmp_path / "memory" / "models" / "demo")
    assert calls == [], "second resolve must skip all already-mirrored files"


def test_size_change_triggers_re_download(tmp_path, memory_fs):
    _seed_memory_model(memory_fs)
    dest = resolve_model_source("memory://models/demo", cache_root=str(tmp_path))

    # Remote file changes size — the stale local copy must be replaced.
    memory_fs.pipe("/models/demo/config.json", b'{"architectures": ["DemoModel"], "revision": 2}')
    resolve_model_source("memory://models/demo", cache_root=str(tmp_path))

    local = tmp_path / "memory" / "models" / "demo" / "config.json"
    assert local.read_bytes() == b'{"architectures": ["DemoModel"], "revision": 2}'
    assert dest == str(tmp_path / "memory" / "models" / "demo")


def test_empty_prefix_raises(tmp_path, memory_fs):
    with pytest.raises(FileNotFoundError, match="no files at"):
        resolve_model_source("memory://models/does-not-exist", cache_root=str(tmp_path))


def test_missing_driver_names_extra(tmp_path, monkeypatch):
    def raise_import_error(protocol, **kwargs):
        raise ImportError(f"Install s3fs to access S3 ({protocol})")

    monkeypatch.setattr(fsspec, "filesystem", raise_import_error)
    with pytest.raises(RuntimeError, match=r"pip install s3fs"):
        resolve_model_source("s3://bucket/models/demo", cache_root=str(tmp_path))
    with pytest.raises(RuntimeError, match=r"pip install gcsfs"):
        resolve_model_source("gs://bucket/models/demo", cache_root=str(tmp_path))
    with pytest.raises(RuntimeError, match=r"pip install adlfs"):
        resolve_model_source("az://container/models/demo", cache_root=str(tmp_path))


def test_hf_dispatch(tmp_path, monkeypatch):
    recorded = {}

    def fake_snapshot_download(repo_id, local_dir, **kwargs):
        recorded["repo_id"] = repo_id
        recorded["local_dir"] = local_dir
        return local_dir

    fake_hub = types.ModuleType("huggingface_hub")
    fake_hub.snapshot_download = fake_snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)

    dest = resolve_model_source("hf://demo-org/demo-model", cache_root=str(tmp_path))

    assert recorded["repo_id"] == "demo-org/demo-model"
    assert dest == str(tmp_path / "hf" / "demo-org" / "demo-model")
    assert recorded["local_dir"] == dest


def test_hf_empty_repo_rejected(tmp_path):
    with pytest.raises(ValueError, match="hf://"):
        resolve_model_source("hf://", cache_root=str(tmp_path))
