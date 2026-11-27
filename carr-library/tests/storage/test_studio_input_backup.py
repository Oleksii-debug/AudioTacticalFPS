"""Portable typed Studio recovery through the canonical v5 library backup.

The roundtrip renders real PCM with FFmpeg, removes the complete original
library and selected external inputs, then renders again after restoration.
"""
from dataclasses import replace
from hashlib import sha256
import math
import os
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
from carr_library.state_backup import snapshot_state
from carr_library.studio.backup_inputs import RECEIPT
from carr_library.studio.project import Cue, Project, ProjectStore, Scene
from carr_library.studio.render import ProductionService


def tone(path, *, frequency=220, seconds=.5):
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = [round(10000 * math.sin(2 * math.pi * frequency * i / 24000))
               for i in range(round(seconds * 24000))]
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(struct.pack('<' + 'h' * len(samples), *samples))
    return path


def project_for(path, *, identifier='retained-project'):
    return Project(identifier, 'Retained local effects', (),
        (Scene('scene-1', 'Local cue', (),
               cues=(Cue('cue-1', str(path), start=.25, gain_db=0, pan=1),),
               minimum_duration=1.25),),
        original_script='Purpose-written local effects project; original words stay exact.')


def production(root, *, inputs=()):
    return ProductionService(root, voices=VoiceRegistry([]), runtime_factory=AudioRuntime,
                             input_roots=inputs)


def manifest_rehash(backup, name, value):
    """Update legitimate outer hashes to exercise the typed internal binding."""
    raw = encode(value)
    (backup / 'state' / name).write_bytes(raw)
    manifest = read_json(backup / 'manifest.json')
    entry = next(item for item in manifest['state'] if item['filename'] == name)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    raw = encode(manifest)
    (backup / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def test_real_render_backup_delete_original_state_restore_and_render_again(tmp_path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('Actual Studio recovery requires installed FFmpeg and ffprobe.')
    app = LibraryApp(tmp_path / 'Початкова бібліотека')
    workspace = app.create_workspace('Retained original documents')
    book = app.add_text(workspace, 'Source', 'A preserved original quotation.')
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    ReadingState(app.root).remember(ReadingDesk(app, workspace, book['edition']).page(1).location)
    selected = tmp_path / 'Вибрані зовнішні звуки'
    first = tone(selected / 'перший.wav')
    second = tone(selected / 'другий.wav', frequency=440)
    original_inputs = {str(path): path.read_bytes() for path in (first, second)}
    service = production(app.root / 'state/studio', inputs=(selected,))
    original_output = tmp_path / 'Початкові результати'
    original_output.mkdir()
    first_project = project_for(first)
    first_job = service.submit(first_project, original_output, stems=False,
                               operation_id='completed-first-revision')
    complete = service.run(first_job)
    expected_master = Path(complete['destination'], 'master.wav').read_bytes()
    current = replace(first_project, revision=2,
        scenes=(replace(first_project.scenes[0],
                        cues=(replace(first_project.scenes[0].cues[0], path=str(second)),)),))
    service.projects.save(current, expected_revision=1)
    pending_job = service.submit(current, original_output, stems=False,
                                 operation_id='pending-second-revision')
    draft = {'schema': 1, 'text': 'Unsaved editable draft stays exact.',
             'title': current.title, 'voice_ids': [], 'project_id': current.id}
    atomic_json(service.root / 'draft.json', draft)
    original_project_bytes = {str(path.relative_to(app.root / 'state')): path.read_bytes()
                              for path in service.root.rglob('*.json')
                              if 'cache' not in path.parts}

    backup = tmp_path / 'verified-backup'
    digest = create_backup(app, backup)
    manifest = read_json(backup / 'manifest.json')
    receipt = read_json(backup / 'state' / RECEIPT)
    assert manifest['version'] == 5 and manifest['generated_audio_included'] is False
    assert set(receipt['inputs']) == set(original_inputs)
    assert len(receipt['projects']) == 5  # current, revision, two jobs, completed export
    assert not any('/cache/' in item['filename'] for item in manifest['state'])
    for original, entry in receipt['inputs'].items():
        assert entry in manifest['state']
        assert (backup / 'state' / entry['filename']).read_bytes() == original_inputs[original]
        assert entry['sha256'] == sha256(original_inputs[original]).hexdigest()
        assert Path(original).read_bytes() == original_inputs[original]
    for name, raw in original_project_bytes.items():
        assert (backup / 'state' / name).read_bytes() == raw
        assert (app.root / 'state' / name).read_bytes() == raw

    shutil.rmtree(selected)
    shutil.rmtree(original_output)
    shutil.rmtree(app.root)
    destination = restore_backup(backup, tmp_path / 'Відновлена бібліотека', digest)
    restored = LibraryApp(destination)
    recovered = production(destination / 'state/studio')
    assert restored.search(workspace, 'quotation')[0].quote == 'quotation'
    assert read_json(recovered.root / 'draft.json') == draft
    assert not (destination / 'state' / RECEIPT).exists()
    rebound = recovered.projects.load(current.id)
    assert rebound.revision == current.revision
    assert rebound.original_script == current.original_script
    assert rebound.fingerprint != current.fingerprint
    expected = sha256(original_inputs[str(second)]).hexdigest()
    assert Path(rebound.scenes[0].cues[0].path) == recovered.inputs / (expected + '.audio')
    histories = list((recovered.projects.directory(current.id) / 'revisions').glob('*.json'))
    assert len(histories) == 1
    historical = Project.from_dict(read_json(histories[0]))
    assert histories[0].name == f'{historical.revision:08d}-{historical.fingerprint}.json'
    assert Path(historical.scenes[0].cues[0].path).read_bytes() == original_inputs[str(first)]
    for job_id in (first_job, pending_job):
        job = recovered.get(job_id)
        assert job['status'] == 'waiting-input' and job['destination'] == ''
        assert job['artifacts'] == [] and job['nodes'] == {} and 'result' not in job
        assert job['recovery']['requires_output_directory'] is True
        with pytest.raises(ValueError, match='new output directory'):
            recovered.run(job_id)
    assert recovered.get(first_job)['recovery']['previous']['status'] == 'complete'
    assert recovered.get(pending_job)['recovery']['previous']['status'] == 'pending'
    assert not original_output.exists() and not selected.exists()
    with pytest.raises(ValueError, match='outside private'):
        recovered.relocate_restored_job(first_job, recovered.root)

    new_outputs = tmp_path / 'Нові результати'
    new_outputs.mkdir()
    relocated = recovered.relocate_restored_job(first_job, new_outputs)
    assert Path(relocated['destination']).parent == new_outputs
    assert not Path(relocated['destination']).exists()
    with pytest.raises(ValueError, match='Only a restored job'):
        recovered.relocate_restored_job(first_job, new_outputs)
    replayed = recovered.run(first_job)
    assert replayed['status'] == 'complete'
    replayed_master = Path(replayed['destination'], 'master.wav')
    assert replayed_master.read_bytes() == expected_master
    with wave.open(str(replayed_master), 'rb') as audio:
        assert (audio.getnframes(), audio.getframerate(), audio.getnchannels()) == (30000, 24000, 2)
        samples = struct.unpack('<' + 'h' * 60000, audio.readframes(30000))
        assert max(abs(value) for value in samples[::2]) == 0
        assert max(abs(value) for value in samples[1::2]) > 9000
    assert not original_output.exists() and not selected.exists()
    assert all(path.stat().st_nlink == 1 for path in recovered.inputs.iterdir())

    # A restored library remains a valid producer; retained bytes are deduplicated.
    second_backup = tmp_path / 'backup-after-recovery'
    second_digest = create_backup(restored, second_backup)
    second_receipt = read_json(second_backup / 'state' / RECEIPT)
    assert len({item['filename'] for item in second_receipt['inputs'].values()}) == 2
    second_restored = restore_backup(second_backup, tmp_path / 'second-restore', second_digest)
    second_service = production(second_restored / 'state/studio')
    second_current = second_service.projects.load(current.id)
    assert Path(second_current.scenes[0].cues[0].path).parent == second_service.inputs
    assert second_service.get(first_job)['status'] == 'waiting-input'


@pytest.fixture
def backed_up(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    source = tone(tmp_path / 'selected' / 'cue.wav')
    store = ProjectStore(app.root / 'state/studio/projects')
    project = project_for(source)
    store.save(project)
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    production(app.root / 'state/studio', inputs=(source.parent,)).submit(
        project, outputs, operation_id='restorable-job')
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    return app, source, project, backup, digest


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'binding', 'omitted-project', 'wrong-project'])
def test_invalid_retained_inputs_or_project_bindings_never_publish(backed_up, tmp_path, damage):
    _app, _source, _project, backup, digest = backed_up
    receipt = read_json(backup / 'state' / RECEIPT)
    entry = next(iter(receipt['inputs'].values()))
    if damage == 'missing':
        (backup / 'state' / entry['filename']).unlink()
    elif damage == 'corrupt':
        asset = backup / 'state' / entry['filename']
        raw = bytearray(asset.read_bytes())
        raw[-1] ^= 1
        asset.write_bytes(raw)
    elif damage == 'binding':
        entry['filename'] = 'studio/inputs/' + '0' * 64 + '.audio'
        digest = manifest_rehash(backup, RECEIPT, receipt)
    elif damage == 'omitted-project':
        receipt['projects'] = []
        digest = manifest_rehash(backup, RECEIPT, receipt)
    else:
        receipt['projects'][0]['project_fingerprint'] = '0' * 64
        digest = manifest_rehash(backup, RECEIPT, receipt)
    target = tmp_path / 'restored'
    with pytest.raises((ValueError, OSError)):
        restore_backup(backup, target, digest)
    assert not target.exists()


def historical_v4(backup, *, forged_review=None, uppercase_job=False):
    from carr_library.studio.backup_inputs import REVIEW
    manifest = read_json(backup / 'manifest.json')
    manifest.pop('extensions', None)
    shutil.rmtree(backup / 'extensions', ignore_errors=True)
    reading = manifest.pop('reading_state', None)
    if reading is not None:
        (backup / reading['filename']).unlink()
    manifest['version'] = 4 if 'pending' in manifest else 3
    omitted = [item for item in manifest['state'] if item['filename'] == RECEIPT
               or item['filename'].startswith('studio/inputs/')]
    manifest['state'] = [item for item in manifest['state'] if item not in omitted]
    for item in omitted:
        (backup / 'state' / item['filename']).unlink()
    if forged_review is not None:
        raw = encode(forged_review)
        (backup / 'state' / REVIEW).write_bytes(raw)
        manifest['state'].append({'filename': REVIEW, 'size_bytes': len(raw),
                                  'sha256': sha256(raw).hexdigest()})
    if uppercase_job:
        entry = next(item for item in manifest['state'] if item['filename'].endswith('/job.json'))
        path = backup / 'state' / entry['filename']
        path.rename(path.with_name('JOB.JSON'))
        entry['filename'] = entry['filename'].removesuffix('job.json') + 'JOB.JSON'
    raw = encode(manifest)
    (backup / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def test_legacy_paths_and_inherited_approvals_cannot_capture_local_files(backed_up, tmp_path, monkeypatch):
    from carr_library.studio.backup_inputs import input_review
    _app, source, _project, backup, _digest = backed_up
    # The same pathname belongs to different data on this machine.
    sentinel = b'Unrelated private local file at an inherited project pathname.'
    source.write_bytes(sentinel)
    inherited = {'schema': 1, 'pending': [], 'approved': {
        str(source): {'size_bytes': len(sentinel), 'sha256': sha256(sentinel).hexdigest()}}}
    digest = historical_v4(backup, forged_review=inherited)
    original_open = Path.open
    def forbid_unreviewed_read(path, *args, **kwargs):
        if path == source:
            raise AssertionError('A legacy reference opened an unapproved local file.')
        return original_open(path, *args, **kwargs)
    with monkeypatch.context() as guarded:
        guarded.setattr(Path, 'open', forbid_unreviewed_read)
        restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
        service = production(restored.root / 'state/studio')
        assert input_review(service.root) == {'schema': 1, 'pending': [str(source)], 'approved': {}}
        assert service.restored_inputs_to_review('restorable-job') == (str(source),)
        with pytest.raises(InterruptedError):
            service.approve_restored_inputs([source], cancelled=lambda: True)
        assert input_review(service.root)['pending'] == [str(source)]
        blocked_backup = tmp_path / 'must-not-capture'
        with pytest.raises(ValueError, match='explicitly approve'):
            create_backup(restored, blocked_backup)
        assert not blocked_backup.exists()
        new_outputs = tmp_path / 'reviewed-outputs'
        new_outputs.mkdir()
        service.relocate_restored_job('restorable-job', new_outputs)
        with pytest.raises(ValueError, match='explicitly approve'):
            service.run('restorable-job')
        assert not list(new_outputs.iterdir())

    # Explicit review is separate from deserialization or output-folder selection.
    service.approve_restored_inputs([source])
    assert service.restored_inputs_to_review('restorable-job') == ()
    approved_backup = tmp_path / 'explicitly-approved-backup'
    create_backup(restored, approved_backup)
    receipt = read_json(approved_backup / 'state' / RECEIPT)
    entry = receipt['inputs'][str(source)]
    assert (approved_backup / 'state' / entry['filename']).read_bytes() == sentinel
    assert source.read_bytes() == sentinel
    source.write_bytes(b'Changed since the explicit local review')
    with pytest.raises(ValueError, match='changed'):
        create_backup(restored, tmp_path / 'changed-input-backup')
    with pytest.raises(ValueError, match='changed'):
        service.run('restorable-job')


def test_case_variant_legacy_job_cannot_retain_external_output_authority(backed_up, tmp_path):
    from carr_library.studio.backup_inputs import input_review
    _app, source, _project, backup, _digest = backed_up
    digest = historical_v4(backup, uppercase_job=True)
    restored = restore_backup(backup, tmp_path / 'restored', digest)
    state = read_json(restored / 'state/studio/jobs/restorable-job/JOB.JSON')
    assert state['status'] == 'waiting-input' and state['destination'] == ''
    assert state['recovery']['requires_output_directory'] is True
    assert input_review(restored / 'state/studio')['pending'] == [str(source)]


def test_case_aliased_reserved_receipt_is_rejected(backed_up, tmp_path):
    _app, _source, _project, backup, _digest = backed_up
    alias = 'studio/Backup-Inputs.json'
    (backup / 'state' / RECEIPT).rename(backup / 'state' / alias)
    manifest = read_json(backup / 'manifest.json')
    next(item for item in manifest['state'] if item['filename'] == RECEIPT)['filename'] = alias
    raw = encode(manifest)
    (backup / 'manifest.json').write_bytes(raw)
    target = tmp_path / 'restored'
    with pytest.raises(ValueError, match='reserved filename'):
        restore_backup(backup, target, sha256(raw).hexdigest())
    assert not target.exists()


def test_required_source_bytes_are_reverified_even_when_metadata_is_unchanged(tmp_path, monkeypatch):
    import carr_library.studio.backup_inputs as inputs
    app = LibraryApp(tmp_path / 'library')
    source = tone(tmp_path / 'cue.wav')
    ProjectStore(app.root / 'state/studio/projects').save(project_for(source))
    before = source.read_bytes()
    # Model the Windows overwrite case where size, creation time and restored
    # modification time do not establish that file bytes stayed unchanged.
    monkeypatch.setattr(inputs, '_stamp', lambda value: (value.st_dev, value.st_ino, value.st_size, 0, 0))
    modified = False
    def edit_after_retained_copy():
        nonlocal modified
        if not modified and list(tmp_path.glob('*/state/studio/inputs/*.audio')):
            changed = bytearray(before)
            changed[-1] ^= 1
            source.write_bytes(changed)
            modified = True
        return False
    backup = tmp_path / 'backup'
    with pytest.raises(ValueError, match='changed'):
        create_backup(app, backup, cancelled=edit_after_retained_copy)
    assert modified and not backup.exists()


@pytest.mark.parametrize('damage', ['missing', 'symlink', 'hardlink'])
def test_unavailable_required_input_never_publishes_backup(tmp_path, damage):
    app = LibraryApp(tmp_path / 'library')
    source = tone(tmp_path / 'cue.wav')
    project = project_for(source)
    ProjectStore(app.root / 'state/studio/projects').save(project)
    if damage == 'missing':
        source.unlink()
    elif damage == 'symlink':
        original = tmp_path / 'elsewhere.wav'
        source.rename(original)
        source.symlink_to(original)
    else:
        os.link(source, tmp_path / 'linked.wav')
    backup = tmp_path / 'backup'
    with pytest.raises((ValueError, OSError)):
        create_backup(app, backup)
    assert not backup.exists()
    assert not list(tmp_path.glob('.backup-*'))


def test_input_copy_cancelled_mid_stream_leaves_no_success_backup(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    source = tone(tmp_path / 'long-cue.wav', seconds=60)
    ProjectStore(app.root / 'state/studio/projects').save(project_for(source))
    interrupted = False
    def cancel_during_input():
        nonlocal interrupted
        pending = list(tmp_path.glob('*/state/studio/inputs/.pending-*'))
        interrupted = interrupted or any(path.stat().st_size >= 1024 * 1024 for path in pending)
        return interrupted
    backup = tmp_path / 'backup'
    with pytest.raises(InterruptedError):
        create_backup(app, backup, cancelled=cancel_during_input)
    assert interrupted and not backup.exists()
    assert not list(tmp_path.glob('*/state/studio/inputs/.pending-*'))


def test_deduplicated_inputs_count_once_in_the_existing_state_budget(tmp_path):
    state, target = tmp_path / 'state', tmp_path / 'target'
    source = tone(tmp_path / 'first.wav', seconds=1)
    same = tmp_path / 'second.wav'
    same.write_bytes(source.read_bytes())
    base = project_for(source)
    project = replace(base, scenes=(replace(base.scenes[0], cues=(
        base.scenes[0].cues[0], Cue('same-bytes', str(same)))),))
    ProjectStore(state / 'studio/projects').save(project)
    # Permit one asset and metadata, but not a second copy of the same PCM.
    target.mkdir()
    entries = snapshot_state(state, target, max_bytes=source.stat().st_size + 10000)
    assets = [entry for entry in entries if entry['filename'].startswith('studio/inputs/')]
    assert len(assets) == 1 and assets[0]['size_bytes'] == source.stat().st_size
    receipt = read_json(target / RECEIPT)
    assert receipt['inputs'][str(source)] == receipt['inputs'][str(same)]


def test_modified_restored_input_is_refused_before_render(backed_up, tmp_path):
    _app, _source, project, backup, digest = backed_up
    destination = restore_backup(backup, tmp_path / 'restored', digest)
    service = production(destination / 'state/studio')
    cue = service.projects.load(project.id).scenes[0].cues[0]
    Path(cue.path).write_bytes(b'modified required input')
    with pytest.raises(ValueError, match='verified content identity'):
        service._cue_source(cue)


def test_legacy_v4_without_studio_receipt_remains_readable(backed_up, tmp_path):
    _app, _source, project, backup, _digest = backed_up
    manifest = read_json(backup / 'manifest.json')
    manifest.pop('extensions', None)
    shutil.rmtree(backup / 'extensions', ignore_errors=True)
    reading = manifest.pop('reading_state', None)
    if reading is not None:
        (backup / reading['filename']).unlink()
    manifest['version'] = 4 if 'pending' in manifest else 3
    omitted = [item for item in manifest['state'] if item['filename'] == RECEIPT
               or item['filename'].startswith('studio/inputs/')]
    manifest['state'] = [item for item in manifest['state'] if item not in omitted]
    for item in omitted:
        (backup / 'state' / item['filename']).unlink()
    raw = encode(manifest)
    (backup / 'manifest.json').write_bytes(raw)
    restored = restore_backup(backup, tmp_path / 'restored', sha256(raw).hexdigest())
    exact = ProjectStore(restored / 'state/studio/projects').load(project.id)
    assert exact == project  # No fabricated retained input for historical backups.
    legacy_job = production(restored / 'state/studio').get('restorable-job')
    assert legacy_job['status'] == 'waiting-input' and legacy_job['destination'] == ''
    assert legacy_job['recovery']['inputs_retained'] is False
    assert legacy_job['recovery']['requires_output_directory'] is True


def test_invalid_typed_project_fails_before_backup_publication(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    source = tone(tmp_path / 'cue.wav')
    project = project_for(source)
    ProjectStore(app.root / 'state/studio/projects').save(project)
    value = project.to_dict()
    value['scenes'][0]['cues'][0]['start'] = 10 ** 1000
    path = app.root / 'state/studio/projects' / project.id / 'project.json'
    atomic_json(path, value, replace=True)
    target = tmp_path / 'backup'
    with pytest.raises(ValueError, match='Invalid typed Studio project'):
        create_backup(app, target)
    assert not target.exists()


def test_imported_paths_are_registered_without_opening_or_inheriting_approval(tmp_path, monkeypatch):
    from carr_library.studio.backup_inputs import (
        input_review, register_pending_inputs, approve_input_review,
    )
    studio = tmp_path / 'studio'
    source = tone(tmp_path / 'explicitly-selected.wav')
    register_pending_inputs(studio, [str(source)])
    approve_input_review(studio, [source])
    assert str(source) in input_review(studio)['approved']
    original_open, original_stat, original_lstat = Path.open, Path.stat, Path.lstat
    def guarded(function):
        def call(path, *args, **kwargs):
            assert path != source, 'Importing a declaration probed the recorded external path.'
            return function(path, *args, **kwargs)
        return call
    monkeypatch.setattr(Path, 'open', guarded(original_open))
    monkeypatch.setattr(Path, 'stat', guarded(original_stat))
    monkeypatch.setattr(Path, 'lstat', guarded(original_lstat))
    value = register_pending_inputs(studio, [str(source), r'C:\Unselected\foreign.wav'])
    assert value == {'schema': 1,
        'pending': sorted([str(source), r'C:\Unselected\foreign.wav']), 'approved': {}}
    assert input_review(studio) == value


def test_repeated_restore_retains_the_original_job_provenance(backed_up, tmp_path):
    app, source, project, backup, digest = backed_up
    first = restore_backup(backup, tmp_path / 'first-restore', digest)
    first_job = production(first / 'state/studio').get('restorable-job')
    expected = first_job['recovery']['previous']
    assert expected['status'] == 'pending'
    next_backup = tmp_path / 'next-backup'
    next_digest = create_backup(LibraryApp(first), next_backup)
    second = restore_backup(next_backup, tmp_path / 'second-restore', next_digest)
    job = production(second / 'state/studio').get('restorable-job')
    assert job['recovery']['previous'] == expected
    assert job['status'] == 'waiting-input' and job['destination'] == ''


def test_cancelled_studio_projection_never_publishes_a_destination(backed_up, tmp_path):
    _app, _source, _project, backup, digest = backed_up
    stopped = False
    def cancel_after_state_copy():
        nonlocal stopped
        stopped = stopped or any(tmp_path.glob('.carr-library-restore-*/state/studio/backup-inputs.json'))
        return stopped
    target = tmp_path / 'cancelled-restore'
    with pytest.raises(InterruptedError):
        restore_backup(backup, target, digest, cancelled=cancel_after_state_copy)
    assert stopped and not target.exists()
