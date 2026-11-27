"""Real child-process lifecycle checks using synthetic metadata, never voices.

The tiny installed-runtime fixtures below expose only the public metadata
shape. They contain no private engine implementation, model or recording.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from threading import Event, Thread
from time import monotonic

import pytest

from carr_library.integrations import dordiai_voices as api


@pytest.fixture
def probe_runtime(tmp_path):
    runtime = tmp_path / "Synthetic metadata runtime"
    (runtime / "models/voices").mkdir(parents=True)
    (runtime / "models/Holos").mkdir()
    worker = "OUTPUT_RATE = 24000\nCLASSMATE_LOCALES = frozenset()\n"
    (runtime / "nvda_worker.py").write_text(worker, encoding="utf-8", newline="\n")
    (runtime / "models/voices/catalog.json").write_text(
        json.dumps({"synthetic_voice": "Synthetic metadata only"}), encoding="utf-8")
    (runtime / "windows_runtime.py").write_text(
        "import os\nfrom pathlib import Path\nfrom time import sleep\n"
        "def reset_inherited_dll_directory():\n"
        " root = Path(__file__).parent\n"
        " (root / 'probe-started').write_text(str(os.getpid()))\n"
        " while (root / 'hold-probe').exists(): sleep(0.01)\n"
        "def configure_worker_import_paths(): pass\n", encoding="utf-8")
    (runtime / "voice_policy.py").write_text(
        "def filter_voice_catalog(values): return values\n", encoding="utf-8")
    (runtime / "voice_assets.py").write_text(
        "def discover_voices(references, engine_id=None): return {}\n", encoding="utf-8")
    (runtime / "wide_languages.py").write_text(
        "def registered(code): return False\n", encoding="utf-8")
    (runtime / "language_catalog.py").write_text(
        "def coverage(models, selected_only=False):\n"
        " return [{'runtime_code': 'en', 'settings_label_uk': 'English fixture',\n"
        "          'resources_present': True, 'status': 'synthetic-unverified'}]\n"
        "def compatible_voice_ids(models, code, allowed): return tuple(allowed)\n",
        encoding="utf-8")
    return runtime, hashlib.sha256(worker.encode("utf-8")).hexdigest()


@pytest.fixture
def children(monkeypatch):
    """Observe the real owned transport; no Popen or pipe behavior is mocked."""
    observed = []
    original = api._JsonLinesProcess

    class ObservedProcess(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.waits = []
            observed.append(self)

        def receive(self, timeout):
            self.waits.append(timeout)
            return super().receive(timeout)

    monkeypatch.setattr(api, "_JsonLinesProcess", ObservedProcess)
    yield observed
    for child in observed:
        child.close()


def adapter_for(probe_runtime, *, timeout=15):
    runtime, checksum = probe_runtime
    return api.DordiaiVoicesAdapter(runtime, python_executable=sys.executable,
        expected_worker_sha256=checksum, request_timeout=timeout)


def assert_closed(child):
    assert child.process.poll() is not None
    assert not child._reader.is_alive()
    assert not child._writer.is_alive()


@pytest.mark.parametrize(("operation", "callback"), [
    ("voices", False), ("capabilities", True),
    ("language_statuses", False), ("stream_pcm", True),
])
def test_cold_probe_cancel_closes_real_child_and_can_retry(probe_runtime, children, operation, callback):
    runtime, _ = probe_runtime
    hold = runtime / "hold-probe"
    hold.touch()
    cancel = Event()
    errors = []
    adapter = adapter_for(probe_runtime)

    def discover():
        try:
            cancelled = cancel.is_set if callback else cancel
            if operation == "stream_pcm":
                list(adapter.stream_pcm("Synthetic text; never rendered.", "synthetic_voice", "en",
                                        cancelled=cancelled))
            else:
                getattr(adapter, operation)(cancelled=cancelled)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=discover)
    thread.start()
    try:
        deadline = monotonic() + 5
        while not (runtime / "probe-started").exists() and thread.is_alive() and monotonic() < deadline:
            cancel.wait(0.01)
        assert (runtime / "probe-started").exists(), "The actual synthetic probe never started."
        assert children[0].process.poll() is None
        cancel.set()
        thread.join(timeout=5)
        assert not thread.is_alive(), "Cold discovery ignored cancellation until its long request timeout."
        assert len(errors) == 1 and isinstance(errors[0], api.DordiaiCancelled)
        assert_closed(children[0])
        assert adapter._inventory is None and adapter._inventory_epoch is None
        assert adapter._worker is None and adapter._active_cancel is None
        assert not adapter._operation.locked()

        hold.unlink()
        assert [voice.id for voice in adapter.voices()] == ["synthetic_voice"]
        assert len(children) == 2
        assert_closed(children[1])
    finally:
        cancel.set()
        hold.unlink(missing_ok=True)
        thread.join(timeout=5)
        adapter.close()


def test_pre_cancelled_discovery_never_launches_child_even_for_cached_inventory(probe_runtime, children):
    cancel = Event()
    cancel.set()
    with adapter_for(probe_runtime) as adapter:
        for operation in (adapter.voices, adapter.capabilities, adapter.language_statuses):
            with pytest.raises(api.DordiaiCancelled):
                operation(cancelled=cancel)
        assert children == []
        assert len(adapter.voices()) == 1
        cached = adapter._inventory
        with pytest.raises(api.DordiaiCancelled):
            adapter.voices(cancelled=cancel.is_set)
        assert adapter._inventory is cached
        assert len(children) == 1


def test_cancel_after_inventory_validation_does_not_cache_it(probe_runtime, children, monkeypatch):
    cancel = Event()
    with adapter_for(probe_runtime) as adapter:
        validate = adapter._validate_inventory

        def cancel_after_validation(value):
            validate(value)
            cancel.set()

        monkeypatch.setattr(adapter, "_validate_inventory", cancel_after_validation)
        with pytest.raises(api.DordiaiCancelled):
            adapter.voices(cancelled=cancel)
        assert adapter._inventory is None and adapter._inventory_epoch is None
        assert_closed(children[0])
        monkeypatch.setattr(adapter, "_validate_inventory", validate)
        assert len(adapter.voices()) == 1


def test_cold_probe_timeout_is_absolute_and_closes_real_child(probe_runtime, children):
    runtime, _ = probe_runtime
    hold = runtime / "hold-probe"
    hold.touch()
    with adapter_for(probe_runtime, timeout=0.2) as adapter:
        started = monotonic()
        with pytest.raises(api.DordiaiTimeout):
            adapter.voices()
        assert monotonic() - started < 4
        assert len(children[0].waits) >= 2
        assert all(0 < wait <= 0.05 for wait in children[0].waits)
        assert_closed(children[0])
        assert adapter._inventory is None and adapter._inventory_epoch is None
        hold.unlink()
        adapter.request_timeout = 3
        assert len(adapter.voices()) == 1
