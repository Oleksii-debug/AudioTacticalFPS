"""Adversarial operation boundaries on the existing local voice adapters."""
import json
import os
from pathlib import Path
import signal
import sys
import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import engines


RECORD = dict(id='exact-installed-id', name='Installed Voice', language='en-US',
              version='fixture', default=True)


def make_wave(path, frames=24000):
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b'\x01\x00' * frames)


def engine_with_voice():
    engine = engines.SAPI5Engine(Path(sys.executable).resolve())
    engine._records = {'sapi5:test': RECORD.copy()}
    return engine


@pytest.mark.parametrize('timeout', [True, 0, -1, float('nan'), float('inf'), '1', 10 ** 1000],
                         ids=['bool', 'zero', 'negative', 'nan', 'infinity', 'string', 'overflow'])
def test_invalid_worker_deadline_never_launches(timeout, monkeypatch):
    monkeypatch.setattr(engines, 'start_owned_process', lambda *args, **kwargs: pytest.fail('Invalid timeout launched a worker.'))
    with pytest.raises(ValueError):
        engines._run([sys.executable, '-c', 'pass'], timeout=timeout)


def test_finished_worker_still_obeys_absolute_deadline(monkeypatch):
    # Deterministic completion between polls, after the deadline has elapsed.
    process = type('Exited', (), {'returncode': 0, 'poll': lambda self: 0})()
    monkeypatch.setattr(engines, 'start_owned_process', lambda *args, **kwargs: process)
    monkeypatch.setattr(engines, '_stop', lambda process: None)
    ticks = iter((0.0, 2.0, 2.0, 2.0))
    monkeypatch.setattr(engines.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(TimeoutError):
        engines._run([sys.executable, '-c', 'pass'], timeout=1)


def test_synthesis_cancellation_precedes_lazy_voice_discovery(tmp_path, monkeypatch):
    engine = engines.SAPI5Engine(Path(sys.executable).resolve())
    def forbidden(*args, **kwargs):
        pytest.fail('A cancelled synthesis attempted voice discovery.')
    monkeypatch.setattr(engine, '_call', forbidden)
    with pytest.raises(InterruptedError):
        engine.synthesize('A sentence.', tmp_path / 'speech.wav', voice='sapi5:default',
                          cancelled=lambda: True)


def test_lazy_voice_discovery_propagates_synthesis_cancellation(tmp_path, monkeypatch):
    engine = engines.SAPI5Engine(Path(sys.executable).resolve())
    script = 'import time;time.sleep(1.5);print(' + repr(json.dumps([RECORD])) + ')'
    started = time.monotonic()
    def call(request, *, cancelled=lambda: False):
        return engines._run([Path(sys.executable).resolve(), '-I', '-c', script],
                            cancelled=cancelled, timeout=3)
    monkeypatch.setattr(engine, '_call', call)
    with pytest.raises(InterruptedError):
        engine.synthesize('A sentence.', tmp_path / 'speech.wav', voice='sapi5:default',
                          cancelled=lambda: time.monotonic() - started > .08)
    assert time.monotonic() - started < 1.0
    assert not (tmp_path / 'speech.wav').exists()


@pytest.mark.parametrize('reply', [
    b'{"complete":true}',
    b'{"complete":1}',
    b'{"complete":false,"complete":true}',
    b'{"complete":true,"voice_id":"another-installed-id"}',
])
def test_sapi_requires_typed_completion_for_the_exact_installed_voice(tmp_path, monkeypatch, reply):
    engine = engine_with_voice()
    def call(request, **kwargs):
        make_wave(request['output'])
        return reply
    monkeypatch.setattr(engine, '_call', call)
    with pytest.raises(engines.EngineUnavailable):
        engine.synthesize('A sentence.', tmp_path / 'speech.wav', voice='sapi5:test')
    assert not (tmp_path / 'speech.wav').exists()


def test_sapi_request_binds_name_and_installed_identifier(tmp_path, monkeypatch):
    engine = engine_with_voice()
    captured = []
    def call(request, **kwargs):
        captured.append(request)
        make_wave(request['output'])
        return json.dumps({'complete': True, 'voice_id': RECORD['id']}).encode()
    monkeypatch.setattr(engine, '_call', call)
    target = engine.synthesize('Literal $(value).', tmp_path / 'speech.wav', voice='sapi5:test')
    assert target.is_file()
    assert captured[0]['voice'] == RECORD['name']
    assert captured[0]['voice_id'] == RECORD['id']


@pytest.mark.parametrize('row', [dict(RECORD, id=''), dict(RECORD, name='bad\nname'),
    dict(RECORD, language=''), dict(RECORD, default=1), dict(RECORD, version='\ud800')])
def test_malformed_voice_catalogue_is_rejected(row, monkeypatch):
    engine = engines.SAPI5Engine(Path(sys.executable).resolve())
    monkeypatch.setattr(engine, '_call', lambda request, **kwargs: json.dumps([row]).encode())
    with pytest.raises(engines.EngineUnavailable):
        engine.voices()


def test_wav_byte_budget_is_enforced_while_worker_runs(tmp_path, monkeypatch):
    output = tmp_path / 'speech.wav'
    monkeypatch.setattr(engines, 'MAX_PCM_BYTES', 100)
    code = 'from pathlib import Path;import sys,time;Path(sys.argv[1]).write_bytes(b"x"*5000);time.sleep(2)'
    started = time.monotonic()
    with pytest.raises(ValueError, match='PCM budget'):
        engines._run([Path(sys.executable).resolve(), '-I', '-c', code, output],
                     output=output, timeout=3)
    assert time.monotonic() - started < 1.0


def test_wav_verification_observes_cancellation(tmp_path):
    output = tmp_path / 'speech.wav'
    make_wave(output, frames=200000)
    polls = 0
    def cancelled():
        nonlocal polls
        polls += 1
        return polls > 2
    with pytest.raises(InterruptedError):
        engines._wave_info(output, cancelled=cancelled)


def test_pcm_frame_budget_is_checked_in_addition_to_container_overhead(tmp_path, monkeypatch):
    output = tmp_path / 'speech.wav'
    make_wave(output, frames=100)
    monkeypatch.setattr(engines, 'MAX_PCM_BYTES', 100)
    with pytest.raises(ValueError, match='PCM budget'):
        engines._wave_info(output)


def test_voice_registry_does_not_swallow_discovery_cancellation():
    class Engine:
        id = 'test'
        def voices(self, *, cancelled=lambda: False):
            raise InterruptedError('Discovery cancelled.')
    registry = engines.VoiceRegistry((Engine(),))
    with pytest.raises(InterruptedError):
        registry.voices()
    assert registry.notices == []


def test_voice_registry_forwards_callback_to_discovery_and_cache_identity():
    callback = lambda: False
    observed = []
    class Engine:
        id = 'test'
        def voices(self, *, cancelled):
            observed.append(cancelled)
            return ()
        def cache_identity(self, voice, *, cancelled):
            observed.append(cancelled)
            return {'voice': voice}
    registry = engines.VoiceRegistry((Engine(),))
    registry.voices(cancelled=callback)
    assert registry.cache_identity('test:voice', cancelled=callback) == {'voice': 'test:voice'}
    assert observed == [callback, callback]


def test_legacy_custom_adapter_contract_remains_supported():
    class Engine:
        id = 'test'
        def voices(self):
            return ()
        def cache_identity(self, voice):
            return {'voice': voice}
    registry = engines.VoiceRegistry((Engine(),))
    assert registry.voices() == ()
    assert registry.cache_identity('test:voice') == {'voice': 'test:voice'}


@pytest.mark.parametrize('adapter', [engines.SAPI5Engine, engines.ESpeakEngine, engines.FliteEngine])
def test_cancelled_cache_identity_never_discovers_voices(adapter, monkeypatch):
    engine = adapter(Path(sys.executable).resolve())
    monkeypatch.setattr(engine, 'voices', lambda **kwargs: pytest.fail('Cancelled identity scan launched discovery.'))
    with pytest.raises(InterruptedError):
        engine.cache_identity('test:voice', cancelled=lambda: True)


def test_engine_publication_moves_private_stage_without_hardlink_alias(tmp_path):
    stage, target = tmp_path / 'stage.wav', tmp_path / 'output.wav'
    make_wave(stage)
    original = stage.read_bytes()
    engines._publish(stage, target)
    assert target.read_bytes() == original
    assert target.stat().st_nlink == 1
    assert not stage.exists()


def test_cancellation_stops_descendant_after_its_process_group_leader_exits(tmp_path, monkeypatch):
    record = tmp_path / 'child.json'
    code = '''
import json, os, subprocess, sys
from pathlib import Path
child = subprocess.Popen([sys.executable, '-I', '-c', 'import time;time.sleep(30)'])
Path(sys.argv[1]).write_text(json.dumps([child.pid]), encoding='utf-8')
'''
    original = engines.start_owned_process
    owned = []

    def launch(*args, **kwargs):
        child = original(*args, **kwargs)
        owned.append(child)
        return child

    monkeypatch.setattr(engines, 'start_owned_process', launch)

    def cancelled():
        if record.exists():
            time.sleep(.05)  # The leader exits before cancellation is observed.
            return True
        return False

    def running(pid):
        if os.name == 'nt':
            import ctypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.WaitForSingleObject.restype = ctypes.c_uint32
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle.restype = ctypes.c_int
            handle = kernel.OpenProcess(0x00100000, False, pid)
            if not handle:
                assert ctypes.get_last_error() == 87, 'Child state is not verifiable.'
                return False
            try:
                outcome = kernel.WaitForSingleObject(handle, 0)
                assert outcome in (0, 0x102)
                return outcome == 0x102
            finally:
                assert kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        status = Path('/proc') / str(pid) / 'stat'
        if status.exists():
            return status.read_text().rsplit(')', 1)[1].split()[0] not in ('Z', 'X')
        return True

    try:
        with pytest.raises(InterruptedError):
            engines._run([Path(sys.executable).resolve(), '-I', '-c', code, record],
                         cancelled=cancelled, timeout=5)
        pid, = json.loads(record.read_text())
        deadline = time.monotonic() + 2
        while running(pid) and time.monotonic() < deadline:
            time.sleep(.025)
        assert not running(pid), 'The exited helper left a live owned descendant.'
    finally:
        for child in owned:
            if os.name == 'nt':
                child.close()
            else:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def test_native_worker_does_not_start_a_console_or_a_taskkill_helper(monkeypatch):
    monkeypatch.setattr(engines.subprocess, 'run',
        lambda *args, **kwargs: pytest.fail('A speech owner launched an unowned cleanup helper.'))
    code = ("import os; "
            "print(__import__('ctypes').windll.kernel32.GetConsoleWindow() if os.name=='nt' else 'posix')")
    result = engines._run([str(Path(sys.executable).resolve()), '-I', '-c', code], timeout=5)
    assert result.strip() == (b'0' if os.name == 'nt' else b'posix')


def test_dordiai_rejects_duplicate_completion_fields_from_actual_worker(tmp_path):
    worker = tmp_path / 'duplicate_reply.py'
    worker.write_text('''import sys,time
sys.stdin.readline()
print('{"chunk":"AQA="}',flush=True)
sys.stdin.readline()
print('{"finished":false,"finished":true}',flush=True)
time.sleep(1)
''', encoding='utf-8')
    engine = object.__new__(engines.DordiaiVoicesEngine)
    engine.manifest = SimpleNamespace(root=tmp_path)
    engine.timeout = 5
    with pytest.raises(ValueError, match='JSON|Duplicate'):
        engine._stream([str(Path(sys.executable).resolve()), '-I', str(worker)],
                       {'text': 'Synthetic protocol test'}, tmp_path / 'audio.wav', lambda: False)


def test_espeak_refresh_drops_removed_installed_voices(monkeypatch):
    engine = engines.ESpeakEngine(Path(sys.executable).resolve())
    lines = iter((b'Pty Language Age/Gender VoiceName File\n5 en M Old en-old\n',
                  b'Pty Language Age/Gender VoiceName File\n5 uk M New uk-new\n'))
    monkeypatch.setattr(engines, '_run', lambda *args, **kwargs: next(lines))
    assert engine.voices()[0].id == 'espeak-ng:en-old'
    assert [voice.id for voice in engine.voices()] == ['espeak-ng:uk-new']
