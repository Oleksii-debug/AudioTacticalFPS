"""Typed Studio media over actual PCM and canonical full/scoped backups."""
from hashlib import sha256
import json
from pathlib import Path
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.recording import (
    validate_recording_receipt, unverified_recording_files,
)
from carr_library.durable_state import atomic_json, encode, read_json
from carr_library.library_backup import create_backup, restore_backup
from carr_library.studio.publishing import NOTICE, validate_publication_receipt


def media_library(tmp_path):
    app = LibraryApp(tmp_path / 'source-library')
    workspace = app.create_workspace('Exact source')
    app.add_text(workspace, 'Source', 'Original wording survives media recovery.')
    recordings = app.root / 'state/studio/recordings'
    recordings.mkdir(parents=True)
    identity = 'a' * 32
    audio = recordings / (identity + '.wav')
    with wave.open(str(audio), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b'\0\0' * 2400)
    raw = audio.read_bytes()
    receipt = dict(schema='carr-recording/1', id=identity, filename=audio.name,
        title='Synthetic retained original', created='2026-01-02T03:04:05+00:00',
        source=dict(kind='microphone', backend='dshow', device_id='d' * 64,
                    name='Declared synthetic device; no microphone is opened'),
        ffmpeg_sha256='f' * 64,
        audio=dict(sha256=sha256(raw).hexdigest(), size_bytes=len(raw),
            sample_rate=24000, channels=1, frames=2400, sample_width=2, duration_seconds=.1),
        limits=dict(max_seconds=1, sample_rate=24000, channels=1, max_bytes=65536),
        completion='stopped')
    validate_recording_receipt(receipt, recording_id=identity)
    atomic_json(recordings / (identity + '.json'), receipt)
    orphan = recordings / ('b' * 32 + '.wav')
    orphan.write_bytes(b'An opaque committed original whose capture receipt was not saved.')
    publication_id = 'c' * 32
    external_root = tmp_path / 'previous-library' / 'state/studio'
    external_epub = tmp_path / 'unavailable-output' / 'book.epub'
    publication = dict(schema='resonance-publication/1', id=publication_id,
        status='complete', studio_root=str(external_root), output=str(external_epub),
        artifact=dict(size=1234, sha256='e' * 64),
        source=dict(job_id='historical-job', job_sha256='1' * 64,
            project_id='historical-project', project_sha256='2' * 64,
            source_script_sha256='3' * 64,
            artifacts=[dict(name=name, size=100, sha256=str(index) * 64)
                       for index, name in enumerate(('master.wav', 'project.json', 'timeline.json'), 4)]),
        created='2026-01-02T03:04:05Z', notice=NOTICE)
    validate_publication_receipt(publication, publication_id=publication_id)
    publication_file = app.root / 'state/studio/publications' / (publication_id + '.json')
    atomic_json(publication_file, publication)
    return app, workspace, audio, orphan, publication_file, (external_root, external_epub)


def forbid_external_paths(monkeypatch, forbidden):
    forbidden = set(forbidden)
    for name in ('open', 'stat', 'lstat'):
        original = getattr(Path, name)
        def guarded(path, *args, _method=original, **kwargs):
            assert path not in forbidden, 'Media recovery probed a recorded external path.'
            return _method(path, *args, **kwargs)
        monkeypatch.setattr(Path, name, guarded)


def reseal(backup, manifest):
    raw = encode(manifest)
    (backup / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def rewrite_state(backup, manifest, name, record):
    raw = encode(record)
    (backup / 'state' / name).write_bytes(raw)
    entry = next(row for row in manifest['state'] if row['filename'] == name)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())


@pytest.mark.parametrize('include_generated', [False, True])
def test_full_media_round_trip_keeps_exact_originals_and_inert_publication(
        tmp_path, monkeypatch, include_generated):
    app, workspace, audio, orphan, publication, forbidden = media_library(tmp_path)
    original = {path.relative_to(app.root): path.read_bytes()
                for path in (audio, audio.with_suffix('.json'), orphan, publication)}
    forbid_external_paths(monkeypatch, forbidden)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup, include_generated=include_generated)
    manifest = read_json(backup / 'manifest.json')
    extension = manifest['extensions']
    assert manifest['version'] == 5 and extension['version'] == 3
    assert extension['studio_input_receipt'] is False
    assert extension['studio_recordings'] == [audio.with_suffix('.json').relative_to(app.root / 'state').as_posix()]
    assert extension['studio_unverified_recordings'] == [orphan.relative_to(app.root / 'state').as_posix()]
    assert extension['studio_publications'] == [publication.relative_to(app.root / 'state').as_posix()]
    assert dict(component='state/' + extension['studio_publications'][0],
                kind='publication-output', reference=str(forbidden[1]), included=False) in extension['required_inputs']
    destination = restore_backup(backup, tmp_path / 'restored', digest)
    assert {relative: (destination / relative).read_bytes() for relative in original} == original
    assert unverified_recording_files(destination) == (destination / orphan.relative_to(app.root),)
    # The historical job hash remains a declaration even after relocation.
    assert read_json(destination / publication.relative_to(app.root))['source']['job_sha256'] == '1' * 64
    assert LibraryApp(destination).search(workspace, 'wording')[0].quote == 'wording'


def test_workspace_backup_omits_all_independent_media_and_temporary_capture(tmp_path):
    app, workspace, *_ = media_library(tmp_path)
    pending = app.root / 'state/studio/recordings/.pending-recording-active'
    pending.mkdir()
    (pending / 'capture.wav').write_bytes(b'Unfinished microphone work')
    (pending.parent / '.recording.lock').write_bytes(b'Owned capture')
    backup = tmp_path / 'scoped'
    digest = create_backup(app, backup, authorized_workspaces={workspace})
    manifest = read_json(backup / 'manifest.json')
    assert not manifest['extensions']['independent_state']
    assert not manifest['state']
    restored = restore_backup(backup, tmp_path / 'restored', digest)
    assert not (restored / 'state/studio').exists()


def test_full_backup_excludes_pending_capture_and_lock_but_keeps_final_orphan(tmp_path):
    app, _, _, orphan, *_ = media_library(tmp_path)
    pending = app.root / 'state/studio/recordings/.pending-recording-active'
    pending.mkdir()
    (pending / 'capture.wav').write_bytes(b'Unfinished microphone work')
    (pending.parent / '.recording.lock').write_bytes(b'Owned capture')
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    manifest = read_json(backup / 'manifest.json')
    names = {row['filename'] for row in manifest['state']}
    assert orphan.relative_to(app.root / 'state').as_posix() in names
    assert not any('.pending-' in name or name.endswith('.lock') for name in names)


@pytest.mark.parametrize('damage', [
    'pcm-header', 'recording-id', 'missing-wav', 'publication-id',
    'unlisted-orphan', 'extension-downgrade',
])
def test_resealed_media_semantic_corruption_fails_before_any_destination(tmp_path, damage):
    app, _, audio, orphan, publication, _ = media_library(tmp_path)
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    manifest = read_json(backup / 'manifest.json')
    recording_name = audio.with_suffix('.json').relative_to(app.root / 'state').as_posix()
    if damage in ('pcm-header', 'recording-id'):
        value = read_json(backup / 'state' / recording_name)
        if damage == 'pcm-header':
            value['audio']['frames'] += 1
            value['audio']['duration_seconds'] = value['audio']['frames'] / 24000
        else:
            value['id'] = 'd' * 32
        rewrite_state(backup, manifest, recording_name, value)
    elif damage == 'missing-wav':
        name = audio.relative_to(app.root / 'state').as_posix()
        (backup / 'state' / name).unlink()
        manifest['state'] = [row for row in manifest['state'] if row['filename'] != name]
    elif damage == 'publication-id':
        name = publication.relative_to(app.root / 'state').as_posix()
        value = read_json(backup / 'state' / name)
        value['source']['job_sha256'] = 'not-a-sha'
        rewrite_state(backup, manifest, name, value)
    elif damage == 'unlisted-orphan':
        manifest['extensions']['studio_unverified_recordings'] = []
    else:
        manifest['extensions']['version'] = 2
        for field in ('studio_input_receipt', 'studio_publications', 'studio_recordings',
                      'studio_unverified_recordings'):
            manifest['extensions'].pop(field)
    digest = reseal(backup, manifest)
    target = tmp_path / 'must-not-exist'
    with pytest.raises(ValueError):
        restore_backup(backup, target, digest)
    assert not target.exists()
