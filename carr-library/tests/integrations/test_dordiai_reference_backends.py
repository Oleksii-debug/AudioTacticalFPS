"""Additional reviewed owner protocols through actual isolated JSONL children.

All metadata, assets and PCM below are purpose-written synthetic fixtures.
These tests qualify the host's metadata/framing bounds, never model inference.
The separate managed-backend suite uses the real library approval and OS lease.
"""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.integrations import dordiai_voices as api

BACKENDS = ('omnivoice', 'openvoice')
PCM = b'\x01\x00\x02\x00\x03\x00'
WORKER = r'''
import argparse
import base64
import json
from pathlib import Path
from queue import Queue
import sys
from threading import Event, Thread

OUTPUT_RATE = 24000
CLASSMATE_LOCALES = frozenset({'sk'})

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('models')
    parser.add_argument('--reference-model', required=True)
    parser.add_argument('--references', required=True)
    parser.add_argument('--reference-engine', choices=('omnivoice','openvoice','holos'), required=True)
    parser.add_argument('--preload-language', required=True)
    args = parser.parse_args()
    with Path(__STARTS__).open('a', encoding='utf-8') as output:
        output.write(json.dumps(vars(args), ensure_ascii=True) + '\n')
    incoming, cancelled = Queue(), Event()
    def read():
        for line in sys.stdin:
            value = json.loads(line)
            with Path(__REQUESTS__).open('a', encoding='utf-8') as output:
                output.write(json.dumps(value, ensure_ascii=True) + '\n')
            if value['mode'] == 'cancel':
                cancelled.set()
                continue
            if value['mode'] == 'reference':
                cancelled.clear()
            incoming.put(value)
        cancelled.set()
        incoming.put(None)
    Thread(target=read, daemon=True).start()
    remaining = 0
    encoded = base64.b64encode(b'\x01\x00\x02\x00\x03\x00').decode('ascii')
    def reply(value):
        print(json.dumps(value), flush=True)
    while (value := incoming.get()) is not None:
        mode = value['mode']
        if mode == 'drop':
            remaining = 0
            reply({'finished': True})
        elif mode == 'next':
            if args.reference_engine == 'omnivoice':
                reply({'error': 'UnexpectedNext'})
            elif not remaining or cancelled.is_set():
                reply({'finished': True})
            else:
                remaining -= 1
                reply({'chunk': encoded})
        elif mode == 'reference':
            terminal = args.reference_engine == 'omnivoice'
            assert value['stream'] is (not terminal)
            assert value['reportLoading'] is False
            assert not any(value[key] for key in ('nativeProfile','nativeLocale','wideLocale'))
            remaining = 1
            text = value['text']
            if text == 'fixture:delay':
                cancelled.wait(10)
                if cancelled.is_set():
                    reply({'finished': True})
                    continue
            field = 'pcm' if terminal else 'chunk'
            if text == 'fixture:malformed':
                reply({field: '@@@'})
            elif text == 'fixture:odd':
                reply({field: 'YQ=='})
            elif text == 'fixture:empty':
                reply({field: ''})
            elif text == 'fixture:mixed':
                reply({'pcm': encoded, 'chunk': encoded})
            elif text == 'fixture:wrong-field':
                reply({'chunk' if terminal else 'pcm': encoded})
            elif text == 'fixture:finished':
                reply({'finished': True})
            elif text == 'fixture:error':
                reply({'error': 'ValueError', 'diagnostics': {'private': 'SYNTHETIC-PRIVATE-TEXT'}})
            elif text == 'fixture:stage':
                reply({'loadingStage': 'language-source'})
                reply({field: encoded})
            else:
                reply({field: encoded})

if __name__ == '__main__':
    main()
'''


def worker_source(requests, starts):
    return WORKER.replace('__REQUESTS__', repr(str(requests))).replace('__STARTS__', repr(str(starts)))


def runtime_files(backend, requests, starts):
    """One inert metadata/runtime fixture, no real model is imported."""
    return {
        'nvda_worker.py': worker_source(requests, starts).encode('utf-8'),
        'windows_runtime.py': b'def reset_inherited_dll_directory(): return {}\ndef configure_worker_import_paths(): return {}\n',
        'voice_policy.py': b'def filter_voice_catalog(values): return {k:v for k,v in values.items() if k != "blocked-fixture"}\n',
        'language_catalog.py': b'def canonical_code(value): return value.lower().replace("_","-")\n',
        'wide_languages.py': b'def registered(code): return None\n',
        'reference_voices.py': b'raise RuntimeError("Synthetic reference model must never be imported for metadata")\n',
        'converted_voices.py': b'VOICE_FILES={"uk":"uk-fixture.onnx","en":"en-fixture.onnx","en_male":"male-fixture.onnx"}\nPROFILES={"male-fixture":("Synthetic","en","en_male")}\n',
        'models/OmniVoice/use_onnx.py': b'raise RuntimeError("Inert synthetic model must never be loaded")\n',
        'models/OpenVoice/model.bin': b'INERT selected model directory marker',
        'models/Holos/model.bin': b'INERT legacy model directory marker',
        'models/OpenVoiceV2/tone_converter.fp32.onnx': b'INERT converter',
        'models/OpenVoiceV2/tone_ref_encoder.onnx': b'INERT encoder',
        'models/Piper/uk-fixture.onnx': b'INERT uk carrier',
        'models/Piper/uk-fixture.onnx.json': b'{}',
        'models/Piper/en-fixture.onnx': b'INERT en carrier',
        'models/Piper/en-fixture.onnx.json': b'{}',
        'models/Piper/male-fixture.onnx': b'INERT male carrier',
        'models/Piper/male-fixture.onnx.json': b'{}',
    }


def synthetic_runtime(tmp_path, backend):
    root = tmp_path / 'Installed Reference Engine' / 'kx47Runtime'
    root.mkdir(parents=True)
    requests, starts = tmp_path / 'requests.jsonl', tmp_path / 'starts.jsonl'
    files = runtime_files(backend, requests, starts)
    for name, data in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    references = root / 'models/voices'
    references.mkdir()
    (references / 'catalog.json').write_bytes(b'{"legacy":"Unmanaged legacy"}')
    roles = ({'audio.dat': {'role': 'reference_audio'}, 'text.dat': {'role': 'reference_text'}}
             if backend == 'omnivoice' else {'embedding.dat': {'role': 'target_embedding'}})
    document = {'id': 'fixture', 'name': 'Synthetic managed reference',
        'engine': {'id': backend}, 'languages': ['uk', 'en', 'sk'], 'files': roles}
    documents = {'fixture': document, 'blocked-fixture': {**document, 'id': 'blocked-fixture'}}
    (references / 'fixture-documents.json').write_bytes(json.dumps(documents).encode('utf-8'))
    (root / 'voice_assets.py').write_bytes(b'''import json
from pathlib import Path
def discover_voices(references,engine_id=None):
 return {key:row for key,row in json.loads((Path(references)/'fixture-documents.json').read_text(encoding='utf-8')).items() if row['engine']['id']==engine_id}
''')
    return SimpleNamespace(root=root, requests=requests, starts=starts, backend=backend,
        model='models/' + ('OmniVoice' if backend == 'omnivoice' else 'OpenVoice'),
        checksum=sha256(files['nvda_worker.py']).hexdigest(), documents=documents)


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def metadata(case, *, backend=None, model=None):
    command = [sys.executable, '-I', '-B', '-u', str(Path(api.__file__).with_name('_dordiai_probe.py')),
               str(case.root), case.backend if backend is None else backend,
               case.model if model is None else model]
    child = api._JsonLinesProcess(command, case.root)
    try:
        result = child.receive(20)
    finally:
        child.close()
    assert child.process.poll() is not None and child._cleanup_done
    return result


@contextmanager
def protocol(case, monkeypatch):
    """Isolate framing from admission; managed tests cover real authority."""
    adapter = api.DordiaiVoicesAdapter(case.root, python_executable=sys.executable,
        expected_worker_sha256=case.checksum, request_timeout=5)
    configuration = {'reference_engine': case.backend, 'models': 'models',
                     'reference_model': case.model, 'references': 'models/voices'}
    inventory = {'reference_engine': case.backend, 'voices': [
        {'id': 'fixture', 'languages': ['uk']}], 'languages': [
        {'code': 'uk', 'resources_present': True, 'route': 'reference'}]}
    adapter._inventory_epoch = adapter._epoch()
    monkeypatch.setattr(adapter._authority, 'runtime_configuration', lambda: dict(configuration))
    monkeypatch.setattr(adapter, '_probe', lambda *args, **kwargs: deepcopy(inventory))
    real, children = api._JsonLinesProcess, []
    def observed(command, cwd, **kwargs):
        child = real(command, cwd, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(api, '_JsonLinesProcess', observed)
    try:
        yield adapter
    finally:
        adapter.close()
        assert all(child.process.poll() is not None and child._cleanup_done for child in children)


@pytest.mark.parametrize('backend', BACKENDS)
def test_actual_reference_metadata_child_uses_only_selected_backend_roles_and_available_languages(tmp_path, backend):
    case = synthetic_runtime(tmp_path, backend)
    before = set(sys.modules)
    result = metadata(case)
    assert result['reference_engine'] == backend
    assert result['worker_sha256'] == case.checksum and result['sample_rate'] == 24000
    assert {row['id'] for row in result['voices']} == {'fixture'}
    expected = ['en', 'sk', 'uk'] if backend == 'omnivoice' else ['en', 'uk']
    assert result['voices'][0]['languages'] == expected
    assert {row['code'] for row in result['languages']} == set(expected)
    assert all(row['resources_present'] is True and row['route'] == 'reference'
               and row['status'] == 'resources-present-unverified' for row in result['languages'])
    assert result['speech_rendering_verified'] is False and result['pronunciation_verified'] is False
    assert not {'voice_assets', 'voice_policy', 'converted_voices', 'reference_voices'}.intersection(set(sys.modules) - before)
    assert not case.requests.exists() and not case.starts.exists()


@pytest.mark.parametrize(('backend', 'missing'), [
    ('omnivoice', 'models/OmniVoice/use_onnx.py'),
    ('omnivoice', 'reference_voices.py'),
    ('openvoice', 'models/OpenVoiceV2/tone_converter.fp32.onnx'),
    ('openvoice', 'models/OpenVoiceV2/tone_ref_encoder.onnx'),
    ('openvoice', 'models/Piper/uk-fixture.onnx.json'),
])
def test_actual_reference_metadata_does_not_advertise_missing_model_or_carrier(tmp_path, backend, missing):
    case = synthetic_runtime(tmp_path, backend)
    (case.root / missing).unlink()
    result = metadata(case)
    uk = next(row for row in result['languages'] if row['code'] == 'uk')
    assert uk['resources_present'] is False and uk['status'] == 'resources-missing'
    assert 'uk' not in result['voices'][0]['languages']
    assert not case.requests.exists()


def test_actual_openvoice_metadata_requires_both_male_and_english_timing_carriers(tmp_path):
    case = synthetic_runtime(tmp_path, 'openvoice')
    document = {**case.documents['fixture'], 'id': 'male-fixture', 'languages': ['en']}
    path = case.root / 'models/voices/fixture-documents.json'
    path.write_bytes(json.dumps({'male-fixture': document}).encode('utf-8'))
    assert metadata(case)['voices'][0]['languages'] == ['en']
    (case.root / 'models/Piper/en-fixture.onnx').unlink()
    result = metadata(case)
    assert result['voices'][0]['languages'] == []
    assert result['languages'][0]['resources_present'] is False


@pytest.mark.parametrize(('backend', 'model'), [
    ('unknown', 'models/OmniVoice'), ('omnivoice', '../outside'),
    ('omnivoice', 'models\\OmniVoice'), ('omnivoice', 'models/Missing'),
])
def test_actual_reference_metadata_rejects_unknown_backend_or_unbound_model_without_private_error_text(tmp_path, backend, model):
    case = synthetic_runtime(tmp_path, 'omnivoice')
    result = metadata(case, backend=backend, model=model)
    assert set(result) == {'probe_error'}
    assert result['probe_error'] in {'ValueError', 'FileNotFoundError'}
    assert str(tmp_path) not in json.dumps(result)
    assert not case.requests.exists()


@pytest.mark.parametrize('backend', BACKENDS)
def test_actual_reference_metadata_refuses_missing_required_package_role(tmp_path, backend):
    case = synthetic_runtime(tmp_path, backend)
    row = deepcopy(case.documents['fixture'])
    row['files'] = {}
    (case.root / 'models/voices/fixture-documents.json').write_bytes(json.dumps({'fixture': row}).encode('utf-8'))
    assert metadata(case) == {'probe_error': 'ValueError'}


@pytest.mark.parametrize('backend', BACKENDS)
def test_actual_reference_pipe_uses_reviewed_argv_controls_and_terminal_or_pull_reply(tmp_path, monkeypatch, backend):
    case = synthetic_runtime(tmp_path, backend)
    progress = []
    with protocol(case, monkeypatch) as adapter:
        chunks = list(adapter._stream_pcm('Synthetic reference.', 'fixture', 'uk',
            speed=1.2, pitch=51, timbre=52, volume=80, progress=progress.append))
    count = 1 if backend == 'omnivoice' else 2
    assert [chunk.data for chunk in chunks] == [PCM] * count
    assert [item.rendered_frames for item in progress] == ([3] if count == 1 else [3, 6])
    sent = lines(case.requests)
    assert [row['mode'] for row in sent] == (['reference'] if count == 1 else ['reference', 'next', 'next'])
    assert sent[0]['stream'] is (backend != 'omnivoice')
    assert sent[0]['controls'] == {'pitch': 51, 'timbre': 52, 'volume': 80}
    assert sent[0]['speed'] == 1.2 and sent[0]['reportLoading'] is False
    assert all(sent[0][key] is False for key in ('nativeProfile','nativeLocale','wideLocale'))
    assert lines(case.starts) == [{'models': str(case.root / 'models'),
        'reference_model': str(case.root / case.model), 'references': str(case.root / 'models/voices'),
        'reference_engine': backend, 'preload_language': 'mixed'}]


@pytest.mark.parametrize('backend', BACKENDS)
@pytest.mark.parametrize('case_name', ['malformed', 'odd', 'empty', 'mixed', 'wrong-field', 'stage', 'finished', 'error'])
def test_actual_reference_pipe_rejects_bad_or_ambiguous_pcm_and_keeps_recovery_synchronized(tmp_path, monkeypatch, backend, case_name):
    case = synthetic_runtime(tmp_path, backend)
    expected = api.DordiaiEngineError if case_name in {'finished', 'error'} else api.DordiaiProtocolError
    with protocol(case, monkeypatch) as adapter:
        with pytest.raises(expected) as caught:
            list(adapter._stream_pcm('fixture:' + case_name, 'fixture', 'uk'))
        assert 'SYNTHETIC-PRIVATE-TEXT' not in str(caught.value)
        assert sum(chunk.frames for chunk in adapter._stream_pcm('Recovery.', 'fixture', 'uk')) == (
            3 if backend == 'omnivoice' else 6)
    assert [row['mode'] for row in lines(case.requests)].count('reference') == 2


@pytest.mark.parametrize('backend', BACKENDS)
@pytest.mark.parametrize('action', ['cancel', 'close'])
def test_actual_reference_pipe_cancel_or_abandoned_yield_never_sends_next_and_releases_child(tmp_path, monkeypatch, backend, action):
    case = synthetic_runtime(tmp_path, backend)
    with protocol(case, monkeypatch) as adapter:
        stopped = Event()
        stream = adapter._stream_pcm('Synthetic reference.', 'fixture', 'uk', cancelled=stopped)
        assert next(stream).data == PCM
        if action == 'cancel':
            stopped.set()
            with pytest.raises(api.DordiaiCancelled):
                next(stream)
        else:
            stream.close()
    assert [row['mode'] for row in lines(case.requests)] == ['reference', 'cancel', 'drop']


@pytest.mark.parametrize('backend', BACKENDS)
def test_actual_reference_pipe_keeps_audio_budget_and_cancel_before_yield(tmp_path, monkeypatch, backend):
    case = synthetic_runtime(tmp_path, backend)
    with protocol(case, monkeypatch) as adapter:
        monkeypatch.setattr(api, 'MAX_RENDERED_FRAMES', 2)
        with pytest.raises(api.DordiaiProtocolError, match='audio budget'):
            list(adapter._stream_pcm('Bounded.', 'fixture', 'uk'))
        monkeypatch.setattr(api, 'MAX_RENDERED_FRAMES', 24000)
        def stop(_):
            adapter.cancel()
        with pytest.raises(api.DordiaiCancelled):
            next(adapter._stream_pcm('Cancelled before delivery.', 'fixture', 'uk', progress=stop))
    assert all(row['mode'] != 'next' for row in lines(case.requests))


@pytest.mark.parametrize('backend', BACKENDS)
def test_actual_reference_pipe_cancels_a_pending_single_or_streaming_reply_without_success(tmp_path, monkeypatch, backend):
    case = synthetic_runtime(tmp_path, backend)
    with protocol(case, monkeypatch) as adapter:
        def cancel_after_request():
            return (case.requests.exists()
                    and '"mode": "reference"' in case.requests.read_text(encoding='utf-8'))
        with pytest.raises(api.DordiaiCancelled):
            list(adapter._stream_pcm('fixture:delay', 'fixture', 'uk', cancelled=cancel_after_request))
    modes = [row['mode'] for row in lines(case.requests)]
    assert modes[0] == 'reference' and modes[-1] == 'drop'
    assert 'cancel' in modes and 'next' not in modes
