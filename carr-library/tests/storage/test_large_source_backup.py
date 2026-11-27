"""Typed large-source backup admission, real copies, and exact resume."""
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.backup_capacity import capacity, LARGE_SOURCE_PROFILE
from carr_library.extract.page_ocr import Config
from carr_library.ingest.pipeline import canonical
from carr_library.ingest.source_profiles import LargeTextProfile, PdfProfile, inspect_source
from carr_library.ingest.streamed_ingest import StreamedTextIngest
from carr_library.ingest.retained_assets import RetainedAssets, RetentionError
from carr_library.ingest.worker import prepare_request, load_request, record_progress
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.pending_backup import validate_pending
from carr_library.storage.revisions import Storage, Page


def prepared(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Explicit large profile')
    source = tmp_path / 'Original.txt'
    source.write_bytes(b'Portableneedle exact evidence\r\n\fSecond field\fLast field')
    profile = LargeTextProfile(1024**2)
    metadata = dict(title='Exact original', authors=[], language='uk', edition=None, volume=None,
                    collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    request = prepare_request(app.root, workspace, source, metadata, Config(),
        expected_manifest=inspect_source(source, profile).manifest(), source_profile=profile)
    return app, workspace, source, request


def manifest(path):
    return json.loads((path / 'manifest.json').read_bytes())


def rewrite_manifest(path, value):
    raw = (canonical(value) + '\n').encode('utf-8')
    (path / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


@pytest.mark.parametrize('state', ['prepared', 'partial', 'complete'])
def test_large_v6_real_restore_resume_keeps_exact_owned_source(tmp_path, monkeypatch, state):
    app, workspace, source, request = prepared(tmp_path)
    if state == 'partial':
        original = StreamedTextIngest.run
        with monkeypatch.context() as patch:
            patch.setattr(StreamedTextIngest, 'run',
                lambda self, *args, **kwargs: original(self, *args, **dict(kwargs, max_steps=1)))
            initial = app._import_file(workspace, source, **request['metadata'],
                _request_id=request['request_id'], _source_profile=request['source_profile'],
                _expected_manifest=request['expected_manifest'])
        assert initial['state'] == 'pending' and initial['completed'] == 1
        record_progress(app.root, request['request_id'], state='cancelled', result=initial)
    elif state == 'complete':
        assert app.resume_import(workspace, request['request_id'])['state'] == 'complete'
    destination = tmp_path / 'complete backup'
    checksum = app.backup(destination, capacity_profile=LARGE_SOURCE_PROFILE)
    value = manifest(destination)
    assert value['version'] == 6 and value['capacity_profile'] == LARGE_SOURCE_PROFILE
    pending = value['pending']
    assert pending['version'] == 4 and pending['capacity_profile'] == LARGE_SOURCE_PROFILE
    assert pending['source_profiles'] == [
        dict(sha256=request['expected_manifest']['sha256'], profile=request['source_profile'])]
    verify_backup(destination, checksum)
    source.unlink()
    restored = LibraryApp(restore_backup(destination, tmp_path / 'restored', checksum))
    rebound = load_request(restored.root, request['request_id'])
    assert rebound['version'] == 3 and rebound['source_profile'] == request['source_profile']
    assert Path(rebound['source']).is_relative_to(restored.assets_dir)
    assert not any(key in rebound for key in ('attempt', 'publication_candidate', 'publication_watermark'))
    completed = restored.resume_import(workspace, request['request_id'])
    assert completed['state'] == 'complete' and completed['asset'] == request['expected_manifest']['sha256']
    hit = restored.search(workspace, 'Portableneedle exact evidence')[0]
    assert hit.verified and restored.copy(workspace, hit.ref, hit.revision, 'quote') == 'Portableneedle exact evidence'
    second = tmp_path / 'second backup'
    repeated = restored.backup(second, capacity_profile=LARGE_SOURCE_PROFILE)
    verify_backup(second, repeated)
    assert manifest(second)['assets'][0]['sha256'] == request['expected_manifest']['sha256']


def test_explicit_v6_with_no_pending_component_has_exact_empty_sidecar_layout(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    app.create_workspace('Empty selected library')
    target = tmp_path / 'backup'
    digest = app.backup(target, capacity_profile=LARGE_SOURCE_PROFILE)
    data = manifest(target)
    assert data['version'] == 6 and data['pending'] is None and data['sidecars'] == []
    assert (target / 'sidecars').is_dir()
    assert not (target / 'pending').exists()
    verify_backup(target, digest)
    restore_backup(target, tmp_path / 'restored', digest)


def test_default_backup_shapes_and_limits_are_not_widened(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    app.create_workspace('Ordinary library')
    plain = tmp_path / 'ordinary'
    app.backup(plain)
    assert manifest(plain)['version'] == 2
    assert 'capacity_profile' not in manifest(plain)
    assert capacity()['database'] == capacity()['journal'] == 512 * 1024**2
    assert capacity()['assets'] == 8 * 1024**3
    for value in ('large-source-v2', True, {}, ''):
        with pytest.raises(ValueError):
            app.backup(tmp_path / 'not-created', capacity_profile=value)
        assert not (tmp_path / 'not-created').exists()


def test_large_request_requires_its_explicit_backup_profile_even_when_fixture_is_small(tmp_path):
    app, _, _, _ = prepared(tmp_path)
    with pytest.raises(ValueError):
        app.backup(tmp_path / 'not-created')
    assert not (tmp_path / 'not-created').exists()


@pytest.mark.parametrize('change', ['legacy-version', 'unknown-profile', 'pending-profile',
                                    'missing-source', 'larger-source'], ids=[
    'legacy-version', 'unknown-profile', 'pending-profile', 'missing-source', 'larger-source'])
def test_rehashed_archive_cannot_invent_a_profile_or_cross_component_contract(tmp_path, change):
    app, _, _, _ = prepared(tmp_path)
    target = tmp_path / 'backup'
    app.backup(target, capacity_profile=LARGE_SOURCE_PROFILE)
    value = manifest(target)
    if change == 'legacy-version':
        value['version'] = 5
    elif change == 'unknown-profile':
        value['capacity_profile'] = 'large-source-v2'
    elif change == 'pending-profile':
        value['pending']['capacity_profile'] = 'large-source-v2'
    elif change == 'missing-source':
        value['pending']['source_profiles'] = []
    else:
        value['pending']['source_profiles'][0]['profile']['maximum_bytes'] *= 2
    with pytest.raises(ValueError):
        verify_backup(target, rewrite_manifest(target, value))
    assert not (tmp_path / 'restored').exists()


def test_snapshot_cancellation_after_sqlite_progress_keeps_no_final_database(tmp_path):
    with closing(Storage(tmp_path / 'source.sqlite')) as source:
        source.publish_revision('w', 'e', 'r', 'a' * 64, '{}',
                                (Page('actual source ' * 100000),))
        calls = 0
        def cancelled():
            nonlocal calls
            calls += 1
            return calls >= 3
        with pytest.raises(InterruptedError):
            source.backup(tmp_path / 'cancelled.sqlite', cancelled=cancelled, maximum_bytes=64 * 1024**2)
        assert calls >= 3 and not (tmp_path / 'cancelled.sqlite').exists()
        source.check()


def test_snapshot_selected_byte_limit_never_publishes_partial_database(tmp_path):
    with closing(Storage(tmp_path / 'source.sqlite')) as source:
        source.publish_revision('w', 'e', 'r', 'a' * 64, '{}', (Page('Original'),))
        with pytest.raises(ValueError, match='byte budget'):
            source.backup(tmp_path / 'oversized.sqlite', maximum_bytes=1)
        assert not (tmp_path / 'oversized.sqlite').exists()
        source.check()


def test_owned_retained_link_recovery_finishes_only_its_proven_twin(tmp_path):
    source = tmp_path / 'incoming.txt'
    raw = b'Exact retained source'
    source.write_bytes(raw)
    root = tmp_path / 'originals'
    root.mkdir()
    store = RetainedAssets(root)
    digest = sha256(raw).hexdigest()
    private = root / '.carr-original-abcdefgh'
    private.mkdir(mode=0o700)
    (private / 'original').write_bytes(raw)
    os.link(private / 'original', root / (digest + '.txt'))
    result = store.retain_file(source, 'text/plain', digest, len(raw), profile=LargeTextProfile(1024))
    assert result.read_bytes() == raw and result.stat().st_nlink == 1 and not private.exists()


def test_foreign_retained_alias_is_preserved_and_refused(tmp_path):
    source = tmp_path / 'incoming.txt'
    raw = b'Exact retained source'
    source.write_bytes(raw)
    root = tmp_path / 'originals'
    root.mkdir()
    digest = sha256(raw).hexdigest()
    target = root / (digest + '.txt')
    target.write_bytes(raw)
    foreign = tmp_path / 'foreign.txt'
    os.link(target, foreign)
    with pytest.raises(RetentionError, match='unaccounted'):
        RetainedAssets(root).retain_file(source, 'text/plain', digest, len(raw),
                                        profile=LargeTextProfile(1024))
    assert foreign.read_bytes() == target.read_bytes() == raw and target.stat().st_nlink == 2


def test_prepared_pdf_restores_exact_original_and_current_page_parser(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Portable PDF source')
    source = tmp_path / 'Original.pdf'
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
        DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    content = DecodedStreamObject()
    content.set_data(b'BT /F1 16 Tf 40 700 Td (Portable PDF proof) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(content)
    with source.open('wb') as output:
        writer.write(output)
    writer.close()
    profile = PdfProfile(1024**2)
    expected = inspect_source(source, profile).manifest()
    metadata = dict(title='PDF', authors=[], language=None, edition=None, volume=None,
                    collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    request = prepare_request(app.root, workspace, source, metadata, Config(),
                              source_profile=profile, expected_manifest=expected)
    backup = tmp_path / 'pdf backup'
    digest = app.backup(backup, capacity_profile=LARGE_SOURCE_PROFILE)
    source.unlink()
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored pdf', digest))
    result = restored.resume_import(workspace, request['request_id'])
    assert result['state'] == 'complete' and result['asset'] == expected['sha256'], result
    hit = restored.search(workspace, 'Portable PDF proof')[0]
    assert not hit.verified
    with pytest.raises(ValueError):
        restored.copy(workspace, hit.ref, hit.revision, 'quote')
    final = tmp_path / 'completed pdf backup'
    final_digest = restored.backup(final, capacity_profile=LARGE_SOURCE_PROFILE)
    verify_backup(final, final_digest)


def test_retained_recovery_streams_inventory_and_refuses_extra_stage_children(tmp_path, monkeypatch):
    source = tmp_path / 'incoming.txt'
    raw = b'Exact retained source'
    source.write_bytes(raw)
    root = tmp_path / 'originals'
    root.mkdir()
    digest = sha256(raw).hexdigest()
    private = root / '.carr-original-abcdefgh'
    private.mkdir(mode=0o700)
    (private / 'original').write_bytes(raw)
    (private / 'foreign').write_bytes(b'Do not remove')
    target = root / (digest + '.txt')
    os.link(private / 'original', target)
    def unbounded(_):
        pytest.fail('Recovery must use bounded scandir, never materialize Path.iterdir/listdir.')
    monkeypatch.setattr(Path, 'iterdir', unbounded)
    with pytest.raises(RetentionError, match='ambiguous'):
        RetainedAssets(root).retain_file(source, 'text/plain', digest, len(raw),
                                        profile=LargeTextProfile(1024))
    assert target.read_bytes() == raw
    assert (private / 'foreign').read_bytes() == b'Do not remove'
    (private / 'foreign').unlink()
    assert RetainedAssets(root).retain_file(source, 'text/plain', digest, len(raw),
        profile=LargeTextProfile(1024)).read_bytes() == raw
