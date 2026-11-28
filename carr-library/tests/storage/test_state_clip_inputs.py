"""Portable clip input closure and local-only Studio output authority."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import shutil

import pytest

from carr_library.durable_state import atomic_json
from carr_library.state_backup import restore_state, snapshot_state
from carr_library.studio.project import Cue, Project, ProjectStore, Scene


def _entry(name, raw):
    return {'filename': name, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()}


def _project(root, *, data=b'Synthetic retained audio bytes.', revision=1):
    digest = sha256(data).hexdigest()
    path = root / 'studio' / 'inputs' / (digest + '.audio')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    project = Project('portable', 'Portable clips', (),
        (Scene('scene', 'Scene', (), cues=(Cue('clip', 'inputs/' + digest + '.audio'),)),),
        revision=revision, schema=2)
    ProjectStore(root / 'studio' / 'projects').save(project)
    return project, path


def _job(project, destination):
    return {'schema': 1, 'id': 'render-1', 'project': project.to_dict(),
            'fingerprint': project.fingerprint, 'status': 'complete',
            'destination': str(destination), 'artifacts': [],
            'origin': {'studio_root': str(destination.parent / 'previous-studio'),
                       'nonce': '1' * 64}}


@pytest.mark.parametrize('include_generated', [False, True])
def test_same_path_roundtrip_keeps_managed_project_bytes_but_not_local_origin(
        tmp_path, include_generated):
    state, copied = tmp_path / 'state', tmp_path / 'snapshot'
    project, retained = _project(state)
    external = tmp_path / 'selected-original-now-deleted.wav'
    old_output = tmp_path / 'previous-output'
    external.write_bytes(b'Original user selection, no longer needed.')
    old_output.mkdir()
    legacy = Project('legacy', 'Unchanged legacy project', (),
        (Scene('scene', 'Scene', (), cues=(Cue('old', str(external)),)),))
    ProjectStore(state / 'studio' / 'projects').save(legacy)
    job_path = state / 'studio' / 'jobs' / 'render-1' / 'job.json'
    atomic_json(job_path, _job(project, old_output))
    opaque = state / 'studio' / 'jobs' / 'opaque' / 'job.json'
    opaque.parent.mkdir(parents=True)
    opaque.write_bytes(b'Historical opaque sidecar; not JSON.')
    note = state / 'studio' / '.local-origin.json.notes'
    note.write_bytes(b'An ordinary user sidecar, preserved exactly.')
    origin = state / 'studio' / '.local-origin.json'
    atomic_json(origin, {'schema': 'carr-studio-origin/1', 'nonce': '1' * 64})
    original_project = (state / 'studio' / 'projects' / project.id / 'project.json').read_bytes()
    original_job = job_path.read_bytes()
    external.unlink()
    old_output.rmdir()

    entries = snapshot_state(state, copied, include_generated=include_generated)
    assert not any('.local-origin.json' == Path(row['filename']).name for row in entries)
    assert next(row for row in entries if row['filename'] == retained.relative_to(state).as_posix())['sha256'] == retained.stem
    # A real restore to the exact old path must not bring its local nonce back.
    shutil.rmtree(state)
    restore_state(copied, state, entries)

    assert not origin.exists()
    assert not external.exists() and not old_output.exists()
    store = ProjectStore(state / 'studio' / 'projects')
    assert store.load(project.id).fingerprint == project.fingerprint
    assert (state / 'studio' / 'projects' / project.id / 'project.json').read_bytes() == original_project
    assert store.load(legacy.id).to_dict() == legacy.to_dict()
    assert job_path.read_bytes() == original_job
    assert opaque.read_bytes() == b'Historical opaque sidecar; not JSON.'
    assert note.read_bytes() == b'An ordinary user sidecar, preserved exactly.'
    assert retained.read_bytes() == b'Synthetic retained audio bytes.'


@pytest.mark.parametrize('name', [
    'studio/.local-origin.json',
    'Studio/.LOCAL-ORIGIN.JSON',
    'studio/.local-origin.json/copied-authority.json',
])
def test_injected_old_nonce_cannot_be_restored_even_to_the_same_path(tmp_path, name):
    same_path, copied = tmp_path / 'state', tmp_path / 'snapshot'
    original = same_path / 'studio' / '.local-origin.json'
    atomic_json(original, {'schema': 'carr-studio-origin/1', 'nonce': 'a' * 64})
    raw = original.read_bytes()
    shutil.rmtree(same_path)
    incoming = copied / name
    incoming.parent.mkdir(parents=True)
    incoming.write_bytes(raw)
    with pytest.raises(ValueError, match='origin authority'):
        restore_state(copied, same_path, [_entry(name, raw)])
    assert not same_path.exists()


@pytest.mark.parametrize('fault', ['missing', 'tampered'])
def test_snapshot_rejects_missing_or_changed_managed_input(tmp_path, fault):
    state = tmp_path / 'state'
    _, retained = _project(state)
    if fault == 'missing':
        retained.unlink()
    else:
        retained.write_bytes(b'X' * retained.stat().st_size)
    with pytest.raises(ValueError, match='managed Studio input'):
        snapshot_state(state, tmp_path / 'snapshot')


@pytest.mark.parametrize('fault', ['missing', 'tampered'])
def test_manifest_consistent_restore_still_requires_exact_managed_inputs(tmp_path, fault):
    state, copied, restored = tmp_path / 'state', tmp_path / 'snapshot', tmp_path / 'restored'
    _, retained = _project(state)
    entries = snapshot_state(state, copied)
    name = retained.relative_to(state).as_posix()
    if fault == 'missing':
        (copied / name).unlink()
        entries = [entry for entry in entries if entry['filename'] != name]
    else:
        raw = b'Y' * (copied / name).stat().st_size
        (copied / name).write_bytes(raw)
        entries = [_entry(name, raw) if entry['filename'] == name else entry for entry in entries]
    # Generic byte manifests alone are consistent in both cases.
    shutil.rmtree(state)
    with pytest.raises(ValueError, match='managed Studio input'):
        restore_state(copied, restored, entries)


def test_snapshot_requires_managed_inputs_used_only_by_historical_revision(tmp_path):
    state = tmp_path / 'state'
    old, old_input = _project(state)
    raw = b'Different retained revision.'
    digest = sha256(raw).hexdigest()
    (old_input.parent / (digest + '.audio')).write_bytes(raw)
    revised = replace(old, revision=2, scenes=(
        replace(old.scenes[0], cues=(replace(old.scenes[0].cues[0],
            path='inputs/' + digest + '.audio'),)),))
    ProjectStore(state / 'studio' / 'projects').save(revised, expected_revision=1)
    old_input.unlink()
    with pytest.raises(ValueError, match='managed Studio input'):
        snapshot_state(state, tmp_path / 'snapshot')


@pytest.mark.parametrize('kind', ['job', 'exported-project'])
def test_snapshot_checks_embedded_job_and_exported_project_inputs(tmp_path, kind):
    state = tmp_path / 'state'
    project, retained = _project(state)
    shutil.rmtree(state / 'studio' / 'projects')
    directory = state / 'studio' / 'jobs' / 'render-1'
    if kind == 'job':
        atomic_json(directory / 'job.json', _job(project, tmp_path / 'never-open-output'))
    else:
        atomic_json(directory / 'project.json', project.to_dict())
    retained.unlink()
    with pytest.raises(ValueError, match='managed Studio input'):
        snapshot_state(state, tmp_path / 'snapshot')


def test_schema2_external_reference_is_rejected_without_opening_it(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    project, _ = _project(state)
    external = tmp_path / 'unapproved-source.wav'
    external.write_bytes(b'Never open this external declaration.')
    value = project.to_dict()
    value['scenes'][0]['cues'][0]['path'] = str(external)
    atomic_json(state / 'studio' / 'projects' / project.id / 'project.json', value, replace=True)
    actual_open = Path.open

    def local_only(path, *args, **kwargs):
        assert path != external, 'A schema2 declaration opened an external source.'
        return actual_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', local_only)
    with pytest.raises(ValueError, match='Portable Studio clips'):
        snapshot_state(state, tmp_path / 'snapshot')

@pytest.mark.parametrize('name', [
    'Studio/projects/portable/project.json',
    'studio/projects/portable/PROJECT.JSON',
    'STUDIO/jobs/render-1/JOB.JSON',
])
def test_portable_case_alias_cannot_hide_a_schema2_input_reference(tmp_path, name):
    state = tmp_path / 'state'
    project, _ = _project(state)
    shutil.rmtree(state)
    value = _job(project, tmp_path / 'never-open-output') if '/jobs/' in name else project.to_dict()
    atomic_json(state / name, value)
    with pytest.raises(ValueError, match='managed Studio input'):
        snapshot_state(state, tmp_path / 'snapshot')

def test_restore_refuses_existing_target_nonce_without_changing_live_authority(tmp_path):
    state, copied = tmp_path / 'state', tmp_path / 'snapshot'
    _project(state)
    origin = state / 'studio' / '.local-origin.json'
    atomic_json(origin, {'schema': 'carr-studio-origin/1', 'nonce': 'b' * 64})
    original = origin.read_bytes()
    entries = snapshot_state(state, copied)
    shutil.rmtree(state / 'studio' / 'inputs')
    shutil.rmtree(state / 'studio' / 'projects')

    with pytest.raises(ValueError, match='existing local Studio origin authority'):
        restore_state(copied, state, entries)

    assert origin.read_bytes() == original
    assert not (state / 'studio' / 'inputs').exists()
    assert not (state / 'studio' / 'projects').exists()
