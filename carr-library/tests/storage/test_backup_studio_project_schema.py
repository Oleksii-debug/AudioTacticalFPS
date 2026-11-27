"""Preserve exact canonical studio project data without installing its runtime."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import read_json
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.studio import Cue, Line, Music, Project, ProjectStore, Role, Scene


def _files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def _manifest(root):
    return json.loads((root / 'manifest.json').read_bytes())


def _resign(root, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')) + '\n').encode('utf-8')
    (root / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def _library(tmp_path, *, pending=False):
    app = LibraryApp(tmp_path / 'source-library')
    workspace = app.create_workspace('Canonical studio data')
    if pending:
        app.add_text(workspace, 'Retained source', 'An unchanged library source.')
    # Engine and voice choices are metadata; no installed engine is invoked
    # to preserve a project that has no external input-media cues.
    first = Project(
        'canonical-project', 'Перша редакція',
        (Role('narrator', 'Оповідач', voice_id='canonical-voice', language='uk',
              pan=-.25, gain_db=-6, rate=1, volume=80),),
        (Scene('chapter-1', 'Розділ 1',
               (Line('line-1', 'narrator', 'Незмінний текст e\u0301.',
                     pause_after=1.25, source_ref='preserved-source-reference'),),
               music=Music('hopeful', 85, -28, 'ace-step', 7),
               minimum_duration=12),),
        original_script='Незмінний текст e\u0301.\n', mode='authored')
    store = ProjectStore(app.root / 'state' / 'studio' / 'projects')
    store.save(first)
    current = replace(first, title='Друга редакція', revision=2)
    store.save(current, expected_revision=1)
    return app, first, current


@pytest.mark.parametrize('layout', ['host-v4', 'host-v5', 'canonical-v4'])
def test_canonical_project_and_revision_round_trip_through_typed_backup(tmp_path, layout):
    app, first, current = _library(tmp_path, pending=layout != 'host-v4')
    project_root = app.root / 'state' / 'studio' / 'projects'
    expected = {name: raw for name, raw in _files(project_root).items()
                if name.endswith('.json')}
    assert len(expected) == 2
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup)
    manifest = _manifest(backup)
    if layout == 'canonical-v4':
        # Exact canonical state+pending field set; sidecar payloads already
        # reside beneath state/, so only the outer inventory is translated.
        sidecars = manifest.pop('sidecars')
        assert all(entry['filename'].startswith('state/') for entry in sidecars)
        manifest.pop('layout')
        manifest.update(version=4, state=[
            dict(entry, filename=entry['filename'][len('state/'):])
            for entry in sidecars])
        checksum = _resign(backup, manifest)
    elif layout == 'host-v4':
        assert manifest['version'] == 4 and 'pending' not in manifest
    else:
        assert manifest['version'] == 5 and manifest['layout'] == 'sidecars-pending-v1'
    before = _files(backup)
    assert verify_backup(backup, checksum) == manifest
    target = restore_backup(backup, tmp_path / 'restored', checksum)
    restored_root = target / 'state' / 'studio' / 'projects'
    assert _files(restored_root) == expected
    restored = ProjectStore(restored_root).load(current.id)
    assert restored == current
    historical = restored_root / first.id / 'revisions' / (
        f'{first.revision:08d}-{first.fingerprint}.json')
    assert Project.from_dict(read_json(historical)) == first
    assert restored.scenes[0].cues == ()
    assert restored.scenes[0].music.engine == 'ace-step'
    assert _files(backup) == before
    assert {name: raw for name, raw in _files(project_root).items()
            if name.endswith('.json')} == expected

    # A second complete backup must retain both exact revisions too.
    repeated = tmp_path / 'second-backup'
    repeated_digest = create_backup(LibraryApp(target), repeated)
    verify_backup(repeated, repeated_digest)
    assert _files(repeated / 'state' / 'studio' / 'projects') == expected


@pytest.mark.parametrize('change', [
    'project-id', 'revision-number', 'revision-fingerprint', 'unknown-field',
])
def test_resigned_project_corruption_is_rejected_before_restore_publication(tmp_path, change):
    app, first, _ = _library(tmp_path)
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    manifest = _manifest(backup)
    if change in ('revision-number', 'revision-fingerprint'):
        name = (f'state/studio/projects/{first.id}/revisions/'
                f'{first.revision:08d}-{first.fingerprint}.json')
    else:
        name = f'state/studio/projects/{first.id}/project.json'
    path = backup / name
    project = json.loads(path.read_bytes())
    if change == 'project-id':
        project['id'] = 'different-project'
    elif change == 'revision-number':
        project['revision'] += 1
    elif change == 'revision-fingerprint':
        project['title'] = 'Changed historical content'
    else:
        project['unrecognized_runtime_options'] = {}
    raw = (json.dumps(project, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')) + '\n').encode('utf-8')
    path.write_bytes(raw)
    entry = next(item for item in manifest['sidecars'] if item['filename'] == name)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    checksum = _resign(backup, manifest)
    before = _files(backup)
    with pytest.raises(ValueError, match='invalid backup sidecar'):
        restore_backup(backup, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()
    assert _files(backup) == before


@pytest.mark.parametrize('operation', ['backup', 'restore'])
@pytest.mark.parametrize('name', [
    'state/studio/jobs/1234567890abcdef1234567890abcdef.json',
    'state/audio/engines.json',
], ids=['studio-job', 'engine-settings'])
def test_project_bridge_does_not_accept_unsupported_runtime_state(tmp_path, operation, name):
    app, _, _ = _library(tmp_path)
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    root = app.root if operation == 'backup' else backup
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = b'{"synthetic":"unsupported runtime state"}\n'
    path.write_bytes(raw)
    if operation == 'restore':
        manifest = _manifest(backup)
        manifest['sidecars'].append(dict(filename=name, size_bytes=len(raw),
                                        sha256=sha256(raw).hexdigest()))
        checksum = _resign(backup, manifest)
    before = _files(root)
    with pytest.raises(ValueError, match='validated backup adapter'):
        if operation == 'backup':
            create_backup(app, tmp_path / 'rejected', include_generated=True)
        else:
            restore_backup(backup, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()
    assert all((root / relative).read_bytes() == content
               for relative, content in before.items()
               if not relative.endswith(('-wal', '-shm')))


def test_rebuildable_canonical_cache_stays_excluded_and_unchanged(tmp_path):
    app, _, _ = _library(tmp_path)
    cached = app.root / 'state' / 'studio' / 'cache' / ('a' * 64 + '.wav')
    cached.parent.mkdir()
    cached.write_bytes(b'Synthetic rebuildable cache; not a source cue.')
    before = cached.read_bytes()
    backup = tmp_path / 'backup'
    checksum = create_backup(app, backup, include_generated=True)
    manifest = verify_backup(backup, checksum)
    assert all(not entry['filename'].startswith('state/studio/cache/')
               for entry in manifest['sidecars'])
    assert not (backup / 'state' / 'studio' / 'cache').exists()
    assert cached.read_bytes() == before


def test_declared_canonical_cache_cannot_be_restored_as_supported_state(tmp_path):
    app, _, _ = _library(tmp_path)
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    name = 'state/studio/cache/' + 'a' * 64 + '.wav'
    cached = backup / name
    cached.parent.mkdir()
    raw = b'Synthetic unsupported cache payload.'
    cached.write_bytes(raw)
    manifest = _manifest(backup)
    manifest['sidecars'].append(dict(filename=name, size_bytes=len(raw),
                                    sha256=sha256(raw).hexdigest()))
    checksum = _resign(backup, manifest)
    before = _files(backup)
    with pytest.raises(ValueError, match='validated backup adapter'):
        restore_backup(backup, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()
    assert _files(backup) == before


@pytest.mark.parametrize('operation', ['backup', 'restore'])
def test_external_media_cues_are_rejected_without_opening_source_media(
        tmp_path, monkeypatch, operation):
    app, _, current = _library(tmp_path)
    cue = tmp_path / 'separate-input-audio.wav'
    cue_bytes = b'Synthetic original input audio: must remain untouched.'
    cue.write_bytes(cue_bytes)
    scene = replace(current.scenes[0], cues=(
        Cue('external-cue', str(cue), start=2.5, gain_db=-9, pan=.5),))
    with_cue = replace(current, scenes=(scene,))
    backup = tmp_path / 'backup'
    if operation == 'backup':
        ProjectStore(app.root / 'state' / 'studio' / 'projects').save(
            replace(with_cue, revision=3), expected_revision=2)
        root = app.root
    else:
        create_backup(app, backup)
        root = backup
        name = f'state/studio/projects/{current.id}/project.json'
        raw = (json.dumps(with_cue.to_dict(), ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')) + '\n').encode('utf-8')
        (backup / name).write_bytes(raw)
        manifest = _manifest(backup)
        entry = next(item for item in manifest['sidecars'] if item['filename'] == name)
        entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
        checksum = _resign(backup, manifest)
    before = _files(root)
    original_open = Path.open

    def reject_source_open(path, *args, **kwargs):
        if path == cue:
            pytest.fail('The project-only backup adapter opened external source media.')
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'open', reject_source_open)
        with pytest.raises(ValueError, match='external media cues'):
            if operation == 'backup':
                create_backup(app, tmp_path / 'rejected')
            else:
                restore_backup(backup, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()
    assert cue.read_bytes() == cue_bytes
    assert all((root / name).read_bytes() == raw for name, raw in before.items()
               if not name.endswith(('-wal', '-shm')))
