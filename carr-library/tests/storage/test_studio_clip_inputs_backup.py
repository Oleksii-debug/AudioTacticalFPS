"""Manifest-bound Scene inputs, passive presets and exact restore revisions.

These are real database/state backup roundtrips. No decoder, voice, capture
device or external audio path is needed to validate copied retained bytes.
"""
from dataclasses import replace
from hashlib import sha256
import io
import json
from pathlib import Path
import shutil
import subprocess
from threading import Event
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library import backup_compat, runtime_settings
from carr_library.audio import Effect
from carr_library.audio.effect_presets import EffectPresetStore
from carr_library.durable_state import atomic_json, encode, read_json
from carr_library.ingest.restore import RestoreError
from carr_library.keybindings import encode_preferences
from carr_library.library_backup import create_backup, restore_backup, INCOMPLETE
from carr_library.storage.revisions import CURRENT_SCHEMA_VERSION
from carr_library.studio import backup_inputs
from carr_library.studio.project import Cue, Project, ProjectStore, Scene
from carr_library.studio.render import ProductionService


PROJECT_ID = '1' * 32
JOB_ID = '2' * 32


def _raw(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def _seal(folder, manifest):
    raw = _raw(manifest)
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def _pcm():
    data = io.BytesIO()
    with wave.open(data, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b'\x01\x00' * 8000)
    return data.getvalue()


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Retained Scene inputs')
    app.add_text(workspace, 'Original evidence', 'Immutable source. Незмінне джерело. 😀',
                 edition_id='original')
    # This is the existing primary sidecar producer, forcing native format 5.
    (app.root / 'keybindings.json').write_bytes(encode_preferences({}))
    studio = app.root / 'state' / 'studio'
    inputs = studio / 'inputs'
    inputs.mkdir(parents=True)
    raw = _pcm()
    selected = inputs / (sha256(raw).hexdigest() + '.audio')
    selected.write_bytes(raw)
    cue = Cue('retained-clip', str(selected), start=.125, role='microphone',
              trim_start=.05, duration=.2, fade_in=.025, fade_out=.03,
              title='Original recording', effects=(Effect('gain', gain_db=-2),))
    scene = Scene('scene', 'Retained scene', (), cues=(cue,),
                  minimum_duration=.5, effects=(Effect('gain', gain_db=-1),))
    first = Project(PROJECT_ID, 'Original performance', (), (scene,),
                    original_script='Original evidence must stay unchanged.',
                    schema=3, effects=(Effect('gain', gain_db=-3),))
    store = ProjectStore(studio / 'projects')
    store.save(first)
    project = replace(first, title='Reviewed performance', revision=2)
    store.save(project, expected_revision=1, expected_fingerprint=first.fingerprint)
    preset = EffectPresetStore(app.root).save('Retained settings',
        (Effect('gain', gain_db=-5), Effect('highpass', frequency=80, enabled=False)))
    output = tmp_path / 'explicit-output'
    output.mkdir()
    def no_runtime(*_args, **_kwargs):
        pytest.fail('A passive backup activated the production runtime.')
    service = ProductionService(studio, voices=SimpleNamespace(voices=lambda: ()),
                                runtime_factory=no_runtime)
    assert service.submit(project, output, operation_id=JOB_ID) == JOB_ID
    # The ordinary exported project is another historical receipt. It must not
    # acquire a new fingerprint just because an editable project is relocated.
    atomic_json(studio / 'jobs' / JOB_ID / 'project.json', project.to_dict())
    job = service.get(JOB_ID)
    service.close()
    return app, workspace, project, first, selected, raw, preset, job


def _backup(app, target, shape):
    checksum = create_backup(app, target)
    manifest = json.loads((target / 'manifest.json').read_bytes())
    assert manifest['version'] == 5
    assert manifest['schema_version'] == CURRENT_SCHEMA_VERSION == 6
    if shape == 'native':
        return manifest, checksum
    entries = [{**row, 'filename': 'state/' + row['filename']} for row in manifest['state']]
    for row in manifest['sidecars']['files']:
        name = row['path']
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target / 'sidecars' / name, destination)
        entries.append({'filename': name, 'size_bytes': row['size_bytes'], 'sha256': row['sha256']})
    shutil.rmtree(target / 'sidecars')
    flat = {key: manifest[key] for key in
            ('kind', 'schema_version', 'schema_fingerprint', 'database', 'assets')}
    flat.update(version=4, sidecars=sorted(entries, key=lambda row: row['filename']),
                generated_audio_included=False)
    if 'pending' in manifest:
        flat['pending'] = manifest['pending']
    return flat, _seal(target, flat)


def _change(folder, manifest, name, *, raw=None, remove=False, renamed=None):
    collection = 'sidecars' if type(manifest.get('sidecars')) is list else 'state'
    relative = name if collection == 'sidecars' else name.removeprefix('state/')
    matches = [row for row in manifest[collection] if row['filename'] == relative]
    assert len(matches) == 1
    row, = matches
    path = folder / name
    if remove:
        path.unlink()
        manifest[collection].remove(row)
    else:
        if renamed is not None:
            target = folder / renamed
            target.parent.mkdir(parents=True, exist_ok=True)
            path.rename(target)
            path = target
            row['filename'] = renamed if collection == 'sidecars' else renamed.removeprefix('state/')
        if raw is not None:
            path.write_bytes(raw)
        captured = path.read_bytes()
        row.update(size_bytes=len(captured), sha256=sha256(captured).hexdigest())
    return _seal(folder, manifest)


def _state_rows(manifest):
    if type(manifest.get('sidecars')) is list:
        return [{**row, 'filename': row['filename'].removeprefix('state/')}
                for row in manifest['sidecars'] if row['filename'].startswith('state/')]
    return manifest['state']


def _no_activation(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail('A passive input backup started a process or local engine.')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(runtime_settings, 'audio_runtime', forbidden)
    monkeypatch.setattr(runtime_settings, 'voice_registry', forbidden)


@pytest.mark.parametrize('shape', ('native', 'flat'))
def test_roundtrip_projects_presets_and_jobs_keep_exact_history_with_one_projection_revision(
        library, tmp_path, monkeypatch, shape):
    app, workspace, project, first, selected, raw, preset, job = library
    studio = app.root / 'state/studio'
    history_name = f'projects/{project.id}/revisions/{first.revision:08d}-{first.fingerprint}.json'
    old_history = (studio / history_name).read_bytes()
    current_raw = (studio / 'projects' / project.id / 'project.json').read_bytes()
    old_job_project = (studio / 'jobs' / JOB_ID / 'project.json').read_bytes()
    preset_name = f'effect-presets/{preset.id}.json'
    preset_raw = (studio / preset_name).read_bytes()
    _no_activation(monkeypatch)
    backup = tmp_path / 'backup'
    manifest, checksum = _backup(app, backup, shape)
    receipt_raw = (backup / 'state' / backup_inputs.SNAPSHOT).read_bytes()
    receipt = json.loads(receipt_raw)
    assert receipt['source_state_root'] == str(app.root / 'state')
    row = next(item for item in receipt['projects'] if item['kind'] == 'current')
    entry = next(item for item in _state_rows(manifest)
                 if item['filename'] == 'studio/inputs/' + selected.name)
    assert row['inputs'] == {str(selected): entry} and row['unretained'] == []
    assert selected.read_bytes() == raw
    shutil.rmtree(app.root)
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    restored_studio = root / 'state/studio'
    store = ProjectStore(restored_studio / 'projects')
    current = store.load(project.id)
    expected_cue = replace(project.scenes[0].cues[0],
                           path=str(restored_studio / 'inputs' / selected.name))
    expected = replace(project, revision=3,
                       scenes=(replace(project.scenes[0], cues=(expected_cue,)),))
    assert current == expected and current.fingerprint != project.fingerprint
    assert Path(current.scenes[0].cues[0].path).read_bytes() == raw
    assert (restored_studio / history_name).read_bytes() == old_history
    preserved_name = f'projects/{project.id}/revisions/00000002-{project.fingerprint}.json'
    projected_name = f'projects/{project.id}/revisions/00000003-{expected.fingerprint}.json'
    assert (restored_studio / preserved_name).read_bytes() == current_raw
    assert (restored_studio / projected_name).read_bytes() == encode(expected.to_dict())
    assert (restored_studio / 'jobs' / JOB_ID / 'project.json').read_bytes() == old_job_project
    assert read_json(restored_studio / 'jobs' / JOB_ID / 'job.json') == {
        **job, 'restore_requires_new_output': True}
    assert (restored_studio / preset_name).read_bytes() == preset_raw
    assert EffectPresetStore(root).get(preset.id) == preset
    assert not (root / 'state' / backup_inputs.SNAPSHOT).exists()
    assert (backup / 'state' / backup_inputs.SNAPSHOT).read_bytes() == receipt_raw
    recovery = read_json(root / 'state' / backup_inputs.RECOVERY)
    assert recovery['projects'][0]['unresolved'] == []
    assert recovery['projects'][0]['retained_inputs'] == ['studio/inputs/' + selected.name]
    assert '1 retained original(s); 0 unavailable clip reference(s)' in backup_inputs.recovery_notice(
        restored_studio, current)
    backup_inputs.require_reviewed_input(restored_studio, current.scenes[0].cues[0])
    with pytest.raises(ValueError, match='changed'):
        store.save(replace(current, revision=4, title='Stale edit'),
                   expected_revision=2, expected_fingerprint=project.fingerprint)
    changed = replace(current, revision=4, title='Checked edit after restore')
    assert store.save(changed, expected_revision=3, expected_fingerprint=current.fingerprint) == changed
    assert (restored_studio / projected_name).read_bytes() == encode(expected.to_dict())
    assert LibraryApp(root).edition_metadata(workspace, 'original').title == 'Original evidence'
    assert not (root / INCOMPLETE).exists()


@pytest.mark.parametrize('shape', ('native', 'flat'))
def test_rebackup_keeps_original_receipts_and_never_publishes_private_stage_paths(
        library, tmp_path, shape):
    app, _, project, _, selected, raw, _, job = library
    _, checksum = _backup(app, tmp_path / 'first-backup', shape)
    root = restore_backup(tmp_path / 'first-backup', tmp_path / 'first-restore', checksum)
    first_project = ProjectStore(root / 'state/studio/projects').load(project.id)
    first_history = {path.name: path.read_bytes()
                     for path in (root / 'state/studio/projects' / project.id / 'revisions').iterdir()}
    manifest, checksum = _backup(LibraryApp(root), tmp_path / 'second-backup', shape)
    second = restore_backup(tmp_path / 'second-backup', tmp_path / 'second-restore', checksum)
    current = ProjectStore(second / 'state/studio/projects').load(project.id)
    assert current.revision == first_project.revision + 1
    assert current.scenes[0].cues[0].path == str(second / 'state/studio/inputs' / selected.name)
    assert Path(current.scenes[0].cues[0].path).read_bytes() == raw
    assert read_json(second / 'state/studio/jobs' / JOB_ID / 'job.json') == {
        **job, 'restore_requires_new_output': True}
    for name, contents in first_history.items():
        assert (second / 'state/studio/projects' / project.id / 'revisions' / name).read_bytes() == contents
    assert not (second / 'state' / backup_inputs.SNAPSHOT).exists()
    assert (second / 'state/studio/projects' / project.id / 'project.json').read_bytes().find(b'.restore-') < 0
    assert manifest['schema_version'] == 6


@pytest.mark.parametrize('shape', ('native', 'flat'))
@pytest.mark.parametrize('lookalike', (False, True), ids=('external', 'other-root-owned-suffix'))
def test_external_cue_is_never_read_or_relocated_and_requires_explicit_reselection(
        library, tmp_path, monkeypatch, shape, lookalike):
    app, _, project, _, _, raw, _, job = library
    external = (tmp_path / 'another-library/state/studio/inputs' /
                (sha256(raw).hexdigest() + '.audio')) if lookalike else tmp_path / 'private-original.wav'
    external.parent.mkdir(parents=True, exist_ok=True)
    external.write_bytes(raw)
    cue = replace(project.scenes[0].cues[0], path=str(external))
    edited = replace(project, revision=3, scenes=(replace(project.scenes[0], cues=(cue,)),))
    ProjectStore(app.root / 'state/studio/projects').save(
        edited, expected_revision=project.revision, expected_fingerprint=project.fingerprint)
    original_open = Path.open
    forbidden_reads = []
    def guard(path, *args, **kwargs):
        if path == external:
            forbidden_reads.append(str(path))
            pytest.fail('An external Cue path became backup read authority.')
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', guard)
    _no_activation(monkeypatch)
    _, checksum = _backup(app, tmp_path / 'backup', shape)
    root = restore_backup(tmp_path / 'backup', tmp_path / 'restored', checksum)
    current = ProjectStore(root / 'state/studio/projects').load(project.id)
    assert current == edited and current.fingerprint == edited.fingerprint
    assert not forbidden_reads
    with original_open(external, 'rb') as stream:
        assert stream.read() == raw
    assert '1 unavailable clip reference(s)' in backup_inputs.recovery_notice(root / 'state/studio', current)
    with pytest.raises(ValueError, match='no retained input binding'):
        backup_inputs.require_reviewed_input(root / 'state/studio', current.scenes[0].cues[0])
    assert read_json(root / 'state/studio/jobs' / JOB_ID / 'job.json') == {
        **job, 'restore_requires_new_output': True}


@pytest.mark.parametrize('shape', ('native', 'flat'))
def test_legacy_missing_binding_stays_unreviewed_after_same_root_restore_and_rebackup(
        library, tmp_path, shape):
    app, _, project, _, selected, raw, _, _ = library
    folder = tmp_path / 'legacy'
    manifest, _ = _backup(app, folder, shape)
    checksum = _change(folder, manifest, 'state/' + backup_inputs.SNAPSHOT, remove=True)
    original_root = app.root
    shutil.rmtree(original_root)
    restored = restore_backup(folder, original_root, checksum)
    current = ProjectStore(restored / 'state/studio/projects').load(project.id)
    assert current == project and Path(current.scenes[0].cues[0].path).read_bytes() == raw
    with pytest.raises(ValueError, match='no retained input binding'):
        backup_inputs.require_reviewed_input(restored / 'state/studio', current.scenes[0].cues[0])
    # The now-existing path is still not new explicit source selection.
    next_folder = tmp_path / 'rebackup'
    _, next_checksum = _backup(LibraryApp(restored), next_folder, shape)
    receipt = read_json(next_folder / 'state' / backup_inputs.SNAPSHOT)
    active = next(row for row in receipt['projects'] if row['kind'] == 'current')
    assert active['inputs'] == {} and active['unretained'] == [str(selected)]
    second = restore_backup(next_folder, tmp_path / 'second-restore', next_checksum)
    again = ProjectStore(second / 'state/studio/projects').load(project.id)
    assert again == project  # No path or revision grant can emerge from backup.
    with pytest.raises(ValueError, match='no retained input binding'):
        backup_inputs.require_reviewed_input(second / 'state/studio', again.scenes[0].cues[0])
    assert '1 unavailable clip reference(s)' in backup_inputs.recovery_notice(second / 'state/studio', again)


def test_missing_managed_input_fails_before_backup_publication(library, tmp_path):
    app, _, _, _, selected, _, _, _ = library
    selected.unlink()
    with pytest.raises(ValueError, match='managed Studio input is missing'):
        create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


@pytest.mark.parametrize('shape', ('native', 'flat'))
@pytest.mark.parametrize('change', (
    'input-bytes', 'missing-input', 'changed-project', 'source-root', 'binding-path',
    'binding-size-bool', 'receipt-extra-field', 'preset-id', 'preset-extra-field',
), ids=str)
def test_resealed_input_project_binding_or_preset_mutation_fails_before_publication(
        library, tmp_path, shape, change):
    app, _, project, _, selected, _, preset, _ = library
    folder = tmp_path / 'backup'
    manifest, _ = _backup(app, folder, shape)
    name = 'state/' + backup_inputs.SNAPSHOT
    value = read_json(folder / name)
    row = next(item for item in value['projects'] if item['kind'] == 'current')
    remove = False
    if change == 'input-bytes':
        name = 'state/studio/inputs/' + selected.name
        raw = bytearray((folder / name).read_bytes())
        raw[-1] ^= 1
        raw = bytes(raw)
    elif change == 'missing-input':
        name, raw, remove = 'state/studio/inputs/' + selected.name, None, True
    elif change == 'changed-project':
        name = f'state/studio/projects/{project.id}/project.json'
        changed = read_json(folder / name)
        changed['title'] = 'Different saved project with a stale binding receipt'
        raw = _raw(changed)
    elif change == 'source-root':
        value['source_state_root'] = str(tmp_path / 'different-source-state')
        raw = _raw(value)
    elif change == 'binding-path':
        row['inputs'][str(selected)]['filename'] = '../outside.audio'
        raw = _raw(value)
    elif change == 'binding-size-bool':
        row['inputs'][str(selected)]['size_bytes'] = True
        raw = _raw(value)
    elif change == 'receipt-extra-field':
        value['selected_external_sources'] = [str(selected)]
        raw = _raw(value)
    else:
        name = f'state/studio/effect-presets/{preset.id}.json'
        value = read_json(folder / name)
        if change == 'preset-id':
            value['id'] = 'f' * 32 if preset.id != 'f' * 32 else 'e' * 32
        else:
            value['executable'] = str(tmp_path / 'must-never-run')
        raw = _raw(value)
    checksum = _change(folder, manifest, name, raw=raw, remove=remove)
    target = tmp_path / 'restored'
    with pytest.raises((ValueError, RestoreError, json.JSONDecodeError)):
        restore_backup(folder, target, checksum)
    assert not target.exists()


@pytest.mark.parametrize('shape', ('native', 'flat'))
def test_retained_originals_stream_and_unreferenced_bytes_remain_passive(library, tmp_path, monkeypatch, shape):
    app, _, _, _, selected, raw, _, _ = library
    orphan_raw = b'An unreferenced retained byte stream is not a decoder request.'
    orphan = app.root / 'state/studio/inputs' / (sha256(orphan_raw).hexdigest() + '.audio')
    orphan.write_bytes(orphan_raw)
    folder = tmp_path / 'backup'
    _, checksum = _backup(app, folder, shape)
    original = backup_compat.read_regular
    calls = []
    def guard(path, maximum, *, allow_empty=False):
        assert Path(path).suffix != '.audio', 'Binary originals reached the JSON materializer.'
        calls.append((Path(path).name, maximum, allow_empty))
        return original(path, maximum, allow_empty=allow_empty)
    monkeypatch.setattr(backup_compat, 'read_regular', guard)
    _no_activation(monkeypatch)
    root = restore_backup(folder, tmp_path / 'restored', checksum)
    assert (root / 'state/studio/inputs' / selected.name).read_bytes() == raw
    assert (root / 'state/studio/inputs' / orphan.name).read_bytes() == orphan_raw
    if shape == 'flat':
        assert calls and all(type(maximum) is int and maximum > 0 for _, maximum, _ in calls)


@pytest.mark.parametrize('shape', ('native', 'flat'))
def test_cancel_during_streamed_input_verification_leaves_no_restored_destination(
        library, tmp_path, monkeypatch, shape):
    app, *_ = library
    folder = tmp_path / 'backup'
    _, checksum = _backup(app, folder, shape)
    original, stopped, checks = backup_inputs._verify_input, Event(), []
    def cancel_after_first_block(root, entry, cancelled):
        def after_block():
            checks.append(entry['filename'])
            if len(checks) == 2:
                stopped.set()  # _verify_input has read its first actual block.
            return cancelled()
        return original(root, entry, after_block)
    monkeypatch.setattr(backup_inputs, '_verify_input', cancel_after_first_block)
    target = tmp_path / 'restored'
    with pytest.raises(InterruptedError, match='cancelled'):
        restore_backup(folder, target, checksum, cancelled=stopped.is_set)
    assert stopped.is_set() and len(checks) == 2 and not target.exists()


def test_primary_audio_project_is_separate_even_when_stable_id_matches_scene_project(library, tmp_path):
    from carr_library.audio.projects import ProjectStore as PrimaryStore, parse_script
    app, _, project, _, _, _, _, _ = library
    primary = parse_script('Narrator: Different primary project.', ['explicit-local-voice'],
                           project_id=project.id)
    PrimaryStore(app.root).create(primary)
    primary_path = Path('studio/projects') / project.id / 'project.json'
    exact = (app.root / primary_path).read_bytes()
    _, checksum = _backup(app, tmp_path / 'backup', 'native')
    root = restore_backup(tmp_path / 'backup', tmp_path / 'restored', checksum)
    assert (root / primary_path).read_bytes() == exact
    assert PrimaryStore(root).load(project.id) == primary
    scene = ProjectStore(root / 'state/studio/projects').load(project.id)
    assert scene.revision == 3 and scene.original_script == project.original_script
    assert primary.script != scene.original_script


@pytest.mark.parametrize('relative', (
    'studio/Inputs/' + 'a' * 64 + '.audio',
    'studio/inputs/' + 'A' * 64 + '.audio',
    'studio/inputs/unknown.wav',
    'studio/effect-presets/' + 'A' * 32 + '.JSON',
    'Studio/input-recovery.json',
), ids=('directory-alias', 'digest-alias', 'unknown-input', 'preset-alias', 'notice-alias'))
def test_reserved_typed_names_never_fall_through_opaque_state(tmp_path, relative):
    entries = [{'filename': relative, 'size_bytes': 2, 'sha256': 'a' * 64}]
    with pytest.raises(ValueError, match='canonical|pathname'):
        backup_inputs.validate_state(tmp_path, entries)


def test_effect_preset_collection_and_metadata_size_keep_existing_bounded_admission(tmp_path):
    entries = [{'filename': f'studio/effect-presets/{number:032x}.json',
                'size_bytes': 2, 'sha256': 'a' * 64} for number in range(257)]
    with pytest.raises(ValueError, match='Too many'):
        backup_inputs.validate_state(tmp_path, entries)
    with pytest.raises(ValueError, match='budget'):
        backup_inputs.validate_state(tmp_path, [{'filename': 'studio/effect-presets/' + 'a' * 32 + '.json',
            'size_bytes': backup_inputs.MAX_PRESET_BYTES + 1, 'sha256': 'a' * 64}])


@pytest.mark.parametrize('shape', ('native', 'flat'))
def test_historical_revision_filename_collision_is_not_rewritten(library, tmp_path, shape):
    app, _, project, first, _, _, _, _ = library
    folder = tmp_path / 'backup'
    manifest, _ = _backup(app, folder, shape)
    name = f'state/studio/projects/{project.id}/revisions/{first.revision:08d}-{first.fingerprint}.json'
    corrupted = read_json(folder / name)
    corrupted['title'] = 'A conflicting history under the old immutable fingerprint'
    checksum = _change(folder, manifest, name, raw=_raw(corrupted))
    with pytest.raises((ValueError, RestoreError), match='identity|identities|revision|fingerprint'):
        restore_backup(folder, tmp_path / 'restored', checksum)
    assert not (tmp_path / 'restored').exists()
    assert (folder / name).read_bytes() == _raw(corrupted)


def test_foreign_flat_preserves_its_existing_aggregate_bound_for_retained_inputs():
    manifest = {'version': 4, 'kind': 'carr-library', 'schema_version': 6,
        'schema_fingerprint': 'a' * 64, 'database': {}, 'assets': [],
        'sidecars': [{'filename': 'state/studio/inputs/' + 'b' * 64 + '.audio',
            'size_bytes': backup_compat.MAX_BYTES + 1, 'sha256': 'b' * 64}],
        'generated_audio_included': False}
    with pytest.raises(RestoreError, match='manifest byte limit'):
        backup_compat.normalize_manifest(manifest)


def test_projection_cannot_expand_the_foreign_flat_snapshot_budget(library, tmp_path, monkeypatch):
    app, _, project, *_ = library
    # A substantial current text makes the additional immutable projection
    # receipts observably larger than the consumed compact binding receipt.
    larger = replace(project, revision=3, original_script='Retained original text.\n' * 4000)
    ProjectStore(app.root / 'state/studio/projects').save(
        larger, expected_revision=project.revision, expected_fingerprint=project.fingerprint)
    folder = tmp_path / 'backup'
    manifest, checksum = _backup(app, folder, 'flat')
    before = sum(row['size_bytes'] for row in manifest['sidecars'])
    # Initial sidecars fit exactly; checked current and projection history
    # plus the passive notice must still fit after restoration.
    monkeypatch.setattr(backup_compat, 'MAX_BYTES', before)
    with pytest.raises(RestoreError, match='projection.*budget'):
        restore_backup(folder, tmp_path / 'restored', checksum)
    assert not (tmp_path / 'restored').exists()
