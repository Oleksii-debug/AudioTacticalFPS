"""Interactive Studio work obeys real resource and retained-byte boundaries."""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import wave

import pytest

from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner
from carr_library.audio.models import AudioBudget, AudioCancelled, AudioError, AudioInfo
from carr_library.operations import ResourceBroker, ResourceCancelled, ResourceLimits, ResourceUnavailable
from carr_library.studio import clips
from carr_library.studio.clips import ClipEditor
from carr_library.studio.project import Cue, Project, Scene
from carr_library.studio.render import ProductionService


def audio(path, *, frames=24000, sample=600):
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(sample.to_bytes(2, 'little', signed=True) * frames)
    return path


def configured(tmp_path, *, budget=None, total_disk_bytes=512 * 1024**2, limits=None):
    library, selected_root = tmp_path / 'library', tmp_path / 'selected'
    library.mkdir()
    selected_root.mkdir()
    source = audio(selected_root / 'immutable.wav')
    broker = ResourceBroker.for_app(library, limits=limits or ResourceLimits(
        cpu_slots=1, memory_bytes=1024**3, disk_bytes=512 * 1024**2, min_free_disk_bytes=0))
    observations = []
    runtime = SimpleNamespace(budget=budget or AudioBudget(
        threads=1, max_output_bytes=8 * 1024**2, reserve_disk_bytes=0))

    def inspect(path, *, cancelled=lambda: False):
        snapshot = broker.snapshot()
        assert snapshot['usage']['cpu_slots'] == 1
        assert len(snapshot['leases']) == 1, 'Interactive work nested resource admissions.'
        observations.append(('inspect', Path(path), snapshot))
        with wave.open(str(path), 'rb') as stream:
            frames, rate, channels = stream.getnframes(), stream.getframerate(), stream.getnchannels()
            assert stream.getsampwidth() == 2
        return AudioInfo(Path(path), frames / rate, rate, channels, 'pcm_s16le', 'wav',
                         frames=frames, size=Path(path).stat().st_size)

    def mix(tracks, output, *, sample_rate, cancelled=lambda: False):
        snapshot = broker.snapshot()
        assert snapshot['usage']['cpu_slots'] == 1
        assert snapshot['usage']['disk_bytes'] > 0
        assert len(snapshot['leases']) == 1, 'Preview nested its metadata reservation.'
        observations.append(('mix', Path(output), snapshot))
        # The fixture models a mixer; admission assertions are backed by the
        # real broker, source PCM, project store and retained files.
        assert len(tracks) == 1 and tracks[0].path == source
        return audio(output, frames=1200)

    runtime.inspect, runtime.mix = inspect, mix
    service = ProductionService(library / 'state/studio', voices=SimpleNamespace(),
        input_roots=(selected_root,), broker=broker, total_disk_bytes=total_disk_bytes)
    def factory(root, **options):
        assert service.inputs in options['input_roots']
        return runtime
    service.runtime_factory = factory
    editor = ClipEditor(library, service=service)
    cue = Cue('clip', str(source), trim_start=.25, duration=.5)
    project = Project('clips', 'Bounded preview', (),
        (Scene('one', 'Scene', (), cues=(cue,)),), schema=2)
    service.projects.save(project)
    return SimpleNamespace(library=library, source=source, broker=broker, runtime=runtime,
        service=service, editor=editor, project=project, cue=cue, observed=observations)


def admission_hook(monkeypatch, fixture, callback):
    reserve, calls = fixture.broker.reserve, []
    @contextmanager
    def checked(request, **options):
        calls.append(request)
        with reserve(request, **options) as lease:
            callback(len(calls), request)
            yield lease
    monkeypatch.setattr(fixture.broker, 'reserve', checked)
    return calls


@pytest.mark.parametrize('operation', ('retain', 'selection', 'preview'))
def test_unavailable_cpu_never_copies_or_starts_a_decoder(tmp_path, operation):
    fixture = configured(tmp_path, budget=AudioBudget(threads=2))
    before = fixture.source.read_bytes()
    with pytest.raises(ResourceUnavailable, match='cpu_slots'):
        if operation == 'retain':
            fixture.editor.retain(fixture.source)
        elif operation == 'selection':
            fixture.editor.edit(fixture.project, 'one', 'clip', pan=.5)
        else:
            fixture.editor.preview(fixture.project, 'one', 'clip')
    assert fixture.observed == []
    assert fixture.source.read_bytes() == before
    assert fixture.service.projects.load('clips') == fixture.project
    assert not list(fixture.service.inputs.glob('*.audio'))
    assert not list(fixture.service.cache.glob('clip-preview-*'))
    assert fixture.broker.snapshot()['leases'] == []


def test_retention_reserves_selected_volume_before_copy_or_inspection(tmp_path):
    fixture = configured(tmp_path)
    original = fixture.source.read_bytes()
    retained = fixture.editor.retain(fixture.source)
    event, stage, snapshot = fixture.observed[0]
    assert event == 'inspect' and stage.parent == fixture.service.inputs
    request = snapshot['leases'][0]['request']
    assert request['disk_bytes'] == len(original) + 64 * 1024**2
    assert Path(request['disk_path']) == fixture.service.inputs
    assert retained.name == sha256(original).hexdigest() + '.audio'
    assert retained.read_bytes() == fixture.source.read_bytes() == original
    assert not list(fixture.service.inputs.glob('.pending-*'))
    assert fixture.broker.snapshot()['leases'] == []


def test_preview_separates_metadata_and_frame_exact_disk_admission(tmp_path, monkeypatch):
    fixture = configured(tmp_path)
    requests = admission_hook(monkeypatch, fixture, lambda *_: None)
    output = fixture.editor.preview(fixture.project, 'one', 'clip')
    assert output.exists()
    assert [event[0] for event in fixture.observed] == ['inspect', 'mix']
    assert len(requests) == 2 and requests[0].disk_bytes == 0
    pcm_bytes = round(.5 * fixture.project.sample_rate) * 2 * 2 + 4096
    assert requests[1].disk_bytes == pcm_bytes * 2 + 1024**2
    assert requests[1].disk_path == fixture.service.cache
    assert fixture.source.read_bytes() != b''
    assert fixture.broker.snapshot()['leases'] == []


def test_preview_output_budget_rejects_before_mix_admission(tmp_path, monkeypatch):
    fixture = configured(tmp_path, budget=AudioBudget(
        threads=1, max_output_bytes=4096, reserve_disk_bytes=0))
    requests = admission_hook(monkeypatch, fixture, lambda *_: None)
    with pytest.raises(ValueError, match='output allowance'):
        fixture.editor.preview(fixture.project, 'one', 'clip')
    assert [event[0] for event in fixture.observed] == ['inspect']
    assert len(requests) == 1 and requests[0].disk_bytes == 0
    assert not list(fixture.service.cache.glob('clip-preview-*'))
    assert fixture.broker.snapshot()['leases'] == []


def test_retention_total_quota_rejects_before_stage_creation(tmp_path):
    fixture = configured(tmp_path, total_disk_bytes=1024**2)
    with pytest.raises(AudioError, match='total private disk'):
        fixture.editor.retain(fixture.source)
    assert fixture.observed == []
    assert not list(fixture.service.inputs.glob('*.audio'))
    assert fixture.broker.snapshot()['leases'] == []


def test_private_quota_is_rechecked_after_resource_admission(tmp_path, monkeypatch):
    fixture = configured(tmp_path, total_disk_bytes=2 * 1024**2)
    retained = fixture.service.cache / 'another-completed-cache.bin'
    def changed(number, request):
        if number == 2:
            retained.write_bytes(b'x' * (1024**2))
    admission_hook(monkeypatch, fixture, changed)
    with pytest.raises(AudioError, match='total private disk'):
        fixture.editor.preview(fixture.project, 'one', 'clip')
    assert [event[0] for event in fixture.observed] == ['inspect']
    assert retained.read_bytes() == b'x' * (1024**2)
    assert not list(fixture.service.cache.glob('clip-preview-*'))
    assert fixture.broker.snapshot()['leases'] == []


@pytest.mark.parametrize('operation,at', [('retain', 1), ('selection', 1), ('preview', 2)])
def test_changed_source_after_admission_never_reaches_decoder_or_mix(
        tmp_path, monkeypatch, operation, at):
    fixture = configured(tmp_path)
    before = fixture.source.read_bytes()
    # Model a metadata-equivalent mutation; the actual changed PCM bytes must
    # still be rejected by their captured digest after resource admission.
    monkeypatch.setattr(clips, '_stamp', lambda value: (value.st_dev, value.st_ino, value.st_size))
    def changed(number, request):
        if number == at:
            with fixture.source.open('r+b') as stream:
                stream.seek(44)
                stream.write(b'\x10\x00')
    admission_hook(monkeypatch, fixture, changed)
    with pytest.raises(ValueError, match='changed'):
        if operation == 'retain':
            fixture.editor.retain(fixture.source)
        elif operation == 'selection':
            fixture.editor.edit(fixture.project, 'one', 'clip', pan=.5)
        else:
            fixture.editor.preview(fixture.project, 'one', 'clip')
    assert fixture.source.read_bytes() != before
    assert [event[0] for event in fixture.observed] == (['inspect'] if operation == 'preview' else [])
    assert fixture.service.projects.load('clips') == fixture.project
    assert not list(fixture.service.inputs.glob('*.audio'))
    assert not list(fixture.service.cache.glob('clip-preview-*'))
    assert fixture.broker.snapshot()['leases'] == []


@pytest.mark.parametrize('operation,at', [('selection', 1), ('preview', 2)])
def test_changed_project_after_admission_cannot_publish_or_preview(
        tmp_path, monkeypatch, operation, at):
    fixture = configured(tmp_path)
    other = replace(fixture.project, revision=2, title='Another window committed this title.')
    def changed(number, request):
        if number == at:
            fixture.service.projects.save(other, expected_revision=1,
                                           expected_fingerprint=fixture.project.fingerprint)
    admission_hook(monkeypatch, fixture, changed)
    with pytest.raises(ValueError, match='changed'):
        if operation == 'selection':
            fixture.editor.edit(fixture.project, 'one', 'clip', pan=.5)
        else:
            fixture.editor.preview(fixture.project, 'one', 'clip')
    assert fixture.service.projects.load('clips') == other
    assert [event[0] for event in fixture.observed] == (['inspect'] if operation == 'preview' else [])
    assert not list(fixture.service.cache.glob('clip-preview-*'))
    assert fixture.broker.snapshot()['leases'] == []


@pytest.mark.parametrize('operation,at', [('retain', 1), ('selection', 1), ('preview', 2)])
def test_late_admission_cancellation_never_starts_more_work(tmp_path, monkeypatch, operation, at):
    fixture = configured(tmp_path)
    stop = Event()
    admission_hook(monkeypatch, fixture, lambda number, request: stop.set() if number == at else None)
    with pytest.raises((InterruptedError, AudioCancelled, ResourceCancelled)):
        if operation == 'retain':
            fixture.editor.retain(fixture.source, cancelled=stop.is_set)
        elif operation == 'selection':
            fixture.editor.edit(fixture.project, 'one', 'clip', pan=.5, cancelled=stop.is_set)
        else:
            fixture.editor.preview(fixture.project, 'one', 'clip', cancelled=stop.is_set)
    assert [event[0] for event in fixture.observed] == (['inspect'] if operation == 'preview' else [])
    assert fixture.service.projects.load('clips') == fixture.project
    assert not list(fixture.service.cache.glob('clip-preview-*'))
    assert not list(fixture.service.inputs.glob('*.audio'))
    assert fixture.broker.snapshot()['leases'] == []


def test_failed_owned_registry_close_forbids_edits_and_allows_exact_owner_retry(tmp_path):
    editor = ClipEditor(tmp_path / 'library')
    project = editor.create()
    closed = []
    ready = Event()
    owner = CleanupOwner(ready.is_set)
    class Registry:
        def close(self):
            closed.append(self)
            if not owner.close():
                raise AudioCleanupPending('Retained fixture registry has not drained.',
                                          cleanup_owner=owner)
    registry = Registry()
    editor.service._voices = registry
    with pytest.raises(AudioCleanupPending):
        editor.close()
    assert editor._closed and editor.service._closed
    assert editor.service._voices is registry and closed == [registry]
    with pytest.raises(ValueError, match='closed'):
        editor.create('No new operation after a failed close.')
    assert editor.service.projects.load(project.id) == project
    ready.set()
    editor.close()
    assert owner.quiescent and closed == [registry, registry]
    assert editor.service._voices is None
    editor.close()
    assert closed == [registry, registry]


def test_two_pending_native_owners_keep_one_input_stage_until_both_drain(tmp_path):
    from carr_library.studio.clips import retain_audio_input
    from carr_library.operations._locks import NativeLock
    inputs = tmp_path / 'inputs'
    inputs.mkdir()
    source = audio(tmp_path / 'source.wav')
    original = source.read_bytes()
    ready = [Event(), Event()]
    owners = [CleanupOwner(event.is_set) for event in ready]
    stage = []
    def inspect(path, *, cancelled):
        stage.append(path)
        first = AudioCleanupPending('First fixture native owner.', cleanup_owner=owners[0])
        second = AudioCleanupPending('Second fixture native owner.', cleanup_owner=owners[1])
        outer = RuntimeError('Both exact children remain owned.')
        outer.__cause__, outer.__context__ = first, second
        raise outer
    with pytest.raises(RuntimeError, match='Both'):
        retain_audio_input(inputs, source, inspect=inspect)
    lock = NativeLock(inputs / '.selected-clip.lock')
    assert stage[0].read_bytes() == original and not lock.acquire()
    ready[0].set()
    assert owners[0].close()
    assert stage[0].read_bytes() == original and not lock.acquire()
    ready[1].set()
    assert owners[1].close()
    assert not stage[0].exists() and lock.acquire()
    lock.release()
    assert source.read_bytes() == original
