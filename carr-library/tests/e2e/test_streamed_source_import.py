from hashlib import sha256
import json
import os
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.citations.anchors import Identity, Locator
from carr_library.ingest.streamed_text import TextProfile, FRAGMENT_CHARS
from carr_library.ingest.worker import load_request


def test_explicit_streamed_import_uses_real_worker_receipt_and_independent_quote(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Streamed originals')
    source = tmp_path / 'Дослідження.txt'
    original = ('Exact railway\r\n' + 'a' * FRAGMENT_CHARS + '\fДруге джерело').encode('utf-8')
    source.write_bytes(original)
    result = app.import_file(workspace, source, source_profile=TextProfile(), title='Immutable source')
    assert result['state'] == 'complete' and result['isolated'] and result['verified']
    record = load_request(app.root, result['request_id'])
    assert record['version'] == 2 and record['source_profile'] == TextProfile().to_dict()
    with app.read(workspace) as db:
        receipt = db.execute('SELECT request_id,attempt FROM import_publication WHERE revision=?',
                             (result['id'],)).fetchone()
        assert tuple(receipt) == (result['request_id'], record['attempt'])
    hits = app.search(workspace, 'Exact railway')
    assert len(hits) == 1 and hits[0].verified
    quote = app.copy(workspace, hits[0].ref, result['id'], 'quote')
    assert 'Exact railway' in str(quote)
    assert source.read_bytes() == original
    source.unlink()
    reopened = LibraryApp(app.root)
    assert reopened.resume_import(workspace, result['request_id'])['state'] == 'complete'
    assert reopened.search(workspace, 'railway')[0].verified


def test_whole_original_proof_is_rechecked_after_same_size_late_change(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Retained integrity')
    source = tmp_path / 'book.txt'
    source.write_bytes(b'railway evidence\f' + b'x' * (2 * FRAGMENT_CHARS))
    result = app.import_file(workspace, source, source_profile=TextProfile())
    assert result['state'] == 'complete'
    hit = app.search(workspace, 'railway')[0]
    retained = app.assets_dir / (result['asset'] + '.txt')
    stamp = retained.stat()
    with retained.open('r+b') as output:
        output.seek(-1, os.SEEK_END)
        output.write(b'y')
    os.utime(retained, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    with pytest.raises(ValueError):
        app.copy(workspace, hit.ref, result['id'], 'quote')
    with pytest.raises(ValueError):
        app.search(workspace, 'railway')


def test_real_worker_cancellation_can_resume_from_owned_bytes_after_source_is_deleted(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Resume')
    source = tmp_path / 'book.txt'
    with source.open('wb') as output:
        for _ in range(256):
            output.write(b'resume railway ' + b'x' * (FRAGMENT_CHARS - 16) + b'\n')
    observed = 0
    def cancelled():
        nonlocal observed
        try:
            with sqlite3.connect((app.root / 'imports.sqlite').as_uri() + '?mode=ro', uri=True, timeout=0) as db:
                observed = db.execute('SELECT count(*) FROM checkpoint').fetchone()[0]
        except sqlite3.Error:
            return False
        return observed >= 2
    stopped = app.import_file(workspace, source, source_profile=TextProfile(), cancelled=cancelled)
    assert stopped['state'] == 'cancelled' and observed >= 2
    assert app.browse(workspace) == ()
    record = load_request(app.root, stopped['request_id'])
    assert record['job'] and record['asset_sha256']
    source.unlink()
    resumed = app.resume_import(workspace, stopped['request_id'])
    assert resumed['state'] == 'complete' and resumed['id'] == record['job']
    assert resumed['completed'] == resumed['total']


def test_profile_is_frozen_in_the_supervising_attempt(tmp_path, monkeypatch):
    from carr_library.ingest import worker
    from carr_library.ingest.publication_identity import PublicationAttempt, request_identity
    from carr_library.extract.page_ocr import Config
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Binding')
    source = tmp_path / 'book.txt'
    source.write_text('Original source', encoding='utf-8')
    request = worker.prepare_request(app.root, workspace, source, dict(title='Bound', authors=[], language=None,
        edition=None, volume=None, collections=[], work_id='work', edition_id='edition'), Config(),
        source_profile=TextProfile())
    attempt = PublicationAttempt('a' * 32, request_identity(request))
    changed = dict(request, source_profile=TextProfile(70 * 1024**2).to_dict())
    assert attempt.request()['source_profile'] != changed['source_profile']
    assert request_identity(changed) != request_identity(request)
    worker.record_progress(app.root, request['request_id'], attempt=attempt.attempt_id,
                           source_profile=changed['source_profile'])
    with pytest.raises(ValueError, match='attempt changed'):
        app._import_file(workspace, source, **request['metadata'], config=Config(),
            _request_id=request['request_id'], _source_profile=changed['source_profile'],
            _publication_attempt=attempt)
    assert app.browse(workspace) == ()


def test_actual_over_64_mib_source_imports_without_the_legacy_byte_api(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Actual large text')
    source = tmp_path / 'Large.txt'
    block = ('Capacity proof synthetic source. Резонанс.\r\n' * 512).encode('utf-8')
    hashed, total = sha256(), 0
    with source.open('wb') as output:
        first = b'Onlyneedle exact evidence\r\n'
        output.write(first)
        hashed.update(first)
        total += len(first)
        while total <= 64 * 1024**2:
            output.write(block)
            hashed.update(block)
            total += len(block)
    # The legacy call still refuses the same real file; explicit opt-in is
    # required. The child runs the installed/current package independently.
    with pytest.raises(ValueError, match='too large'):
        app.import_file(workspace, source)
    result = app.import_file(workspace, source, source_profile=TextProfile(65 * 1024**2), timeout=180)
    assert result['state'] == 'complete', result
    expected_characters = len(first.decode('utf-8')) + ((total - len(first)) // len(block)) * len(block.decode('utf-8'))
    assert result['asset'] == hashed.hexdigest()
    assert result['completed'] == (expected_characters + FRAGMENT_CHARS - 1) // FRAGMENT_CHARS > 500
    assert source.stat().st_size == total
    reloaded = LibraryApp(app.root)
    hits = reloaded.search_page(workspace, 'Onlyneedle exact evidence', limit=10, timeout=60).results
    assert len(hits) == 1 and hits[0].verified
    assert 'Onlyneedle exact evidence' in str(reloaded.copy(workspace, hits[0].ref, result['id'], 'quote'))
    from carr_library.reader.source_file import prepare_source_file, launch_source_file
    from carr_library.audio.jobs import sha256_file
    view = prepare_source_file(reloaded, workspace, hits[0].ref, result['id'])
    retained = reloaded.assets_dir / (result['asset'] + '.txt')
    assert not os.path.samefile(view.path, retained)
    assert view.path.stat().st_size == total and sha256_file(view.path) == result['asset']
    opened = []
    launch_source_file(reloaded, view, lambda path: opened.append(path) or True)
    assert opened == [str(view.path)]
    with view.path.open('r+b') as output:
        output.seek(-1, os.SEEK_END)
        output.write(b'X')
    replacement = prepare_source_file(reloaded, workspace, hits[0].ref, result['id'])
    assert replacement.path != view.path
    with view.path.open('rb') as original_view:
        original_view.seek(-1, os.SEEK_END)
        assert original_view.read(1) == b'X'
    assert sha256_file(replacement.path) == sha256_file(retained) == result['asset']



def test_pdf_and_streamed_request_allowances_remain_separate(tmp_path):
    from carr_library.extract.page_ocr import Config
    from carr_library.ingest.worker import prepare_request, _store
    from carr_library.ingest.publication_identity import request_identity
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Separate profiles')
    metadata = dict(title='Bound source', authors=[], language=None, edition=None,
                    volume=None, collections=[], work_id='work', edition_id='edition')
    source = tmp_path / 'unopened.txt'
    with pytest.raises(ValueError, match='allowance'):
        prepare_request(app.root, workspace, source, metadata, Config(),
                        source_profile=TextProfile(), pdf_max_bytes=128 * 1024**2)
    request = prepare_request(app.root, workspace, source, metadata, Config(),
                              source_profile=TextProfile(70 * 1024**2))
    assert request_identity(request) != request_identity(dict(request,
        source_profile=TextProfile(80 * 1024**2).to_dict()))
    mixed = dict(request, pdf_max_bytes=128 * 1024**2)
    with pytest.raises(ValueError, match='schema'):
        request_identity(mixed)
    _store(app.root, request['request_id'], mixed)
    with pytest.raises(ValueError, match='PDF byte allowance'):
        load_request(app.root, request['request_id'])


def test_sealed_profile_lookup_preserves_bounded_pdf_and_rejects_bare_large_text():
    from carr_library.ingest.streamed_text import catalog_profile
    from carr_library.catalog.streaming_pdf import MAX_PDF_BYTES
    with sqlite3.connect(':memory:') as db:
        # The existing PDF contract is independent of the streamed TXT tables.
        assert catalog_profile(db, 'a' * 64, 'application/pdf', 65 * 1024**2) is None
        with pytest.raises(ValueError, match='format-specific'):
            catalog_profile(db, 'a' * 64, 'application/pdf', MAX_PDF_BYTES + 1)
        db.execute('CREATE TABLE revision(id TEXT, asset TEXT, config TEXT)')
        db.execute('CREATE TABLE sealed(revision TEXT)')
        with pytest.raises(ValueError, match='sealed streamed-text'):
            catalog_profile(db, 'a' * 64, 'text/plain', 65 * 1024**2)
