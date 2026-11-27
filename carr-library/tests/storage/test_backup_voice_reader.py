"""Actual live-reader producers through common backup, verification and restore."""
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import engine_settings
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume, validate_sessions_bytes
from carr_library.speech.source import read_source_range, source_key
from carr_library.storage.revisions import Page, Storage


NAMES = ('audio-engine-settings.json', 'voice-reader-state.json')


def sidecar_bytes(root):
    return {name: (root / name).read_bytes() for name in NAMES}


def files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def replace_declared_sidecar(folder, name, raw):
    """Exercise semantic validation even with a self-consistent manifest."""
    (folder / name).write_bytes(raw)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    entry = next(item for item in manifest['sidecars'] if item['filename'] == name)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    content = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')) + '\n').encode('utf-8')
    (folder / 'manifest.json').write_bytes(content)
    return sha256(content).hexdigest()


@pytest.fixture
def voice_state(tmp_path):
    app = LibraryApp(tmp_path / 'Бібліотека čítanie')
    workspace = app.create_workspace('Exact reading source')
    other = app.create_workspace('Independent source library')
    text = 'Незмінне джерело e\u0301.\r\nУкраїна 🚂.\f\fSecond unchanged segment.'
    imported = app.add_text(workspace, 'Original', text,
                            edition_id='edition/source/slash', authors=('Автор',))
    foreign = app.add_text(other, 'Other original', text + ' Foreign ending.',
                           edition_id=imported['edition'])
    app.add_text(workspace, 'Other edition', text, edition_id='other/edition')
    source = read_source_range(ReadingDesk(app, workspace, imported['edition']), 1, 3)
    document = ReadingDocument(source.text, title=source.title, source_id=source.identity,
                               source_binding=source.binding, private_root=app.root)
    document.set_text('A separately edited spoken copy. Україна 🚂.')
    resume = ReaderResume(document, 'fixture:local', 2, 77, 4, 2.5, 'a' * 64, 'b' * 64)
    ReaderDraftStore(app.root, slot=source.identity).save(resume)
    manual = ReadingDocument('Manual original\r\nText', private_root=app.root)
    manual.set_text('Manual spoken\r\nText')
    manual_resume = ReaderResume(manual, 'fixture:manual', cursor=3)
    ReaderDraftStore(app.root).save(manual_resume)
    executable = tmp_path / 'converter.bin'
    executable.write_bytes(b'Synthetic selected local file; never executed.')
    settings = engine_settings.EngineSettings(ffmpeg=str(executable))
    engine_settings.save_settings(app.root, settings)
    # Backup must preserve declarative settings even if the original external
    # installation is no longer present; activation revalidates it separately.
    executable.unlink()
    return app, workspace, other, imported, foreign, source, resume, manual_resume, settings


def test_real_producers_roundtrip_exact_bytes_without_applying_engine_paths(voice_state, tmp_path, monkeypatch):
    app, workspace, _, imported, _, source, resume, manual, settings = voice_state
    before = sidecar_bytes(app.root)
    original = app.assets.resolve(imported['asset']).read_bytes()
    folder = tmp_path / 'Копія'
    def forbidden(*args, **kwargs):
        pytest.fail('Backup validation must not activate or dereference engine settings.')
    with monkeypatch.context() as patch:
        patch.setattr(engine_settings, '_verify_selected', forbidden)
        checksum = create_backup(app, folder)
        manifest = verify_backup(folder, checksum)
        snapshot = files(folder)
        target = restore_backup(folder, tmp_path / 'Відновлена', checksum)
    assert manifest['version'] == 4 and manifest['generated_audio_included'] is False
    assert manifest['sidecars'] == [dict(filename=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest())
                                    for name, raw in sorted(before.items())]
    assert sidecar_bytes(folder) == sidecar_bytes(target) == before
    assert files(folder) == snapshot
    assert all((folder / name).stat().st_nlink == 1 for name in NAMES)
    restored = LibraryApp(target)
    recovered = ReaderDraftStore(target, slot=source.identity).load()
    assert recovered.to_dict() == resume.to_dict()
    assert recovered.document.source_text == source.text
    assert recovered.document.undo() and recovered.document.text == source.text
    assert recovered.document.redo() and recovered.document.text == resume.document.text
    assert ReaderDraftStore(target).load().to_dict() == manual.to_dict()
    assert engine_settings.load_settings(target) == settings
    with pytest.raises((OSError, ValueError)):
        engine_settings.configured_registry(target)
    assert restored.assets.resolve(imported['asset']).read_bytes() == original
    assert ReadingDesk(restored, workspace, imported['edition']).page(2).raw_text == ''


@pytest.mark.parametrize('archived', [False, True])
def test_historical_and_archived_reading_state_is_preserved_without_retarget(voice_state, tmp_path, archived):
    app, workspace, _, imported, _, source, resume, _, _ = voice_state
    before = sidecar_bytes(app.root)
    app.update_metadata(workspace, imported['edition'], title='Later reviewed metadata',
                        authors=('Автор',), expected_metadata=app.edition_metadata(workspace, imported['edition']))
    if archived:
        app.archive_workspace(workspace)
    folder = tmp_path / 'historical'
    checksum = create_backup(app, folder)
    verify_backup(folder, checksum)
    target = restore_backup(folder, tmp_path / 'restored', checksum)
    restored = LibraryApp(target)
    assert sidecar_bytes(target) == before
    assert ReaderDraftStore(target, slot=source.identity).load().to_dict() == resume.to_dict()
    with restored.read() as db:
        current = db.execute('SELECT revision FROM active WHERE workspace=? AND edition=?',
                             (workspace, imported['edition'])).fetchone()[0]
        assert current != source.revision
        assert db.execute('SELECT 1 FROM sealed WHERE revision=?', (source.revision,)).fetchone()
    new_slot = source_key(workspace, imported['edition'], current, 1, 3)
    assert ReaderDraftStore(target, slot=new_slot).load() is None
    assert next(row for row in restored.workspaces(include_archived=True) if row.id == workspace).archived == archived
    if archived:
        with pytest.raises(ValueError, match='archived'):
            ReadingDesk(restored, workspace, imported['edition'])


@pytest.mark.parametrize('field', ['workspace', 'edition', 'revision', 'asset', 'text', 'ordinal', 'index', 'kind'])
def test_structurally_valid_foreign_source_is_rejected_by_all_backup_entrypoints(voice_state, tmp_path, field):
    app, _, other, imported, foreign, source, _, _, _ = voice_state
    folder = tmp_path / 'snapshot'
    create_backup(app, folder)
    binding, text = deepcopy(source.binding), source.text
    if field == 'workspace':
        binding['workspace'] = other
    elif field == 'edition':
        binding['edition'] = 'other/edition'
    elif field == 'revision':
        binding['revision'] = foreign['id']
    elif field == 'text':
        text = 'З' + text[1:]
        position = binding['positions'][0]
        position['text_sha256'] = sha256(text[position['start']:position['end']].encode('utf-8')).hexdigest()
    else:
        for position in binding['positions']:
            if field == 'asset':
                position['asset_sha256'] = foreign['asset']
            elif field in ('ordinal', 'index'):
                position[field] += 10
            else:
                position['kind'] = 'physical_page'
    identity = source_key(binding['workspace'], binding['edition'], binding['revision'],
                          binding['positions'][0]['ordinal'], binding['positions'][-1]['ordinal'])
    document = ReadingDocument(text, source_id=identity, source_binding=binding)
    ReaderDraftStore(app.root, slot=identity).save(ReaderResume(document))
    raw = (app.root / 'voice-reader-state.json').read_bytes()
    validate_sessions_bytes(raw)  # The actual producer accepts the shape and edit/source hashes.
    with pytest.raises(ValueError, match='exact saved source position'):
        create_backup(app, tmp_path / 'rejected-backup')
    assert not (tmp_path / 'rejected-backup').exists()
    checksum = replace_declared_sidecar(folder, 'voice-reader-state.json', raw)
    before = files(folder)
    with pytest.raises(ValueError, match='exact saved source position'):
        verify_backup(folder, checksum)
    with pytest.raises(ValueError, match='exact saved source position'):
        restore_backup(folder, tmp_path / 'rejected-restore', checksum)
    assert not (tmp_path / 'rejected-restore').exists() and files(folder) == before
    assert app.assets.resolve(imported['asset']).is_file()


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('mutation', ['same-size', 'missing'])
def test_corrupt_or_missing_voice_sidecar_never_verifies_or_restores(voice_state, tmp_path, name, mutation):
    app, *_ = voice_state
    folder = tmp_path / 'backup'
    checksum = create_backup(app, folder)
    path = folder / name
    if mutation == 'missing':
        path.unlink()
    else:
        raw = path.read_bytes()
        changed = raw.replace(b'fixture:local', b'fixture:other') if name.startswith('voice') else raw.replace(b'converter.bin', b'converted.bin')
        assert len(changed) == len(raw) and changed != raw
        path.write_bytes(changed)
    before = files(folder)
    with pytest.raises(ValueError, match='digest|missing'):
        verify_backup(folder, checksum)
    with pytest.raises(ValueError, match='digest|missing'):
        restore_backup(folder, tmp_path / 'rejected', checksum)
    assert not (tmp_path / 'rejected').exists() and files(folder) == before


@pytest.mark.parametrize('name', NAMES)
@pytest.mark.parametrize('mutation', ['unknown-field', 'boolean-version', 'byte-limit'])
def test_invalid_known_voice_schema_is_not_silently_omitted(voice_state, tmp_path, name, mutation):
    app, *_ = voice_state
    path = app.root / name
    value = json.loads(path.read_bytes())
    if mutation == 'byte-limit':
        maximum = 8 * 1024 * 1024 if name.startswith('voice') else 64 * 1024
        raw = b' ' * (maximum + 1)
    else:
        if mutation == 'boolean-version':
            value['version'] = True
        else:
            value['api_key'] = 'synthetic unrecognized field'
        raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists() and path.read_bytes() == raw


@pytest.mark.parametrize('wrong_ordinal', [False, True])
def test_physical_locator_and_reading_ordinal_are_independent_exact_bindings(tmp_path, wrong_ordinal):
    from reportlab.pdfgen.canvas import Canvas
    source_file = tmp_path / 'source.pdf'
    canvas = Canvas(str(source_file), invariant=1, pageCompression=0)
    for number in range(1, 10):
        canvas.drawString(50, 700, f'Synthetic physical page {number}.')
        canvas.showPage()
    canvas.save()
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Sparse physical source')
    imported = app.import_file(workspace, source_file, title='Exact PDF')
    assert imported['state'] == 'complete'
    with closing(Storage(app.database, schema_version=4)) as storage:
        with storage.transaction():
            rows = storage.db.execute('SELECT raw,physical FROM page WHERE revision=? AND physical IN (5,9) ORDER BY physical',
                                      (imported['id'],)).fetchall()
            storage.stage_revision(workspace, imported['edition'], 'voice-sparse', imported['asset'],
                                   'synthetic-extraction', [Page(row['raw'], row['physical']) for row in rows])
            storage.db.execute('INSERT INTO source_metadata SELECT workspace,edition,?,author,title,volume,edition_label '
                               'FROM source_metadata WHERE workspace=? AND edition=? AND revision=?',
                               ('voice-sparse', workspace, imported['edition'], imported['id']))
    source = read_source_range(ReadingDesk(app, workspace, imported['edition']), 1, 2)
    binding = deepcopy(source.binding)
    assert [(p['ordinal'], p['kind'], p['index']) for p in binding['positions']] == [
        (1, 'physical_page', 5), (2, 'physical_page', 9)]
    if wrong_ordinal:
        for position in binding['positions']:
            position['ordinal'] += 10
    identity = source_key(workspace, imported['edition'], source.revision,
                          binding['positions'][0]['ordinal'], binding['positions'][-1]['ordinal'])
    document = ReadingDocument(source.text, source_id=identity, source_binding=binding)
    resume = ReaderResume(document)
    ReaderDraftStore(app.root, slot=identity).save(resume)
    folder = tmp_path / 'backup'
    if wrong_ordinal:
        with pytest.raises(ValueError, match='exact saved source position'):
            create_backup(app, folder)
        assert not folder.exists()
    else:
        checksum = create_backup(app, folder)
        verify_backup(folder, checksum)
        target = restore_backup(folder, tmp_path / 'restored', checksum)
        assert ReaderDraftStore(target, slot=identity).load().to_dict() == resume.to_dict()
        assert LibraryApp(target).assets.resolve(imported['asset']).read_bytes() == source_file.read_bytes()
