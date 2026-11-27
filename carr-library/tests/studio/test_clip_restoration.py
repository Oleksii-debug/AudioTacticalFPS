"""Portable originals do not restore authority over old output directories."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
import struct
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import AudioRuntime
from carr_library.durable_state import atomic_json, encode, read_json
from carr_library.library_backup import create_backup, restore_backup
from carr_library.speech import VoiceRegistry
from carr_library.studio.clips import ClipEditor
from carr_library.studio.project import Cue, Project, Scene
from carr_library.studio.render import ProductionService


def audio(path):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<' + 'h' * 12000, *[1000 if i % 2 else -1000 for i in range(12000)]))
    return path


def service_at(root):
    return ProductionService(root / 'state' / 'studio', voices=VoiceRegistry([]),
                             runtime_factory=AudioRuntime)


def retained_project(service, selected):
    editor = ClipEditor(service.root.parent.parent, service=service)
    project = editor.create('Portable recording')
    return editor.add(project, project.scenes[0].id, selected,
                      role='microphone', trim_start=.1, duration=.25,
                      gain_db=0, pan=1, fade_in=.02)


def deny_old_paths(monkeypatch, forbidden):
    """Fail on old file-system inspection as well as actual content opens."""
    forbidden = tuple(Path(path) for path in forbidden)
    for method in ('open', 'stat', 'lstat', 'exists', 'mkdir', 'iterdir'):
        actual = getattr(Path, method)

        def checked(path, *args, _actual=actual, **kwargs):
            path = Path(path)
            if any(path == old or path.is_relative_to(old) for old in forbidden):
                pytest.fail('A restored job touched a retired source or output path.')
            return _actual(path, *args, **kwargs)

        monkeypatch.setattr(Path, method, checked)


@pytest.mark.parametrize('same_path', [False, True])
def test_actual_completed_clip_restore_requires_fresh_output_before_any_old_path_io(
        tmp_path, monkeypatch, same_path):
    library = tmp_path / 'original-library'
    app = LibraryApp(library)
    service = service_at(library)
    selected = audio(tmp_path / 'selected-original.wav')
    original = selected.read_bytes()
    project = retained_project(service, selected)
    old_outputs = tmp_path / 'retired-outputs'
    old_outputs.mkdir()
    job = service.submit(project, old_outputs, operation_id='portable-recovery')
    complete = service.run(job)
    master = Path(complete['destination'], 'master.wav').read_bytes()
    old_origin = dict(complete['origin'])
    original_job = (service.jobs_root / job / 'job.json').read_bytes()
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)

    selected.unlink()
    shutil.rmtree(library)
    shutil.rmtree(old_outputs)
    root = restore_backup(backup, library if same_path else tmp_path / 'restored-library', digest)
    # Reuse the old service object for the same-path case: an in-memory origin
    # comparison would incorrectly accept the old completed output authority.
    restored = service if same_path else service_at(root)
    assert (restored.jobs_root / job / 'job.json').read_bytes() == original_job
    assert not (restored.root / '.local-origin.json').exists()
    current = restored.projects.load(project.id)
    assert current.to_dict() == project.to_dict() and current.fingerprint == project.fingerprint
    retained = restored.root / current.scenes[0].cues[0].path
    assert retained.read_bytes() == original

    prohibited = [selected, old_outputs]
    if not same_path:
        prohibited.append(library)
    with monkeypatch.context() as guarded:
        deny_old_paths(guarded, prohibited)
        runtime_calls = []

        def forbidden_runtime(*args, **kwargs):
            runtime_calls.append(True)
            pytest.fail('The restored output boundary must precede runtime admission.')

        guarded.setattr(restored, '_runtime', forbidden_runtime)
        saved = restored.get(job)
        assert saved['status'] == 'complete'
        assert saved['recovery']['requires_output_directory'] is True
        assert saved['origin'] == old_origin
        assert restored.jobs()[0]['recovery']['requires_output_directory'] is True
        assert read_json(restored.root / '.local-origin.json')['nonce'] != old_origin['nonce']
        with pytest.raises(ValueError, match='newly selected output'):
            restored.run(job)
        with pytest.raises(ValueError, match='newly selected output'):
            restored.submit(current, old_outputs, operation_id=job)
        assert not runtime_calls
        assert (restored.jobs_root / job / 'job.json').read_bytes() == original_job

        new_outputs = tmp_path / 'new-approved-output'
        new_outputs.mkdir()
        approved = restored.relocate_restored_job(job, new_outputs)
        assert approved['status'] == 'pending'
        assert approved['nodes'] == {} and approved['artifacts'] == []
        assert approved['runtime'] is None and 'result' not in approved
        assert approved['recovery']['requires_output_directory'] is False
        assert approved['origin']['nonce'] != old_origin['nonce']
        previous, = approved['recovery']['previous']
        for name in ('origin', 'destination', 'status', 'nodes', 'artifacts', 'runtime', 'result'):
            assert previous[name] == complete[name]
        assert Path(approved['destination']).parent == new_outputs
        assert not Path(approved['destination']).exists()
        assert not runtime_calls  # Selecting a destination alone never renders.

    with monkeypatch.context() as guarded:
        deny_old_paths(guarded, prohibited)
        final = restored.run(job)
        assert final['status'] == 'complete'
        assert Path(final['destination'], 'master.wav').read_bytes() == master
        assert restored.run(job)['status'] == 'complete'
        assert final['result']['lines'] == []
        assert final['result']['clips'][0]['source_sha256'] == sha256(original).hexdigest()
    assert not selected.exists() and not old_outputs.exists()


def test_pending_job_and_stale_service_recheck_live_origin_before_output_admission(tmp_path, monkeypatch):
    service = service_at(tmp_path / 'library')
    project = retained_project(service, audio(tmp_path / 'selected.wav'))
    output = tmp_path / 'output'
    output.mkdir()
    job = service.submit(project, output, operation_id='pending')
    previous = service.get(job)
    # A new local admission identity, such as the one generated after a restore,
    # invalidates this already-existing service instance too.
    atomic_json(service.root / '.local-origin.json',
                {'schema': 'carr-studio-origin/1', 'nonce': 'f' * 64}, replace=True)
    assert service.get(job)['recovery']['requires_output_directory'] is True
    with monkeypatch.context() as guarded:
        deny_old_paths(guarded, [output])
        guarded.setattr(service, '_runtime', lambda: pytest.fail('Foreign origin admitted a runtime.'))
        with pytest.raises(ValueError, match='newly selected output'):
            service.run(job)
        with pytest.raises(ValueError, match='newly selected output'):
            service.submit(project, output, operation_id=job)
    assert service.get(job)['origin'] == previous['origin']


@pytest.mark.parametrize('fault', ['missing', 'tampered'])
def test_missing_or_tampered_managed_original_never_publishes_audio(tmp_path, fault):
    service = service_at(tmp_path / 'library')
    selected = audio(tmp_path / 'selected.wav')
    project = retained_project(service, selected)
    output = tmp_path / 'output'
    output.mkdir()
    job = service.submit(project, output)
    retained = service.root / project.scenes[0].cues[0].path
    if fault == 'missing':
        retained.unlink()
    else:
        data = bytearray(retained.read_bytes())
        data[100] ^= 1
        retained.write_bytes(data)
    with pytest.raises((ValueError, FileNotFoundError)):
        service.run(job)
    assert list(output.iterdir()) == []
    assert service.get(job)['status'] == 'failed'


@pytest.mark.parametrize('operation', ['add', 'edit', 'preview'])
def test_legacy_absolute_cues_fail_portable_promotion_before_read_or_revision(
        tmp_path, monkeypatch, operation):
    service = service_at(tmp_path / 'library')
    editor = ClipEditor(tmp_path / 'library', service=service)
    external = tmp_path / 'legacy-file-need-not-exist.wav'
    project = Project('legacy', 'Legacy', (),
        (Scene('one', 'One', (), cues=(Cue('old', str(external)),)),))
    service.projects.save(project)
    original = encode(project.to_dict())
    monkeypatch.setattr(editor, 'retain', lambda *a, **k: pytest.fail('Legacy promotion tried to retain a source.'))
    monkeypatch.setattr(service, '_runtime', lambda: pytest.fail('Legacy promotion admitted a decoder.'))
    with monkeypatch.context() as guarded:
        deny_old_paths(guarded, [external])
        with pytest.raises(ValueError, match='legacy project'):
            if operation == 'add':
                editor.add(project, 'one', external)
            elif operation == 'edit':
                editor.edit(project, 'one', 'old', gain_db=-3)
            else:
                editor.preview(project, 'one', 'old')
    assert encode(service.projects.load(project.id).to_dict()) == original
    assert not list(service.projects.directory(project.id).glob('revisions/*.json'))


def test_legacy_remove_reorder_and_scene_duration_preserve_schema_one_without_source_io(tmp_path, monkeypatch):
    service = service_at(tmp_path / 'library')
    editor = ClipEditor(tmp_path / 'library', service=service)
    external = tmp_path / 'absent-legacy.wav'
    project = Project('legacy', 'Legacy', (), (
        Scene('one', 'One', (), cues=(Cue('old', str(external)),)),
        Scene('two', 'Two', (), minimum_duration=.2)))
    service.projects.save(project)
    with monkeypatch.context() as guarded:
        deny_old_paths(guarded, [external])
        moved = editor.move(project, 'one', 'old', 'two')
        changed = editor.scene_duration(moved, 'one', .4)
        removed = editor.remove(changed, 'two', 'old')
    assert [item.schema for item in (moved, changed, removed)] == [1, 1, 1]
    assert [item.revision for item in (moved, changed, removed)] == [2, 3, 4]
    saved = json.loads(encode(moved.to_dict()))
    assert set(saved['scenes'][1]['cues'][0]) == {'id', 'path', 'start', 'gain_db', 'pan', 'role'}
    assert saved['scenes'][1]['cues'][0]['path'] == str(external)
    assert not removed.scenes[1].cues


def test_empty_schema_one_project_promotes_only_after_explicit_managed_selection(tmp_path):
    service = service_at(tmp_path / 'library')
    editor = ClipEditor(tmp_path / 'library', service=service)
    project = Project('empty-legacy', 'Empty', (), (Scene('one', 'One', (), minimum_duration=.1),))
    service.projects.save(project)
    before = encode(project.to_dict())
    selected = audio(tmp_path / 'selected.wav')
    edited = editor.add(project, 'one', selected)
    assert edited.schema == 2 and edited.revision == 2
    cue = edited.scenes[0].cues[0]
    assert cue.path == 'inputs/' + sha256(selected.read_bytes()).hexdigest() + '.audio'
    revision, = service.projects.directory(project.id).glob('revisions/*.json')
    assert revision.read_bytes() == before


@pytest.mark.parametrize('path', [
    '/external.wav', '../escape.wav', 'inputs/../escape.audio',
    'inputs/' + 'A' * 64 + '.audio', 'inputs/' + '1' * 64 + '.wav',
    'inputs\\' + '1' * 64 + '.audio',
])
def test_schema_two_only_accepts_exact_portable_content_names(path):
    with pytest.raises(ValueError, match='Portable Studio clips'):
        Project('invalid', 'Invalid', (), (Scene('scene', 'Scene', (), cues=(Cue('clip', path),)),), schema=2)
