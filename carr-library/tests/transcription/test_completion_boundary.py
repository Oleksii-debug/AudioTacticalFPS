"""Repeated actual protocol subprocesses; no model or acoustic claim."""
import importlib.util
import os
from pathlib import Path

import pytest

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
        budget=ProcessBudget(timeout=15, reserve_disk_bytes=0))
    profile = _builder.register(service, runtime)
    source = tmp_path / "synthetic.wav"
    source.write_bytes(b"Purpose-written protocol fixture, never decoded speech.")
    for attempt in range(32):
        job = service.create_job(profile["id"], [source])
        result = service.run(job["id"])
        assert result["status"] == "complete", {
            "attempt": attempt, "status": result["status"],
            "error": result["error"], "done": result["done"],
            "total": result["total"]}
        assert len(result["results"]) == 1


def test_verified_protocol_uses_the_same_stable_live_status_observation(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "completion_verified_fixture", Path(__file__).with_name("fixture_verified_runtime.py"))
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    runtime = builder.make_runtime(tmp_path / "runtime", options={"status_replacements": 2000})
    service = TranscriptionService(
        tmp_path / "library/state/transcription",
        budget=ProcessBudget(timeout=15, reserve_disk_bytes=0))
    profile = builder.register(service, runtime)
    source = tmp_path / "synthetic.wav"
    source.write_bytes(b"Public synthetic input; no acoustic recognition.")
    job = service.create_job(profile["id"], [source])
    result = service.run(job["id"])
    assert result["status"] == "complete", {
        key: result[key] for key in ("status", "error", "done", "total")}
    assert result["worker_completion"]["source_ids"] == [job["sources"][0]["id"]]
    assert service.read_transcript(job["id"])["text"] == "Synthetic verified protocol result."


@pytest.mark.parametrize("replacements", [2, 3])
def test_actual_replace_while_handle_is_open_is_bounded_and_reobserved(
        tmp_path, monkeypatch, replacements):
    path = tmp_path / "job_status.json"
    atomic_json(path, {"counter": 0})
    original = module._open_worker_status
    calls = []
    def replace_after_open(selected):
        descriptor = original(selected)
        calls.append(descriptor)
        if len(calls) <= replacements:
            atomic_json(path, {"counter": len(calls)}, replace=True)
        return descriptor
    monkeypatch.setattr(module, "_open_worker_status", replace_after_open)
    observed = module._observe_worker_status(path)
    assert len(calls) == 3
    assert observed == ({"counter": 2} if replacements == 2 else None)
    monkeypatch.setattr(module, "_open_worker_status", original)
    assert module._observe_worker_status(path) == {"counter": replacements}


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
