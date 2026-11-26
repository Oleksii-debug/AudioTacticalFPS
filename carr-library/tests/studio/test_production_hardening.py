"""Focused persistence/safety regressions for the integrated production service.

Speech integration uses the actual installed FFmpeg Flite provider. Boundary
wrappers only arrange cancellation or count calls; they do not synthesize fake
speech and do not stand in for native Windows SAPI acceptance.
"""
from pathlib import Path
from dataclasses import replace
from hashlib import sha256
import json
import os
import shutil
import wave

import pytest

from carr_library.speech import FliteProvider, VoiceRegistry
from carr_library.speech.runtime import SapiProvider
from carr_library.speech import _io
from carr_library.studio.render import ProductionBudget, ProductionService
from carr_library.studio.project import Cue
from carr_library.studio import render as rendering


@pytest.fixture
def registry():
    binary = shutil.which('ffmpeg')
    if not binary or not shutil.which('ffprobe'):
        pytest.skip('Actual production requires installed FFmpeg and ffprobe.')
    value = VoiceRegistry([FliteProvider(binary)])
    if 'flite:slt' not in {voice.id for voice in value.voices()}:
        pytest.skip('Actual speech requires the installed Flite slt voice.')
    return value


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


def test_completed_actual_speech_is_receipted_before_cancellation(tmp_path, registry):
    voices = CountSpeech(registry)
    stopped = [False]
    voices.after = lambda result: stopped.__setitem__(0, True)
    service = ProductionService(tmp_path / 'state', voices=voices)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    project = service.create('One durable speech segment.', voice_ids=('flite:slt',))
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


def make_job(tmp_path, registry, *, budget=None, text='One local sentence.'):
    voices = CountSpeech(registry)
    service = ProductionService(tmp_path / 'state', voices=voices, budget=budget)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    project = service.create(text, voice_ids=('flite:slt',))
    return service, voices, service.submit(project, outputs, stems=False)


def test_retained_cache_budget_stops_before_actual_synthesis(tmp_path, registry):
    service, voices, job = make_job(tmp_path, registry,
        budget=ProductionBudget(max_cache_bytes=100000, reserve_disk_bytes=0))
    retained = service.cache / 'unreferenced-old-generation.bin'
    retained.write_bytes(b'preserved older output' * 10000)
    with pytest.raises(ValueError, match='whole-job storage budget'):
        service.run(job)
    assert not voices.completed and retained.exists()


def test_cumulative_cache_growth_stops_before_the_next_real_speech_call(tmp_path, registry):
    service, voices, job = make_job(tmp_path, registry,
        budget=ProductionBudget(max_cache_bytes=1000000, reserve_disk_bytes=0),
        text='First short line.\nSecond short line.')
    voices.after = lambda _: (service.cache / 'retained-fixture.bin').write_bytes(b'x' * 1000000)
    with pytest.raises(ValueError, match='whole-job storage budget'):
        service.run(job)
    assert len(voices.completed) == 1
    assert service.get(job)['status'] == 'failed'


def test_whole_job_deadline_checkpoints_returned_speech_and_resumes(tmp_path, registry, monkeypatch):
    service, voices, job = make_job(tmp_path, registry, budget=ProductionBudget(timeout=120))
    original, offset = rendering.time.monotonic, [0]
    monkeypatch.setattr(rendering.time, 'monotonic', lambda: original() + offset[0])
    voices.after = lambda _: offset.__setitem__(0, 1000)
    with pytest.raises(TimeoutError, match='whole-production time budget'):
        service.run(job)
    assert len(voices.completed) == 1
    assert any(node['kind'] == 'speech' for node in service.get(job)['nodes'].values())
    assert service.get(job)['status'] == 'failed'
    voices.after = None
    assert service.run(job)['status'] == 'complete'
    assert len(voices.completed) == 1


def test_free_disk_reserve_is_checked_before_actual_synthesis(tmp_path, registry, monkeypatch):
    service, voices, job = make_job(tmp_path, registry)
    monkeypatch.setattr(rendering.shutil, 'disk_usage', lambda _: type('Space', (), {'free': 1})())
    with pytest.raises(ValueError, match='free disk'):
        service.run(job)
    assert not voices.completed


def test_shared_cache_guard_prevents_concurrent_duplicate_synthesis(tmp_path, registry):
    service, voices, first = make_job(tmp_path, registry)
    project = service.projects.load(service.get(first)['project']['id'])
    second = service.submit(project, tmp_path / 'outputs', stems=False)
    other = ProductionService(service.root, voices=voices)
    checked = []

    def overlap(message):
        if message == 'speech: rendering local audio':
            with pytest.raises(RuntimeError, match='already being changed'):
                other.run(second)
            checked.append(True)

    assert service.run(first, progress=overlap)['status'] == 'complete'
    assert checked and len(voices.completed) == 1
    assert other.run(second)['status'] == 'complete'
    assert len(voices.completed) == 1


def test_reexport_after_restore_uses_bound_cue_and_completed_speech(tmp_path, registry):
    inputs = tmp_path / 'original-assets'
    inputs.mkdir()
    effect = inputs / 'fixture.wav'
    with wave.open(str(effect), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x10\x10' * 2400)
    outputs = tmp_path / 'old-outputs'
    outputs.mkdir()
    voices = CountSpeech(registry)
    service = ProductionService(tmp_path / 'state', voices=voices, input_roots=(inputs,))
    base = service.create('Original preserved narration.', voice_ids=('flite:slt',))
    scene = replace(base.scenes[0], cues=(Cue('effect', str(effect), start=4),))
    project = replace(base, scenes=(scene,), revision=2)
    old_job = service.submit(project, outputs, stems=False)
    first = service.run(old_job)
    expected = sha256(Path(first['destination'], 'master.wav').read_bytes()).hexdigest()
    assert first['cue_assets'] and len(voices.completed) == 1
    restored = tmp_path / 'restored-state'
    shutil.copytree(service.root, restored)
    shutil.rmtree(outputs)
    shutil.rmtree(inputs)
    new_outputs = tmp_path / 'new-outputs'
    new_outputs.mkdir()
    reopened = ProductionService(restored, voices=voices)
    with pytest.raises(ValueError, match='re-export'):
        reopened.run(old_job)
    # Older project revisions can be re-exported without changing current state.
    latest = replace(project, title='Newer project draft', revision=3)
    reopened.projects.save(latest, expected_revision=2)
    new_job = reopened.submit_reexport(old_job, new_outputs)
    second = reopened.run(new_job)
    assert second['status'] == 'complete' and len(voices.completed) == 1
    assert sha256(Path(second['destination'], 'master.wav').read_bytes()).hexdigest() == expected
    assert second['project'] == first['project'] and second['cue_assets'] == first['cue_assets']
    assert reopened.projects.load(project.id).revision == 3
    assert reopened.get(old_job)['destination'] == first['destination']


def test_caption_markup_and_extra_cue_syntax_never_change_source_json(tmp_path, registry):
    payload = '<v Speaker>Literal & text</v>\n\n00:00:01.000 --> 00:00:02.000\n<b>another literal line</b>'
    service, voices, job = make_job(tmp_path, registry)
    base = service.projects.load(service.get(job)['project']['id'])
    line = replace(base.scenes[0].lines[0], text=payload)
    project = replace(base, original_script=payload, mode='authored', revision=2,
                      scenes=(replace(base.scenes[0], lines=(line,)),))
    job = service.submit(project, tmp_path / 'outputs', stems=False)
    complete = service.run(job)
    for name in ('script.vtt', 'script.srt'):
        content = Path(complete['destination'], name).read_text(encoding='utf-8')
        assert content.count(' --> ') == 1
        assert '<v Speaker>' not in content and '<b>' not in content
        assert '&lt;v Speaker&gt;' in content and '\n\u200b\n' in content
    assert complete['result']['lines'][0]['text'] == payload
    assert complete['project']['original_script'] == payload
    assert complete['result']['source_sha256'] == sha256(payload.encode('utf-8')).hexdigest()


def test_completed_export_verification_honors_pre_cancel(tmp_path, registry):
    service, voices, job = make_job(tmp_path, registry)
    service.run(job)
    with pytest.raises(InterruptedError):
        service.run(job, cancelled=lambda: True)
    assert len(voices.completed) == 1


def test_whole_export_budget_rejects_publication_without_overwrite(tmp_path, registry):
    service, voices, job = make_job(tmp_path, registry,
        budget=ProductionBudget(max_export_bytes=100))
    with pytest.raises(ValueError, match='export byte budget'):
        service.run(job)
    assert len(voices.completed) == 1
    assert not list((tmp_path / 'outputs').iterdir())
    assert service.get(job)['status'] == 'failed'


def test_unselected_cue_is_rejected_before_any_speech(tmp_path, registry):
    service, voices, first = make_job(tmp_path, registry)
    outside = tmp_path / 'outside.wav'
    with wave.open(str(outside), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x10\x10' * 2400)
    base = service.projects.load(service.get(first)['project']['id'])
    project = replace(base, revision=2,
        scenes=(replace(base.scenes[0], cues=(Cue('outside', str(outside)),)),))
    job = service.submit(project, tmp_path / 'outputs', stems=False)
    with pytest.raises(ValueError, match='selected input'):
        service.run(job)
    assert not voices.completed
