"""Identifier security at the actual LibraryApp/SQLite boundary.

Only public-provider transfer bytes are synthetic. Authorization, immutable
preview binding, parsing, reference persistence and duplicate decisions are
production code; no endpoint is contacted and no source is uploaded.
"""
from dataclasses import replace
from hashlib import sha256
import json

import pytest

from carr_library.application import LibraryApp
from carr_library.acquisition.service import AcquisitionService
from carr_library.research.academic_library import AcademicLibrary


ISBN10 = '0306406152'
ISBN13 = '9780306406157'
OTHER_ISBN = '9780140328721'
X_ISBN10 = '080442957X'
X_ISBN13 = '9780804429573'
DOI = '10.5555/synthetic-identifier-security'
PRIVATE_NOTE = 'PRIVATE: зберегти мій аналіз і посилання на рукопис.'
AUTHOR_ORDER = ('Олена Автор', 'Synthetic Research Group')


def book_payload(isbn=ISBN13, **changes):
    equivalents = {ISBN10: ISBN13, ISBN13: ISBN10,
                    X_ISBN10: X_ISBN13, X_ISBN13: X_ISBN10}
    identifiers = {'isbn_' + str(len(isbn)): [isbn]}
    if isbn in equivalents:
        other = equivalents[isbn]
        identifiers['isbn_' + str(len(other))] = [other]
    record = {'title': 'Synthetic public textbook',
        'authors': [{'name': name} for name in AUTHOR_ORDER],
        'publish_date': '2026', 'publishers': [{'name': 'Synthetic Publisher'}],
        'identifiers': identifiers, 'url': 'https://openlibrary.org/books/OL123M'}
    record.update(changes)
    return json.dumps({'ISBN:' + isbn: record}, ensure_ascii=False).encode('utf-8')


def article_payload(**changes):
    record = {'doi': 'https://doi.org/' + DOI,
        'ids': {'doi': 'https://doi.org/' + DOI},
        'title': 'Synthetic exact DOI article', 'type': 'article',
        'publication_year': 2026,
        'authorships': [{'raw_author_name': name} for name in AUTHOR_ORDER]}
    record.update(changes)
    return json.dumps(record, ensure_ascii=False).encode('utf-8')


class Transfers:
    def __init__(self):
        self.permissions = []
        self.requests = []
        self.downloads = []
        self.after_response = lambda: None
        self.payloads = {'openlibrary': lambda request: book_payload(
            dict(request.query)['bibkeys'][5:]), 'openalex': article_payload()}

    def __call__(self, permission):
        self.permissions.append(permission)
        owner = self

        class Transfer:
            def __call__(self, request, *, cancelled):
                assert permission.allowed is True and permission.purpose == 'metadata'
                assert not cancelled()
                owner.requests.append(request)
                payload = owner.payloads[request.provider]
                raw = payload(request) if callable(payload) else payload
                owner.after_response()
                return raw

            def download(self, url, stream, *, cancelled):
                owner.downloads.append(url)
                raise AssertionError('This metadata-only test must never download a document.')
        return Transfer()


@pytest.fixture
def sample(tmp_path):
    app = LibraryApp(tmp_path / 'private-library')
    workspace = app.create_workspace('Identifier security')
    transfers = Transfers()
    app._acquisition_service = AcquisitionService(app, transport_factory=transfers)
    return app, workspace, transfers


def lookup(app, workspace, isbn=ISBN13):
    return app.lookup_isbn(workspace, isbn, online_allowed=True)


def reference_rows(app):
    # The test observer may read archived rows; application operations still use
    # their required active-workspace authorization and the real write boundary.
    with app.read() as db:
        return tuple(tuple(row) for row in db.execute(
            'SELECT * FROM academic_reference ORDER BY workspace,id'))


def references(app, workspace, *, archived=False):
    with app.read(workspace) as db:
        return AcademicLibrary(db, workspace).list(include_archived=archived)


def private_reference(app, workspace, isbn, **changes):
    fields = dict(title='My corrected title', authors=('My chosen author',),
        isbn=isbn, notes=PRIVATE_NOTE, reading_status='reading',
        tags=('Private research',), progress=45, priority='high')
    fields.update(changes)
    with app.write(workspace) as db:
        return AcademicLibrary(db, workspace).create(**fields)


@pytest.mark.parametrize('consent', [False, None, 1, 'true'])
def test_isbn_lookup_requires_literal_consent_before_any_transfer(sample, consent):
    app, workspace, transfers = sample
    with pytest.raises(PermissionError):
        app.lookup_isbn(workspace, ISBN13, online_allowed=consent)
    assert transfers.permissions == transfers.requests == []
    assert reference_rows(app) == ()


@pytest.mark.parametrize('isbn', ['', '9780306406158', '9780306406157,9780140328721'])
def test_invalid_or_multiple_isbns_fail_before_transfer(sample, isbn):
    app, workspace, transfers = sample
    with pytest.raises(ValueError):
        lookup(app, workspace, isbn)
    assert transfers.permissions == transfers.requests == []
    assert reference_rows(app) == ()


def test_exact_normalized_isbn_preview_has_no_write_and_transmits_only_public_identifier(sample):
    app, workspace, transfers = sample
    original = private_reference(app, workspace, OTHER_ISBN)
    before = reference_rows(app)
    preview = lookup(app, workspace, 'ISBN-13: 978-0-306-40615-7')
    assert preview.identifier_kind == 'isbn' and preview.identifier == ISBN13
    assert preview.doi == '' and preview.metadata.doi == ''
    assert preview.metadata.isbn == (ISBN13, ISBN10)
    assert preview.metadata.authors == AUTHOR_ORDER
    assert preview.metadata.raw_json == book_payload()
    assert preview.metadata.raw_sha256 == sha256(book_payload()).hexdigest()
    request, = transfers.requests
    assert request.endpoint == 'https://openlibrary.org/api/books'
    assert dict(request.query) == {'bibkeys': 'ISBN:' + ISBN13, 'format': 'json', 'jscmd': 'data'}
    assert PRIVATE_NOTE not in str((request.endpoint, request.query))
    assert original.title not in str((request.endpoint, request.query))
    assert reference_rows(app) == before
    assert not app.browse(workspace)
    cached = lookup(app, workspace)
    assert cached.cached and cached.token != preview.token
    assert len(transfers.requests) == 1
    with pytest.raises(PermissionError):
        app.lookup_isbn(workspace, ISBN13)  # Cached data cannot bypass consent.


@pytest.mark.parametrize(('stored', 'queried'), [
    (ISBN10, ISBN13), (ISBN13, ISBN10), (X_ISBN10, X_ISBN13), (X_ISBN13, X_ISBN10)])
def test_equivalent_isbn_dedup_preserves_all_existing_private_metadata(sample, stored, queried):
    app, workspace, transfers = sample
    original = private_reference(app, workspace, stored)
    before = reference_rows(app)
    preview = lookup(app, workspace, queried)
    assert preview.metadata.isbn[0] == queried
    saved = app.save_doi_reference(workspace, preview)
    assert saved == original
    assert saved.isbn == stored and saved.notes == PRIVATE_NOTE
    assert reference_rows(app) == before
    assert references(app, workspace) == (original,)


def test_different_isbns_with_blank_doi_do_not_collide(sample):
    app, workspace, transfers = sample
    first = app.save_doi_reference(workspace, lookup(app, workspace, ISBN13))
    second = app.save_doi_reference(workspace, lookup(app, workspace, OTHER_ISBN))
    assert first.doi == second.doi == ''
    assert first.id != second.id
    assert {row.isbn for row in references(app, workspace)} == {ISBN13, OTHER_ISBN}
    assert app.save_doi_reference(workspace, lookup(app, workspace, ISBN10)) == first


def test_ambiguous_equivalent_isbn_duplicates_require_manual_review_without_mutation(sample):
    app, workspace, transfers = sample
    private_reference(app, workspace, ISBN10)
    private_reference(app, workspace, ISBN13, title='Second existing record')
    before = reference_rows(app)
    preview = lookup(app, workspace)
    with pytest.raises(ValueError, match='[Dd]uplicate|[Mm]ore than one'):
        app.save_doi_reference(workspace, preview)
    assert reference_rows(app) == before


def test_archived_and_other_workspace_references_are_not_resurrected_or_reused(sample):
    app, workspace, transfers = sample
    archived = private_reference(app, workspace, ISBN10, archived=True)
    other = app.create_workspace('Other private library')
    unrelated = private_reference(app, other, ISBN13)
    saved = app.save_doi_reference(workspace, lookup(app, workspace))
    assert saved.id not in {archived.id, unrelated.id}
    assert saved.isbn == ISBN13 and saved.notes != PRIVATE_NOTE
    assert references(app, workspace) == (saved,)
    assert {row.id: row for row in references(app, workspace, archived=True)}[archived.id] == archived
    assert references(app, other) == (unrelated,)


@pytest.mark.parametrize('operation', ['save', 'apply', 'acquire'])
def test_cross_workspace_preview_replay_fails_before_write_or_download(sample, tmp_path, operation):
    app, workspace, transfers = sample
    preview = lookup(app, workspace)
    other = app.create_workspace('Unrelated library')
    source = app.add_text(other, 'Keep this title', 'Keep this exact source text.')
    expected = app.edition_metadata(other, source['edition'])
    before = reference_rows(app)
    destination = tmp_path / 'must-not-exist'
    with pytest.raises(ValueError, match='stale|another library'):
        if operation == 'save':
            app.save_doi_reference(other, preview)
        elif operation == 'apply':
            app.apply_doi_metadata(other, source['edition'], preview, expected_metadata=expected)
        else:
            app.acquire_doi_source(other, preview, 0, destination,
                online_allowed=True, rights_confirmed=True)
    assert reference_rows(app) == before
    assert app.edition_metadata(other, source['edition']) == expected
    assert not destination.exists() and transfers.downloads == []
    assert len(transfers.permissions) == 1


@pytest.mark.parametrize('field', ['identifier', 'metadata', 'identity_copy'])
def test_copied_or_forged_isbn_preview_token_cannot_save(sample, field):
    app, workspace, transfers = sample
    preview = lookup(app, workspace)
    changes = {'identifier': OTHER_ISBN} if field == 'identifier' else (
        {'metadata': replace(preview.metadata, title='FORGED')} if field == 'metadata' else {})
    forged = replace(preview, **changes)
    assert forged.token == preview.token and forged is not preview
    with pytest.raises(ValueError, match='stale'):
        app.save_doi_reference(workspace, forged)
    assert reference_rows(app) == ()
    assert app.save_doi_reference(workspace, preview).isbn == ISBN13


def test_isbn_preview_is_not_replayable_in_a_reopened_application(sample):
    app, workspace, transfers = sample
    preview = lookup(app, workspace)
    reopened = LibraryApp(app.root)
    with pytest.raises(ValueError, match='stale'):
        reopened.save_doi_reference(workspace, preview)
    assert reference_rows(app) == ()


def test_revoked_workspace_blocks_saved_preview_and_cached_lookup(sample):
    app, workspace, transfers = sample
    private_reference(app, workspace, OTHER_ISBN)
    preview = lookup(app, workspace)
    before = reference_rows(app)
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        app.save_doi_reference(workspace, preview)
    with pytest.raises(ValueError, match='archived'):
        lookup(app, workspace)
    assert len(transfers.requests) == 1
    assert reference_rows(app) == before


def test_workspace_revoked_during_transfer_does_not_publish_or_cache_preview(sample):
    app, workspace, transfers = sample
    transfers.after_response = lambda: app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        lookup(app, workspace)
    assert reference_rows(app) == ()
    app.restore_workspace(workspace)
    transfers.after_response = lambda: None
    preview = lookup(app, workspace)
    assert not preview.cached and len(transfers.requests) == 2


def test_cancelled_response_is_not_published_cached_or_saved(sample):
    app, workspace, transfers = sample
    state = {'cancelled': False}
    transfers.after_response = lambda: state.update(cancelled=True)
    with pytest.raises(InterruptedError):
        app.lookup_isbn(workspace, ISBN13, online_allowed=True,
            cancelled=lambda: state['cancelled'])
    assert reference_rows(app) == ()
    transfers.after_response = lambda: None
    preview = lookup(app, workspace)
    assert not preview.cached and len(transfers.requests) == 2
    with pytest.raises(InterruptedError):
        app.save_doi_reference(workspace, preview, cancelled=lambda: True)
    assert reference_rows(app) == ()


@pytest.mark.parametrize('payload', [
    book_payload(OTHER_ISBN),
    book_payload(ISBN13, identifiers={'isbn_13': [OTHER_ISBN]}),
    book_payload(ISBN13, identifiers={'isbn_10': [ISBN10]}),
    book_payload(ISBN13, identifiers={'isbn_13': [ISBN13, OTHER_ISBN]}),
    b'{"ISBN:9780306406157":',
], ids=['wrong-envelope', 'wrong-record', 'equivalent-only', 'ambiguous-record', 'malformed'])
def test_unbound_isbn_reply_cannot_mutate_or_poison_cache(sample, payload):
    app, workspace, transfers = sample
    private_reference(app, workspace, OTHER_ISBN)
    before = reference_rows(app)
    transfers.payloads['openlibrary'] = payload
    with pytest.raises(ValueError, match='exact identifier'):
        lookup(app, workspace)
    assert reference_rows(app) == before
    transfers.payloads['openlibrary'] = book_payload()
    preview = lookup(app, workspace)
    assert not preview.cached and len(transfers.requests) == 2
    assert reference_rows(app) == before


def test_full_preview_and_borrow_links_never_authorize_book_download(sample, tmp_path):
    app, workspace, transfers = sample
    transfers.payloads['openlibrary'] = book_payload(preview='full',
        ebooks=[{'preview': 'full', 'availability': 'full',
            'preview_url': 'https://openlibrary.org/borrow/synthetic.pdf'}],
        links=[{'title': 'Read PDF', 'url': 'https://archive.org/synthetic.pdf'}])
    preview = lookup(app, workspace)
    assert len(preview.sources) == 3
    assert all(source.reported_open_access is not True for source in preview.sources)
    for index in range(len(preview.sources)):
        destination = tmp_path / f'forbidden-book-{index}'
        with pytest.raises(ValueError, match='open-access PDF'):
            app.acquire_doi_source(workspace, preview, index, destination,
                online_allowed=True, rights_confirmed=True)
        assert not destination.exists()
    assert len(transfers.permissions) == 1 and transfers.downloads == []
    assert reference_rows(app) == () and not app.browse(workspace)


def test_openalex_different_doi_or_version_cannot_produce_preview_or_persist(sample):
    app, workspace, transfers = sample
    transfers.payloads['openalex'] = article_payload(doi='https://doi.org/' + DOI + '-v2')
    with pytest.raises(ValueError, match='exact identifier'):
        app.lookup_doi(workspace, DOI, online_allowed=True, providers=('openalex',))
    assert reference_rows(app) == ()
    transfers.payloads['openalex'] = article_payload()
    preview = app.lookup_doi(workspace, DOI, online_allowed=True, providers=('openalex',))
    assert preview.doi == DOI and preview.identifier_kind == 'doi'
    assert preview.metadata.authors == AUTHOR_ORDER
    assert preview.metadata.raw_json == article_payload()
    assert not preview.cached and len(transfers.requests) == 2
    request = transfers.requests[-1]
    assert request.endpoint == 'https://api.openalex.org/works/doi:10.5555%2Fsynthetic-identifier-security'
    assert request.query == ()
    saved = app.save_doi_reference(workspace, preview)
    assert saved.doi == DOI and saved.isbn == ''


@pytest.mark.parametrize('location_oa', [False, None])
def test_openalex_global_open_access_does_not_authorize_an_unverified_location(sample, tmp_path, location_oa):
    app, workspace, transfers = sample
    location = {'pdf_url': 'https://repository.example.org/copy.pdf',
        'is_oa': location_oa, 'version': 'acceptedVersion'}
    transfers.payloads['openalex'] = article_payload(open_access={'is_oa': True}, locations=[location])
    preview = app.lookup_doi(workspace, DOI, online_allowed=True, providers=('openalex',))
    assert len(preview.sources) == 1
    assert preview.sources[0].reported_open_access is location_oa
    destination = tmp_path / 'forbidden-article'
    with pytest.raises(ValueError, match='open-access PDF'):
        app.acquire_doi_source(workspace, preview, 0, destination,
            online_allowed=True, rights_confirmed=True)
    assert not destination.exists() and transfers.downloads == []
    assert len(transfers.permissions) == 1
    assert reference_rows(app) == ()
