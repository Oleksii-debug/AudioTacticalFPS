"""Independent real-process acceptance of CARR's external Dordiai adapter.

No inference, voices, or private assets are used. These tests establish transport
and policy behavior, not audio quality, Windows device output, or NVDA acceptance.
"""
from __future__ import annotations

import hashlib
import json
import struct
import sys
from threading import Event, Thread
from time import monotonic, sleep

import pytest


def records(runtime):
    log = runtime / "requests.jsonl"
    return [json.loads(row) for row in log.read_text().splitlines()] if log.exists() else []


def scenario(runtime, value):
    (runtime / "scenario.json").write_text(json.dumps({"mode": value}))


def wait_for(predicate, timeout=2.0):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.005)
    pytest.fail("Synthetic worker did not reach its expected bounded checkpoint")


def test_exact_pull_protocol_preserves_unicode_and_pcm_without_inference(adapter, runtime):
    text = "Synthetic: \u041a\u0438\u0457\u0432; a\u0301; newline\nsecond line."
    chunks = list(adapter.stream_pcm(text, "speaker_a", "uk"))
    assert [chunk.data for chunk in chunks] == [struct.pack("<3h", 100, 101, 102),
                                              struct.pack("<3h", 110, 111, 112)]
    assert all((c.sample_rate, c.channels, c.sample_width, c.frames) == (24000, 1, 2, 3)
               for c in chunks)
    requests = records(runtime)
    assert [r["mode"] for r in requests] == ["reference", "next", "next"]
    assert requests[0]["text_sha256"] == hashlib.sha256(text.encode()).hexdigest()


def test_external_cancel_between_chunks_never_dispatches_next_generation(api, adapter, runtime):
    cancelled = Event()
    stream = adapter.stream_pcm("Synthetic cancellation.", "speaker_a", "uk", cancelled=cancelled)
    assert next(stream).frames == 3
    cancelled.set()
    with pytest.raises(api.DordiaiCancelled):
        next(stream)
    modes = [r["mode"] for r in records(runtime)]
    assert "next" not in modes, "Cancellation was known before resume, but new generation was dispatched"
    assert modes[0] == "reference" and "cancel" in modes and modes[-1] == "drop"


def test_cancel_drains_loading_and_inflight_chunk_before_drop_ack(api, adapter, runtime):
    scenario(runtime, "late_cancel_chunk")
    output, errors = [], []

    def run():
        try:
            output.extend(adapter.stream_pcm("Late synthetic reply.", "speaker_a", "uk"))
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=run, daemon=True)
    thread.start()
    wait_for(lambda: (runtime / "awaiting-cancel").exists())
    adapter.cancel()
    thread.join(timeout=3)
    assert not thread.is_alive() and not output
    assert len(errors) == 1 and isinstance(errors[0], api.DordiaiCancelled)
    assert [r["mode"] for r in records(runtime)][-1] == "drop"
    # Replace the old process explicitly to make the following scenario normal.
    adapter.voices(refresh=True)
    scenario(runtime, "normal")
    assert len(list(adapter.stream_pcm("After cancellation.", "speaker_a", "uk"))) == 2


def test_bad_drop_ack_disposes_owned_process_before_following_request(api, adapter, runtime):
    scenario(runtime, "bad_drop")
    stream = adapter.stream_pcm("Incomplete fixture.", "speaker_a", "uk")
    next(stream)
    old = adapter._worker.process
    stream.close()
    assert old.poll() is not None and adapter._worker is None
    scenario(runtime, "normal")
    assert len(list(adapter.stream_pcm("Recovered fixture.", "speaker_a", "uk"))) == 2
    assert adapter._worker.process.pid != old.pid


def test_discovery_obeys_installed_resources_and_policy_after_explicit_refresh(api, adapter, runtime):
    assert {v.id: v.languages for v in adapter.voices()} == {"speaker_a": ("uk", "en")}
    inventory_file = runtime / "inventory.json"
    state = json.loads(inventory_file.read_text())
    state["present"]["uk"] = False
    state["managed"] = {
        "managed_a": {"name": "Synthetic managed", "languages": ["en"],
                      "files": {"synthetic.bin": {"role": "pocket_english_state"}}},
        "blocked:legacy": {"name": "Blocked managed", "languages": ["en"],
                           "files": {"synthetic.bin": {"role": "pocket_english_state"}}},
        "wrong_role": {"name": "Wrong role", "languages": ["en"],
                       "files": {"synthetic.bin": {"role": "holos_profile"}}},
    }
    inventory_file.write_text(json.dumps(state))
    assert {v.id: v.languages for v in adapter.voices(refresh=True)} == {
        "speaker_a": ("en",), "managed_a": ("en",)}
    with pytest.raises(api.DordiaiUnavailable):
        list(adapter.stream_pcm("Unavailable language.", "speaker_a", "uk"))
    assert not records(runtime)


def test_capabilities_never_turn_transport_success_into_quality_or_timing_claims(adapter):
    caps = adapter.capabilities()
    assert caps.upstream_protocol_version is None
    assert caps.transport == "local-jsonl-stdio" and caps.styles == ("neutral",)
    assert not any((caps.ssml, caps.phoneme_input, caps.pronunciation_dictionary,
                    caps.word_timestamps, caps.emotion, caps.pronunciation_verified))
    with pytest.raises(ValueError):
        list(adapter.stream_pcm("Plain fixture.", "speaker_a", "uk", style="dramatic"))


def test_deep_json_is_an_immediate_protocol_failure_not_a_dead_reader(api, tmp_path):
    # The C JSON parser's recursion ceiling can differ from Python's frame
    # recursion limit. This is only 20 KiB, well below the framing budget.
    depth = 10000
    source = ("print('{\"value\":'+'['*" + str(depth)
              + "+'0'+']'*" + str(depth) + "+'}',flush=True)")
    child = api._JsonLinesProcess([sys.executable, "-I", "-B", "-u", "-c", source], tmp_path)
    try:
        with pytest.raises(api.DordiaiProtocolError):
            child.receive(0.75)
    finally:
        child.close()
    assert child.process.poll() is not None


def test_request_deadline_also_bounds_a_worker_that_never_reads_stdin(api, adapter, runtime):
    adapter.voices()
    scenario(runtime, "stall_reader")
    adapter.request_timeout = 0.1
    errors = []

    def render():
        try:
            list(adapter.stream_pcm("\u0416" * 16384, "speaker_a", "uk"))
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=render, daemon=True)
    thread.start()
    wait_for(lambda: (runtime / "worker-ready.json").exists())
    # Permit the adapter's documented two-second drain and process shutdown
    # grace in addition to the request timeout. The actor otherwise stalls 15s.
    thread.join(timeout=4.5)
    terminated_in_budget = not thread.is_alive()
    # Always release the deliberately blocked pipe before assertions/teardown.
    worker = adapter._worker
    if worker is not None and worker.process.poll() is None:
        worker.process.kill()
    thread.join(timeout=3)
    assert not thread.is_alive(), "Fixture cleanup could not release the owned worker"
    assert terminated_in_budget, "A blocked stdin write exceeded the request and finite cleanup budget"
    assert errors and isinstance(errors[0], (api.DordiaiTimeout, api.DordiaiProtocolError))

