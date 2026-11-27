"""Application-level backup/restore with real files, research and corruption."""
from hashlib import sha256
import json
import os
from pathlib import Path

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


@pytest.mark.parametrize('operation', ('backup', 'restore'))
def test_original_copy_cancels_mid_stream_without_publishing_destination(tmp_path, monkeypatch, operation):
    from threading import Event

    app = LibraryApp(tmp_path / 'data')
    workspace = app.create_workspace('Registered original before extraction')
    original_bytes = b'Original retained source.\n' * 131072
    digest = sha256(original_bytes).hexdigest()
    original = app.assets.retain(original_bytes, 'text/plain', digest)
    with app.catalog_transaction(workspace) as catalog:
        catalog.add_work('large-original', 'Retained original')
        catalog.add_edition('large-edition', 'large-original',
            dict(sha256=digest, mime='text/plain', size=len(original_bytes)))
    backup, destination = tmp_path / 'backup', tmp_path / 'destination'
    if operation == 'restore':
        manifest_digest = create_backup(app, backup)
        selected = backup / 'originals' / original.name
        action = lambda cancelled: restore_backup(backup, destination, manifest_digest, cancelled=cancelled)
    else:
        selected = original
        action = lambda cancelled: create_backup(app, destination, cancelled=cancelled)
    stopped, reads = Event(), []
    path_open = Path.open

    class ObservedSource:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def read(self, size):
            data = self.stream.read(size)
            reads.append((size, len(data)))
            stopped.set()
            return data

    def observe_source(path, mode='r', *args, **kwargs):
        stream = path_open(path, mode, *args, **kwargs)
        return ObservedSource(stream) if path == selected and mode == 'rb' else stream

    # Trigger cancellation after the real source stream has delivered a block,
    # rather than cancelling before entry or replacing the copy implementation.
    with monkeypatch.context() as context:
        context.setattr(Path, 'open', observe_source)
        with pytest.raises(InterruptedError, match='cancelled'):
            action(stopped.is_set)
    assert reads and all(0 < requested <= 1024 * 1024 for requested, _ in reads)
    assert 0 < sum(length for _, length in reads) < len(original_bytes)
    assert not destination.exists()
    assert not list(tmp_path.glob('.carr-library-backup-*'))
    assert not list(tmp_path.glob('.carr-library-restore-*'))
    assert original.read_bytes() == selected.read_bytes() == original_bytes
    assert original.stat().st_nlink == selected.stat().st_nlink == 1
