"""Focused persistence/safety regressions for the integrated production service.

Speech integration uses actual native SAPI on Windows and the original installed
FFmpeg Flite provider on Linux. Boundary wrappers only arrange cancellation or
count calls; they do not synthesize fake speech or qualify physical playback.
"""
from pathlib import Path
import os
import shutil

import pytest

from carr_library.speech import FliteProvider, VoiceRegistry
from carr_library.speech.runtime import NativeSapiProvider, SapiProvider
from carr_library.speech import _io
from carr_library.studio.render import ProductionService


@pytest.fixture
def registry():
    binary = shutil.which('ffmpeg')
    if not binary or not shutil.which('ffprobe'):
        pytest.skip('Actual production requires installed FFmpeg and ffprobe.')
    provider = NativeSapiProvider() if os.name == 'nt' else FliteProvider(binary)
    value = VoiceRegistry([provider])
    try:
        if os.name == 'nt':
            english = tuple(voice for voice in value.voices()
                            if voice.language.casefold().split('-')[0] == 'en')
            assert len({voice.id for voice in english}) >= 2, (
                'Actual Windows production requires two distinct installed English SAPI tokens.',
                value.notices, tuple((voice.id, voice.language) for voice in value.voices()))
        elif 'flite:slt' not in {voice.id for voice in value.voices()}:
            pytest.skip('Actual speech requires the installed Flite slt voice.')
        yield value
    finally:
        value.close()


@pytest.fixture
def actual_voice_id(registry):
    if os.name == 'nt':
        return next(voice.id for voice in registry.voices()
                    if voice.language.casefold().split('-')[0] == 'en')
    return 'flite:slt'


class CountSpeech:
    def __init__(self, registry):
        self.registry = registry
        self.completed = []
        self.after = None

    def __getattr__(self, name):
        return getattr(self.registry, name)

    def synthesize(self, *args, **kwargs):
        result = self.registry.synthesize(*args, **kwargs)
        self.completed.append(result)
        if self.after:
            self.after(result)
        return result


def test_completed_actual_speech_is_receipted_before_cancellation(tmp_path, registry, actual_voice_id):
    voices = CountSpeech(registry)
    stopped = [False]
    voices.after = lambda result: stopped.__setitem__(0, True)
    service = ProductionService(tmp_path / 'state', voices=voices)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    project = service.create('One durable speech segment.', voice_ids=(actual_voice_id,))
    job = service.submit(project, outputs, stems=False)
    with pytest.raises(InterruptedError):
        service.run(job, cancelled=lambda: stopped[0])
    state = service.get(job)
    accepted = [node for node in state['nodes'].values() if node['kind'] == 'speech']
    assert len(accepted) == 1, 'A real completed speech file must be accepted before honoring next-step cancellation.'
    assert len(voices.completed) == 1
    first = voices.completed[0]
    voices.after = None
    complete = service.run(job)
    assert complete['status'] == 'complete'
    assert len(voices.completed) == 1, 'Resume must not synthesize that accepted segment again.'
    assert first.path.is_file()


def test_speech_publication_flush_uses_writable_descriptor(tmp_path, monkeypatch):
    """Check the actual descriptor mode; real Windows execution is a separate gate."""
    original, modes = Path.open, []

    def observed(path, mode='r', *args, **kwargs):
        if path.name == 'output.wav' and mode in ('rb', 'r+b'):
            modes.append(mode)
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', observed)
    with _io.new_output(tmp_path / 'output.wav') as staged:
        staged.write_bytes(b'publication descriptor test')
    assert modes == ['r+b']


@pytest.mark.parametrize('architecture,folder', [('64', 'System32'), ('32', 'SysWOW64')])
def test_serviced_windows_powershell_host_allows_only_its_exact_hardlink(tmp_path, monkeypatch, architecture, folder):
    """Simulated serviced OS layout; actual SAPI is still a separate Windows gate."""
    system = tmp_path / 'Windows'
    host = system / folder / 'WindowsPowerShell/v1.0/powershell.exe'
    host.parent.mkdir(parents=True)
    original = tmp_path / 'WinSxS-host-fixture'
    original.write_bytes(b'OS-host path fixture, never executed')
    os.link(original, host)
    monkeypatch.setattr(_io, 'windows_directory', lambda: system)
    assert _io.system_powershell(host, architecture=architecture) == host
    with pytest.raises(ValueError, match='must not have links'):
        _io.local_file(host)  # No general relaxation for a document/output path.
    with pytest.raises(ValueError, match='system PowerShell'):
        _io.system_powershell(original, architecture=architecture)
    with pytest.raises(ValueError, match='system PowerShell'):
        _io.system_powershell(host, architecture='32' if architecture == '64' else '64')


def test_system_host_exception_still_rejects_a_canonical_symlink(tmp_path, monkeypatch):
    system = tmp_path / 'Windows'
    host = system / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    host.parent.mkdir(parents=True)
    outside = tmp_path / 'other.exe'
    outside.write_bytes(b'Fixture only')
    try:
        host.symlink_to(outside)
    except OSError:
        pytest.skip('Symlink creation is unavailable in this test environment.')
    monkeypatch.setattr(_io, 'windows_directory', lambda: system)
    with pytest.raises(ValueError, match='must not have links'):
        _io.system_powershell(host, architecture='64')
