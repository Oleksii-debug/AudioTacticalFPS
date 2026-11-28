"""Native and foreign-flat recovery of passive primary Director effect data."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import runpy
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.director import ProductionDirector
from carr_library.audio.models import Effect
from carr_library.audio.projects import ProjectStore, parse_script, read_json
from carr_library.audio.voices import VoiceRegistry
from carr_library.ingest.restore import RestoreError
from carr_library.keybindings import Command, KeybindingStore
from carr_library.library_backup import create_backup, restore_backup


FLAT = runpy.run_path(str(Path(__file__).with_name('test_canonical_backup_compat.py')))
KEYS = {'fixture.director-effects': 'Ctrl+F9'}


def prepared(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Passive effect recovery')
    original = 'Український оригінал.\r\nExact immutable railway source.'
    app.add_text(workspace, 'Source', original)
    KeybindingStore(app.root).save(
        [Command('fixture.director-effects', 'Synthetic Director effect recovery')], KEYS)
    store = ProjectStore(app.root)
    base = parse_script('Narrator: ' + original, ['fixture:pcm'])
    project = replace(base,
        roles=(replace(base.roles[0], effects=(Effect('equalizer', frequency=900, gain_db=-3),)),),
        scenes=(replace(base.scenes[0], effects=(Effect('reverb', enabled=False),)),),
        effects=(Effect('compressor'), Effect('gain', gain_db=-2)))
    store.create(project)
    director = ProductionDirector(app.root)
    job_id = director.create_job(project.id, tmp_path / 'unapproved-output')
    return app, workspace, store, project, job_id, original


def snapshot(app, folder, flat):
    if flat:
        return FLAT['_flat_backup'](app, folder)
    checksum = create_backup(app, folder)
    return read_json(folder / 'manifest.json'), checksum


@pytest.mark.parametrize('flat', [False, True], ids=['native5', 'foreign-flat4'])
def test_typed_effects_survive_real_backup_without_restoring_execution_authority(tmp_path, monkeypatch, flat):
    app, workspace, store, project, job_id, original = prepared(tmp_path)
    project_path = store.path(project.id) / 'project.json'
    before = project_path.read_bytes()
    monkeypatch.setattr(subprocess, 'Popen',
                        lambda *a, **k: pytest.fail('Passive effect backup or recovery launched a process.'))
    monkeypatch.setattr(VoiceRegistry, 'for_library',
                        classmethod(lambda *a, **k: pytest.fail('Passive recovery discovered a voice.')))
    folder = tmp_path / 'backup'
    manifest, checksum = snapshot(app, folder, flat)
    assert manifest['version'] == (4 if flat else 5) and manifest['schema_version'] == 6
    recovered_root = restore_backup(folder, tmp_path / 'recovered', checksum)
    recovered = ProjectStore(recovered_root).load(project.id)
    assert recovered == project and recovered.fingerprint == project.fingerprint
    assert (recovered_root / project_path.relative_to(app.root)).read_bytes() == before
    assert project_path.read_bytes() == before
    assert KeybindingStore(recovered_root).load() == KEYS
    restored = ProductionDirector(recovered_root)
    state = restored.status(project.id, job_id)
    assert state['restore_requires_new_output'] is True and state['state'] == 'paused'
    assert state['project'] == json.loads(before) and state['result'] is None and state['outputs'] == []
    with pytest.raises(ValueError, match='restored from backup'):
        restored.render(project.id, job_id=job_id)
    assert not (tmp_path / 'unapproved-output').exists()
    hit, = LibraryApp(recovered_root).search(workspace, 'railway')
    assert hit.quote == 'railway'
    assert recovered.script == 'Narrator: ' + original


@pytest.mark.parametrize('flat', [False, True], ids=['native5', 'foreign-flat4'])
@pytest.mark.parametrize('scope', ['role', 'scene', 'master'])
@pytest.mark.parametrize('damage', ['raw-filter', 'nonboolean-bypass'])
def test_rehashed_invalid_effects_fail_typed_recovery_before_publication(tmp_path, flat, scope, damage):
    app, _, store, project, _, _ = prepared(tmp_path)
    folder = tmp_path / 'bad-backup'
    manifest, _ = snapshot(app, folder, flat)
    name = (store.path(project.id) / 'project.json').relative_to(app.root).as_posix()
    path = folder / name if flat else folder / 'sidecars' / name
    value = read_json(path)
    holder = value if scope == 'master' else value['roles' if scope == 'role' else 'scenes'][0]
    if damage == 'raw-filter':
        holder['effects'] = ['amovie=private.wav']
    else:
        holder['effects'][0]['enabled'] = 1
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    path.write_bytes(raw)
    rows = manifest['sidecars'] if flat else manifest['sidecars']['files']
    key = 'filename' if flat else 'path'
    row, = [item for item in rows if item[key] == name]
    row.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    encoded = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    (folder / 'manifest.json').write_bytes(encoded)
    destination = tmp_path / 'must-not-exist'
    with pytest.raises((ValueError, RestoreError)):
        restore_backup(folder, destination, sha256(encoded).hexdigest())
    assert not destination.exists() and path.read_bytes() == raw
    assert (folder / 'manifest.json').read_bytes() == encoded


@pytest.mark.parametrize('flat', [False, True], ids=['native5', 'foreign-flat4'])
def test_completed_primary_job_is_restored_as_inert_metadata_in_both_envelopes(
        tmp_path, monkeypatch, flat):
    from carr_library.audio.projects import atomic_json
    app, workspace, store, project, job_id, original = prepared(tmp_path)
    job_path = store.path(project.id) / 'jobs' / (job_id + '.json')
    value = read_json(job_path)
    old_output = value['output_dir']
    value.update(state='complete',
        outputs=[{'file': 'must-not-open.wav', 'sha256': 'a' * 64, 'size_bytes': 123}],
        result={'historical_claim': 'No renewed execution or output authority'})
    atomic_json(job_path, value)
    original_job = job_path.read_bytes()
    original_project = (store.path(project.id) / 'project.json').read_bytes()
    folder = tmp_path / 'backup'
    manifest, checksum = snapshot(app, folder, flat)
    assert manifest['version'] == (4 if flat else 5)
    actual_open, actual_stat = Path.open, Path.stat
    external = Path(old_output)
    def forbidden_open(path, *args, **kwargs):
        if path == external or path.is_relative_to(external):
            pytest.fail('Passive primary restoration opened a historical output.')
        return actual_open(path, *args, **kwargs)
    def forbidden_stat(path, *args, **kwargs):
        if path == external or path.is_relative_to(external):
            pytest.fail('Passive primary restoration inspected a historical output.')
        return actual_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', forbidden_open)
    monkeypatch.setattr(Path, 'stat', forbidden_stat)
    monkeypatch.setattr(VoiceRegistry, 'for_library',
                        classmethod(lambda *a, **k: pytest.fail('Passive restore discovered a voice.')))
    destination = tmp_path / 'restored'
    restore_backup(folder, destination, checksum)
    recovered = ProductionDirector(destination)
    state = recovered.status(project.id, job_id)
    assert state['restore_requires_new_output'] is True
    assert state['state'] == 'paused' and state['result'] is None and state['outputs'] == []
    assert state['output_dir'] == '' and state['restored_from_output_dir'] == old_output
    assert state['project'] == json.loads(original_project)
    assert ProjectStore(destination).load(project.id).fingerprint == project.fingerprint
    assert job_path.read_bytes() == original_job and KeybindingStore(destination).load() == KEYS
    with pytest.raises(ValueError, match='restored from backup'):
        recovered.render(project.id, job_id=job_id)


def test_flat_primary_metadata_requires_its_declared_current_project(tmp_path):
    app, _, store, project, _, _ = prepared(tmp_path)
    folder = tmp_path / 'backup'
    manifest, _ = snapshot(app, folder, True)
    name = (store.path(project.id) / 'project.json').relative_to(app.root).as_posix()
    original = (folder / name).read_bytes()
    (folder / name).unlink()
    manifest['sidecars'] = [row for row in manifest['sidecars'] if row['filename'] != name]
    checksum = FLAT['_seal'](folder, manifest)
    destination = tmp_path / 'must-not-exist'
    with pytest.raises(RestoreError, match='current project'):
        restore_backup(folder, destination, checksum)
    assert not destination.exists()
    assert (store.path(project.id) / 'project.json').read_bytes() == original


@pytest.mark.parametrize('relative', [
    'unrecognized.json', 'cache/' + 'a' * 64 + '.json', 'assets/' + 'a' * 64 + '.wav'])
def test_flat_primary_metadata_bridge_cannot_admit_unknown_or_binary_names(tmp_path, relative):
    app, _, _, project, _, _ = prepared(tmp_path)
    folder = tmp_path / 'backup'
    manifest, _ = snapshot(app, folder, True)
    name = 'studio/projects/' + project.id + '/' + relative
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = b'{"untrusted":"not an admitted primary metadata path"}\n'
    path.write_bytes(raw)
    manifest['sidecars'].append({'filename': name, 'size_bytes': len(raw),
                                'sha256': sha256(raw).hexdigest()})
    manifest['sidecars'].sort(key=lambda row: row['filename'])
    checksum = FLAT['_seal'](folder, manifest)
    destination = tmp_path / 'must-not-exist'
    with pytest.raises(RestoreError, match='No validated backup adapter'):
        restore_backup(folder, destination, checksum)
    assert not destination.exists() and path.read_bytes() == raw


@pytest.mark.parametrize('flat', [False, True], ids=['native5', 'foreign-flat4'])
@pytest.mark.parametrize('damage', ['project-id', 'project-fingerprint'])
def test_rehashed_primary_job_cannot_change_its_typed_project_binding(tmp_path, flat, damage):
    app, _, store, project, job_id, _ = prepared(tmp_path)
    folder = tmp_path / 'backup'
    manifest, _ = snapshot(app, folder, flat)
    name = 'studio/projects/' + project.id + '/jobs/' + job_id + '.json'
    path = folder / name if flat else folder / 'sidecars' / name
    value = read_json(path)
    value['project_id' if damage == 'project-id' else 'project_fingerprint'] = (
        'other-project' if damage == 'project-id' else '0' * 64)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')) + '\n').encode('utf-8')
    path.write_bytes(raw)
    rows, key = (manifest['sidecars'], 'filename') if flat else (manifest['sidecars']['files'], 'path')
    row, = [row for row in rows if row[key] == name]
    row.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    checksum = FLAT['_seal'](folder, manifest)
    destination = tmp_path / 'must-not-exist'
    with pytest.raises((ValueError, RestoreError), match='studio job'):
        restore_backup(folder, destination, checksum)
    assert not destination.exists() and path.read_bytes() == raw
