"""Complete local model inventory contracts; no model execution or downloads."""
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from carr_library.audio import model_manifest as module
from carr_library.audio.model_manifest import LocalModelManifest, ModelManifestError


def model(root):
    asset = root / "voice.onnx"
    asset.write_bytes(b"Purpose-written inventory fixture, not a model.")
    path = root / "manifest.json"
    path.write_text(json.dumps({
        "schema_version": 1, "engine": "piper", "model_id": "synthetic-inventory",
        "revision": "a" * 40, "license": "synthetic-only", "license_accepted": True,
        "files": {"voice.onnx": hashlib.sha256(asset.read_bytes()).hexdigest()},
    }), encoding="utf-8")
    return LocalModelManifest.load(path)


@pytest.mark.parametrize("failure", [PermissionError, OSError])
def test_complete_inventory_rejects_unreadable_subtree(tmp_path, monkeypatch, failure):
    selected = model(tmp_path)
    hidden = tmp_path / "unreadable"
    hidden.mkdir()
    (hidden / "unreviewed.py").write_text("pass\n", encoding="utf-8")
    original = module.os.scandir

    def unreadable(path):
        if Path(path) == hidden:
            raise failure("synthetic enumeration failure")
        return original(path)

    monkeypatch.setattr(module.os, "scandir", unreadable)
    with pytest.raises(ModelManifestError, match="inventory is unreadable"):
        selected.verify(require_complete=True)


def test_empty_directories_count_toward_complete_inventory_bound(tmp_path, monkeypatch):
    selected = model(tmp_path)
    for number in range(4):
        (tmp_path / f"empty-{number}").mkdir()
    monkeypatch.setattr(module, "_MAX_INVENTORY_ENTRIES", 3)
    with pytest.raises(ModelManifestError, match="directory entry budget"):
        selected.verify(require_complete=True)


def test_native_inventory_iterator_is_bounded_before_whole_directory_allocation(tmp_path, monkeypatch):
    for number in range(8):
        (tmp_path / f"empty-{number}").mkdir()
    original = module.os.scandir
    observed = []

    class GuardedScan:
        def __init__(self, path):
            self.native = original(path)

        def __enter__(self):
            self.native.__enter__()
            return self

        def __exit__(self, *args):
            return self.native.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.native)
            observed.append(entry.name)
            if len(observed) > 4:
                raise AssertionError("The iterator allocated beyond the entry allowance.")
            return entry

    monkeypatch.setattr(module, "_MAX_INVENTORY_ENTRIES", 3)
    monkeypatch.setattr(module.os, "scandir", GuardedScan)
    with pytest.raises(ModelManifestError, match="directory entry budget"):
        list(module.inventory_files(tmp_path))
    assert len(observed) == 4


def test_inventory_cancellation_is_checked_between_native_entries(tmp_path, monkeypatch):
    for number in range(4):
        (tmp_path / f"empty-{number}").mkdir()
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls == 3

    with pytest.raises(InterruptedError, match="cancelled"):
        list(module.inventory_files(tmp_path, cancelled=cancelled))
    assert calls == 3


@pytest.mark.parametrize("name", ["unreviewed.py", "unreviewed.PY"])
def test_complete_inventory_keeps_case_variant_python_files_in_scope(tmp_path, name):
    selected = model(tmp_path)
    assert selected.verify(require_complete=True)["files"] == 1
    (tmp_path / name).write_text("pass\n", encoding="utf-8")
    with pytest.raises(ModelManifestError, match="missing from its reviewed manifest"):
        selected.verify(require_complete=True)


def test_music_cache_inventory_failure_prevents_worker_start(tmp_path, monkeypatch):
    """The existing MusicGen chunk boundary, with no executable model fixture."""
    from carr_library.audio import music

    cache = tmp_path / "hf-cache"
    hidden = cache / "unreadable"
    hidden.mkdir(parents=True)
    (hidden / "unreviewed.bin").write_bytes(b"synthetic cache entry")
    original = module.os.scandir

    def unreadable(path):
        if Path(path) == hidden:
            raise PermissionError("synthetic cache enumeration failure")
        return original(path)

    def forbidden(*args, **kwargs):
        pytest.fail("An incomplete cache inventory must never start a worker.")

    selected = SimpleNamespace(
        root=tmp_path, files=(),
        settings={"source_revision": music.MUSICGEN_SOURCE, "python": "selected-python"},
        asset=lambda _: Path(sys.executable).resolve(),
    )
    monkeypatch.setattr(module.os, "scandir", unreadable)
    monkeypatch.setattr(music, "_run", forbidden)
    with pytest.raises(ModelManifestError, match="inventory is unreadable"):
        music._render_musicgen_chunk(
            tmp_path / "unpublished.wav", frame_count=8000, sample_rate=8000,
            mood="calm", tempo=90, seed=1, gain=.1, ffmpeg=Path(sys.executable).resolve(),
            manifest=selected, previous=None, cancelled=lambda: False)
    assert not list(tmp_path.glob("*.wav"))
