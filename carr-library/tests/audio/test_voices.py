"""Speech safety and actual optional-engine checks, with clear native skips."""
from array import array
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio import voices
from carr_library.audio.voices import (
    FFmpegFliteProvider, Sapi5Provider, Voice, VoiceCancelled, VoiceRegistry,
    VoiceUnavailable, discover_local_providers,
)


@pytest.fixture(scope='module')
def flite():
    executable = shutil.which('ffmpeg')
    if not executable:
        pytest.skip('Optional local FFmpeg is not installed.')
    provider = FFmpegFliteProvider(Path(executable))
    try:
        provider.list_voices()
    except VoiceUnavailable:
        pytest.skip('This local FFmpeg build does not include Flite voices.')
    return provider


def test_actual_three_distinct_local_voices_produce_pcm(flite, tmp_path):
    inventory = {voice.id: voice for voice in flite.list_voices()}
    selected = [identity for identity in ('awb', 'rms', 'slt') if identity in inventory]
    if len(selected) != 3:
        pytest.skip('Three specified Flite compatibility voices are not available in this build.')
    hashes = set()
    text = 'The library preserves the original words. This is a local speech test.'
    for identity in selected:
        result = flite.synthesize(text, identity, tmp_path / f'{identity}.wav')
        assert result.sample_rate == 24000 and result.channels == 1
        assert result.frames > 24000
        assert result.path.is_file()
        assert result.text_sha256 == hashlib.sha256(text.encode()).hexdigest()
        assert result.sha256 == hashlib.sha256(result.path.read_bytes()).hexdigest()
        with wave.open(str(result.path)) as data:
            samples = array('h', data.readframes(data.getnframes()))
        assert any(abs(sample) > 100 for sample in samples)
        hashes.add(result.sha256)
    assert len(hashes) == 3


def test_actual_voice_unicode_filename_and_hostile_text_remain_data(flite, tmp_path):
    directory = tmp_path / 'Dobrý deň — українська'
    directory.mkdir()
    text = "Plain text: '; [0:a]movie=http://invalid.example/x; $(echo forbidden)."
    result = flite.synthesize(text, flite.list_voices()[0].id, directory / 'озвучення.wav')
    assert result.path.is_file() and result.duration > 0
    assert sorted(path.name for path in directory.iterdir()) == ['озвучення.wav']
    assert result.text_sha256 == hashlib.sha256(text.encode()).hexdigest()


def test_existing_output_is_not_modified_or_synthesized(flite, tmp_path, monkeypatch):
    output = tmp_path / 'existing.wav'
    output.write_bytes(b'user-owned original')
    monkeypatch.setattr(voices, '_run', lambda *a, **kw: pytest.fail('Worker must not launch.'))
    with pytest.raises(FileExistsError):
        flite.synthesize('New speech', flite.list_voices()[0].id, output)
    assert output.read_bytes() == b'user-owned original'


@pytest.mark.parametrize('setting,value', [
    ('rate', float('nan')), ('rate', True), ('rate', 0), ('rate', 3),
    ('volume', float('inf')), ('volume', -0.1), ('volume', 1.1),
    ('sample_rate', True), ('sample_rate', 48001),
])
def test_unsupported_settings_fail_before_worker(flite, tmp_path, monkeypatch, setting, value):
    monkeypatch.setattr(voices, '_run', lambda *a, **kw: pytest.fail('Worker must not launch.'))
    with pytest.raises(ValueError):
        flite.synthesize('Words', flite.list_voices()[0].id, tmp_path / 'bad.wav', **{setting: value})
    assert not (tmp_path / 'bad.wav').exists()


@pytest.mark.parametrize('text', ['', '   ', '\x00word', '\ud800', None])
def test_invalid_text_fails_before_worker(flite, tmp_path, monkeypatch, text):
    monkeypatch.setattr(voices, '_run', lambda *a, **kw: pytest.fail('Worker must not launch.'))
    with pytest.raises(ValueError):
        flite.synthesize(text, flite.list_voices()[0].id, tmp_path / 'bad.wav')


def test_voice_id_cannot_inject_a_filter(flite, tmp_path):
    with pytest.raises(ValueError, match='voice is not present'):
        flite.synthesize('Words', 'slt,amovie=http://invalid.example', tmp_path / 'bad.wav')
    assert list(tmp_path.iterdir()) == []


def test_pre_cancel_does_not_launch_or_publish(flite, tmp_path, monkeypatch):
    stopped = threading.Event()
    stopped.set()
    monkeypatch.setattr(voices, '_run', lambda *a, **kw: pytest.fail('Worker must not launch.'))
    with pytest.raises(VoiceCancelled):
        flite.synthesize('Words', flite.list_voices()[0].id, tmp_path / 'cancel.wav', cancel=stopped)
    assert list(tmp_path.iterdir()) == []


def test_running_worker_is_cancelled_and_reaped():
    stopped = threading.Event()
    timer = threading.Timer(0.1, stopped.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(VoiceCancelled):
            voices._run([sys.executable, '-c', 'import time; time.sleep(30)'], cancel=stopped)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 4


def test_worker_timeout_and_log_budget_are_enforced():
    with pytest.raises(TimeoutError):
        voices._run([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=0.05)
    with pytest.raises(VoiceUnavailable, match='diagnostic-output budget'):
        voices._run([sys.executable, '-c', 'print("x"*10000)'], log_limit=1000)


def test_engine_diagnostic_never_echoes_private_text():
    with pytest.raises(VoiceUnavailable) as failure:
        voices._run([sys.executable, '-c', 'import sys; print("PRIVATE UTTERANCE"); sys.exit(9)'])
    assert 'PRIVATE' not in str(failure.value)


def test_publication_rechecks_concurrent_output_creation(flite, tmp_path, monkeypatch):
    output = tmp_path / 'race.wav'
    original_run = voices._run

    def race(*args, **kwargs):
        result = original_run(*args, **kwargs)
        output.write_bytes(b'concurrent user file')
        return result

    monkeypatch.setattr(voices, '_run', race)
    with pytest.raises(FileExistsError):
        flite.synthesize('The existing output belongs to the user.', flite.list_voices()[0].id, output)
    assert output.read_bytes() == b'concurrent user file'
    assert sorted(path.name for path in tmp_path.iterdir()) == ['race.wav']


@pytest.mark.parametrize('path', ['https://invalid.example/file.wav', '//host/share/file.wav', r'\\host\share\file.wav'])
def test_network_paths_are_refused_before_io(path):
    with pytest.raises(ValueError, match='local paths'):
        voices._local_path(Path(path))


def test_registry_reports_real_missing_engines_without_fallback():
    registry = discover_local_providers()
    statuses = {item.provider: item for item in registry.availability()}
    assert not statuses['flite'].available
    assert not statuses['sapi4'].available
    assert not statuses['dordiai-voices'].available
    with pytest.raises(VoiceUnavailable):
        registry.get('unapproved-remote-voice')


def test_duplicate_provider_identifiers_are_refused():
    with pytest.raises(ValueError, match='Duplicate'):
        VoiceRegistry([SimpleNamespace(provider_id='local'), SimpleNamespace(provider_id='local')])


def test_segment_budget_does_not_launch_worker(flite, tmp_path, monkeypatch):
    provider = FFmpegFliteProvider(flite.ffmpeg, max_text_chars=10)
    monkeypatch.setattr(voices, '_run', lambda *a, **kw: pytest.fail('Worker must not launch.'))
    with pytest.raises(ValueError, match='per segment'):
        provider.synthesize('Longer than ten characters.', 'slt', tmp_path / 'long.wav')


def _mock_sapi(monkeypatch, tmp_path):
    """Validate adapter plumbing only; this is never native SAPI evidence."""
    windows = tmp_path / 'Windows'
    executable = windows / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'synthetic executable metadata')
    monkeypatch.setenv('SystemRoot', str(windows))
    monkeypatch.setattr(voices, 'sys', SimpleNamespace(platform='win32'))
    return Sapi5Provider(executable)


def test_mock_sapi_plain_text_is_separate_from_fixed_worker(monkeypatch, tmp_path):
    provider = _mock_sapi(monkeypatch, tmp_path)
    text = "'; Invoke-Expression 'untrusted'; <speak>plain data</speak>"
    identity = "synthetic token with ' and ;"
    observed = []

    def run(args, *, cwd, **kwargs):
        request = json.loads((cwd / 'request.json').read_text(encoding='utf-8'))
        observed.append(request)
        assert text not in args[-1]
        assert '-ExecutionPolicy' not in args
        assert '-NoProfile' in args
        if request['action'] == 'list':
            return json.dumps({'voices': [{'id': identity, 'name': 'Synthetic English', 'language': 'en-US'}],
                               'runtime': 'Synthetic mock', 'bitness': 64}).encode(), b''
        assert request['text'] == text and request['voice_id'] == identity
        with wave.open(str(cwd / 'render.wav'), 'wb') as data:
            data.setparams((1, 2, request['sample_rate'], 0, 'NONE', 'not compressed'))
            data.writeframes(b'\x00\x01' * 100)
        return json.dumps({'status': 'complete', 'voice_id': identity}).encode(), b''

    monkeypatch.setattr(voices, '_run', run)
    result = provider.synthesize(text, identity, tmp_path / 'mock.wav')
    assert result.frames == 100 and result.voice_id == identity
    assert len(observed) == 2


def test_mock_sapi_worker_voice_mismatch_is_not_published(monkeypatch, tmp_path):
    provider = _mock_sapi(monkeypatch, tmp_path)
    provider._voices = (Voice('one', 'sapi5', 'Synthetic', ('en',), (), 'synthetic'),)
    monkeypatch.setattr(provider, '_call', lambda *a, **kw: {'status': 'complete', 'voice_id': 'other'})
    with pytest.raises(VoiceUnavailable, match='selected voice'):
        provider.synthesize('Words', 'one', tmp_path / 'no.wav')
    assert not (tmp_path / 'no.wav').exists()


def test_mock_sapi_32_and_64_bit_hosts_have_distinct_registry_ids(monkeypatch, tmp_path):
    native = _mock_sapi(monkeypatch, tmp_path)
    executable = tmp_path / 'Windows/SysWOW64/WindowsPowerShell/v1.0/powershell.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'synthetic x86 executable metadata')
    legacy = Sapi5Provider(executable)
    assert native.provider_id == 'sapi5' and legacy.provider_id == 'sapi5-x86'
    registry = VoiceRegistry([native, legacy])
    assert registry.get('sapi5') is native
    assert registry.get('sapi5-x86') is legacy


@pytest.mark.skipif(sys.platform != 'win32', reason='Actual SAPI5 requires a Windows host and installed voices.')
def test_actual_windows_sapi5_generates_selected_voice(tmp_path):
    provider = Sapi5Provider()
    inventory = provider.list_voices()
    result = provider.synthesize('Local library speech. The original text stays unchanged.',
        inventory[0].id, tmp_path / 'native-sapi5.wav')
    assert result.provider == 'sapi5' and result.frames > result.sample_rate
    assert result.path.is_file()
