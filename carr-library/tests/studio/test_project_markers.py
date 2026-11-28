"""Versioned marker authority through the real Studio revision store."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import threading

import pytest

from carr_library.durable_state import atomic_json, encode
from carr_library.studio.project import (
    Cue, Marker, Project, ProjectStore, Scene, MAX_PROJECT_MARKERS,
)
from carr_library.studio.project_history import revisions, restored_project


def legacy_wire(schema):
    cue = {'id': 'cue', 'path': '/explicit-original.wav', 'start': 0,
           'gain_db': -6, 'pan': 0, 'role': 'sfx'}
    if schema >= 2:
        cue.update(path='inputs/' + 'a' * 64 + '.audio', trim_start=.25,
                   duration=.5, fade_in=.1, fade_out=.1, title='Retained')
    role = {'id': 'role', 'name': 'Narrator', 'voice_id': 'reviewed-voice',
            'language': 'en', 'pan': 0, 'gain_db': -3, 'rate': 0, 'volume': 100}
    scene = {'id': 'scene', 'title': 'One', 'music': None, 'minimum_duration': 0,
             'lines': [{'id': 'line', 'role': 'role', 'text': 'Exact source.',
                        'pause_after': .3, 'source_ref': ''}], 'cues': [cue]}
    value = {'id': 'legacy', 'title': 'Original', 'roles': [role], 'scenes': [scene],
             'original_script': 'Narrator: Exact source.', 'mode': 'faithful',
             'sample_rate': 24000, 'revision': 1, 'schema': schema}
    if schema == 3:
        effect = {'kind': 'gain', 'parameters': {'gain_db': -4}, 'enabled': True}
        value['effects'] = [effect]
        role['effects'], scene['effects'], cue['effects'] = [], [], []
    return value


def stored(tmp_path, schema=2):
    store = ProjectStore(tmp_path / 'state/studio/projects')
    project = Project('project', 'Named positions', (),
        (Scene('first', 'First', (), minimum_duration=1),
         Scene('second', 'Second', (), minimum_duration=2)),
        original_script='An unchanged authored timing plan.', mode='authored', schema=schema)
    return store, store.save(project)


def records(store, project):
    root = store.directory(project.id)
    historical = root / 'revisions'
    return ((root / 'project.json').read_bytes(),
            {path.name: path.read_bytes() for path in historical.glob('*.json')}
            if historical.exists() else {})


def add(store, project):
    return store.add_marker(project, scene_id='first', position=.25,
                            label='Arrival — початок', marker_id='arrival')


@pytest.mark.parametrize('schema', [1, 2, 3])
def test_every_legacy_project_wire_and_fingerprint_stays_exact(schema):
    value = legacy_wire(schema)
    raw = encode(value)
    project = Project.from_dict(json.loads(raw))
    assert project.markers == ()
    assert encode(project.to_dict()) == raw
    assert project.fingerprint == sha256(raw).hexdigest()
    assert 'markers' not in project.to_dict()
    with pytest.raises(ValueError, match='schema 4'):
        replace(project, markers=(Marker('point', 'scene', 0, 'Point'),))
    value['markers'] = []
    with pytest.raises(ValueError, match='Legacy'):
        Project.from_dict(value)


@pytest.mark.parametrize('rate', [16000, 22050, 24000, 44100, 48000])
def test_schema_four_roundtrip_keeps_sample_positions_without_changing_scene_duration(tmp_path, rate):
    store, original = stored(tmp_path)
    original = replace(original, sample_rate=rate)
    first = Marker('one-sample', 'first', 1 / rate, 'First sample')
    later = Marker('beyond-current-audio', 'second', 31536000, '界' * 256)
    project = replace(original, schema=4, markers=(first, later))
    raw = encode(project.to_dict())
    loaded = Project.from_dict(json.loads(raw))
    assert loaded == project and loaded.fingerprint == sha256(raw).hexdigest()
    assert loaded.markers == (first, later)
    assert loaded.scenes == original.scenes
    assert tuple(scene.minimum_duration for scene in loaded.scenes) == (1, 2)
    assert loaded.original_script == original.original_script
    assert encode(loaded.to_dict()) == raw


@pytest.mark.parametrize('damage', [
    'unknown-scene', 'off-grid', 'negative', 'too-late', 'boolean', 'nan',
    'infinite', 'empty-label', 'long-label', 'nul-label', 'bad-id', 'duplicate-id',
])
def test_marker_wire_rejects_invalid_positions_labels_and_ambiguous_identity(tmp_path, damage):
    _, original = stored(tmp_path)
    point = {'id': 'point', 'scene_id': 'first', 'position': .25, 'label': 'Point'}
    if damage == 'unknown-scene':
        point['scene_id'] = 'absent'
    elif damage == 'off-grid':
        point['position'] = 1.5 / original.sample_rate
    elif damage == 'negative':
        point['position'] = -1
    elif damage == 'too-late':
        point['position'] = 31536001
    elif damage == 'boolean':
        point['position'] = True
    elif damage == 'nan':
        point['position'] = float('nan')
    elif damage == 'infinite':
        point['position'] = float('inf')
    elif damage == 'empty-label':
        point['label'] = ' \t'
    elif damage == 'long-label':
        point['label'] = 'x' * 257
    elif damage == 'nul-label':
        point['label'] = 'A\0B'
    elif damage == 'bad-id':
        point['id'] = '../point'
    wire = original.to_dict()
    wire['schema'], wire['markers'] = 4, [point, dict(point)] if damage == 'duplicate-id' else [point]
    with pytest.raises(ValueError):
        Project.from_dict(wire)


@pytest.mark.parametrize('damage', [
    'missing-list', 'mapping-list', 'untyped-item', 'unknown-field',
    'missing-label', 'missing-position', 'unknown-project-field',
])
def test_schema_four_requires_closed_typed_marker_wire(tmp_path, damage):
    _, original = stored(tmp_path)
    wire = json.loads(encode(replace(original, schema=4,
        markers=(Marker('point', 'first', .25, 'Point'),)).to_dict()))
    if damage == 'missing-list':
        wire.pop('markers')
    elif damage == 'mapping-list':
        wire['markers'] = {'point': wire['markers'][0]}
    elif damage == 'untyped-item':
        wire['markers'] = ['point']
    elif damage == 'unknown-field':
        wire['markers'][0]['audio_path'] = '/not-an-authority'
    elif damage == 'missing-label':
        wire['markers'][0].pop('label')
    elif damage == 'missing-position':
        wire['markers'][0].pop('position')
    else:
        wire['marker_store'] = '/second-authority'
    with pytest.raises(ValueError):
        Project.from_dict(wire)


def test_marker_collection_admits_exact_bound_and_refuses_growth_before_store_write(tmp_path):
    store, original = stored(tmp_path)
    points = tuple(Marker('m' + str(index), 'first', index / original.sample_rate, 'Point')
                   for index in range(MAX_PROJECT_MARKERS))
    full = replace(original, schema=4, revision=2, markers=points)
    store.save(full, expected_revision=original.revision, expected_fingerprint=original.fingerprint)
    before = records(store, full)
    assert len(Project.from_dict(json.loads(encode(full.to_dict()))).markers) == 1024
    with pytest.raises(ValueError, match='1024'):
        store.add_marker(full, scene_id='first', position=1, label='Overflow')
    with pytest.raises(ValueError, match='bounded'):
        replace(full, markers=points + (Marker('overflow', 'first', 1, 'Overflow'),))
    assert records(store, full) == before


@pytest.mark.parametrize('schema', [1, 2, 3])
def test_add_edit_remove_and_history_share_exact_existing_revision_cas(tmp_path, schema):
    store, original = stored(tmp_path, schema)
    original_raw = encode(original.to_dict())
    first = add(store, original)
    assert first.schema == 4 and first.revision == original.revision + 1
    assert first.scenes == original.scenes and first.roles == original.roles
    edited = store.update_marker(first, 'arrival', position=.75, label='Reviewed arrival')
    assert edited.markers[0].id == first.markers[0].id
    assert edited.markers[0].scene_id == first.markers[0].scene_id
    assert edited.revision == first.revision + 1
    removed = store.remove_marker(edited, 'arrival')
    assert removed.schema == 4 and removed.markers == () and removed.revision == edited.revision + 1
    assert removed.scenes == original.scenes and removed.original_script == original.original_script
    current, saved = records(store, removed)
    assert current == encode(removed.to_dict()) and len(saved) == 3
    assert saved['00000001-' + original.fingerprint + '.json'] == original_raw
    refs = revisions(store, removed)
    chosen = next(item for item in refs if item.fingerprint == first.fingerprint)
    restored = restored_project(store, removed, chosen)
    store.save(restored, expected_revision=removed.revision, expected_fingerprint=removed.fingerprint)
    assert restored.markers == first.markers and restored.revision == removed.revision + 1
    assert restored.scenes == original.scenes
    assert (store.directory(original.id) / 'revisions' /
            ('00000001-' + original.fingerprint + '.json')).read_bytes() == original_raw


@pytest.mark.parametrize('operation', ['add', 'update', 'remove'])
@pytest.mark.parametrize('conflict', ['new-revision', 'same-revision-other-bytes'])
def test_stale_marker_edits_cannot_overwrite_an_actual_competing_saved_project(tmp_path, operation, conflict):
    store, original = stored(tmp_path)
    current = add(store, original)
    competitor = replace(current, title='Another writer',
                         revision=current.revision + (conflict == 'new-revision'))
    if conflict == 'new-revision':
        store.save(competitor, expected_revision=current.revision,
                   expected_fingerprint=current.fingerprint)
    else:
        atomic_json(store.directory(current.id) / 'project.json', competitor.to_dict(), replace=True)
    before = records(store, current)
    with pytest.raises(ValueError, match='changed since'):
        if operation == 'add':
            store.add_marker(current, scene_id='second', position=1, label='Late', marker_id='late')
        elif operation == 'update':
            store.update_marker(current, 'arrival', position=.5, label='Late')
        else:
            store.remove_marker(current, 'arrival')
    assert records(store, current) == before
    assert store.load(current.id) == competitor


def test_noop_marker_update_keeps_bytes_inode_history_and_fresh_cas(tmp_path):
    store, original = stored(tmp_path)
    current = add(store, original)
    path = store.directory(current.id) / 'project.json'
    before = records(store, current)
    stamp = path.stat()
    same = store.update_marker(current, 'arrival', position=.25, label='Arrival — початок')
    after = path.stat()
    assert same is current and records(store, current) == before
    assert (stamp.st_dev, stamp.st_ino, stamp.st_mtime_ns, stamp.st_size) == (
        after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)
    competitor = replace(current, title='Same revision was substituted')
    atomic_json(path, competitor.to_dict(), replace=True)
    with pytest.raises(ValueError, match='content changed'):
        store.update_marker(current, 'arrival', position=.25, label='Arrival — початок')
    assert store.load(current.id) == competitor


@pytest.mark.parametrize('operation', ['duplicate-add', 'missing-update', 'missing-remove'])
def test_marker_identity_refusals_leave_current_and_history_exact(tmp_path, operation):
    store, original = stored(tmp_path)
    current = add(store, original)
    before = records(store, current)
    with pytest.raises(ValueError):
        if operation == 'duplicate-add':
            add(store, current)
        elif operation == 'missing-update':
            store.update_marker(current, 'absent', position=0, label='Absent')
        else:
            store.remove_marker(current, 'absent')
    assert records(store, current) == before


@pytest.mark.parametrize('kind', ['true', 'interrupted', 'timeout'])
def test_marker_cancellation_before_publication_preserves_original_files_and_exception(tmp_path, kind):
    store, original = stored(tmp_path)
    before = records(store, original)
    error = InterruptedError('Original stop') if kind == 'interrupted' else TimeoutError('Original deadline')
    def stopped():
        if kind == 'true':
            return True
        raise error
    with pytest.raises((InterruptedError, TimeoutError)) as caught:
        store.add_marker(original, scene_id='first', position=0, label='Stopped', cancelled=stopped)
    if kind != 'true':
        assert caught.value is error
    assert records(store, original) == before


def test_late_cancel_after_actual_marker_publication_returns_committed_revision(tmp_path, monkeypatch):
    import carr_library.studio.project as module
    store, original = stored(tmp_path)
    path = store.directory(original.id) / 'project.json'
    stopped = threading.Event()
    actual = module.atomic_json
    def published(target, value, **kwargs):
        result = actual(target, value, **kwargs)
        if Path(target) == path and value.get('schema') == 4:
            assert path.read_bytes() == encode(value)
            stopped.set()
        return result
    monkeypatch.setattr(module, 'atomic_json', published)
    changed = store.add_marker(original, scene_id='first', position=0, label='Committed',
                               cancelled=stopped.is_set)
    assert stopped.is_set()
    assert changed.revision == original.revision + 1 and changed.markers[0].label == 'Committed'
    assert store.load(changed.id) == changed
    assert (store.directory(original.id) / 'revisions' /
            ('00000001-' + original.fingerprint + '.json')).read_bytes() == encode(original.to_dict())


@pytest.mark.parametrize('with_effects', [False, True])
def test_actual_book_plan_ownership_refuses_marker_edits_and_preserves_backup_linkage(tmp_path, with_effects):
    from carr_library.application import LibraryApp
    from carr_library.audio import Effect
    from carr_library.library_backup import create_backup, restore_backup
    from carr_library.studio.book_audio import BookAudioService, SplitOptions, capture_plan
    from carr_library.studio.project import Role
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Books')
    source = app.add_text(workspace, 'Source', 'These are exact retained book words.')
    role = Role('narrator', 'Narrator', 'review-only-local-id', 'en',
                effects=(Effect('gain', gain_db=-3),) if with_effects else ())
    plan = capture_plan(app, workspace, source['edition'], options=SplitOptions(), role=role)
    workflow = BookAudioService(app, workspace)
    try:
        workflow.save(plan)
        project = workflow.production.projects.save(plan.project(1))
        before = records(workflow.production.projects, project)
        plan_bytes = (workflow.plans / (plan.id + '.json')).read_bytes()
        assert plan.schema == (2 if with_effects else 1)
        assert project.schema == (3 if with_effects else 2)
        for operation in ('add', 'update', 'remove'):
            with pytest.raises(ValueError, match='Book-plan'):
                if operation == 'add':
                    workflow.production.projects.add_marker(
                        project, scene_id=project.scenes[0].id, position=0, label='Not editable')
                elif operation == 'update':
                    workflow.production.projects.update_marker(project, 'absent', position=0, label='No')
                else:
                    workflow.production.projects.remove_marker(project, 'absent')
        with pytest.raises(ValueError, match='Book-plan'):
            replace(project, schema=4)
        assert records(workflow.production.projects, project) == before
        assert (workflow.plans / (plan.id + '.json')).read_bytes() == plan_bytes
        backup = tmp_path / 'backup'
        digest = create_backup(app, backup)
    finally:
        workflow.close()
    restored = restore_backup(backup, tmp_path / 'restored', digest)
    restored_store = ProjectStore(restored / 'state/studio/projects')
    assert encode(restored_store.load(project.id).to_dict()) == encode(project.to_dict())
    assert (restored / 'state/studio/book-plans' / workflow.plans.name /
            (plan.id + '.json')).read_bytes() == plan_bytes


def test_legacy_external_clip_cannot_gain_marker_schema_without_managed_input_admission(tmp_path, monkeypatch):
    selected = tmp_path / 'legacy-original.wav'
    selected.write_bytes(b'An unadmitted legacy original.')
    original_bytes = selected.read_bytes()
    store = ProjectStore(tmp_path / 'state/studio/projects')
    project = store.save(Project('legacy', 'Legacy', (),
        (Scene('scene', 'One', (), cues=(Cue('cue', str(selected)),)),), schema=1))
    before = records(store, project)
    actual_open = Path.open
    def guarded(path, *args, **kwargs):
        if Path(path) == selected:
            pytest.fail('Adding metadata opened an unadmitted legacy original.')
        return actual_open(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', guarded)
        with pytest.raises(ValueError, match='Portable Studio'):
            store.add_marker(project, scene_id='scene', position=0, label='Refused')
    assert records(store, project) == before and selected.read_bytes() == original_bytes
