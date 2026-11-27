import base64
import json
import os
from pathlib import Path
import shutil
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
import wave

import pytest

from carr_library.audio.engines import (DordiaiVoicesEngine, EngineUnavailable, FliteEngine, ESpeakEngine,
    SAPI5Engine, Voice, VoiceRegistry, _DORDIAI_OFFLINE_BOOTSTRAP, _SAPI_SCRIPT, _run, default_registry)


def wav(path, data=b'\x01\x00' * 240):
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(24000)
        stream.writeframes(data)


def test_registry_never_silently_substitutes_missing_engine(tmp_path):
    registry = VoiceRegistry()
    with pytest.raises(EngineUnavailable, match='No substitute'):
        registry.synthesize('Test', tmp_path / 'voice.wav', voice='sapi5:default')
    assert not (tmp_path / 'voice.wav').exists()


@pytest.mark.parametrize('setting,value', [('rate', float('nan')), ('volume', float('inf')),
                                        ('pitch', True), ('volume', 1.2), ('rate', -11)])
def test_invalid_controls_fail_before_engine_execution(tmp_path, setting, value):
    with pytest.raises(ValueError):
        VoiceRegistry().synthesize('Test', tmp_path / 'voice.wav', voice='missing:voice', **{setting: value})


def test_sapi_command_is_constant_and_text_remains_json_data(tmp_path, monkeypatch):
    # Protocol fixture only; the real installed-host case below stays unpatched.
    monkeypatch.setattr('carr_library.audio.engines._system_powershell',
                        lambda path=None, **options: Path(sys.executable).resolve())
    engine = SAPI5Engine(Path(sys.executable).resolve())
    captured = []
    def fake(command, **kwargs):
        captured.append((command, kwargs))
        request = json.loads(kwargs['data'])
        if request['operation'] == 'voices':
            return json.dumps([{'id':'test-id','name':'Test Voice','language':'en-US',
                                'default':True,'version':'fixture'}]).encode()
        wav(request['output'])
        return b'{"complete":true,"voice_id":"test-id"}'
    hostile = '$(Remove-Item C:\\sample); <speak>not executable SSML</speak> "quotes"'
    with patch('carr_library.audio.engines._run', fake):
        engine.synthesize(hostile, tmp_path / 'voice.wav', voice='sapi5:default')
        assert engine.cache_identity('sapi5:default')['version'] == 'fixture'
    command, kwargs = captured[-1]
    assert hostile not in ' '.join(map(str, command))
    assert json.loads(kwargs['data'])['text'] == hostile
    assert base64.b64decode(command[-1]).decode('utf-16-le') == _SAPI_SCRIPT
    assert '.SpeakSsml' not in _SAPI_SCRIPT
    assert (tmp_path / 'voice.wav').is_file()


def test_sapi_unavailable_pitch_and_language_fail_closed(tmp_path, monkeypatch):
    # Protocol fixture only; the real installed-host case below stays unpatched.
    monkeypatch.setattr('carr_library.audio.engines._system_powershell',
                        lambda path=None, **options: Path(sys.executable).resolve())
    engine = SAPI5Engine(Path(sys.executable).resolve())
    engine._records = {'sapi5:test': {'id':'id','name':'voice','language':'en-US','version':'fixture'}}
    with pytest.raises(EngineUnavailable, match='pitch'):
        engine.synthesize('Test', tmp_path / 'v.wav', voice='sapi5:test', pitch=2)
    with pytest.raises(EngineUnavailable, match='language'):
        engine.synthesize('Test', tmp_path / 'v.wav', voice='sapi5:test', language='uk')


def test_espeak_does_not_substitute_another_language(tmp_path):
    engine = ESpeakEngine(Path(sys.executable).resolve())
    engine._voices = {'espeak-ng:en': Voice('espeak-ng:en','English',('en',),'espeak-ng')}
    with pytest.raises(EngineUnavailable, match='language'):
        engine.synthesize('Привіт.',tmp_path/'speech.wav',voice='espeak-ng:en',language='uk')


def test_local_process_cancellation_stops_owned_worker():
    started = time.monotonic()
    with pytest.raises(InterruptedError):
        _run([Path(sys.executable).resolve(), '-I', '-c', 'import time; time.sleep(20)'],
             cancelled=lambda: time.monotonic() - started > .12)
    assert time.monotonic() - started < 5


def test_engine_logs_do_not_expose_document_content():
    with pytest.raises(EngineUnavailable) as caught:
        _run([Path(sys.executable).resolve(), '-I', '-c',
              'import sys; print("PRIVATE DOCUMENT",file=sys.stderr);sys.exit(3)'])
    assert 'PRIVATE DOCUMENT' not in str(caught.value)


def protocol_engine(tmp_path):
    engine = object.__new__(DordiaiVoicesEngine)
    engine.manifest = SimpleNamespace(root=tmp_path)
    engine.timeout = 5
    return engine


def test_dordiai_real_process_protocol_streams_pcm_and_requests_next(tmp_path):
    # This is a protocol fixture, not neural speech acceptance.
    worker = tmp_path / 'worker.py'
    worker.write_text('''import sys,json,base64
request=json.loads(sys.stdin.readline())
assert request['text']=='Literal test text'
print(json.dumps({'chunk':base64.b64encode(b'\\x01\\x00'*24).decode()}),flush=True)
assert json.loads(sys.stdin.readline())=={'mode':'next'}
print(json.dumps({'chunk':base64.b64encode(b'\\x02\\x00'*36).decode()}),flush=True)
assert json.loads(sys.stdin.readline())=={'mode':'next'}
print(json.dumps({'finished':True}),flush=True)
''', encoding='utf-8')
    target = tmp_path / 'audio.wav'
    protocol_engine(tmp_path)._stream([str(Path(sys.executable).resolve()), '-I', str(worker)],
                                     {'text':'Literal test text'}, target, lambda: False)
    with wave.open(str(target),'rb') as source:
        assert source.getframerate() == 24000
        assert source.getnframes() == 60
        assert source.readframes(60) == b'\x01\x00'*24 + b'\x02\x00'*36


@pytest.mark.parametrize('reply', [{'chunk':'!!invalid'}, {'chunk':'AA=='}, {'finished':True},
                                 {'pcm':'AQI=', 'chunk':'AQI='}, {'finished':True,'pcm':'AQI='},
                                 ['unexpected']])
def test_dordiai_protocol_rejects_invalid_or_empty_pcm(tmp_path, reply):
    worker = tmp_path / 'worker.py'
    worker.write_text('import sys;sys.stdin.readline();print(' + repr(json.dumps(reply)) + ',flush=True)\n', encoding='utf-8')
    with pytest.raises((ValueError, EngineUnavailable)):
        protocol_engine(tmp_path)._stream([str(Path(sys.executable).resolve()), '-I', str(worker)],
                                         {'text':'test'}, tmp_path/'audio.wav', lambda: False)


def test_dordiai_worker_denies_network_before_external_code(tmp_path):
    worker = tmp_path / 'worker.py'
    worker.write_text('import socket;socket.create_connection(("127.0.0.1",9))', encoding='utf-8')
    with pytest.raises(EngineUnavailable):
        _run([Path(sys.executable).resolve(), '-I', '-c', _DORDIAI_OFFLINE_BOOTSTRAP, worker])


def test_dordiai_cancels_worker_that_never_reads_large_unicode_request(tmp_path):
    worker = tmp_path/'worker.py'
    worker.write_text('import time;time.sleep(20)',encoding='utf-8')
    started = time.monotonic()
    with pytest.raises(InterruptedError):
        protocol_engine(tmp_path)._stream([str(Path(sys.executable).resolve()), '-I', str(worker)],
            {'text':'\U0001f600'*8000}, tmp_path/'audio.wav', lambda: time.monotonic()-started > .15)
    assert time.monotonic()-started < 4


def local_flite():
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('No local FFmpeg build is installed.')
    engine = FliteEngine(Path(ffmpeg).resolve())
    try:
        engine.voices()
    except EngineUnavailable:
        pytest.skip('This installed FFmpeg does not include libflite.')
    return engine


def test_actual_flite_speech_has_nonzero_pcm_and_cannot_overwrite(tmp_path):
    engine = local_flite()
    voice = engine.voices()[0].id
    target = engine.synthesize('A real local voice reads this sentence.', tmp_path/'actual.wav', voice=voice)
    with wave.open(str(target),'rb') as source:
        pcm = source.readframes(source.getnframes())
        assert source.getnframes() > 2400
        assert source.getframerate() == 24000
        assert any(pcm)
    previous = target.read_bytes()
    with pytest.raises(FileExistsError):
        engine.synthesize('Different text.', target, voice=voice)
    assert target.read_bytes() == previous


def test_actual_flite_treats_filter_metacharacters_as_text(tmp_path):
    engine = local_flite()
    marker = tmp_path/'not-created'
    text = 'Hello. [out]; amovie=https://invalid.test/private. $(touch not-created)'
    target = engine.synthesize(text, tmp_path/'safe.wav', voice=engine.voices()[0].id)
    assert target.is_file() and not marker.exists()
    with pytest.raises(EngineUnavailable, match='English only'):
        engine.synthesize('Привіт.', tmp_path/'uk.wav',voice=engine.voices()[0].id,language='uk')


@pytest.mark.skipif(os.name != 'nt', reason='Actual SAPI synthesis requires Windows.')
def test_actual_installed_windows_sapi_voice(tmp_path):
    engine = SAPI5Engine()
    voices = engine.voices()
    assert voices, 'No usable installed Windows SAPI 5 voice is available.'
    selected = voices[0]
    target = engine.synthesize('This is a local Windows speech test.', tmp_path/'sapi.wav',
                              voice=selected.id, language=selected.languages[0])
    with wave.open(str(target),'rb') as source:
        assert source.getnframes() > 0
        assert any(source.readframes(source.getnframes()))
    # SelectVoice itself accepts substrings. Our actual helper must reject a
    # request that differs from the full installed name or its opaque ID.
    record = engine._records[selected.id]
    for name, identity in ((record['name'][:-1], record['id']),
                           (record['name'], record['id'] + '-not-installed')):
        rejected = tmp_path / 'wrong-voice.wav'
        with pytest.raises(EngineUnavailable):
            engine._call({'operation': 'speak', 'voice': name, 'voice_id': identity,
                          'text': 'Synthetic rejection probe.', 'output': str(rejected),
                          'rate': 0, 'volume': 100})
        assert not rejected.exists()
