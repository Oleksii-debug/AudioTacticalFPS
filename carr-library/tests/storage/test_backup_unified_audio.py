"""Reading foundation plus canonical managed Audio Studio in one snapshot."""
from dataclasses import asdict
from hashlib import sha256
import json
import os
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.jobs import project_lock
from carr_library.extract.ocr_languages import load_presets, store_preset
from carr_library.extract.page_ocr import Config
from carr_library.keybindings import Command, KeybindingStore
from carr_library.library_backup import create_backup, restore_backup
from carr_library.reader.narration import NarrationStore, new_project, revise
from carr_library.reader.narration_text import NarrationDraft
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')
    path.write_bytes(raw)
    return sha256(raw).hexdigest()


def wav(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        output.writeframes(b'\0\0' * 80)


@pytest.fixture(params=[1, 2])
def combined(tmp_path, request):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Research')
    book = app.add_text(workspace, 'Source', 'Exact original source. Незмінний текст.')
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    ReadingState(app.root).remember(page.location)
    bookmark = ReadingState(app.root).add_bookmark(page.location, 'Повернутися')
    reading = new_project(page.raw_text, source_location=page.location)
    NarrationStore(app.root).save(reading)
    reading_draft = revise(reading, 'Explicit reading derivative.')
    NarrationStore(app.root).save(reading_draft, draft=True)
    KeybindingStore(app.root).save((Command('Library/Backup', 'Backup', 'Ctrl+F8'),),
                                 {'Library/Backup': 'Ctrl+Shift+F8'})
    store_preset(app.root, 'Ukrainian and English', Config(languages='ukr+eng'))
    identity = '7' * 32
    root = app.root / 'audio-projects' / identity
    (root / 'audio').mkdir(parents=True)
    reference = {'kind': 'library-source-reading-copy', 'workspace': workspace,
                 'edition': page.location.edition, 'revision': page.location.revision,
                 'pages': [{'location': asdict(page.location), 'source_start': 0,
                            'source_end': len(page.raw_text), 'verified': page.verified}],
                 'separator': 'form-feed between exact source pages or text segments'}
    draft = NarrationDraft(page.raw_text, source_id='library:' + workspace)
    payload = {'kind': 'research-audio-studio', 'version': 1, 'mode': 'LOCAL ONLY',
               'project_id': identity, 'title': 'Narration', 'voice': 'fixture:local',
               'language': 'uk', 'output_dir': str(root / 'audio'),
               'source_reference': reference, 'draft': draft.to_dict()}
    if request.param == 2:
        payload.update(version=2, input_kind='narration')
    saved_hash = save_json(root / 'studio' / 'project.json', payload)
    draft.edit_text('A separate spoken reading copy.')
    save_json(root / 'studio' / 'project.json.recovery.json',
              {'kind': 'audio-studio-recovery', 'version': 1, 'project_id': identity,
               'base_sha256': saved_hash, 'project': dict(payload, draft=draft.to_dict())})
    wav(root / 'audio' / 'assets' / 'effect.wav')
    cached = root / 'audio' / 'cache' / ('a' * 64 + '.wav')
    wav(cached)
    save_json(cached.with_suffix('.json'), {'sha256': sha256(cached.read_bytes()).hexdigest()})
    cached.with_suffix('.mid').write_bytes(b'MThd\0\0\0\x06\0\0\0\x01\0\x60')
    save_json(cached.with_suffix('.music.json'), {'source': 'synthetic procedural fixture'})
    published = root / 'audio' / 'renders' / ('b' * 64)
    published.mkdir(parents=True)
    # Actual production preserves cache bytes using internal hard links.
    os.link(cached, published / 'chapter-000001.wav')
    wav(published / 'master.wav')
    (published / 'subtitles.srt').write_bytes(b'')
    (published / 'chapters.ffmeta').write_text(';FFMETADATA1\n')
    save_json(published / 'complete.json', {'status': 'complete'})
    save_json(root / 'audio' / 'render-journal.json', {'state': 'complete'})
    save_json(root / 'audio' / ('plan-' + 'b' * 64 + '.json'), {'version': 1})
    # Historical bindings are retained after catalog metadata changes.
    app.update_metadata(workspace, book['edition'], title='Updated catalog title',
                        expected_metadata=app.edition_metadata(workspace, book['edition']))
    return app, root, page, bookmark, reading, reading_draft


def test_one_v5_snapshot_preserves_reader_editor_and_canonical_studio(combined, tmp_path):
    app, root, page, bookmark, reading, reading_draft = combined
    for name in ('models/private.bin', 'profiles/account.json', 'cache/quarantine/private.wav',
                 '.work-unsafe/private.wav', 'unrelated.txt'):
        path = root / 'audio' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'excluded')
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    assert manifest['version'] == 5
    entries = manifest['sidecars']
    names = {entry['path'] for entry in entries}
    assert {'reading-state.json', 'keybindings.json', 'ocr-language-presets.json',
            f'narration-projects/{reading.id}.json', f'narration-projects/{reading.id}.draft.json'} <= names
    prefix = 'audio-projects/' + root.name + '/'
    assert {prefix + 'studio/project.json', prefix + 'studio/project.json.recovery.json',
            prefix + 'audio/render-journal.json', prefix + 'audio/cache/' + 'a' * 64 + '.mid',
            prefix + 'audio/renders/' + 'b' * 64 + '/subtitles.srt'} <= names
    assert all(not any(part in name for part in ('/models/', '/profiles/', '/quarantine/', '.lock', '.work-'))
               for name in names)
    original = {name: (app.root / name).read_bytes() for name in names}
    restored = restore_backup(backup, tmp_path / 'restored', digest)
    assert {name: (restored / name).read_bytes() for name in names} == original
    assert all((restored / name).stat().st_nlink == 1 for name in names)
    state = ReadingState(restored)
    assert state.resume(page.location.workspace, page.location.edition) == page.location
    assert state.bookmarks(page.location.workspace, page.location.edition) == (bookmark,)
    assert NarrationStore(restored).load(reading.id)[0] == reading
    assert NarrationStore(restored).load(reading.id, draft=True)[0] == reading_draft
    assert KeybindingStore(restored).load() == {'Library/Backup': 'Ctrl+Shift+F8'}
    assert load_presets(restored) == load_presets(app.root)
    recovery = json.loads((restored / prefix / 'studio/project.json.recovery.json').read_bytes())
    assert recovery['base_sha256'] == sha256(original[prefix + 'studio/project.json']).hexdigest()
    assert NarrationDraft.from_dict(recovery['project']['draft']).text == 'A separate spoken reading copy.'
    from carr_library.ui.audio_studio import StudioController
    studio = StudioController(private_root=restored)
    studio.open(restored / prefix / 'studio/project.json')
    assert studio.output_dir == restored / prefix / 'audio'
    studio.recover()
    assert studio.script == 'A separate spoken reading copy.'
    assert studio.output_dir == restored / prefix / 'audio'
    assert {name: (restored / name).read_bytes() for name in names} == original


def test_canonical_render_lock_prevents_snapshot_before_database_access(combined, tmp_path, monkeypatch):
    from carr_library import library_backup
    app, root, *_ = combined
    monkeypatch.setattr(library_backup, 'Storage', lambda *a, **k: pytest.fail('database opened while rendering'))
    with project_lock(root / 'audio'):
        with pytest.raises(ValueError, match='Retry the backup'):
            create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_unaccounted_external_cache_hard_link_is_rejected(combined, tmp_path):
    app, root, *_ = combined
    os.link(root / 'audio' / 'cache' / ('a' * 64 + '.wav'), tmp_path / 'external.wav')
    with pytest.raises(ValueError, match='outside the verified project inventory'):
        create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


@pytest.mark.parametrize('damage', ['workspace', 'source_text', 'offset', 'recovery_source', 'project_id'])
def test_resigned_audio_metadata_cannot_retarget_historical_sources(combined, tmp_path, damage):
    app, root, *_ = combined
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    relative = 'audio-projects/' + root.name + '/studio/project.json'
    if damage == 'recovery_source':
        relative += '.recovery.json'
    path = backup / 'sidecars' / relative
    value = json.loads(path.read_bytes())
    payload = value['project'] if damage == 'recovery_source' else value
    reference = payload['source_reference']
    if damage == 'workspace':
        reference['workspace'] = 'missing-workspace'
        reference['pages'][0]['location']['workspace'] = 'missing-workspace'
    elif damage in ('source_text', 'recovery_source'):
        text = 'Forged original wording.'
        payload['draft'] = NarrationDraft(text).to_dict()
        reference['pages'][0]['source_end'] = len(text)
        reference['pages'][0]['location']['text_sha256'] = sha256(text.encode()).hexdigest()
    elif damage == 'offset':
        reference['pages'][0]['source_start'] = 1
    else:
        payload['project_id'] = '8' * 32
    file_hash = save_json(path, value)
    manifest_path = backup / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    entry = next(row for row in manifest['sidecars'] if row['path'] == relative)
    entry.update(size_bytes=path.stat().st_size, sha256=file_hash)
    digest = save_json(manifest_path, manifest)
    with pytest.raises(ValueError):
        restore_backup(backup, tmp_path / 'restored', digest)
    assert not (tmp_path / 'restored').exists()


def test_v3_does_not_guess_a_concurrent_unpublished_audio_schema(combined, tmp_path):
    app, *_ = combined
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    manifest_path = backup / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    manifest['version'] = 3
    digest = save_json(manifest_path, manifest)
    with pytest.raises(ValueError):
        restore_backup(backup, tmp_path / 'ambiguous', digest)
    assert not (tmp_path / 'ambiguous').exists()
