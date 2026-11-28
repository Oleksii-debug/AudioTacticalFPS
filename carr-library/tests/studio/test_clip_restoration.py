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
from carr_library.operations import (ResourceBroker, ResourceLimits, ResourceRequest,
                                     ResourceStateError, ResourceTimeout)
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
    policy = ResourceLimits(cpu_slots=2, memory_bytes=2 * 1024**3,
                            disk_bytes=4 * 1024**3, min_free_disk_bytes=0)
    original_broker = service.broker
    original_broker.configure(policy)
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
    policy_bytes = original_broker.policy_path.read_bytes()
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
    assert (root / 'state/operations/resources.json').read_bytes() == policy_bytes
    assert not (root / 'runtime/resources/leases').exists()
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
        if same_path:
            assert restored.broker is not original_broker
            assert restored.broker.limits == policy
            assert restored.broker.policy_path.read_bytes() == policy_bytes
            assert restored.broker.snapshot()['leases'] == []
            # Explicit new-output approval does not rehabilitate the old owner.
            # Both stale paths fail before trying to acquire any native lock.
            import carr_library.operations.broker as broker_module
            with monkeypatch.context() as revoked:
                def no_old_admission(*args, **kwargs):
                    pytest.fail('The revoked coordinator attempted native admission.')
                revoked.setattr(broker_module.NativeLock, 'acquire', no_old_admission)
                with pytest.raises(ResourceStateError, match='selected resource directory changed'):
                    original_broker.snapshot(timeout=0)
                with pytest.raises(ResourceStateError, match='selected resource directory changed'):
                    original_broker.limits
            assert restored.broker.snapshot()['leases'] == []

    with monkeypatch.context() as guarded:
        deny_old_paths(guarded, prohibited)
        final = restored.run(job)
        assert final['status'] == 'complete'
        assert restored.broker.limits == policy
        assert restored.broker.policy_path.read_bytes() == policy_bytes
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


def _relocation_with_cached_admission(tmp_path):
    service = service_at(tmp_path / 'library')
    project = retained_project(service, audio(tmp_path / 'selected.wav'))
    output = tmp_path / 'output'
    output.mkdir()
    job = service.submit(project, output, operation_id='admission-recovery')
    broker = service.broker
    policy = ResourceLimits(cpu_slots=2, memory_bytes=2 * 1024**3,
                            disk_bytes=4 * 1024**3, min_free_disk_bytes=0)
    broker.configure(policy)
    fresh = tmp_path / 'fresh-output'
    fresh.mkdir()
    return service, job, broker, policy, fresh


def _change_local_origin(service):
    atomic_json(service.root / '.local-origin.json',
                {'schema': 'carr-studio-origin/1', 'nonce': 'f' * 64}, replace=True)


def test_explicit_relocation_keeps_current_policy_and_other_native_admission(tmp_path):
    service, job, previous, policy, fresh = _relocation_with_cached_admission(tmp_path)
    before = previous.policy_path.read_bytes()
    with previous.reserve(ResourceRequest(cpu_slots=2), operation_id='other-held-job',
                          timeout=0) as held:
        _change_local_origin(service)
        state = service.relocate_restored_job(job, fresh)
        current = service.broker
        assert current is not previous and current.limits == policy
        assert current.policy_path.read_bytes() == before
        assert [row['id'] for row in current.snapshot()['leases']] == [held.id]
        held.checkpoint()
        with pytest.raises(ResourceTimeout):
            with current.reserve(ResourceRequest(cpu_slots=1),
                                 operation_id='cannot-bypass-held-job', timeout=0):
                pytest.fail('Reopening the coordinator must not discard an active native lease.')
        assert state['status'] == 'pending'
        assert not Path(state['destination']).exists()
    assert current.snapshot()['leases'] == []


def test_unchanged_origin_never_recreates_a_missing_admission_inventory(tmp_path):
    service, job, previous, policy, fresh = _relocation_with_cached_admission(tmp_path)
    original = (service.jobs_root / job / 'job.json').read_bytes()
    policy_bytes = previous.policy_path.read_bytes()
    previous.leases_root.rmdir()
    with pytest.raises(ValueError, match='Only a restored portable production'):
        service.relocate_restored_job(job, fresh)
    assert service.broker is previous
    with pytest.raises(ResourceStateError, match='selected resource directory changed') as snapshot_error:
        service.broker.snapshot(timeout=0)
    assert isinstance(snapshot_error.value.__cause__, FileNotFoundError)
    with pytest.raises(ResourceStateError, match='selected resource directory changed') as limits_error:
        service.broker.limits
    assert isinstance(limits_error.value.__cause__, FileNotFoundError)
    assert not previous.leases_root.exists()
    assert (service.jobs_root / job / 'job.json').read_bytes() == original
    assert previous.policy_path.read_bytes() == policy_bytes
    assert ResourceLimits.from_dict(read_json(previous.policy_path)['limits']) == policy
    assert list(fresh.iterdir()) == []


@pytest.mark.parametrize('damage', ['missing', 'invalid'])
def test_relocation_never_replaces_missing_or_invalid_retained_policy(tmp_path, damage):
    service, job, previous, policy, fresh = _relocation_with_cached_admission(tmp_path)
    _change_local_origin(service)
    original = (service.jobs_root / job / 'job.json').read_bytes()
    if damage == 'missing':
        previous.policy_path.unlink()
    else:
        previous.policy_path.write_bytes(b'{"schema":"invalid-current-policy"}')
    damaged = None if damage == 'missing' else previous.policy_path.read_bytes()
    with pytest.raises((FileNotFoundError, ValueError, RuntimeError)):
        service.relocate_restored_job(job, fresh)
    assert service.broker is previous
    assert (service.jobs_root / job / 'job.json').read_bytes() == original
    if damage == 'missing':
        assert not previous.policy_path.exists()
    else:
        assert previous.policy_path.read_bytes() == damaged
    assert list(fresh.iterdir()) == []


@pytest.mark.parametrize('damage', ['denied', 'not-directory', 'reparse'])
def test_relocation_rejects_invalid_runtime_namespace_without_resetting_authority(
        tmp_path, monkeypatch, damage):
    from types import SimpleNamespace
    service, job, previous, policy, fresh = _relocation_with_cached_admission(tmp_path)
    _change_local_origin(service)
    original = (service.jobs_root / job / 'job.json').read_bytes()
    original_policy = previous.policy_path.read_bytes()
    if damage == 'not-directory':
        previous.leases_root.rmdir()
        previous.leases_root.write_bytes(b'not a directory')
    else:
        actual = Path.lstat
        def inspect(path, *args, **kwargs):
            info = actual(path, *args, **kwargs)
            if Path(path) == previous.leases_root:
                if damage == 'denied':
                    raise PermissionError('Actual admission inventory is unreadable.')
                # Exercise the same cross-platform Win32 metadata rejection;
                # this is not a claim to create a real junction on POSIX.
                return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink,
                                       st_file_attributes=0x400)
            return info
        monkeypatch.setattr(Path, 'lstat', inspect)
    with pytest.raises((OSError, ValueError)):
        service.relocate_restored_job(job, fresh)
    assert service.broker is previous
    assert (service.jobs_root / job / 'job.json').read_bytes() == original
    assert previous.policy_path.read_bytes() == original_policy
    assert list(fresh.iterdir()) == []


def test_policy_removed_between_review_and_reopen_never_becomes_defaults(tmp_path, monkeypatch):
    service, job, previous, policy, fresh = _relocation_with_cached_admission(tmp_path)
    _change_local_origin(service)
    original = (service.jobs_root / job / 'job.json').read_bytes()
    actual = ResourceBroker.for_app
    entered = []

    def remove_reviewed_policy(cls, root, **kwargs):
        # The early current-policy read has succeeded. Remove the real file
        # before the constructor reaches its authoritative locked decision.
        entered.append(True)
        previous.policy_path.unlink()
        return actual(root, **kwargs)

    monkeypatch.setattr(ResourceBroker, 'for_app', classmethod(remove_reviewed_policy))
    with pytest.raises(ResourceStateError, match='retained resource policy is missing'):
        service.relocate_restored_job(job, fresh)
    assert entered == [True]
    assert service.broker is previous
    assert not previous.policy_path.exists()
    assert (service.jobs_root / job / 'job.json').read_bytes() == original
    assert list(fresh.iterdir()) == []
