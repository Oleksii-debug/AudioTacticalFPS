"""Repeated actual protocol subprocesses; no model or acoustic claim."""
import importlib.util
from pathlib import Path

from carr_library.operations.process import ProcessBudget
from carr_library.transcription import TranscriptionService

_spec = importlib.util.spec_from_file_location(
    "completion_fixture", Path(__file__).with_name("fixture_runtime.py"))
_builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_builder)


def test_fast_atomic_status_publication_retains_complete_results(tmp_path):
    runtime = _builder.make_runtime(tmp_path / "runtime")
    service = TranscriptionService(
        tmp_path / "library/state/transcription",
        budget=ProcessBudget(timeout=15, reserve_disk_bytes=0))
    profile = _builder.register(service, runtime)
    source = tmp_path / "synthetic.wav"
    source.write_bytes(b"Purpose-written protocol fixture, never decoded speech.")
    for attempt in range(64):
        job = service.create_job(profile["id"], [source])
        result = service.run(job["id"])
        assert result["status"] == "complete", {
            "attempt": attempt, "status": result["status"],
            "error": result["error"], "done": result["done"],
            "total": result["total"]}
        assert len(result["results"]) == 1
        assert service.read_transcript(job["id"])["text"]
    starts = runtime.worker.with_name("fixture-starts.log").read_text().splitlines()
    assert len(starts) == 64


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
