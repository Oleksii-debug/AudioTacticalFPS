"""Finite-profile and actual source/parser contracts; no large-size qualification here."""
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
import json
import os

import pytest

from carr_library.application import LibraryApp
from carr_library.extract.page_ocr import Config
from carr_library.ingest.file_pdf import _BoundedReader, MAX_READ_BYTES, inspect_pdf, open_pdf
from carr_library.ingest.source_profiles import (
    LargeTextProfile, PdfProfile, profile_from_value, profile_from_config, is_large)
from carr_library.ingest.streamed_text import TextProfile, inspect_text, FRAGMENT_CHARS
from carr_library.ingest.worker import load_request, prepare_request
from carr_library.reader.legacy_reading import MAX_POSITIONS, MAX_SOURCE_ORDINAL, validate_anchor


def pdf(path, text='Exact original PDF evidence', *, visible=True):
    """Ordinary painted text, written by the installed pypdf producer."""
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
        DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT ' + (b'' if visible else b'3 Tr ') +
                    b'/F1 16 Tf 40 700 Td (' + text.encode('ascii') + b') Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    with path.open('wb') as output:
        writer.write(output)
    writer.close()
    return path


@pytest.mark.parametrize('kind', [LargeTextProfile, PdfProfile], ids=['txt', 'pdf'])
@pytest.mark.parametrize('value', [0, -1, True, 4 * 1024**3 + 1, 1.5, '4096'], ids=[
    'zero', 'negative', 'boolean', 'over-four-gib', 'float', 'text'])
def test_profile_refuses_unbounded_or_coerced_allowance(kind, value):
    with pytest.raises(ValueError):
        kind(value)


@pytest.mark.parametrize('profile', [LargeTextProfile(129 * 1024**2), PdfProfile(2 * 1024**3)],
                         ids=['large-txt', 'file-pdf'])
def test_explicit_profile_roundtrip_is_exact_and_old_shape_unchanged(profile):
    assert profile_from_value(profile.to_dict()) == profile and is_large(profile)
    legacy = TextProfile()
    assert not is_large(legacy)
    assert legacy.maximum_bytes == 128 * 1024**2 and legacy.maximum_fragments == 8192
    assert profile_from_value(legacy.to_dict()) == legacy
    for change in ({**profile.to_dict(), 'approved': True},
                   {**profile.to_dict(), 'format': 'docx'}):
        with pytest.raises(ValueError):
            profile_from_value(change)
    if isinstance(profile, LargeTextProfile):
        assert profile_from_config(profile.configuration()) == profile
        assert profile.configuration()['maximum_fragments'] == 65536
    else:
        settings = profile.configuration(Config(), {}, pypdf='pinned-test', pdfium='pinned-test')
        assert profile_from_config(settings) == profile
        with pytest.raises(ValueError):
            profile_from_config({**settings, 'config': {**asdict(Config()), 'shell': 'unsafe'}})


def test_absolute_source_ordinal_is_separate_from_selected_position_count():
    text = 'Only selected late source'
    anchor = dict(workspace='w', edition='e', revision='a' * 64, asset_sha256='b' * 64,
        quotation_authority=False, positions=[dict(ordinal=65536, kind='logical_segment',
            index=65536, start=0, end=len(text), text_sha256=sha256(text.encode()).hexdigest(),
            text_verified_at_capture=True)])
    assert MAX_SOURCE_ORDINAL == 65536 and MAX_POSITIONS == 10000
    assert validate_anchor(anchor, text).positions[0].ordinal == 65536
    wrong = json.loads(json.dumps(anchor))
    wrong['positions'][0]['ordinal'] = 65537
    with pytest.raises(ValueError):
        validate_anchor(wrong, text)


def test_request_version_and_profile_cannot_be_crossed(tmp_path):
    from carr_library.ingest.worker import record_progress
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Profile identity')
    source = tmp_path / 'book.txt'
    source.write_text('Original', encoding='utf-8')
    metadata = dict(title='Bound', authors=[], language=None, edition=None, volume=None,
                    collections=[], work_id='work', edition_id='edition')
    request = prepare_request(app.root, workspace, source, metadata, Config(),
                              source_profile=LargeTextProfile(1024**2))
    assert load_request(app.root, request['request_id'])['version'] == 3
    record_progress(app.root, request['request_id'], version=2)
    with pytest.raises(ValueError):
        load_request(app.root, request['request_id'])


@pytest.mark.parametrize('amount', [-1, MAX_READ_BYTES + 1, True], ids=['read-all', 'oversized', 'boolean'])
def test_pdf_parser_cannot_request_one_unbounded_read(amount):
    class Source:
        def read(self, _):
            pytest.fail('the parser request must be rejected before source allocation')
    with pytest.raises(ValueError):
        _BoundedReader(Source(), lambda: False).read(amount)


def test_pdf_reader_cancellation_and_zero_length_contract():
    source = BytesIO(b'first\nsecond')
    wrapper = _BoundedReader(source, lambda: False)
    assert wrapper.read(0) == wrapper.readline(0) == b'' and source.tell() == 0
    assert wrapper.readline(1) == b'f' and source.tell() == 1
    assert wrapper.readline(2) == b'ir' and source.tell() == 3
    assert wrapper.readline() == b'st\n'
    wrapper.seek(0)
    assert wrapper.read(5) == b'first'
    with pytest.raises(InterruptedError):
        _BoundedReader(source, lambda: True).read(1)


def test_current_pdf_parsers_use_independent_held_cursors_and_original_hash(tmp_path):
    path = pdf(tmp_path / 'Український шлях.pdf')
    profile = PdfProfile(1024**2)
    plan = inspect_pdf(path, profile)
    with open_pdf(path, profile, expected_digest=plan.digest, expected_identity=plan.identity) as (reader, doc, held):
        assert held.digest == sha256(path.read_bytes()).hexdigest()
        assert 'Exact original PDF evidence' in reader.pages[0].extract_text()
        with closing(doc[0]) as page:
            with closing(page.get_textpage()) as text:
                assert 'Exact original PDF evidence' in text.get_text_range()
        assert len(reader.pages) == len(doc) == 1
    path.rename(tmp_path / 'released.pdf')


def test_pdf_complete_hash_rejects_same_size_changed_source(tmp_path):
    path = pdf(tmp_path / 'source.pdf')
    profile = PdfProfile(1024**2)
    plan = inspect_pdf(path, profile)
    stamp = path.stat()
    data = path.read_bytes().replace(b'Exact original', b'Other original')
    assert len(data) == plan.size
    path.write_bytes(data)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    with pytest.raises(ValueError, match='checksum|identity'):
        with open_pdf(path, profile, expected_digest=plan.digest):
            pytest.fail('changed bytes must not reach a parser consumer')


def test_actual_owned_large_txt_profile_keeps_exact_crlf_and_frozen_identity(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Large text profile')
    source = tmp_path / 'Джерело.txt'
    original = ('Onlyprofileproof exact source\r\n' + '\\' * FRAGMENT_CHARS + '\fTail source').encode()
    source.write_bytes(original)
    result = app.import_file(workspace, source, source_profile=LargeTextProfile(1024**2), timeout=90)
    assert result['state'] == 'complete' and result['isolated'] and result['verified'], result
    request = load_request(app.root, result['request_id'])
    assert request['version'] == 3 and request['source_profile'] == LargeTextProfile(1024**2).to_dict()
    with app.read(workspace) as db:
        assert db.execute('SELECT attempt FROM import_publication WHERE request_id=?',
                          (result['request_id'],)).fetchone()[0] == request['attempt']
    hit = app.search(workspace, 'Onlyprofileproof exact source')[0]
    assert hit.verified and app.copy(workspace, hit.ref, hit.revision, 'quote') == 'Onlyprofileproof exact source'
    assert source.read_bytes() == original
    source.unlink()
    assert LibraryApp(app.root).resume_import(workspace, result['request_id'])['state'] == 'complete'


def test_actual_owned_file_pdf_preserves_review_authority(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('PDF profile')
    source = pdf(tmp_path / 'source.pdf')
    original = source.read_bytes()
    result = app.import_file(workspace, source, source_profile=PdfProfile(1024**2), timeout=90)
    assert result['state'] == 'complete' and result['isolated'] and not result['verified'], result
    hit = app.search(workspace, 'Exact original PDF evidence')[0]
    assert not hit.verified
    with pytest.raises(ValueError):
        app.copy(workspace, hit.ref, hit.revision, 'quote')
    app.review_pdf_page(workspace, hit.ref, hit.revision, 'Explicit test reviewer', confirmed=True)
    reviewed = app.search(workspace, 'Exact original PDF evidence')[0]
    assert reviewed.verified
    assert app.copy(workspace, reviewed.ref, reviewed.revision, 'quote') == 'Exact original PDF evidence'
    assert source.read_bytes() == original


def test_original_inspection_cancellation_does_not_enter_native_parser(tmp_path, monkeypatch):
    import pypdf
    path = pdf(tmp_path / 'source.pdf')
    monkeypatch.setattr(pypdf, 'PdfReader', lambda *_: pytest.fail('must cancel before native parser admission'))
    with pytest.raises(InterruptedError):
        with open_pdf(path, PdfProfile(1024**2), cancelled=lambda: True):
            pytest.fail('cancelled input was admitted')


def test_cancelled_large_pdf_review_does_not_launch_renderer_or_save_approval(tmp_path, monkeypatch):
    from carr_library.ingest import worker
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Pending PDF review')
    source = pdf(tmp_path / 'source.pdf', visible=False)
    result = app.import_file(workspace, source, source_profile=PdfProfile(1024**2), timeout=90)
    assert result['state'] == 'quarantined', result
    request_path = app.root / 'import-requests' / (result['request_id'] + '.json')
    before = request_path.read_bytes()
    monkeypatch.setattr(worker, '_invoke', lambda *args, **kwargs:
                        pytest.fail('cancelled review must not launch a renderer'))
    with pytest.raises(InterruptedError):
        app.pending_review(workspace, result['request_id'], cancelled=lambda: True)
    with pytest.raises(InterruptedError):
        app.confirm_import_review(workspace, result['request_id'], 'Reviewer',
            confirmed=True, expected_checksum='a' * 64, cancelled=lambda: True)
    assert request_path.read_bytes() == before
    assert not (app.root / 'import-requests' / 'reviews' / (result['request_id'] + '.json')).exists()
