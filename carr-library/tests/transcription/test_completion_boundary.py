"""Repeated actual protocol subprocesses; no model or acoustic claim."""
import importlib.util
import json
import os
import threading
from pathlib import Path

import pytest

from carr_library import durable_state
from carr_library.durable_state import atomic_json
from carr_library.transcription import service as module

from carr_library.operations.process import ProcessBudget
from carr_library.transcription import TranscriptionService

_spec = importlib.util.spec_from_file_location(
    "completion_fixture", Path(__file__).with_name("fixture_runtime.py"))
_builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_builder)


def test_atomic_live_status_replacements_do_not_abort_verified_results(tmp_path):
    runtime = _builder.make_runtime(
        tmp_path / "runtime", options={"status_replacements": 2000})
    service = TranscriptionService(
        tmp_path / "library/state/transcription",
        budget=ProcessBudget(timeout=60 if os.name == 'nt' else 15, reserve_disk_bytes=0))
    profile = _builder.register(service, runtime)
    source = tmp_path / "synthetic.wav"
    source.write_bytes(b"Purpose-written protocol fixture, never decoded speech.")
    for attempt in range(32):
        job = service.create_job(profile["id"], [source])
        result = service.run(job["id"])
        if result["status"] != "complete":
            pytest.fail(json.dumps({
                "attempt": attempt, "status": result["status"],
                "error": result["error"], "done": result["done"],
                "total": result["total"]}, ensure_ascii=True), pytrace=False)
        assert len(result["results"]) == 1


def test_verified_protocol_uses_the_same_stable_live_status_observation(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "completion_verified_fixture", Path(__file__).with_name("fixture_verified_runtime.py"))
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    runtime = builder.make_runtime(tmp_path / "runtime", options={"status_replacements": 2000})
    service = TranscriptionService(
        tmp_path / "library/state/transcription",
        budget=ProcessBudget(timeout=60 if os.name == 'nt' else 15, reserve_disk_bytes=0))
    profile = builder.register(service, runtime)
    source = tmp_path / "synthetic.wav"
    source.write_bytes(b"Public synthetic input; no acoustic recognition.")
    job = service.create_job(profile["id"], [source])
    result = service.run(job["id"])
    if result["status"] != "complete":
        pytest.fail(json.dumps({key: result[key] for key in
            ("status", "error", "done", "total")}, ensure_ascii=True), pytrace=False)
    assert result["worker_completion"]["source_ids"] == [job["sources"][0]["id"]]
    assert service.read_transcript(job["id"])["text"] == "Synthetic verified protocol result."


@pytest.mark.parametrize("replacements", [2, 3])
def test_actual_replacement_between_path_and_open_is_bounded_and_reobserved(
        tmp_path, monkeypatch, replacements):
    path = tmp_path / "job_status.json"
    atomic_json(path, {"counter": 0})
    original = module._open_worker_status
    calls = []
    def replace_before_open(selected):
        calls.append(selected)
        if len(calls) <= replacements:
            atomic_json(path, {"counter": len(calls)}, replace=True)
        return original(selected)
    monkeypatch.setattr(module, "_open_worker_status", replace_before_open)
    observed = module._observe_worker_status(path)
    assert len(calls) == 3
    assert observed == ({"counter": 2} if replacements == 2 else None)
    monkeypatch.setattr(module, "_open_worker_status", original)
    assert module._observe_worker_status(path) == {"counter": replacements}


def test_windows_compatible_producer_retries_until_short_reader_closes(tmp_path, monkeypatch):
    """Actual file handles and atomic writes on both Windows and POSIX."""
    path = tmp_path / "job_status.json"
    atomic_json(path, {"counter": 0})
    original_replace = os.replace
    attempted = threading.Event()
    finished = threading.Event()
    errors = []

    def observed_replace(source, destination):
        try:
            return original_replace(source, destination)
        finally:
            attempted.set()

    def produce():
        try:
            atomic_json(path, {"counter": 1}, replace=True)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    monkeypatch.setattr(durable_state.os, "replace", observed_replace)
    descriptor = module._open_worker_status(path)
    producer = threading.Thread(target=produce, daemon=True)
    try:
        producer.start()
        assert attempted.wait(2), "The actual producer never attempted replacement."
        # A POSIX writer can already have replaced the pathname. A Windows
        # MoveFileEx writer retries while this handle still reads the old file.
        assert os.read(descriptor, 4096) == durable_state.encode({"counter": 0})
    finally:
        os.close(descriptor)
        producer.join(timeout=3)
    assert not producer.is_alive() and finished.is_set()
    if errors:
        raise errors[0]
    assert module._observe_worker_status(path) == {"counter": 1}


def test_persistent_status_hardlink_and_directory_are_rejected(tmp_path):
    path = tmp_path / "job_status.json"
    atomic_json(path, {"state": "running"})
    os.link(path, tmp_path / "alias.json")
    with pytest.raises(ValueError, match="without links"):
        module._observe_worker_status(path)
    with pytest.raises(ValueError, match="without links"):
        module._observe_worker_status(tmp_path)


@pytest.mark.parametrize("payload", [
    b'{"state":"running","state":"complete"}',
    b'{"progress":NaN}', b"null", b"[]", b"\xff", b"{",
])
def test_stable_invalid_status_is_not_treated_as_a_replacement(tmp_path, payload):
    path = tmp_path / "job_status.json"
    path.write_bytes(payload)
    with pytest.raises(ValueError):
        module._observe_worker_status(path)


def test_status_byte_budget_applies_before_read(tmp_path):
    path = tmp_path / "job_status.json"
    path.write_bytes(b" " * (256 * 1024 + 1))
    with pytest.raises(ValueError, match="byte budget"):
        module._observe_worker_status(path)
