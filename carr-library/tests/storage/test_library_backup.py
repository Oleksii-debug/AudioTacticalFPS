"""Application-level backup/restore with real files, research and corruption."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup, INCOMPLETE


@pytest.fixture
def populated(tmp_path):
    app = LibraryApp(tmp_path / 'data')
    a = app.create_workspace('Library A')
    b = app.create_workspace('Library B')
    collection = app.create_collection(a,'Selected sources')
    topic = app.create_topic(a,'History')
    text = 'Точна цитата. Railway archive.'
    app.add_text(a,'Shared title',text,authors=('Author A',),collections=(collection,))
    app.add_text(b,'Shared title',text,authors=('Author B',))
    hit, = app.search(a,'Точна',collection=collection)
    evidence = app.save_hit(a,topic,hit.ref,hit.revision,'Separate note')
    query = app.save_query(a,topic,'Exact citation','Точна')
    with app.write(a) as db:
        db.execute('INSERT INTO research_tag VALUES(?,?,?)',(a,'railway','Railway'))
        db.execute('INSERT INTO research_evidence_tag VALUES(?,?,?,?)',(a,topic,evidence,'railway'))
    with app.catalog_transaction(a) as catalog:
        catalog.remove(collection,hit.edition_id)
    app.archive_workspace(b)
    return app,a,b,topic,query


def test_full_backup_restores_independent_libraries_and_immutable_research(populated,tmp_path):
    app,a,b,topic,query = populated
    before = app.evidence(a,topic)[0]
    folder = tmp_path / 'backup'
    digest = create_backup(app,folder)
    assert digest == sha256((folder / 'manifest.json').read_bytes()).hexdigest()
    source_bytes = (folder / 'library.sqlite').read_bytes()
    target = restore_backup(folder,tmp_path / 'restored',digest)
    assert (folder / 'library.sqlite').read_bytes() == source_bytes
    restored = LibraryApp(target)
    assert [(w.id,w.archived) for w in restored.workspaces(include_archived=True)] == [(a,False),(b,True)]
    after = restored.evidence(a,topic)[0]
    assert after.anchor == before.anchor
    assert after.note == 'Separate note'
    assert len(restored.run_query(a,topic,query)) == 1
    assert len(restored.search(a,'Railway')) == 1
    with pytest.raises(ValueError,match='archived'):
        restored.search(b,'Railway')
    restored.restore_workspace(b)
    hit, = restored.search(b,'Railway')
    assert hit.author == 'Author B'
    with restored.read(a) as db:
        assert db.execute('SELECT count(*) FROM research_evidence_tag').fetchone()[0] == 1
    assert not (target / INCOMPLETE).exists()
    assert all(p.stat().st_nlink == 1 for p in (target / 'originals').iterdir())


def test_backup_copies_only_registered_assets(populated,tmp_path):
    app,*_ = populated
    orphan = b'Retained before an interrupted import.'
    app.assets.retain(orphan,'text/plain',sha256(orphan).hexdigest())
    target = tmp_path / 'backup'
    create_backup(app,target)
    assert len(tuple(app.assets_dir.iterdir())) == 2
    assert len(tuple((target / 'originals').iterdir())) == 1
    manifest = json.loads((target / 'manifest.json').read_text())
    assert len(manifest['assets']) == 1


@pytest.mark.parametrize('part',['database','asset','manifest','extra','missing'])
def test_corrupt_or_incomplete_backup_never_publishes_restore(populated,tmp_path,part):
    app,*_ = populated
    folder = tmp_path / 'backup'
    digest = create_backup(app,folder)
    if part == 'database':
        file = folder / 'library.sqlite'
        raw = bytearray(file.read_bytes())
        raw[-1] ^= 1
        file.write_bytes(raw)
    elif part == 'asset':
        file = next((folder / 'originals').iterdir())
        file.write_bytes(b'x' * file.stat().st_size)
    elif part == 'manifest':
        (folder / 'manifest.json').write_text('{}')
    elif part == 'extra':
        (folder / 'unexpected').write_text('unregistered data')
    else:
        next((folder / 'originals').iterdir()).unlink()
    destination = tmp_path / 'restored'
    with pytest.raises(ValueError):
        restore_backup(folder,destination,digest)
    assert not destination.exists()
    assert len(app.workspaces(include_archived=True)) == 2


def test_no_overwrite_and_no_nested_destination(populated,tmp_path):
    app,*_ = populated
    folder = tmp_path / 'backup'
    digest = create_backup(app,folder)
    with pytest.raises(FileExistsError):
        create_backup(app,folder)
    target = tmp_path / 'existing'
    target.mkdir()
    (target / 'keep.txt').write_text('Keep this file')
    with pytest.raises(FileExistsError):
        restore_backup(folder,target,digest)
    assert (target / 'keep.txt').read_text() == 'Keep this file'
    with pytest.raises(ValueError,match='outside'):
        restore_backup(folder,folder / 'nested',digest)
    with pytest.raises(ValueError,match='outside'):
        create_backup(app,app.root / 'nested')


def test_interrupted_publication_is_explicitly_unopenable(populated,tmp_path,monkeypatch):
    app,*_ = populated
    folder = tmp_path / 'backup'
    digest = create_backup(app,folder)
    destination = tmp_path / 'restored'
    original_link = os.link

    def interrupted(source,target,*args,**kwargs):
        if str(target).startswith(str(destination / 'originals')):
            raise OSError('simulated publication interruption')
        return original_link(source,target,*args,**kwargs)

    monkeypatch.setattr(os,'link',interrupted)
    with pytest.raises(OSError,match='interruption'):
        restore_backup(folder,destination,digest)
    assert (destination / INCOMPLETE).is_file()
    with pytest.raises(ValueError,match='incomplete'):
        LibraryApp(destination)
    assert len(app.workspaces(include_archived=True)) == 2


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def _wav(path, frames=80):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        block = b'\0\0' * min(frames, 32000)
        remaining = frames
        while remaining:
            count = min(remaining, len(block) // 2)
            output.writeframesraw(block[:count * 2])
            remaining -= count


@pytest.fixture
def with_sidecars(populated):
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.reader.narration_text import NarrationDraft
    app, a, *_ = populated
    hit, = app.search(a, 'Точна')
    location = ReadingDesk(app, a, hit.edition_id).page(1).location
    state = ReadingState(app.root)
    state.remember(location)
    bookmark = state.add_bookmark(location, 'Повернутися до розділу')
    project = app.root / 'audio-projects' / ('1' * 32)
    payload = dict(kind='research-audio-studio', version=2, mode='LOCAL ONLY',
                   project_id=project.name, title='Мій проєкт', voice='fixture:local',
                   language='uk', input_kind='narration', output_dir=str(project / 'audio'),
                   source_reference=None, draft=NarrationDraft('Мій текст {unfinished').to_dict())
    saved = project / 'studio' / 'project.json'
    _json(saved, payload)
    _json(saved.with_name(saved.name + '.recovery.json'),
          dict(kind='audio-studio-recovery', version=1, project_id=project.name,
               base_sha256=sha256(saved.read_bytes()).hexdigest(), project=payload))
    _wav(project / 'audio' / 'assets' / 'recording.wav')
    _wav(project / 'audio' / 'assets' / 'unsaved-track.wav')
    _wav(project / 'audio' / 'cache' / ('a' * 64 + '.wav'))
    _json(project / 'audio' / 'cache' / ('a' * 64 + '.json'), {'source': 'synthetic fixture'})
    _json(project / 'audio' / 'render-journal.json', {'state': 'cancelled'})
    return app, project, location, bookmark


def test_v4_exact_reading_and_audio_drafts_roundtrip(with_sidecars, tmp_path):
    from carr_library.reader.reading_state import ReadingState
    app, project, location, bookmark = with_sidecars
    expected = {path.relative_to(app.root).as_posix(): path.read_bytes()
                for path in app.root.rglob('*') if path.is_file()
                and (path == app.root / 'reading-state.json' or project in path.parents)}
    # These files are not part of the defined project artifact namespaces.
    for relative in ('private.txt', 'profiles/token.json', 'models/voice.bin',
                     'credentials/key.json', 'speech-cache/unregistered.wav'):
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'not an included project artifact')
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_text())
    assert manifest['version'] == 4
    assert {entry['path'] for entry in manifest['sidecars']} == set(expected)
    assert all(entry['sha256'] == sha256(expected[entry['path']]).hexdigest()
               and entry['size_bytes'] == len(expected[entry['path']])
               for entry in manifest['sidecars'])
    target = restore_backup(folder, tmp_path / 'restored', digest)
    assert {name: (target / name).read_bytes() for name in expected} == expected
    assert all((target / name).stat().st_nlink == 1 for name in expected)
    reopened = ReadingState(target)
    assert reopened.resume(location.workspace, location.edition) == location
    assert reopened.bookmarks(location.workspace, location.edition) == (bookmark,)
    assert not (target / 'audio-projects' / project.name / 'profiles').exists()
    assert not tuple((target / 'audio-projects').rglob('*.lock'))
    assert len(LibraryApp(target).search(location.workspace, 'Точна')) == 1


@pytest.mark.parametrize('lock_name', ['.draft.lock', 'render'])
def test_backup_refuses_active_project_writers_before_database_snapshot(with_sidecars, tmp_path,
                                                                       monkeypatch, lock_name):
    from filelock import FileLock
    from carr_library.audio.jobs import project_lock
    from carr_library import library_backup
    app, project, *_ = with_sidecars
    lock = project_lock(project / 'audio') if lock_name == 'render' else FileLock(str(project / lock_name))
    monkeypatch.setattr(library_backup, 'Storage', lambda *a, **k: pytest.fail('opened database while project busy'))
    with lock:
        with pytest.raises(ValueError, match='Retry the backup'):
            create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_copy_holds_draft_narration_and_renderer_locks(with_sidecars, tmp_path, monkeypatch):
    from filelock import FileLock, Timeout
    from carr_library.audio.jobs import project_lock
    from carr_library import library_backup
    app, project, *_ = with_sidecars
    with project_lock(project / 'audio'):
        pass
    original = library_backup._copy_sidecar
    attempts = []
    def inspect(*args, **kwargs):
        for name in ('.draft.lock',):
            with pytest.raises(Timeout):
                with FileLock(str(project / name), timeout=0):
                    pytest.fail('writer entered a running backup')
        with pytest.raises(RuntimeError, match='render'):
            with project_lock(project / 'audio'):
                pytest.fail('renderer entered a running backup')
        attempts.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(library_backup, '_copy_sidecar', inspect)
    create_backup(app, tmp_path / 'backup')
    assert attempts


@pytest.mark.parametrize('change', ['replace', 'new_file', 'new_project'])
def test_noncooperating_changes_abort_before_publication(with_sidecars, tmp_path, monkeypatch, change):
    from carr_library import library_backup
    app, project, *_ = with_sidecars
    original = library_backup._copy_sidecar
    changed = False
    def mutate(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            changed = True
            if change == 'replace':
                _json(project / 'studio' / 'project.json', {'plan': 'changed'})
            elif change == 'new_file':
                _wav(project / 'audio' / 'assets' / 'new.wav')
            else:
                _json(project.parent / ('2' * 32) / 'studio' / 'project.json', {'plan': ''})
        return result
    monkeypatch.setattr(library_backup, '_copy_sidecar', mutate)
    with pytest.raises(ValueError, match='changed'):
        create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


@pytest.mark.parametrize('budget', [{'max_sidecar_files': 1}, {'max_sidecar_bytes': 1}])
def test_sidecar_budgets_fail_before_snapshot_and_restore(with_sidecars, tmp_path, monkeypatch, budget):
    from carr_library import library_backup
    app, *_ = with_sidecars
    digest = create_backup(app, tmp_path / 'backup')
    monkeypatch.setattr(library_backup, 'Storage', lambda *a, **k: pytest.fail('opened writable database before budgets'))
    with pytest.raises(ValueError, match='limit|budget|count'):
        create_backup(app, tmp_path / 'over-budget', **budget)
    with pytest.raises(ValueError, match='limit|budget|count'):
        restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest, **budget)
    assert not (tmp_path / 'over-budget').exists()
    assert not (tmp_path / 'restored').exists()


@pytest.mark.parametrize('damage', ['bytes', 'size', 'missing', 'extra', 'hardlink'])
def test_bad_sidecars_rejected_before_writable_storage(with_sidecars, tmp_path, monkeypatch, damage):
    from carr_library import library_backup
    app, *_ = with_sidecars
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    file = folder / 'sidecars' / 'reading-state.json'
    if damage == 'bytes':
        file.write_bytes(b'x' * file.stat().st_size)
    elif damage == 'size':
        with file.open('ab') as output:
            output.write(b'extra')
    elif damage == 'missing':
        file.unlink()
    elif damage == 'extra':
        (file.parent / 'unexpected.txt').write_text('not declared')
    else:
        os.link(file, tmp_path / 'alias')
    database = (folder / 'library.sqlite').read_bytes()
    monkeypatch.setattr(library_backup, 'Storage', lambda *a, **k: pytest.fail('opened writable database before sidecar verification'))
    with pytest.raises(ValueError):
        restore_backup(folder, tmp_path / 'restored', digest)
    assert not (tmp_path / 'restored').exists()
    assert (folder / 'library.sqlite').read_bytes() == database


@pytest.mark.parametrize('path', ['../outside.wav', '/tmp/outside.wav',
    'audio-projects/11111111111111111111111111111111/../outside.wav', 'audio-projects/11111111111111111111111111111111/profiles/private.wav',
    'audio-projects/11111111111111111111111111111111/CON.wav', 'audio-projects/11111111111111111111111111111111/a\\b.wav'])
def test_untrusted_manifest_cannot_publish_external_or_private_paths(with_sidecars, tmp_path, path):
    app, *_ = with_sidecars
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    manifest_path = folder / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['sidecars'][0]['path'] = path
    _json(manifest_path, manifest)
    digest = sha256(manifest_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        restore_backup(folder, tmp_path / 'restored', digest)
    assert not (tmp_path / 'restored').exists()


def test_windows_path_aliases_rejected_before_publication(with_sidecars, tmp_path):
    app, *_ = with_sidecars
    folder = tmp_path / 'backup'
    create_backup(app, folder)
    manifest_path = folder / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    entry = next(item for item in manifest['sidecars'] if item['path'].endswith('recording.wav'))
    manifest['sidecars'].append(dict(entry, path=entry['path'].replace('recording', 'RECORDING')))
    _json(manifest_path, manifest)
    digest = sha256(manifest_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='duplicate'):
        restore_backup(folder, tmp_path / 'restored', digest)
    assert not (tmp_path / 'restored').exists()


def test_external_hardlinked_audio_never_enters_backup(with_sidecars, tmp_path):
    app, project, *_ = with_sidecars
    os.link(project / 'audio' / 'assets' / 'recording.wav', tmp_path / 'outside.wav')
    with pytest.raises(ValueError, match='hard links'):
        create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_unicode_audio_aliases_cannot_merge_on_restore(with_sidecars, tmp_path):
    app, project, *_ = with_sidecars
    first, second = 'audio/assets/Cafe\u0301.wav', 'audio/assets/Caf\u00e9.wav'
    _wav(project / first)
    _wav(project / second)
    with pytest.raises(ValueError, match='conflicting'):
        create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


@pytest.mark.parametrize('restore', [False, True])
def test_linked_project_parent_is_never_traversed(with_sidecars, tmp_path, restore):
    app, project, *_ = with_sidecars
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder) if restore else None
    parent = folder / 'sidecars' / 'audio-projects' if restore else project / 'audio' / 'assets'
    external = tmp_path / 'external'
    parent.rename(external)
    if os.name == 'nt':
        # Directory junctions exercise real Windows reparse checks without
        # requiring the optional symbolic-link privilege.
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(parent), str(external)],
                       check=True, capture_output=True)
    else:
        parent.symlink_to(external, target_is_directory=True)
    before = {p.relative_to(external): p.read_bytes() for p in external.rglob('*') if p.is_file()}
    destination = tmp_path / 'restored' if restore else folder
    with pytest.raises(ValueError):
        if restore:
            restore_backup(folder, destination, digest)
        else:
            create_backup(app, destination)
    assert not destination.exists()
    assert {p.relative_to(external): p.read_bytes() for p in external.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('restore', [False, True])
def test_streamed_audio_copy_can_cancel_without_publication(with_sidecars, tmp_path, restore):
    app, project, *_ = with_sidecars
    _wav(project / 'audio' / 'assets' / 'long.wav', frames=2_000_000)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder) if restore else None
    operation = 'restore' if restore else 'backup'
    destination = tmp_path / 'restored' if restore else folder
    copied = []
    def cancelled():
        for stage in tmp_path.glob('.carr-library-' + operation + '-*'):
            file = stage / 'sidecars' / 'audio-projects' / project.name / 'audio' / 'assets' / 'long.wav'
            if file.exists() and file.stat().st_size >= 1024 * 1024:
                copied.append(file.stat().st_size)
                return True
        return False
    with pytest.raises(InterruptedError, match='cancelled'):
        if restore:
            restore_backup(folder, destination, digest, cancelled=cancelled)
        else:
            create_backup(app, destination, cancelled=cancelled)
    assert copied and max(copied) <= 2 * 1024 * 1024
    assert not destination.exists()
    assert not tuple(tmp_path.glob('.carr-library-' + operation + '-*'))


@pytest.mark.parametrize('restore', [False, True])
def test_sidecar_publication_interruption_keeps_completion_marker_absent(with_sidecars, tmp_path,
                                                                        monkeypatch, restore):
    app, *_ = with_sidecars
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder) if restore else None
    target = tmp_path / 'restored' if restore else folder
    original = os.link
    def fail_sidecar(source, destination, *args, **kwargs):
        if 'audio-projects' in Path(destination).parts:
            raise OSError('sidecar publication interrupted')
        return original(source, destination, *args, **kwargs)
    monkeypatch.setattr(os, 'link', fail_sidecar)
    with pytest.raises(OSError, match='interrupted'):
        if restore:
            restore_backup(folder, target, digest)
        else:
            create_backup(app, target)
    if restore:
        assert (target / INCOMPLETE).is_file()
        with pytest.raises(ValueError, match='incomplete'):
            LibraryApp(target)
    else:
        assert not (target / 'manifest.json').exists()
