import threading

import pytest

from carr_library.audio.contracts import digest
from carr_library.audio.jobs import RenderJournal
from carr_library.audio.project import read_json


def test_completed_checkpoint_is_hashed_before_reuse_and_damage_is_rebuilt(tmp_path):
    journal = RenderJournal(tmp_path)
    calls = []

    def produce(path):
        calls.append(path)
        path.write_bytes(b"verified engine artifact")

    key = digest({"text": "same", "voice": "same"})
    first = journal.run(key, "test", produce)
    assert not first.reused
    reopened = RenderJournal(tmp_path)
    assert reopened.run(key, "test", produce).reused
    assert len(calls) == 1
    first.path.write_bytes(b"bad")
    assert not reopened.run(key, "test", produce).reused
    assert len(calls) == 2
    assert list((tmp_path / "damaged-checkpoints").iterdir())[0].read_bytes() == b"bad"


def test_cancelled_inflight_output_is_not_published_but_prior_work_survives(tmp_path):
    cancel = threading.Event()
    journal = RenderJournal(tmp_path, cancel=cancel)
    first = journal.run(digest("first"), "first", lambda p: p.write_bytes(b"complete"))

    def cancel_worker(path):
        path.write_bytes(b"partial")
        cancel.set()

    with pytest.raises(InterruptedError):
        journal.run(digest("second"), "second", cancel_worker)
    assert first.path.read_bytes() == b"complete"
    assert not (tmp_path / "cache" / (digest("second") + ".wav")).exists()
    assert read_json(tmp_path / "render-state.json")["status"] == "interrupted"


def test_failed_producer_retains_checkpoints_and_records_failure(tmp_path):
    journal = RenderJournal(tmp_path)

    def fail(path):
        path.write_bytes(b"partial")
        raise OSError("disk full")

    with pytest.raises(OSError):
        journal.run(digest("failure"), "speech", fail)
    state = read_json(tmp_path / "render-state.json")
    assert state["status"] == "failed"
    assert not list((tmp_path / "cache").glob("*.wav"))


def test_checkpoint_key_cannot_be_a_path(tmp_path):
    with pytest.raises(ValueError):
        RenderJournal(tmp_path).run("../../outside", "test", lambda p: p.write_bytes(b"x"))
