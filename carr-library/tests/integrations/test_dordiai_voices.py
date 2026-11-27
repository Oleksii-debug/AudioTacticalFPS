"""Actual pipe contract regression, with explicitly synthetic audio fixtures.

Real upstream no-model lifecycle/inventory checks are a separate explicitly
invoked dordiai_private_probe.py command; the default suite needs no private
installation and contains no skip gate.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from threading import Event, Thread
from time import monotonic, sleep

import pytest

from carr_library.integrations import dordiai_voices as api


# This tiny actor implements observed public framing. It is not acoustic code
# and is not copied from dordiai-voices. Every waveform and identity is synthetic.
ACTOR = r'''
import base64
import json
import os
from pathlib import Path
from queue import Queue
import sys
from threading import Event, Thread

OUTPUT_RATE = 24000
CLASSMATE_LOCALES = frozenset({'sk'})

def main():
    incoming, cancelled = Queue(), Event()
    def read():
        for line in sys.stdin:
            request = json.loads(line)
            with Path(__file__).with_name('requests.jsonl').open('a', encoding='utf8') as log:
                log.write(json.dumps(request, ensure_ascii=True) + '\n')
            if request['mode'] == 'cancel':
                cancelled.set()
                continue
            if request['mode'] == 'reference':
                cancelled.clear()
            incoming.put(request)
        cancelled.set()
        incoming.put(None)
    Thread(target=read, daemon=True).start()
    remaining = 0
    def reply(value):
        print(json.dumps(value), flush=True)
    def pcm():
        reply({'chunk': base64.b64encode(b'\x01\x00\x02\x00\x03\x00').decode('ascii'),
               'engine': 'synthetic-fixture', 'profile': 'fixture_one'})
    while (request := incoming.get()) is not None:
        mode = request['mode']
        if mode == 'preload':
            reply({'ready': True})
        elif mode == 'drop':
            remaining = 0
            reply({'finished': True})
        elif mode == 'next':
            if cancelled.is_set() or not remaining:
                reply({'finished': True})
            else:
                remaining -= 1
                pcm()
        elif mode == 'reference':
            text = request['text']
            remaining = 1
            if text in {'fixture:delay', 'fixture:timeout'}:
                cancelled.wait(10)
                if cancelled.is_set():
                    reply({'finished': True})
                    continue
            if text == 'fixture:error':
                reply({'error': 'ValueError', 'diagnostics': {'message': 'PRIVATE-TRANSCRIPT'}})
            elif text == 'fixture:empty':
                reply({'finished': True})
            elif text == 'fixture:malformed':
                reply({'chunk': '@@@'})
            elif text == 'fixture:odd':
                reply({'chunk': 'YQ=='})
            elif text == 'fixture:unknown-stage':
                reply({'loadingStage': 'unknown'})
                pcm()
            else:
                if text == 'fixture:progress':
                    reply({'loadingStage': 'language-source'})
                pcm()

if __name__ == '__main__':
    main()
'''


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "Installed Voice Engine" / "kx47Runtime"
    root.mkdir(parents=True)
    (root / "models/voices").mkdir(parents=True)
    (root / "models/Holos").mkdir()
    (root.parent / "manifest.ini").write_text('name = dordiai-voices\nversion = "fixture.1"\n')
    (root / "nvda_worker.py").write_text(ACTOR, newline="\n")
    (root / "models/voices/catalog.json").write_text(json.dumps({
        "fixture_one": "Fixture One", "blocked_fixture": "Excluded fixture",
        "fixture_missing": "No installed profile"}))
    (root / "windows_runtime.py").write_text(
        'def reset_inherited_dll_directory(): return {}\n'
        'def configure_worker_import_paths(): return {}\n')
    (root / "voice_policy.py").write_text(
        'def filter_voice_catalog(values):\n'
        ' return {key: value for key, value in values.items() if key != "blocked_fixture"}\n')
    (root / "voice_assets.py").write_text(
        'def discover_voices(references, engine_id=None):\n'
        ' return {"fixture_managed": {"name": "Managed fixture", "languages": ["en"],\n'
        '  "files": {"profile.bin": {"role": "pocket_english_state"}}}}\n')
    (root / "language_catalog.py").write_text('''
def canonical_code(value): return value.lower().replace('_', '-')
def coverage(models=None, selected_only=True):
 return [{"runtime_code": code, "settings_label_uk": code, "resources_present": present,
          "status": "resources-present-unverified" if present else "missing-resources"}
         for code, present in [("uk", True), ("en", True), ("sk", True), ("ja", True), ("ru", False)]]
def compatible_voice_ids(models, language, allowed_voice_ids):
 return tuple(key for key in allowed_voice_ids if key == "fixture_one")
''')
    (root / "wide_languages.py").write_text('def registered(code): return code if code == "ja" else None\n')
    return root


@pytest.fixture
def adapter(runtime):
    with api.DordiaiVoicesAdapter(runtime, python_executable=sys.executable,
                                 expected_worker_sha256=hashlib.sha256(ACTOR.encode()).hexdigest(),
                                 request_timeout=3.0) as value:
        yield value


def requests(runtime):
    return [json.loads(line) for line in (runtime / "requests.jsonl").read_text().splitlines()]


def test_private_probe_dynamic_catalogue_policy_and_managed_roles(adapter):
    before = set(sys.modules)
    voices = {voice.id: voice for voice in adapter.voices()}
    assert set(voices) == {"fixture_one", "fixture_managed"}
    assert voices["fixture_managed"].languages == ("en",)
    assert voices["fixture_one"].languages == ("uk", "en", "sk", "ja")
    assert not {"voice_policy", "voice_assets", "language_catalog"}.intersection(set(sys.modules) - before)
    assert next(row for row in adapter.language_statuses() if row["code"] == "ru")["resources_present"] is False


def test_capabilities_do_not_promise_ssml_emotions_phonemes_or_pronunciation(adapter):
    caps = adapter.capabilities()
    assert (caps.engine_id, caps.adapter_api_version, caps.engine_version) == ("dordiai-voices", 1, "fixture.1")
    assert (caps.sample_rate, caps.channels, caps.sample_width, caps.sample_format) == (24000, 1, 2, "s16le")
    assert caps.upstream_protocol_version is None
    assert not any((caps.ssml, caps.phoneme_input, caps.emotion, caps.pronunciation_dictionary,
                    caps.word_timestamps, caps.pronunciation_verified))
    assert caps.styles == ("neutral",)


@pytest.mark.parametrize(("language", "flag"), [("uk", None), ("en", "nativeProfile"),
                                                  ("sk", "nativeLocale"), ("ja", "wideLocale")])
def test_actual_reference_pull_framing_routes_and_pcm(adapter, runtime, language, flag):
    chunks = list(adapter.stream_pcm("A synthetic fixture.", "fixture_one", language, speed=1.2,
                                     pitch=51, timbre=52, volume=80))
    assert [chunk.frames for chunk in chunks] == [3, 3]
    assert all(chunk.data == b"\x01\x00\x02\x00\x03\x00" for chunk in chunks)
    sent = requests(runtime)
    assert [row["mode"] for row in sent] == ["reference", "next", "next"]
    assert sent[0]["controls"] == {"pitch": 51, "timbre": 52, "volume": 80}
    for name in ("nativeProfile", "nativeLocale", "wideLocale"):
        assert sent[0][name] is (name == flag)


def test_streamed_loading_and_frame_progress_are_not_percentages(adapter):
    progress = []
    chunks = list(adapter.stream_pcm("fixture:progress", "fixture_one", "uk", progress=progress.append))
    assert len(chunks) == 2
    assert [(entry.stage, entry.rendered_frames) for entry in progress] == [
        ("language-source", 0), ("pcm", 3), ("pcm", 6)]
    assert progress[-1].rendered_seconds == 6 / 24000


@pytest.mark.parametrize(("text", "error"), [("fixture:malformed", api.DordiaiProtocolError),
                                               ("fixture:odd", api.DordiaiProtocolError),
                                               ("fixture:empty", api.DordiaiEngineError),
                                               ("fixture:unknown-stage", api.DordiaiProtocolError),
                                               ("fixture:error", api.DordiaiEngineError)])
def test_bad_audio_and_engine_errors_do_not_report_success(adapter, text, error):
    with pytest.raises(error) as caught:
        list(adapter.stream_pcm(text, "fixture_one", "uk"))
    assert "PRIVATE-TRANSCRIPT" not in str(caught.value)
    assert len(list(adapter.stream_pcm("Recovery.", "fixture_one", "uk"))) == 2


def test_cancel_out_of_band_drains_old_finished_then_drop_ack(adapter, runtime):
    adapter.voices()  # Discovery is separate from a currently running render.
    errors = []
    def run():
        try:
            list(adapter.stream_pcm("fixture:delay", "fixture_one", "uk"))
        except Exception as error:
            errors.append(error)
    thread = Thread(target=run)
    thread.start()
    for _ in range(200):
        log = runtime / "requests.jsonl"
        if log.exists() and '"reference"' in log.read_text():
            break
        sleep(0.005)
    adapter.cancel()
    thread.join(timeout=4)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], api.DordiaiCancelled)
    # A leftover finished reply would incorrectly turn this into EmptyAudio.
    assert len(list(adapter.stream_pcm("New request.", "fixture_one", "uk"))) == 2
    sent = [row["mode"] for row in requests(runtime)]
    assert "cancel" in sent and "drop" in sent


def test_cancel_from_progress_callback_yields_no_stale_pcm(adapter):
    def progress(_):
        adapter.cancel()
    with pytest.raises(api.DordiaiCancelled):
        next(adapter.stream_pcm("Fixture.", "fixture_one", "uk", progress=progress))
    assert len(list(adapter.stream_pcm("Recovery.", "fixture_one", "uk"))) == 2


def test_cancel_after_yield_does_not_request_more_native_generation(adapter, runtime):
    cancelled = Event()
    stream = adapter.stream_pcm("Fixture.", "fixture_one", "uk", cancelled=cancelled)
    assert next(stream).frames == 3
    cancelled.set()
    with pytest.raises(api.DordiaiCancelled):
        next(stream)
    assert [row["mode"] for row in requests(runtime)] == ["reference", "cancel", "drop"]


def test_external_pre_cancel_does_not_start_worker(adapter, runtime):
    cancel = Event()
    cancel.set()
    with pytest.raises(api.DordiaiCancelled):
        list(adapter.stream_pcm("Fixture.", "fixture_one", "uk", cancelled=cancel))
    assert not (runtime / "requests.jsonl").exists()


def test_generic_worker_cancellation_callback_is_supported(adapter):
    with pytest.raises(api.DordiaiCancelled):
        list(adapter.stream_pcm("Fixture.", "fixture_one", "uk", cancelled=lambda: True))


def test_timeout_is_bounded_and_next_render_is_synchronized(adapter):
    adapter.voices()
    adapter.request_timeout = 0.1
    with pytest.raises(api.DordiaiTimeout):
        list(adapter.stream_pcm("fixture:timeout", "fixture_one", "uk"))
    adapter.request_timeout = 3.0
    assert len(list(adapter.stream_pcm("Recovery.", "fixture_one", "uk"))) == 2


def test_generator_close_and_busy_are_safe(adapter):
    stream = adapter.stream_pcm("First.", "fixture_one", "uk")
    next(stream)
    with pytest.raises(api.DordiaiBusy):
        list(adapter.stream_pcm("Second.", "fixture_one", "uk"))
    stream.close()
    assert len(list(adapter.stream_pcm("After close.", "fixture_one", "uk"))) == 2


def test_no_unbounded_audio_when_worker_never_finishes(adapter, monkeypatch):
    monkeypatch.setattr(api, "MAX_RENDERED_FRAMES", 3)
    stream = adapter.stream_pcm("Budget.", "fixture_one", "uk")
    assert next(stream).frames == 3
    with pytest.raises(api.DordiaiProtocolError, match="audio budget"):
        next(stream)


def test_midstream_package_change_invalidates_render(adapter, runtime):
    stream = adapter.stream_pcm("First.", "fixture_one", "uk")
    next(stream)
    (runtime / "installed_resource_packs.json").write_text('{"packs":{}}')
    with pytest.raises(api.DordiaiUnavailable, match="packages changed"):
        next(stream)
    assert len(list(adapter.stream_pcm("After refresh.", "fixture_one", "uk"))) == 2


@pytest.mark.parametrize("kwargs", [{"speed": float("nan")}, {"speed": 0}, {"pitch": True},
                                    {"volume": 101}, {"timbre": -1}, {"style": "sad"}])
def test_unsupported_controls_are_not_silently_ignored(adapter, kwargs):
    with pytest.raises(ValueError):
        list(adapter.stream_pcm("Fixture.", "fixture_one", "uk", **kwargs))


@pytest.mark.parametrize(("voice", "language"), [("blocked_fixture", "uk"), ("fixture_missing", "uk"),
                                                  ("fixture_one", "ru"), ("fixture_one", "en-GB")])
def test_unavailable_voice_language_or_unverified_accent_fails_explicitly(adapter, voice, language):
    with pytest.raises(api.DordiaiUnavailable):
        list(adapter.stream_pcm("Fixture.", voice, language))


def test_changed_worker_cannot_be_silently_used(runtime):
    with pytest.raises(api.DordiaiProtocolError, match="fingerprint"):
        api.DordiaiVoicesAdapter(runtime, python_executable=sys.executable)


def test_remote_path_and_missing_interpreter_are_rejected(runtime):
    with pytest.raises(api.DordiaiUnavailable, match="local"):
        api.DordiaiVoicesAdapter("https://example.invalid/voices")
    with pytest.raises(api.DordiaiUnavailable):
        api.DordiaiVoicesAdapter(runtime)


def test_resolved_path_outside_selected_runtime_is_rejected(runtime):
    outside = runtime.parent / "external-catalogue.json"
    outside.write_text("{}")
    with pytest.raises(api.DordiaiUnavailable, match="escapes"):
        api._inside(runtime, "../external-catalogue.json")


@pytest.mark.parametrize("source", ["print('x' * 512, flush=True)", "print('[]', flush=True)",
                                   "print('{\"value\": NaN}', flush=True)"])
def test_process_limits_frames_and_rejects_invalid_json(tmp_path, source):
    child = api._JsonLinesProcess([sys.executable, "-I", "-B", "-u", "-c", source], tmp_path, max_reply_bytes=128)
    try:
        with pytest.raises(api.DordiaiProtocolError):
            child.receive(3)
    finally:
        child.close()
    assert child.process.poll() is not None


def test_private_worker_gets_offline_flags_without_host_environment_changes(tmp_path):
    before = dict(os.environ)
    source = "import os,json; print(json.dumps({k:os.environ.get(k) for k in ['HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE','DO_NOT_TRACK']}),flush=True)"
    child = api._JsonLinesProcess([sys.executable, "-I", "-B", "-u", "-c", source], tmp_path)
    try:
        assert child.receive(3) == {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "DO_NOT_TRACK": "1"}
    finally:
        child.close()
    assert dict(os.environ) == before


def test_deep_bounded_json_fails_as_protocol_error_without_dead_reader(tmp_path):
    source = "print('{\"value\":'+'['*10000+'0'+']'*10000+'}',flush=True)"
    child = api._JsonLinesProcess([sys.executable, "-I", "-B", "-u", "-c", source], tmp_path)
    try:
        with pytest.raises(api.DordiaiProtocolError, match="invalid JSON"):
            child.receive(1.0)
    finally:
        child.close()
    assert not child._reader.is_alive()


def test_stalled_worker_cannot_block_large_stdin_or_shutdown(tmp_path):
    child = api._JsonLinesProcess([sys.executable, "-I", "-B", "-u", "-c", "import time; time.sleep(30)"], tmp_path)
    started = monotonic()
    try:
        child.send({"text": "x" * 900000})
        with pytest.raises(api.DordiaiTimeout):
            child.receive(0.05)
    finally:
        child.close()
    assert monotonic() - started < 8
    assert child.process.poll() is not None
    assert not child._writer.is_alive()
