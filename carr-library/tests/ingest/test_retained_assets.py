from hashlib import sha256
import os
from pathlib import Path

import pytest

from carr_library.ingest.retained_assets import RetainedAssets, RetentionError


def retain(store, data=b'Original\r\ntext', mime='text/plain'):
    return store.retain(data, mime, sha256(data).hexdigest())


def test_duplicate_bytes_retained_once(tmp_path):
    store = RetainedAssets(tmp_path)
    data = 'Дослідження\r\n\fsecond'.encode()
    first = retain(store, data)
    assert first.read_bytes() == data
    assert retain(store, data) == first
    assert list(tmp_path.iterdir()) == [first]
    assert first.name == sha256(data).hexdigest() + '.txt'


def test_original_survives_external_source_replacement(tmp_path):
    root = tmp_path / 'private'
    root.mkdir()
    source = tmp_path / 'book.txt'
    source.write_bytes(b'Original')
    store = RetainedAssets(root)
    saved = retain(store, source.read_bytes())
    source.write_bytes(b'Replaced')
    assert store.resolve(saved.stem).read_bytes() == b'Original'
    source.unlink()
    assert store.resolve(saved.stem).read_bytes() == b'Original'


def test_corrupt_existing_file_never_overwritten(tmp_path):
    store = RetainedAssets(tmp_path)
    path = retain(store)
    path.write_bytes(b'corrupt')
    with pytest.raises(RetentionError, match='digest mismatch'):
        retain(store)
    assert path.read_bytes() == b'corrupt'


def test_bad_digest_and_type(tmp_path):
    store = RetainedAssets(tmp_path)
    with pytest.raises(RetentionError, match='digest mismatch'):
        store.retain(b'wrong', 'text/plain', '0' * 64)
    with pytest.raises(RetentionError, match='unsupported'):
        retain(store, mime='application/rtf')
    with pytest.raises(RetentionError, match='invalid asset digest'):
        store.resolve('../escape')
    assert not list(tmp_path.iterdir())


def test_missing_and_ambiguous_asset(tmp_path):
    store = RetainedAssets(tmp_path)
    with pytest.raises(RetentionError, match='missing or ambiguous'):
        store.resolve('0' * 64)
    path = retain(store)
    path.with_suffix('.pdf').write_bytes(path.read_bytes())
    with pytest.raises(RetentionError, match='missing or ambiguous'):
        store.resolve(path.stem)


def test_link_rejected(tmp_path):
    store = RetainedAssets(tmp_path)
    path = retain(store)
    other = tmp_path / 'external'
    os.link(path, other)
    with pytest.raises(RetentionError, match='invalid retained asset'):
        store.resolve(path.stem)
    assert other.read_bytes() == b'Original\r\ntext'


def test_same_digest_other_mime_rejected(tmp_path):
    store = RetainedAssets(tmp_path)
    path = retain(store)
    with pytest.raises(RetentionError, match='type mismatch'):
        retain(store, mime='application/pdf')
    assert path.read_bytes() == b'Original\r\ntext'


def test_no_clobber_on_publication_race(tmp_path, monkeypatch):
    store = RetainedAssets(tmp_path)
    original_link = os.link
    def raced(stage, target):
        Path(target).write_bytes(b'concurrent')
        original_link(stage, target)
    monkeypatch.setattr(os, 'link', raced)
    with pytest.raises(FileExistsError):
        retain(store)
    assert len(list(tmp_path.iterdir())) == 1
    assert next(tmp_path.iterdir()).read_bytes() == b'concurrent'


def test_run_checks_scope_before_asset_lookup(tmp_path):
    store = RetainedAssets(tmp_path)
    class Journal:
        def inspect(self, workspace, job):
            raise ValueError('unknown job in workspace')
    with pytest.raises(ValueError, match='unknown job in workspace'):
        store.run(Journal(), 'wrong-workspace', 'job')


def test_real_ingest_retained_roundtrip(tmp_path):
    """Requires the assembled catalog/extract/storage/ingest candidate tree."""
    from carr_library.ingest.writer_guard import open_ingest_session
    private = tmp_path / 'library'
    private.mkdir()
    assets = private / 'assets'
    assets.mkdir()
    source = tmp_path / 'book.txt'
    data = 'Дослідження railway\r\n\fSecond page'.encode()
    source.write_bytes(data)
    store = RetainedAssets(assets)
    db, journal = private / 'library.sqlite', private / 'jobs.sqlite'
    with open_ingest_session(private, db, journal) as ingest:
        a = store.start(ingest, 'A', 'edition', tmp_path, source.name)
        b = store.start(ingest, 'B', 'edition', tmp_path, source.name)
        assert a != b
        assert len(list(assets.iterdir())) == 1
        assert store.run(ingest, 'A', a, max_steps=1)['completed'] == 1
    source.unlink()
    with open_ingest_session(private, db, journal) as ingest:
        assert store.run(ingest, 'A', a)['state'] == 'complete'
        assert store.run(ingest, 'B', b)['state'] == 'complete'
        assert ingest.storage.get_page('A', a, 1)['raw'] == 'Дослідження railway\r\n'
        assert ingest.storage.get_page('B', a, 1) is None
        assert store.resolve(sha256(data).hexdigest()).read_bytes() == data
