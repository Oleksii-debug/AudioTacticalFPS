"""Actual scoped SQLite, retained PDF, source history and backup round trips.

Provider bytes and the transfer boundary are synthetic; parsing, author order,
scope authorization, native PDF import, references and backup are production.
No public endpoint or private source is contacted by this suite.
"""
from dataclasses import replace
import errno
from hashlib import sha256
import io
import json
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.acquisition.service import AcquisitionService
from carr_library.acquisition.transport import DownloadInfo
from carr_library.library_backup import restore_backup
from carr_library.research.academic_library import AcademicLibrary


DOI = '10.5555/synthetic-acquisition'
SOURCE = 'https://open.example.org/author-manuscript.pdf'


def crossref(**changes):
    record = {'DOI': DOI, 'title': ['Synthetic railway study'], 'type': 'journal-article',
        'author': [{'given': 'Olena', 'family': 'Shevchenko'}, {'name': 'Synthetic Academy'}],
        'published': {'date-parts': [[2026, 5, 3]]}, 'container-title': ['Synthetic Review'],
        'volume': '7', 'issue': '2', 'page': '101-115', 'publisher': 'Synthetic Publisher',
        'URL': 'https://doi.org/' + DOI}
    record.update(changes)
    return json.dumps({'status': 'ok', 'message-type': 'work', 'message': record}).encode()


def unpaywall(**changes):
    location = {'url': SOURCE, 'url_for_pdf': SOURCE,
        'url_for_landing_page': 'https://open.example.org/article',
        'license': 'cc-by', 'version': 'acceptedVersion', 'host_type': 'repository'}
    record = {'doi': DOI, 'title': 'Synthetic railway study', 'is_oa': True,
        'year': 2026, 'genre': 'journal-article', 'best_oa_location': location,
        'oa_locations': [location], 'z_authors': [{'given': 'Olena', 'family': 'Shevchenko'}]}
    record.update(changes)
    return json.dumps(record).encode()


def pdf_bytes():
    from reportlab.pdfgen.canvas import Canvas
    buffer = io.BytesIO()
    canvas = Canvas(buffer)
    canvas.drawString(72, 700, 'Synthetic public research preserves source wording.')
    canvas.showPage()
    canvas.save()
    return buffer.getvalue()


class Transfers:
    def __init__(self):
        self.requests = []
        self.downloads = []
        self.permissions = []
        self.payloads = {'crossref': crossref(), 'unpaywall': unpaywall()}
        self.pdf = None
        self.on_download = lambda: None

    def __call__(self, permission):
        self.permissions.append(permission)
        owner = self

        class Transfer:
            def __call__(self, request, *, cancelled):
                assert permission.allowed is True and permission.purpose == 'metadata'
                assert not cancelled()
                owner.requests.append(request)
                return owner.payloads[request.provider]

            def download(self, url, stream, *, cancelled):
                assert permission.allowed is True and permission.purpose == 'download'
                assert not cancelled()
                owner.downloads.append(url)
                data = owner.pdf if owner.pdf is not None else pdf_bytes()
                stream.write(data)
                owner.on_download()
                return DownloadInfo(url, sha256(data).hexdigest(), len(data), 'application/pdf')
        return Transfer()


@pytest.fixture
def sample(tmp_path):
    app = LibraryApp(tmp_path / 'private-library')
    workspace = app.create_workspace('Академічні джерела')
    transfers = Transfers()
    app._acquisition_service = AcquisitionService(app, transport_factory=transfers)
    return app, workspace, transfers


def lookup(app, workspace, *, oa=False):
    return app.lookup_doi(workspace, DOI, online_allowed=True,
        providers=('crossref', 'unpaywall') if oa else ('crossref',),
        unpaywall_email='synthetic-contact@example.org' if oa else '')


def references(app, workspace):
    with app.read(workspace) as db:
        return AcademicLibrary(db, workspace).list()


def test_lookup_requires_consent_and_never_reads_or_transmits_private_source(sample):
    app, workspace, transfers = sample
    app.add_text(workspace, 'PRIVATE TITLE', 'PRIVATE SOURCE THAT MUST NEVER LEAVE')
    with pytest.raises(PermissionError):
        app.lookup_doi(workspace, DOI)
    assert transfers.permissions == transfers.requests == []
    preview = lookup(app, workspace)
    assert preview.doi == DOI and preview.metadata.title == 'Synthetic railway study'
    assert references(app, workspace) == ()
    assert 'PRIVATE' not in repr(transfers.requests)
    assert len(transfers.requests) == 1
    assert not preview.cached
    cached = lookup(app, workspace)
    assert cached.cached and cached.token != preview.token
    assert len(transfers.requests) == 1
    with pytest.raises(PermissionError):
        app.lookup_doi(workspace, DOI, online_allowed=False)


def test_reference_saves_full_metadata_deduplicates_and_survives_backup(sample, tmp_path):
    app, workspace, transfers = sample
    preview = lookup(app, workspace)
    reference = app.save_doi_reference(workspace, preview)
    assert reference.doi == DOI and reference.item_type == 'article'
    assert reference.year == '2026' and reference.pages == '101-115'
    assert reference.volume == '7' and reference.issue == '2'
    assert 'Synthetic Academy' in reference.authors
    assert preview.metadata.raw_sha256 in reference.notes
    assert reference.edition_id is None and not app.browse(workspace)
    assert app.save_doi_reference(workspace, preview) == reference
    assert references(app, workspace) == (reference,)
    other = app.create_workspace('Other')
    assert references(app, other) == ()
    checksum = app.backup(tmp_path / 'backup')
    restore_backup(tmp_path / 'backup', tmp_path / 'restored', checksum)
    restored = LibraryApp(tmp_path / 'restored')
    assert references(restored, workspace) == (reference,)
    with pytest.raises(ValueError, match='stale'):
        restored.save_doi_reference(workspace, preview)


def test_stale_forged_other_library_and_archived_preview_cannot_write(sample):
    app, workspace, transfers = sample
    preview = lookup(app, workspace)
    other = app.create_workspace('Other')
    for target, candidate in ((other, preview), (workspace, replace(preview, doi='10.5555/forged')),
                               (workspace, replace(preview, metadata=replace(preview.metadata, title='FORGED')))):
        with pytest.raises(ValueError):
            app.save_doi_reference(target, candidate)
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        app.save_doi_reference(workspace, preview)
    assert references(app, other) == ()


def test_metadata_update_preserves_original_and_historical_evidence(sample):
    app, workspace, transfers = sample
    source = app.add_text(workspace, 'Original title', 'Exact authentic quotation.\r\nДжерело.',
        authors=('Original Author',), language='uk', edition='Original edition', volume='I')
    topic = app.create_topic(workspace, 'Evidence')
    hit, = app.search(workspace, 'authentic quotation')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Private note')
    before = app.evidence(workspace, topic)
    original = app.assets.resolve(source['asset']).read_bytes()
    current = app.edition_metadata(workspace, source['edition'])
    preview = lookup(app, workspace)
    app.apply_doi_metadata(workspace, source['edition'], preview, expected_metadata=current)
    after = app.edition_metadata(workspace, source['edition'])
    assert after.title == preview.metadata.title and after.volume == '7'
    assert after.language == 'uk' and after.edition == 'Original edition'
    assert app.assets.resolve(source['asset']).read_bytes() == original
    assert app.evidence(workspace, topic) == before
    assert 'Original title' in app.export_topic(workspace, topic)
    with pytest.raises(ValueError, match='changed'):
        app.apply_doi_metadata(workspace, source['edition'], preview, expected_metadata=current)


def test_cancelled_reference_save_rolls_back(sample):
    app, workspace, transfers = sample
    preview = lookup(app, workspace)
    with pytest.raises(InterruptedError):
        app.save_doi_reference(workspace, preview, cancelled=lambda: True)
    assert references(app, workspace) == ()


def test_download_needs_separate_permission_and_new_folder(sample, tmp_path):
    app, workspace, transfers = sample
    preview = lookup(app, workspace, oa=True)
    assert preview.sources
    for flags in ({}, {'online_allowed': True}, {'rights_confirmed': True},
                  {'online_allowed': 1, 'rights_confirmed': True}):
        with pytest.raises(PermissionError):
            app.acquire_doi_source(workspace, preview, 0, tmp_path / 'download', **flags)
    assert transfers.downloads == []
    target = tmp_path / 'existing'
    target.mkdir()
    (target / 'keep.txt').write_text('untouched')
    with pytest.raises(ValueError, match='new folder'):
        app.acquire_doi_source(workspace, preview, 0, target,
            online_allowed=True, rights_confirmed=True)
    assert transfers.downloads == [] and (target / 'keep.txt').read_text() == 'untouched'


def test_real_pdf_download_import_receipt_dedup_and_backup(sample, tmp_path):
    app, workspace, transfers = sample
    transfers.pdf = pdf_bytes()
    preview = lookup(app, workspace, oa=True)
    collection = app.create_collection(workspace, 'Downloaded papers')
    result = app.acquire_doi_source(workspace, preview, 0, tmp_path / 'Наукова стаття',
        online_allowed=True, rights_confirmed=True, collection=collection)
    assert result.state == 'complete', result.message
    assert result.path.read_bytes() == transfers.pdf
    assert result.path.stat().st_nlink == result.receipt_path.stat().st_nlink == 1
    assert not (result.path.parent / '.acquisition-incomplete').exists()
    receipt = json.loads(result.receipt_path.read_text(encoding='utf-8'))
    assert receipt['sha256'] == sha256(transfers.pdf).hexdigest() == result.sha256
    assert receipt['provider_license'] == 'cc-by'
    assert receipt['provider_version'] == 'acceptedVersion'
    assert receipt['source_text_verified'] is False
    assert 'synthetic-contact' not in result.receipt_path.read_text(encoding='utf-8')
    edition, = app.browse(workspace, collection=collection)
    assert edition.edition_id == result.edition_id
    assert app.assets.resolve(edition.asset).read_bytes() == transfers.pdf
    hit, = app.search(workspace, 'source wording')
    assert hit.verified is False  # DOI and a PDF never approve extracted text.
    reference, = references(app, workspace)
    assert reference.id == result.reference_id and reference.edition_id == edition.edition_id
    assert result.sha256 in reference.notes and 'cc-by' in reference.notes
    second_collection = app.create_collection(workspace, 'Same source another collection')
    duplicate = app.acquire_doi_source(workspace, preview, 0, tmp_path / 'Second receipt',
        online_allowed=True, rights_confirmed=True, collection=second_collection)
    assert duplicate.state == 'duplicate' and duplicate.edition_id == result.edition_id
    assert len(app.browse(workspace)) == len(references(app, workspace)) == 1
    assert app.browse(workspace, collection=second_collection)[0].edition_id == edition.edition_id
    checksum = app.backup(tmp_path / 'source-backup')
    restore_backup(tmp_path / 'source-backup', tmp_path / 'source-restored', checksum)
    restored = LibraryApp(tmp_path / 'source-restored')
    assert result.sha256 in references(restored, workspace)[0].notes
    assert restored.assets.resolve(edition.asset).read_bytes() == transfers.pdf


def test_cancellation_after_transfer_does_not_publish_or_import(sample, tmp_path):
    app, workspace, transfers = sample
    preview = lookup(app, workspace, oa=True)
    cancelled = [False]
    transfers.on_download = lambda: cancelled.__setitem__(0, True)
    target = tmp_path / 'cancelled'
    with pytest.raises(InterruptedError):
        app.acquire_doi_source(workspace, preview, 0, target, online_allowed=True,
            rights_confirmed=True, cancelled=lambda: cancelled[0])
    assert not target.exists() and not app.browse(workspace)
    assert references(app, workspace) == ()
    assert not tuple(tmp_path.glob('.carr-acquire-*'))


def test_revoked_scope_during_transfer_does_not_publish_or_import(sample, tmp_path):
    app, workspace, transfers = sample
    preview = lookup(app, workspace, oa=True)
    transfers.on_download = lambda: app.archive_workspace(workspace)
    target = tmp_path / 'revoked'
    with pytest.raises(ValueError, match='archived'):
        app.acquire_doi_source(workspace, preview, 0, target,
            online_allowed=True, rights_confirmed=True)
    assert not target.exists()


def test_failed_import_keeps_verified_download_with_truthful_partial_result(sample, tmp_path, monkeypatch):
    app, workspace, transfers = sample
    preview = lookup(app, workspace, oa=True)
    def fail(*args, **kwargs):
        raise ValueError('Synthetic parser failure')
    monkeypatch.setattr(app, 'import_file', fail)
    result = app.acquire_doi_source(workspace, preview, 0, tmp_path / 'recovery',
        online_allowed=True, rights_confirmed=True)
    assert result.state == 'downloaded' and result.edition_id is None
    assert result.path.is_file() and result.receipt_path.is_file()
    assert 'did not finish' in result.message and not app.browse(workspace)
    assert result.sha256 == sha256(result.path.read_bytes()).hexdigest()


def test_corrupt_retained_original_is_not_accepted_as_duplicate(sample, tmp_path):
    app, workspace, transfers = sample
    transfers.pdf = pdf_bytes()
    preview = lookup(app, workspace, oa=True)
    first = app.acquire_doi_source(workspace, preview, 0, tmp_path / 'first-source',
        online_allowed=True, rights_confirmed=True)
    assert first.state == 'complete'
    retained = app.assets.resolve(first.sha256)
    retained.write_bytes(b'CORRUPT RETAINED ORIGINAL')
    collection = app.create_collection(workspace, 'Must remain empty')
    second = app.acquire_doi_source(workspace, preview, 0, tmp_path / 'good-recovery-copy',
        online_allowed=True, rights_confirmed=True, collection=collection)
    assert second.state == 'downloaded' and second.edition_id is None
    assert second.path.read_bytes() == transfers.pdf
    with app.read(workspace) as db:
        assert not db.execute('SELECT 1 FROM catalog_membership WHERE workspace=? AND collection=?',
                              (workspace, collection)).fetchone()


def test_unverified_access_claim_cannot_download(sample, tmp_path):
    app, workspace, transfers = sample
    transfers.payloads['unpaywall'] = unpaywall(is_oa=False)
    preview = lookup(app, workspace, oa=True)
    if preview.sources:
        with pytest.raises(ValueError, match='open-access PDF'):
            app.acquire_doi_source(workspace, preview, 0, tmp_path / 'closed',
                online_allowed=True, rights_confirmed=True)
    assert not transfers.downloads


def test_download_publication_on_filesystem_without_hardlinks(sample, tmp_path, monkeypatch):
    from carr_library.acquisition import service
    app, workspace, transfers = sample
    preview = lookup(app, workspace, oa=True)
    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, 'Synthetic filesystem has no hard links')
    monkeypatch.setattr(service.os, 'link', unsupported)
    # RetainedAssets has a separate filesystem contract, so this case verifies
    # completed download publication and its truthful import-recovery result.
    monkeypatch.setattr(app, 'import_file', lambda *a, **k: {'state': 'quarantined'})
    result = app.acquire_doi_source(workspace, preview, 0, tmp_path / 'portable-destination',
        online_allowed=True, rights_confirmed=True)
    assert result.state == 'quarantined'
    assert result.sha256 == sha256(result.path.read_bytes()).hexdigest()
    assert result.receipt_path.is_file() and result.path.stat().st_nlink == 1
    assert not (result.path.parent / '.acquisition-incomplete').exists()
    assert not tuple(tmp_path.glob('.carr-acquire-*'))


def test_copy_publication_fallback_never_overwrites_and_cancels_cleanly(tmp_path, monkeypatch):
    from carr_library.acquisition import service
    source = tmp_path / 'source'
    source.write_bytes(b'A' * 200000)
    target = tmp_path / 'target'
    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, 'Synthetic filesystem has no hard links')
    monkeypatch.setattr(service.os, 'link', unsupported)
    target.write_bytes(b'EXISTING')
    with pytest.raises(FileExistsError):
        service._publish_file(source, target, lambda: False)
    assert target.read_bytes() == b'EXISTING'
    target.unlink()
    checks = [False, True]
    with pytest.raises(InterruptedError):
        service._publish_file(source, target, lambda: checks.pop(0))
    assert not target.exists() and source.read_bytes() == b'A' * 200000


def test_ambiguous_identical_editions_do_not_auto_link_or_assign(sample, tmp_path):
    app, workspace, transfers = sample
    transfers.pdf = pdf_bytes()
    original = tmp_path / 'same-original.pdf'
    original.write_bytes(transfers.pdf)
    for identity, title in (('a' * 32, 'Synthetic edition A'), ('b' * 32, 'Synthetic edition B')):
        imported = app.import_file(workspace, original, title=title,
            authors=('Synthetic Author',), edition_id=identity)
        assert imported['state'] == 'complete'
    before = app.browse(workspace)
    assert len(before) == 2
    assert {e.asset for e in before} == {sha256(transfers.pdf).hexdigest()}
    collection = app.create_collection(workspace, 'Requires explicit edition selection')
    preview = lookup(app, workspace, oa=True)
    result = app.acquire_doi_source(workspace, preview, 0, tmp_path / 'ambiguous-download',
        online_allowed=True, rights_confirmed=True, collection=collection)
    assert result.state == 'downloaded'
    assert result.edition_id is None and result.reference_id is None
    assert 'Multiple editions' in result.message
    assert result.path.read_bytes() == transfers.pdf
    assert json.loads(result.receipt_path.read_text(encoding='utf-8'))['sha256'] == result.sha256
    assert app.browse(workspace) == before
    assert not app.browse(workspace, collection=collection)
    assert references(app, workspace) == ()

def test_duplicate_match_changes_during_asset_verification(sample, tmp_path, monkeypatch):
    app, workspace, transfers = sample
    transfers.pdf = pdf_bytes()
    original = tmp_path / 'race-original.pdf'
    original.write_bytes(transfers.pdf)
    first = app.import_file(workspace, original, title='Synthetic first edition',
        edition_id='a' * 32)
    assert first['state'] == 'complete'
    collection = app.create_collection(workspace, 'Must remain empty after race')
    preview = lookup(app, workspace, oa=True)
    resolve = app.assets.resolve
    armed = True

    def add_competing_edition(digest):
        nonlocal armed
        verified = resolve(digest)
        if armed and digest == sha256(transfers.pdf).hexdigest():
            armed = False
            # Another app instance commits after the read snapshot, before
            # the acquisition publishes a reference or collection membership.
            competitor = LibraryApp(app.root)
            second = competitor.import_file(workspace, original,
                title='Synthetic second edition', edition_id='b' * 32)
            assert second['state'] == 'complete'
        return verified

    monkeypatch.setattr(app.assets, 'resolve', add_competing_edition)
    result = app.acquire_doi_source(workspace, preview, 0, tmp_path / 'race-download',
        online_allowed=True, rights_confirmed=True, collection=collection)
    assert not armed
    assert result.state == 'downloaded'
    assert result.edition_id is None and result.reference_id is None
    assert result.path.read_bytes() == transfers.pdf
    assert result.receipt_path.is_file()
    assert len(app.browse(workspace)) == 2
    assert not app.browse(workspace, collection=collection)
    assert references(app, workspace) == ()


def test_duplicate_membership_rolls_back_on_reference_failure(sample, tmp_path, monkeypatch):
    app, workspace, transfers = sample
    transfers.pdf = pdf_bytes()
    original = tmp_path / 'rollback-original.pdf'
    original.write_bytes(transfers.pdf)
    imported = app.import_file(workspace, original, title='Synthetic retained edition')
    assert imported['state'] == 'complete'
    collection = app.create_collection(workspace, 'Must remain empty on rollback')
    preview = lookup(app, workspace, oa=True)

    def fail(*args, **kwargs):
        raise ValueError('Synthetic reference update failure')

    monkeypatch.setattr(AcademicLibrary, 'update', fail)
    result = app.acquire_doi_source(workspace, preview, 0, tmp_path / 'rollback-download',
        online_allowed=True, rights_confirmed=True, collection=collection)
    assert result.state == 'downloaded' and result.edition_id is None
    assert result.path.read_bytes() == transfers.pdf and result.receipt_path.is_file()
    assert len(app.browse(workspace)) == 1
    assert not app.browse(workspace, collection=collection)
    assert references(app, workspace) == ()
