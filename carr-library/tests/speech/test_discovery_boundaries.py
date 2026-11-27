"""Executable lookup and cancellation boundaries; fixture binaries never run."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library import bundled_engines
from carr_library.audio import AudioRuntime
from carr_library.speech import Voice, VoiceRegistry


def test_audio_constructor_verifies_one_bundle_component_for_both_programs(tmp_path, monkeypatch):
    paths = {}
    for name in ('ffmpeg', 'ffprobe'):
        path = tmp_path / (name + '.exe')
        path.write_bytes(b'Never executed bundled-path fixture')
        paths[name] = str(path)
    calls = []
    def programs(component):
        calls.append(component)
        return paths
    monkeypatch.setattr(bundled_engines, 'bundled_programs', programs)
    monkeypatch.setattr(bundled_engines, 'executable_on_path', lambda *a: pytest.fail('Unexpected PATH fallback'))
    runtime = AudioRuntime(tmp_path / 'outputs')
    assert (runtime.ffmpeg, runtime.ffprobe) == (paths['ffmpeg'], paths['ffprobe'])
    assert calls == ['audio']


def test_explicit_audio_paths_do_not_probe_an_unrelated_installed_bundle(tmp_path, monkeypatch):
    program = tmp_path / 'explicit.exe'
    program.write_bytes(b'Never executed explicit-path fixture')
    monkeypatch.setattr(bundled_engines, 'bundled_programs', lambda *a: pytest.fail('Explicit paths probed bundle'))
    runtime = AudioRuntime(tmp_path / 'outputs', ffmpeg=program, ffprobe=program)
    assert runtime.ffmpeg == runtime.ffprobe == str(program)


def test_damaged_included_bundle_never_falls_back_to_path(tmp_path, monkeypatch):
    def damaged(*args):
        raise ValueError('Bundled engine file differs from its sealed identity')
    monkeypatch.setattr(bundled_engines, 'bundled_programs', damaged)
    monkeypatch.setattr(bundled_engines, 'executable_on_path', lambda *a: pytest.fail('Damaged bundle fell back'))
    with pytest.raises(ValueError, match='sealed identity'):
        AudioRuntime(tmp_path / 'outputs')


@pytest.mark.parametrize('name', ['ffmpeg', 'ffprobe', 'espeak-ng', 'espeak'])
def test_windows_311_path_lookup_uses_explicit_exe_and_ignores_relative_directories(tmp_path, monkeypatch, name):
    installed = tmp_path / 'programs'
    installed.mkdir()
    program = installed / (name + '.exe')
    program.write_bytes(b'Never executed executable-name fixture')
    monkeypatch.setattr(bundled_engines, 'os', SimpleNamespace(name='nt',
        get_exec_path=lambda: ['', '.', 'relative/tools', str(installed)]))
    seen = []
    def which(candidate):
        seen.append(candidate)
        # Reproduce Python 3.11's exact-file lookup for an explicit directory.
        return candidate if candidate == str(program) else None
    monkeypatch.setattr(bundled_engines.shutil, 'which', which)
    assert bundled_engines.executable_on_path(name) == str(program)
    assert seen == [str(program)]


def test_registry_preserves_last_inventory_when_cancellable_reload_is_interrupted():
    voice = Voice('fixture:test', 'Fixture', 'Metadata fixture', 'en')
    class Provider:
        name = 'Fixture'
        stopping = False
        def discover_voices(self, *, cancelled):
            if self.stopping:
                raise InterruptedError('Synthetic interrupted discovery')
            return (voice,)
    provider = Provider()
    registry = VoiceRegistry([provider])
    provider.stopping = True
    with pytest.raises(InterruptedError):
        registry.reload()
    assert registry.voices() == (voice,)
