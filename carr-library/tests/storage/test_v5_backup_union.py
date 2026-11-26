"""One archive for exact reading state, pending originals and production recovery."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.intake import snapshot
from carr_library.evidence_drafts import EvidenceDrafts
from carr_library.extract.page_ocr import Config
from carr_library.ingest.worker import prepare_request, record_progress, load_request
from carr_library.keyboard_preferences import validate_keymap
from carr_library.library_backup import create_backup, restore_backup, INCOMPLETE
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState
from carr_library import pending_backup, state_backup


BASE_FIELDS = {'version', 'kind', 'schema_version', 'schema_fingerprint', 'database', 'assets'}


def _manifest(backup):
    return json.loads((backup / 'manifest.json').read_bytes())


def _seal(backup, manifest):
    raw = (json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    (backup / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


@pytest.fixture
def pending_library(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Combined recovery')
    book = app.add_text(workspace, 'A completed original', 'Stored source citation.\fA second section.')
    page = ReadingDesk(app, workspace, book['edition']).page(2)
    reading = ReadingState(app.root)
    reading.remember(page.location)
    bookmark = reading.add_bookmark(page.location, 'Продовжити тут')
    topic = app.create_topic(workspace, 'Source-bound recovery')
    hit, = app.search(workspace, 'citation')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Saved note remains separate.')
    evidence, = app.evidence(workspace, topic)
    draft = EvidenceDrafts(app.root).checkpoint(evidence, 'Synthetic unsaved recovery note.')
    keymap = b'{"version":1,"bindings":{"reader.next":"Ctrl+Alt+N"}}'
    validate_keymap(keymap)
    (app.root / 'keyboard-shortcuts.json').write_bytes(keymap)
    sidecars = {name: (app.root / name).read_bytes() for name in
                ('reading-state.json', 'keyboard-shortcuts.json', 'evidence-drafts.json')}
    original = tmp_path / 'unfinished.txt'
    original.write_text('First unfinished page.\fSecond unfinished page.\fThird unfinished page.', encoding='utf-8')
    metadata = dict(title='Unfinished import', authors=[], language=None, edition=None, volume=None,
                    collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    planned = snapshot(original.parent, original.name).manifest()
    request = prepare_request(app.root, workspace, original, metadata, Config(), expected_manifest=planned)
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 2
    initial = app._import_file(workspace, original, **metadata, cancelled=cancelled,
                              _request_id=request['request_id'], _expected_manifest=planned)
    assert initial['state'] == 'cancelled' and initial['completed'] == 1
    record_progress(app.root, request['request_id'], state='cancelled', result=initial)
    return SimpleNamespace(app=app, workspace=workspace, book=book, page=page, bookmark=bookmark,
                           topic=topic, evidence=evidence, draft=draft, sidecars=sidecars,
                           original=original, request=request, initial=initial)


def _assert_resume(restored, fixture):
    rebound = load_request(restored.root, fixture.request['request_id'])
    assert rebound['data_dir'] == str(restored.root)
    assert Path(rebound['source']).parent == restored.assets_dir
    final = restored.resume_import(fixture.workspace, fixture.request['request_id'])
    assert final['state'] == 'complete' and final['id'] == fixture.initial['id']
    assert final['edition'] == fixture.initial['edition']
    assert restored.search(fixture.workspace, 'unfinished')[1].quote == 'unfinished'


def test_v5_combines_real_pending_resume_and_generated_speech_reexport(pending_library, tmp_path, monkeypatch):
    fixture, app = pending_library, pending_library.app
    from carr_library.speech import VoiceRegistry, FliteProvider
    from carr_library.studio.render import ProductionService
    binary = shutil.which('ffmpeg')
    if not binary or not shutil.which('ffprobe'):
        pytest.skip('Actual production recovery requires installed FFmpeg and ffprobe.')
    voices = (VoiceRegistry.discover(include_flite=False) if os.name == 'nt'
              else VoiceRegistry([FliteProvider(binary)]))
    if not voices.voices():
        pytest.skip('Actual speech requires an installed SAPI or Flite voice.')
    voice = voices.voices()[0]
    exports = tmp_path / 'old-exports'
    exports.mkdir()
    service = ProductionService(app.root / 'state/studio', voices=voices)
    project = service.create('One preserved narration.', voice_ids=(voice.id,))
    job = service.submit(project, exports, stems=False)
    first = service.run(job)
    master = Path(first['destination']) / 'master.wav'
    original_audio_sha = sha256(master.read_bytes()).hexdigest()
    assert first['status'] == 'complete' and first['result']['duration'] > .2

    backup = tmp_path / 'complete-backup'
    checksum = create_backup(app, backup, include_generated=True)
    manifest = _manifest(backup)
    assert manifest['version'] == 5
    assert set(manifest) == BASE_FIELDS | {'sidecars', 'state', 'generated_audio_included', 'pending'}
    assert {entry['filename'] for entry in manifest['sidecars']} == set(fixture.sidecars)
    assert manifest['pending']['files'] and manifest['pending']['sources']
    assert any(entry['filename'].endswith('.wav') for entry in manifest['state'])
    source_bytes = {path.relative_to(backup): path.read_bytes() for path in backup.rglob('*') if path.is_file()}
    fixture.original.unlink()
    shutil.rmtree(exports)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum))
    for name, raw in fixture.sidecars.items():
        assert (restored.root / name).read_bytes() == raw
    assert validate_keymap((restored.root / 'keyboard-shortcuts.json').read_bytes()) == {'reader.next': 'Ctrl+Alt+N'}
    reading = ReadingState(restored.root)
    assert reading.resume(fixture.workspace, fixture.book['edition']) == fixture.page.location
    assert reading.bookmarks(fixture.workspace, fixture.book['edition']) == (fixture.bookmark,)
    saved, = restored.evidence(fixture.workspace, fixture.topic)
    assert saved == fixture.evidence
    recovered = EvidenceDrafts(restored.root).get(*fixture.draft.key)
    assert recovered == fixture.draft and recovered.matches(saved)
    _assert_resume(restored, fixture)

    def no_duplicate_speech(*args, **kwargs):
        pytest.fail('Restored accepted speech must be reused, not synthesized a second time.')
    monkeypatch.setattr(voices, 'synthesize', no_duplicate_speech)
    new_exports = tmp_path / 'new-exports'
    new_exports.mkdir()
    reopened = ProductionService(restored.root / 'state/studio', voices=voices)
    new_job = reopened.submit_reexport(job, new_exports)
    final = reopened.run(new_job)
    assert final['status'] == 'complete'
    assert sha256((Path(final['destination']) / 'master.wav').read_bytes()).hexdigest() == original_audio_sha
    assert reopened.get(job)['destination'] == first['destination']
    assert {path.relative_to(backup): path.read_bytes() for path in backup.rglob('*') if path.is_file()} == source_bytes


@pytest.mark.parametrize('layout', ['sidecars', 'pending'])
def test_both_actual_legacy_v4_shapes_restore_exactly(pending_library, tmp_path, layout):
    fixture, backup = pending_library, tmp_path / 'backup'
    create_backup(fixture.app, backup)
    manifest = _manifest(backup)
    manifest['version'] = 4
    if layout == 'sidecars':
        del manifest['pending']
        shutil.rmtree(backup / 'pending')
        assert set(manifest) == BASE_FIELDS | {'sidecars', 'state', 'generated_audio_included'}
    else:
        # The historical pending writer always had a state list/directory, even
        # when no state existed. It had no sidecar fields or root sidecar files.
        for item in manifest.pop('sidecars'):
            (backup / item['filename']).unlink()
        manifest['state'] = []
        (backup / 'state').mkdir()
        assert set(manifest) == BASE_FIELDS | {'pending', 'state', 'generated_audio_included'}
    checksum = _seal(backup, manifest)
    fixture.original.unlink()
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum))
    assert restored.evidence(fixture.workspace, fixture.topic) == (fixture.evidence,)
    if layout == 'sidecars':
        assert not (restored.root / 'imports.sqlite').exists()
        for name, raw in fixture.sidecars.items():
            assert (restored.root / name).read_bytes() == raw
    else:
        assert not (restored.root / 'reading-state.json').exists()
        assert (restored.root / 'state').is_dir()
        _assert_resume(restored, fixture)


@pytest.mark.parametrize('damage', ['mixed-v4', 'v5-missing-sidecars', 'v5-missing-state',
    'v5-missing-generated_audio_included', 'v5-missing-pending', 'v4-pending-null-state'])
def test_union_never_guesses_an_incomplete_or_mixed_schema(pending_library, tmp_path, monkeypatch, damage):
    backup = tmp_path / 'backup'
    create_backup(pending_library.app, backup)
    manifest = _manifest(backup)
    if damage == 'mixed-v4':
        manifest['version'] = 4
    elif damage == 'v4-pending-null-state':
        manifest['version'] = 4
        for item in manifest.pop('sidecars'):
            (backup / item['filename']).unlink()
    else:
        del manifest[damage.removeprefix('v5-missing-')]
    checksum = _seal(backup, manifest)
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid outer manifest reached writable catalog storage.')
    monkeypatch.setattr('carr_library.library_backup.Storage', forbidden)
    with pytest.raises(ValueError, match='unsupported|ambiguous|incomplete'):
        restore_backup(backup, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('damage', ['missing-component', 'corrupt-file', 'unlisted-file'])
def test_pending_component_damage_cannot_publish_a_library(pending_library, tmp_path, damage):
    backup = tmp_path / 'backup'
    checksum = create_backup(pending_library.app, backup)
    if damage == 'missing-component':
        shutil.rmtree(backup / 'pending')
    elif damage == 'corrupt-file':
        (backup / 'pending/imports.sqlite').write_bytes(b'corrupted journal')
    else:
        (backup / 'pending/unlisted.json').write_bytes(b'{}')
    with pytest.raises(ValueError):
        restore_backup(backup, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists()


def test_pending_copy_cancellation_stays_before_destination_publication(pending_library, tmp_path):
    backup = tmp_path / 'backup'
    checksum = create_backup(pending_library.app, backup)
    def cancelled():
        return any((stage / '.pending-import-restore/imports.sqlite').exists()
                   for stage in tmp_path.glob('.carr-library-restore-*'))
    with pytest.raises(InterruptedError):
        restore_backup(backup, tmp_path / 'cancelled', checksum, cancelled=cancelled)
    assert not (tmp_path / 'cancelled').exists()
    assert not list(tmp_path.glob('.carr-library-restore-*'))


def test_pending_publication_interruption_retains_incomplete_marker(pending_library, tmp_path, monkeypatch):
    backup, restored = tmp_path / 'backup', tmp_path / 'restored'
    checksum = create_backup(pending_library.app, backup)
    publish = state_backup.publish_state
    def interrupted(source, target, **options):
        if Path(target) == restored / 'import-requests':
            raise OSError('simulated pending journal publication failure')
        return publish(source, target, **options)
    monkeypatch.setattr(state_backup, 'publish_state', interrupted)
    with pytest.raises(OSError, match='simulated'):
        restore_backup(backup, restored, checksum)
    assert (restored / INCOMPLETE).exists()
    with pytest.raises(ValueError, match='incomplete'):
        LibraryApp(restored)


def test_pending_reads_hashes_and_copies_use_portable_descriptor_identity(tmp_path, monkeypatch):
    source, copy = tmp_path / 'request.json', tmp_path / 'copy.json'
    source.write_bytes(b'{"synthetic":"unchanged file"}')
    actual_fstat = os.fstat
    def descriptor_identity(descriptor):
        info = actual_fstat(descriptor)
        fields = {name: getattr(info, name) for name in ('st_dev', 'st_ino', 'st_mode',
            'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
        fields.update(st_dev=info.st_dev + 100, st_ino=info.st_ino + 100)
        return SimpleNamespace(**fields)
    platform = {name: getattr(os, name) for name in ('O_RDONLY', 'O_NONBLOCK', 'O_NOFOLLOW',
        'O_BINARY', 'open', 'fdopen') if hasattr(os, name)}
    platform.update(name='nt', fstat=descriptor_identity)
    monkeypatch.setattr(state_backup, 'os', SimpleNamespace(**platform))
    raw = pending_backup._read(source, 1024)
    entry = pending_backup._entry(source, source.name, lambda: False)
    assert entry['sha256'] == sha256(raw).hexdigest()
    pending_backup._copy(source, copy, entry, lambda: False)
    assert copy.read_bytes() == raw
