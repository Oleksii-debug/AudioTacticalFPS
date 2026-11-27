"""Typed writer, exact legacy readers and whole-backup byte attestation.

All fixtures are synthetic local data. No provider, speech engine or external
recorded path is opened by backup/verification/restoration.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from carr_library import backup_sidecars, library_backup
from carr_library.application import LibraryApp
from carr_library.assistant.profiles import ProfileStore
from carr_library.durable_state import atomic_json
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.progress_preferences import ProgressPreferences, ProgressPreferenceStore
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.studio.render import ProductionService


def files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def resign(root, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (root / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


@pytest.fixture
def current(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic typed backup')
    app.add_text(workspace, 'Original', 'Original exact source.', edition_id='source')
    document = ReadingDocument('Original exact source.', title='Private reading copy')
    document.replace(0, 8, 'Edited')
    reader = ReaderDraftStore(app.root, slot='typed-backup-regression')
    resume = ReaderResume(document, 'test:synthetic', rate=2, volume=73, cursor=3, audio_seconds=.25)
    reader.save(resume)
    preferences = ProgressPreferences(mode='beep', volume=15, muted=True)
    ProgressPreferenceStore(app.root / 'progress-preferences.json').save(preferences)
    return app, reader, resume, preferences


def test_one_typed_writer_roundtrips_canonical_root_settings_and_reader_edits(current, tmp_path, monkeypatch):
    app, reader, resume, preferences = current
    saved_reader = reader.path.read_bytes()
    saved_preferences = (app.root / 'progress-preferences.json').read_bytes()
    monkeypatch.setattr(subprocess, 'Popen',
        lambda *a, **k: pytest.fail('Passive backup may not launch a provider or an engine.'))
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    value = json.loads((folder / 'manifest.json').read_bytes())
    assert value['version'] == 5 and value['extensions']['version'] == 4
    assert value['extensions']['canonical_sidecars'] == ['progress-preferences.json']
    assert (folder / 'extensions/progress-preferences.json').read_bytes() == saved_preferences
    before = files(folder)
    assert verify_backup(folder, digest) == value
    destination = restore_backup(folder, tmp_path / 'restored', digest)
    recovered = ReaderDraftStore(destination, slot='typed-backup-regression').load()
    assert recovered.document.to_dict() == resume.document.to_dict()
    assert (recovered.voice_id, recovered.rate, recovered.volume, recovered.cursor,
            recovered.audio_seconds) == ('test:synthetic', 2, 73, 3, .25)
    assert recovered.document.undo()
    assert recovered.document.text == 'Original exact source.'
    assert ProgressPreferenceStore(destination / 'progress-preferences.json').load() == preferences
    assert (destination / reader.path.relative_to(app.root)).read_bytes() == saved_reader
    assert (destination / 'progress-preferences.json').read_bytes() == saved_preferences
    assert files(folder) == before


def test_total_budget_includes_core_pending_state_extensions_and_manifest(current, tmp_path):
    app, *_ = current
    reference = tmp_path / 'reference'
    create_backup(app, reference)
    expected = sum(len(raw) for raw in files(reference).values())
    manifest = json.loads((reference / 'manifest.json').read_bytes())
    assert manifest['state'] and manifest['extensions']['files']
    assert manifest['pending']['files']
    with pytest.raises(ValueError, match='budget|byte limit'):
        create_backup(app, tmp_path / 'short', max_bytes=expected - 1)
    assert not (tmp_path / 'short').exists()
    exact = tmp_path / 'exact'
    digest = create_backup(app, exact, max_bytes=expected)
    assert sum(len(raw) for raw in files(exact).values()) == expected
    verify_backup(exact, digest)


@pytest.mark.parametrize('operation', ['verify', 'restore'])
@pytest.mark.parametrize('change', ['database', 'reader', 'extension', 'extra-file', 'empty-directory'])
def test_final_attestation_rejects_changes_after_component_validation(
        current, tmp_path, monkeypatch, operation, change):
    app, reader, *_ = current
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    prepared = library_backup._prepare_restore
    changed = []

    def mutate_after_validation(*args, **kwargs):
        result = prepared(*args, **kwargs)
        if change in ('extra-file', 'empty-directory'):
            path = folder / 'originals' / 'unlisted-after-validation'
            if change == 'extra-file':
                path.write_bytes(b'unlisted bytes')
            else:
                path.mkdir()
        else:
            relative = {'database': 'library.sqlite',
                        'reader': reader.path.relative_to(app.root).as_posix(),
                        'extension': 'extensions/progress-preferences.json'}[change]
            path = folder / relative
            raw = path.read_bytes()
            before = path.stat()
            offset = 100 if change == 'database' else len(raw) // 2
            path.write_bytes(raw[:offset] + bytes([raw[offset] ^ 1]) + raw[offset + 1:])
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        changed.append(True)
        return result

    monkeypatch.setattr(library_backup, '_prepare_restore', mutate_after_validation)
    with pytest.raises(ValueError, match='digest|unexpected|missing'):
        if operation == 'verify':
            verify_backup(folder, digest)
        else:
            restore_backup(folder, tmp_path / 'rejected', digest)
    assert changed
    assert not (tmp_path / 'rejected').exists()


def test_second_byte_pass_catches_an_already_hashed_database(current, tmp_path, monkeypatch):
    app, *_ = current
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    actual = backup_sidecars._stream_file
    database_reads, changed = [], []

    def mutate_earlier_file(path, **kwargs):
        if path == folder / 'library.sqlite':
            database_reads.append(True)
        result = actual(path, **kwargs)
        if path == folder / 'manifest.json' and not changed:
            target = folder / 'library.sqlite'
            raw, stamp = target.read_bytes(), target.stat()
            target.write_bytes(raw[:100] + bytes([raw[100] ^ 1]) + raw[101:])
            os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            changed.append(True)
        return result

    monkeypatch.setattr(backup_sidecars, '_stream_file', mutate_earlier_file)
    with pytest.raises(ValueError, match='digest'):
        verify_backup(folder, digest)
    assert changed and len(database_reads) == 2


@pytest.mark.parametrize('failure', [InterruptedError, TimeoutError])
def test_second_attestation_pass_propagates_stop_without_destination(
        current, tmp_path, monkeypatch, failure):
    app, *_ = current
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    before = files(folder)
    actual = backup_sidecars._stream_file
    visits = []
    ready = []

    def entering_second_pass(path, **kwargs):
        if path == folder / 'library.sqlite':
            visits.append(True)
            if len(visits) == 2:
                ready.append(True)
        return actual(path, **kwargs)

    def cancelled():
        if ready:
            raise failure('Synthetic stop in second byte pass.')
        return False

    monkeypatch.setattr(backup_sidecars, '_stream_file', entering_second_pass)
    with pytest.raises(failure, match='Synthetic stop'):
        restore_backup(folder, tmp_path / 'rejected', digest, cancelled=cancelled)
    assert len(visits) == 2
    assert not (tmp_path / 'rejected').exists()
    assert files(folder) == before


@pytest.mark.parametrize('version', [3, 4])
def test_exact_canonical_sidecar_envelope_is_a_reader_without_relabelling(
        current, tmp_path, version):
    app, reader, *_ = current
    folder = tmp_path / 'canonical-envelope'
    create_backup(app, folder)
    value = json.loads((folder / 'manifest.json').read_bytes())
    extension = value.pop('extensions')
    assert extension['canonical_sidecars'] == ['progress-preferences.json']
    sidecars = [{**row, 'filename': 'state/' + row['filename']} for row in value.pop('state')]
    for row in extension['files']:
        assert row['filename'] in extension['canonical_sidecars']
        (folder / 'extensions' / row['filename']).rename(folder / row['filename'])
        sidecars.append(dict(row))
    (folder / 'extensions').rmdir()
    value.update(version=version, sidecars=sorted(sidecars, key=lambda row: row['filename']))
    if version == 3:
        value.pop('generated_audio_included')
        value.pop('pending', None)
        if (folder / 'pending').exists():
            shutil.rmtree(folder / 'pending')
    digest = resign(folder, value)
    before = files(folder)
    assert verify_backup(folder, digest) == value
    restored = restore_backup(folder, tmp_path / 'restored', digest)
    assert ReaderDraftStore(restored, slot='typed-backup-regression').load().document.text == 'Edited exact source.'
    assert (restored / reader.path.relative_to(app.root)).read_bytes() == reader.path.read_bytes()
    assert files(folder) == before


def test_old_opaque_state_is_retained_without_becoming_current_execution_authority(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Exact older opaque state envelope')
    folder = tmp_path / 'old-format'
    create_backup(app, folder)
    value = json.loads((folder / 'manifest.json').read_bytes())
    for field in ('pending', 'extensions', 'state', 'generated_audio_included'):
        value.pop(field, None)
    for name in ('pending', 'extensions', 'state'):
        if (folder / name).exists():
            shutil.rmtree(folder / name)
    opaque = {
        'speech/settings.json': b'{"voice":"installed-voice","rate":1.0}',
        'studio/jobs/job-1/job.json': b'{"status":"paused","completed_chunks":["first"]}',
        'providers/account.profile.json': b'{"credential_ref":"synthetic old declaration"}',
        'assistant/job.json': b'{"status":"paused","plan":{"legacy":true}}',
        'studio/cache/chunk.wav': b'opaque older bytes, never qualified as PCM',
        'empty.txt': b'',
    }
    entries = []
    for name, raw in sorted(opaque.items()):
        target = folder / 'state' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        entries.append(dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest()))
    value.update(version=3, state=entries, generated_audio_included=True)
    digest = resign(folder, value)
    before = files(folder)
    monkeypatch.setattr(subprocess, 'Popen',
        lambda *a, **k: pytest.fail('Historical bytes cannot start a provider, job or decoder.'))
    verify_backup(folder, digest)
    restored = restore_backup(folder, tmp_path / 'restored', digest)
    assert {name: (restored / 'state' / name).read_bytes() for name in opaque} == opaque
    assert files(folder) == before
    with pytest.raises(ValueError, match='profile'):
        ProfileStore(restored / 'state/providers').get('account')
    with pytest.raises(ValueError):
        ProductionService(restored / 'state/studio', voices=object()).get('job-1')
    # The current writer does not silently promote those opaque declarations.
    with pytest.raises(ValueError, match='validated backup adapter|invalid backup sidecar'):
        create_backup(LibraryApp(restored), tmp_path / 'not-promoted', include_generated=True)
    assert not (tmp_path / 'not-promoted').exists()


def test_current_reader_discovery_links_are_bounded_passive_state(current, tmp_path):
    app, reader, *_ = current
    key = 'b' * 64
    link = app.root / 'state/speech' / (key + '.reader-links.json')
    atomic_json(link, {'schema': 'carr.reader-recovery-links.v1', 'copies': [reader.key]})
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    restored = restore_backup(backup, tmp_path / 'restored', digest)
    assert (restored / link.relative_to(app.root)).read_bytes() == link.read_bytes()
    atomic_json(link, {'schema': 'carr.reader-recovery-links.v1', 'copies': [reader.key, reader.key]}, replace=True)
    with pytest.raises(ValueError, match='discovery inventory'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()

@pytest.mark.parametrize('keep_pending', [False, True])
def test_exact_alternate_v5_reading_state_envelope_preserves_saved_edits(
        tmp_path, keep_pending):
    """A historical V5 reading_state field is never mistaken for extensions."""
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Historical exact reading state')
    book = app.add_text(workspace, 'Source', 'Original exact words.')
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    reading = ReadingState(app.root)
    reading.remember(page.location)
    bookmark = reading.add_bookmark(page.location, 'Historical bookmark')
    document = ReadingDocument(page.text)
    document.replace(0, 8, 'Edited')
    drafts = ReaderDraftStore(app.root, slot='alternate-v5')
    drafts.save(ReaderResume(document, 'test:synthetic', cursor=2))
    saved_reading, saved_draft = reading.path.read_bytes(), drafts.path.read_bytes()

    folder = tmp_path / 'exact-v5'
    create_backup(app, folder)
    value = json.loads((folder / 'manifest.json').read_bytes())
    extension = value.pop('extensions')
    assert len(extension['files']) == 1
    row, = extension['files']
    assert row['filename'] == 'reading-state.json'
    (folder / 'extensions' / row['filename']).rename(folder / row['filename'])
    (folder / 'extensions').rmdir()
    value.update(version=5, reading_state=dict(row))
    if not keep_pending:
        value['pending'] = None
        shutil.rmtree(folder / 'pending')
    digest = resign(folder, value)
    before = files(folder)

    assert verify_backup(folder, digest) == value
    destination = restore_backup(folder, tmp_path / 'restored', digest)
    recovered = ReaderDraftStore(destination, slot='alternate-v5').load()
    assert recovered.document.to_dict() == document.to_dict()
    assert recovered.cursor == 2 and recovered.voice_id == 'test:synthetic'
    assert (destination / drafts.path.relative_to(app.root)).read_bytes() == saved_draft
    assert (destination / 'reading-state.json').read_bytes() == saved_reading
    state = ReadingState(destination)
    assert state.resume(workspace, book['edition']) == page.location
    assert state.bookmarks(workspace, book['edition']) == (bookmark,)
    assert files(folder) == before
