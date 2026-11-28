"""Actual SQLite/source/job/restore paths with a synthetic provider transport.

No live provider calls, keys, acoustic output or translation quality are claimed.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import shutil
from time import time
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import canonical_gateway
from carr_library.assistant.models import canonical
from carr_library.assistant.profiles import ProfileStore, ProviderProfile
from carr_library.assistant.providers import Authorization, Gateway, HTTPResult, HTTPTransport, ProviderError
from carr_library.assistant.translation import TranslationService, translation_approval, ENDPOINT
from carr_library.assistant.library_translation import (
    LibrarySelection, capture_library_sources, validate_source_binding, verify_library_sources,
    authorize_library_binding,
)
from carr_library.library_backup import create_backup, restore_backup
from carr_library.speech.reading import ReadingDocument
from carr_library.assistant.state import read_json
from carr_library import translation_cli


@pytest.fixture
def setup(tmp_path, monkeypatch):
    calls, vault_calls = [], []
    class Vault:
        def get(self, identity, reference):
            vault_calls.append((identity, reference))
            return 'SYNTHETIC_SELECTED_TRANSLATION_SECRET'
    monkeypatch.setattr(canonical_gateway, 'WindowsCredentialVault', Vault)
    def transport(self, profile, route, body, headers, *, method='POST', **kwargs):
        calls.append((method, json.loads(body) if body is not None else None))
        if method == 'GET':
            payload = {'data': {'languages': [{'language': 'en', 'name': 'English'},
                                              {'language': 'uk', 'name': 'Ukrainian'}]}}
        else:
            payload = {'data': {'translations': [{
                'translatedText': 'Переклад: ' + json.loads(body)['q'], 'detectedSourceLanguage': 'en'}]}}
        return HTTPResult('application/json', json.dumps(payload, ensure_ascii=False).encode(), 200)
    monkeypatch.setattr(HTTPTransport, 'request', transport)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Selected originals')
    topic = app.create_topic(workspace, 'Authorized topic')
    profile = ProviderProfile('google_source', 'Synthetic source account', 'google-translation-v2', ENDPOINT,
        'nmt', capabilities=('translation.text', 'translation.languages'), enabled=True,
        credential_ref='carr/provider/google_source/' + 'b' * 32)
    ProfileStore(app.root / 'state/providers').save(profile)
    service = TranslationService(app.root / 'state/translation', library=app, workspace=workspace,
        gateway=Gateway(app.root / 'state/translation/provider', mode='APPROVED_REMOTE'))
    language = Authorization('source_languages', 'source_languages', ((profile.id, profile.endpoint),),
        ('translation.languages',), ('user-instruction',), '0', 1, time() + 600)
    service.refresh_languages(profile, authorization=language)
    original = 'First source section.\r\nExact spelling.\fУкраїнський розділ.\fThird chapter e\u0301.'
    book = app.add_text(workspace, 'An original book', original)
    return SimpleNamespace(app=app, workspace=workspace, topic=topic, profile=profile,
        service=service, book=book, original=original, calls=calls, vault_calls=vault_calls)


def capture(s, *, first=None, last=None, **options):
    return capture_library_sources(s.app, s.workspace,
        (LibrarySelection(s.book['edition'], first, last),), **options)


def submit(s, captured):
    approval, cost = translation_approval(s.profile, captured.text, 'auto', 'uk', '1',
                                          source_binding=captured.binding)
    identity = s.service.submit(s.profile, captured.text, 'auto', 'uk', title='Linked translation',
        authorization=approval, per_call_cost=cost, source_binding=captured.binding)
    return identity, approval


def test_chapter_capture_preserves_exact_source_positions_and_never_sends_text(setup):
    s = setup
    selected = capture(s, first=2, last=3, topic=s.topic)
    assert selected.text == 'Український розділ.\n\nThird chapter e\u0301.'
    row, = selected.binding['sources']
    assert row['selection'] == 'chapter' and (row['first'], row['last']) == (2, 3)
    assert row['anchor']['edition'] == s.book['edition']
    assert [p['ordinal'] for p in row['anchor']['positions']] == [2, 3]
    assert selected.binding['quotation_authority'] is False
    assert len(s.calls) == 1 and s.calls[0][0] == 'GET'
    assert s.app.assets.resolve(s.book['asset']).read_bytes() == s.original.encode('utf-8')


def test_explicit_batch_translation_keeps_originals_and_saved_quotation(setup, tmp_path):
    s = setup
    hit, = s.app.search(s.workspace, 'Exact spelling')
    s.app.save_hit(s.workspace, s.topic, hit.ref, hit.revision, 'Unchanged research note.')
    evidence = s.app.evidence(s.workspace, s.topic)
    second = s.app.add_text(s.workspace, 'Second exact source', 'Another original.\r\nFinal line.')
    selected = capture_library_sources(s.app, s.workspace,
        (LibrarySelection(s.book['edition']), LibrarySelection(second['edition'])), topic=s.topic)
    before = {asset: s.app.assets.resolve(asset).read_bytes() for asset in (s.book['asset'], second['asset'])}
    preview = s.service.preview(s.profile, selected.text, 'auto', 'uk', source_binding=selected.binding)
    assert preview['library_source']['documents'] == 2
    assert preview['library_source']['positions'] == 4
    identity, approval = submit(s, selected)
    assert approval.task_id.endswith(':source:' + selected.binding['sha256'])
    assert s.service.resume(identity)['status'] == 'complete'
    job = s.service.get(identity)
    assert job['source_binding'] == selected.binding
    assert ''.join(payload['q'] for method, payload in s.calls if method == 'POST') == selected.text
    target = tmp_path / 'translated'
    s.service.export(identity, target)
    report = read_json(target / 'translation-provenance.json')
    assert report['source_binding'] == selected.binding and report['quotation_authority'] is False
    assert (target / 'original.txt').read_bytes() == selected.text.encode('utf-8')
    assert b'ORIGINAL' in (target / 'bilingual.txt').read_bytes()
    assert not (target / 'INCOMPLETE.txt').exists()
    assert s.app.evidence(s.workspace, s.topic) == evidence
    assert s.app.copy(s.workspace, hit.ref, hit.revision, 'quote') == 'Exact spelling'
    assert {asset: s.app.assets.resolve(asset).read_bytes() for asset in before} == before


def test_identical_text_from_other_edition_cannot_retarget_approved_binding(setup, tmp_path):
    s = setup
    selected = capture(s)
    identity, _ = submit(s, selected)
    other = s.app.add_text(s.workspace, 'Another identity', s.original)
    replacement = capture_library_sources(s.app, s.workspace, (LibrarySelection(other['edition']),))
    assert replacement.text == selected.text and replacement.binding != selected.binding
    job = s.service.jobs.load(identity)
    job['source_binding'] = replacement.binding
    s.service.jobs.save(job)
    count = len(s.calls)
    with pytest.raises(ValueError, match='authority'):
        s.service.resume(identity)
    assert len(s.calls) == count
    assert s.service.jobs.load(identity)['receipts'] == []


def test_plain_text_approval_cannot_authorize_a_library_source_identity(setup):
    s = setup
    selected = capture(s)
    approval, cost = translation_approval(s.profile, selected.text, 'auto', 'uk', '1')
    with pytest.raises(PermissionError, match='translation-specific'):
        s.service.submit(s.profile, selected.text, 'auto', 'uk', authorization=approval,
                         per_call_cost=cost, source_binding=selected.binding)
    assert len(s.calls) == 1


@pytest.mark.parametrize('change', ['original', 'revision', 'workspace', 'topic'])
def test_changed_source_or_permission_blocks_transfer_before_credentials(setup, change):
    s = setup
    selected = capture(s, topic=s.topic)
    identity, _ = submit(s, selected)
    if change == 'original':
        path = s.app.assets.resolve(s.book['asset'])
        old = path.stat()
        path.write_bytes(b'X' * old.st_size)
    elif change == 'revision':
        s.app.update_metadata(s.workspace, s.book['edition'], title='Changed source metadata')
    elif change == 'workspace':
        s.app.archive_workspace(s.workspace)
    else:
        with s.app.topic_transaction(s.workspace) as tree:
            tree.archive(s.topic)
    before = len(s.calls), len(s.vault_calls)
    with pytest.raises((ValueError, PermissionError)):
        s.service.resume(identity)
    assert (len(s.calls), len(s.vault_calls)) == before
    assert s.service.jobs.load(identity)['receipts'] == []


def test_partial_batch_revocation_keeps_receipt_and_never_sends_next_chunk(setup):
    s = setup
    large = s.app.add_text(s.workspace, 'Long selected document', 'Exact bounded source. ' * 500)
    selected = capture_library_sources(s.app, s.workspace, (LibrarySelection(large['edition']),))
    identity, _ = submit(s, selected)
    def progress(done, total, phase):
        if done == 1:
            s.app.archive_workspace(s.workspace)
    with pytest.raises((ValueError, PermissionError)):
        s.service.resume(identity, progress=progress)
    job = s.service.jobs.load(identity)
    assert job['status'] == 'attention' and len(job['receipts']) == 1
    assert sum(method == 'POST' for method, _ in s.calls) == 1
    accepted = deepcopy(job['receipts'][0])
    s.app.restore_workspace(s.workspace)
    assert s.service.resume(identity)['status'] == 'complete'
    after = s.service.get(identity)
    assert after['receipts'][0] == accepted
    assert sum(method == 'POST' for method, _ in s.calls) == len(after['chunks'])


def test_preflight_cancel_saves_paused_without_any_new_provider_reservation(setup):
    s = setup
    identity, approval = submit(s, capture(s))
    before = len(s.calls), len(s.vault_calls)
    result = s.service.resume(identity, cancelled=lambda: True)
    assert result['status'] == 'paused' and result['completed'] == 0
    assert (len(s.calls), len(s.vault_calls)) == before
    assert not s.service.gateway.ledger._path(approval.id).exists()


def test_bound_job_requires_original_library_context_and_workspace(setup):
    s = setup
    identity, _ = submit(s, capture(s))
    unbound = TranslationService(s.service.directory, gateway=s.service.gateway)
    with pytest.raises(PermissionError, match='original library'):
        unbound.resume(identity)
    foreign = TranslationService(s.service.directory, gateway=s.service.gateway, library=s.app, workspace='other')
    with pytest.raises(PermissionError, match='another selected library'):
        foreign.get(identity)
    assert foreign.list() == ()
    assert len(s.calls) == 1


@pytest.mark.parametrize('first,last', [(True, 2), (0, 1), (2, 1), (1, None), (None, 2)])
def test_source_range_is_typed_and_bounded(first, last):
    with pytest.raises(ValueError):
        LibrarySelection('edition', first, last)


def test_batch_limit_and_duplicate_selection_precede_source_loading(setup):
    s = setup
    with pytest.raises(ValueError, match='between 1 and 16'):
        capture_library_sources(s.app, s.workspace, [LibrarySelection(str(index)) for index in range(17)])
    with pytest.raises(ValueError, match='each edition once'):
        capture_library_sources(s.app, s.workspace, [LibrarySelection(s.book['edition'])] * 2)
    assert len(s.calls) == 1


def test_changed_snapshot_position_hash_fails_even_after_outer_digest_resealed(setup):
    selected = capture(setup)
    value = deepcopy(selected.binding)
    value['sources'][0]['anchor']['positions'][0]['text_sha256'] = 'a' * 64
    value['sha256'] = sha256(canonical({k: v for k, v in value.items() if k != 'sha256'}).encode()).hexdigest()
    with pytest.raises(ValueError, match='checksum'):
        validate_source_binding(value, selected.text)


def test_bilingual_reader_uses_existing_document_and_keeps_original_lineage(setup):
    s = setup
    selected = capture(s, first=1, last=2)
    identity, _ = submit(s, selected)
    s.service.resume(identity)
    source = s.service.reading_source(identity, view='bilingual')
    document = source.reading_document()
    assert isinstance(document, ReadingDocument)
    assert document.source_text == selected.text
    assert document.source_anchor == selected.binding['sources'][0]['anchor']
    assert 'ORIGINAL\n' in document.text and 'TRANSLATION\n' in document.text
    assert document.source_span(0, len(document.text)).exact is False
    assert source.warnings and len(source.positions) == 2
    s.app.archive_workspace(s.workspace)
    with pytest.raises((PermissionError, ValueError)):
        authorize_library_binding(s.app, selected.binding)


def test_complete_backup_keeps_linkage_and_revokes_transmission_not_offline_reading(setup, tmp_path):
    s = setup
    selected = capture(s)
    identity, _ = submit(s, selected)
    s.service.resume(identity)
    before = s.service.get(identity)
    folder = tmp_path / 'backup'
    checksum = create_backup(s.app, folder)
    restored_root = restore_backup(folder, tmp_path / 'restored', checksum)
    restored = TranslationService(restored_root / 'state/translation',
        library=LibraryApp(restored_root), workspace=s.workspace)
    job = restored.get(identity)
    assert job['source_binding'] == before['source_binding']
    assert job['receipts'] == before['receipts'] and job['authorization'] == before['authorization']
    assert job['profile']['credential_ref'] is None and job['profile']['enabled'] is False
    assert job['original_profile_sha256'] == sha256(canonical(before['profile']).encode()).hexdigest()
    assert job['restore_requires_review'] is True
    count = len(s.calls), len(s.vault_calls)
    with pytest.raises(PermissionError, match='Restored translation'):
        restored.resume(identity)
    output = tmp_path / 'restored-export'
    restored.export(identity, output)
    assert restored.reading_document(identity, view='original').source_text == selected.text
    assert (output / 'original.txt').read_bytes() == selected.text.encode()
    assert (len(s.calls), len(s.vault_calls)) == count


def test_export_revalidates_after_staging_and_does_not_publish_stale_source(setup, tmp_path, monkeypatch):
    s = setup
    identity, _ = submit(s, capture(s))
    s.service.resume(identity)
    original_export = ReadingDocument.export
    def revoke_after_stage(document, path, **kwargs):
        result = original_export(document, path, **kwargs)
        if Path(path).name == 'original.txt':
            s.app.archive_workspace(s.workspace)
        return result
    monkeypatch.setattr(ReadingDocument, 'export', revoke_after_stage)
    output = tmp_path / 'must-not-publish'
    with pytest.raises((ValueError, PermissionError)):
        s.service.export(identity, output)
    assert not output.exists()
    assert not list(tmp_path.glob('.translation-*'))


def test_export_never_replaces_raced_empty_destination(setup, tmp_path, monkeypatch):
    s = setup
    identity, _ = submit(s, capture(s))
    s.service.resume(identity)
    output = tmp_path / 'raced'
    original_check = s.service._check_source
    checks, inode = [], []
    def race(job, **kwargs):
        original_check(job, **kwargs)
        checks.append(1)
        if len(checks) == 2:
            output.mkdir()
            inode.append(output.stat().st_ino)
    monkeypatch.setattr(s.service, '_check_source', race)
    with pytest.raises(FileExistsError):
        s.service.export(identity, output)
    assert output.stat().st_ino == inode[0] and list(output.iterdir()) == []


def test_cli_preview_digest_binds_source_and_explicit_remote_consent(setup, tmp_path, capsys):
    s = setup
    common = ['--data-dir', str(s.app.root), '--workspace', s.workspace]
    inputs = ['--profile', s.profile.id, '--edition', s.book['edition'], '--first', '2', '--last', '3',
              '--source', 'auto', '--target', 'uk', '--budget', '1']
    assert translation_cli.main(common + ['preview'] + inputs) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview['library_source']['positions'] == 2
    assert len(s.calls) == 1
    assert translation_cli.main(common + ['submit'] + inputs + ['--approve-preview', preview['approval_sha256']]) == 1
    assert 'allow-remote' in capsys.readouterr().err and len(s.calls) == 1
    assert translation_cli.main(common + ['submit'] + inputs + [
        '--allow-remote', '--approve-preview', '0' * 64]) == 1
    capsys.readouterr()
    assert len(s.calls) == 1
    assert translation_cli.main(common + ['submit'] + inputs + [
        '--allow-remote', '--approve-preview', preview['approval_sha256']]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'complete'
    assert translation_cli.main(common + ['inspect', '--job', result['id']]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved['source']['positions'] == 2 and len(saved['resume_approval_sha256']) == 64
    reading = tmp_path / 'bilingual.reading.json'
    assert translation_cli.main(common + ['reading', '--job', result['id'], '--output', str(reading)]) == 0
    capsys.readouterr()
    document = ReadingDocument.load(reading)
    assert document.source_anchor['edition'] == s.book['edition'] and 'TRANSLATION\n' in document.text


def test_cli_changed_source_invalidates_prior_transfer_digest(setup, capsys):
    s = setup
    common = ['--data-dir', str(s.app.root), '--workspace', s.workspace]
    inputs = ['--profile', s.profile.id, '--edition', s.book['edition'],
              '--source', 'en', '--target', 'uk', '--budget', '1']
    assert translation_cli.main(common + ['preview'] + inputs) == 0
    review = json.loads(capsys.readouterr().out)
    s.app.update_metadata(s.workspace, s.book['edition'], title='A changed exact source revision')
    assert translation_cli.main(common + ['submit'] + inputs + [
        '--allow-remote', '--approve-preview', review['approval_sha256']]) == 1
    assert 'exact current preview' in capsys.readouterr().err
    assert len(s.calls) == 1


def test_linked_reading_save_rechecks_source_at_writer_publication(setup, tmp_path, monkeypatch):
    s = setup
    identity, _ = submit(s, capture(s))
    s.service.resume(identity)
    original_dict = ReadingDocument.to_dict
    revoked = []
    def scope_guard():
        if revoked:
            raise PermissionError('The native source context changed before publication.')
    s.service.scope_guard = scope_guard
    def revoke_after_encoding(document):
        payload = original_dict(document)
        revoked.append(True)
        return payload
    monkeypatch.setattr(ReadingDocument, 'to_dict', revoke_after_encoding)
    output = tmp_path / 'blocked.reading.json'
    with pytest.raises((ValueError, PermissionError)):
        s.service.export_reading(identity, output, view='bilingual')
    assert not output.exists()
    assert not list(tmp_path.glob('.speech-*'))


@pytest.mark.parametrize('started', [False, True], ids=['before-first-request', 'accepted-receipt'])
@pytest.mark.parametrize('rewrite_approval', [False, True], ids=['keep-source-approval', 'rewrite-unbound'])
def test_source_identity_cannot_drop_binding_to_bypass_archival(setup, tmp_path, started, rewrite_approval):
    from uuid import UUID
    s = setup
    identity, _ = submit(s, capture(s, first=2, last=2))
    assert UUID(identity).version == 5
    if started:
        s.service.resume(identity)
    s.app.archive_workspace(s.workspace)
    job = s.service.jobs.load(identity)
    del job['source_binding']
    del job['execution_origin']
    if rewrite_approval:
        job['authorization']['task_id'] = job['authorization']['id']
    s.service.jobs.save(job)
    count = len(s.calls), len(s.vault_calls)
    with pytest.raises(ValueError, match='legacy unbound'):
        s.service.resume(identity)
    with pytest.raises(ValueError, match='legacy unbound'):
        s.service.export(identity, tmp_path / 'must-not-downgrade')
    assert (len(s.calls), len(s.vault_calls)) == count
    assert not (tmp_path / 'must-not-downgrade').exists()


def test_copied_library_keeps_readonly_linkage_without_inheriting_remote_authority(setup, tmp_path):
    s = setup
    identity, _ = submit(s, capture(s))
    copied_root = tmp_path / 'out-of-band-copy'
    shutil.copytree(s.app.root, copied_root)
    copied_app = LibraryApp(copied_root)
    copied = TranslationService(copied_root / 'state/translation',
        library=copied_app, workspace=s.workspace,
        gateway=Gateway(copied_root / 'state/translation/provider', mode='APPROVED_REMOTE'))
    assert copied.get(identity)['source_binding'] == s.service.get(identity)['source_binding']
    count = len(s.calls), len(s.vault_calls)
    with pytest.raises(PermissionError, match='moved or copied'):
        copied.resume(identity)
    assert (len(s.calls), len(s.vault_calls)) == count


def test_deleted_decoded_proof_cannot_turn_source_job_into_legacy_receipt(setup, tmp_path):
    from carr_library.assistant.state import atomic_json
    s = setup
    identity, approval = submit(s, capture(s, first=2, last=2))
    s.service.resume(identity)
    job = s.service.jobs.load(identity)
    del job['receipts'][0]['decoded_translation_sha256']
    s.service.jobs.save(job)
    ledger = s.service.gateway.ledger.get(approval.id)
    del ledger['operations'][identity + '_0']['usage']['decoded_translation_sha256']
    atomic_json(s.service.gateway.ledger._path(approval.id), ledger)
    count = len(s.calls)
    with pytest.raises(ValueError, match='decoded translation'):
        s.service.export(identity, tmp_path / 'missing-decoded-proof')
    assert len(s.calls) == count and not (tmp_path / 'missing-decoded-proof').exists()


def test_paid_output_budget_keeps_accepted_prefix_and_blocks_further_calls(setup, monkeypatch):
    s = setup
    large = s.app.add_text(s.workspace, 'Bounded long source', 'Source paragraph. ' * 6000)
    selected = capture_library_sources(s.app, s.workspace, (LibrarySelection(large['edition']),))
    identity, _ = submit(s, selected)
    def large_response(transport, profile, route, body, headers, **kwargs):
        s.calls.append(('POST', json.loads(body)))
        payload = {'data': {'translations': [{'translatedText': 'T' * 190000}]}}
        return HTTPResult('application/json', json.dumps(payload).encode(), 200)
    monkeypatch.setattr(HTTPTransport, 'request', large_response)
    with pytest.raises(ValueError, match='2,000,000-character'):
        s.service.resume(identity)
    job = s.service.jobs.load(identity)
    assert job['status'] == 'attention'
    assert len(job['receipts']) == 11 < len(job['chunks'])
    assert sum(len(row['text']) for row in job['receipts']) == 2_090_000
    count = len(s.calls)
    with pytest.raises(ValueError, match='Accepted chunks were retained'):
        s.service.resume(identity)
    assert len(s.calls) == count


def test_source_permission_rechecked_after_vault_before_transport(setup, monkeypatch):
    s = setup
    identity, approval = submit(s, capture(s))
    class RevokingVault:
        def get(self, profile_id, reference):
            s.app.archive_workspace(s.workspace)
            return 'SYNTHETIC_BOUNDARY_SECRET'
    s.service.gateway.vault = RevokingVault()
    count = len(s.calls)
    with pytest.raises((PermissionError, ValueError, ProviderError)):
        s.service.resume(identity)
    assert len(s.calls) == count
    assert s.service.jobs.load(identity)['receipts'] == []
    assert s.service.gateway.ledger.get(approval.id)['operations'][identity + '_0']['status'] == 'not_sent'


def test_stop_after_final_accepted_chunk_keeps_complete_outcome(setup):
    s = setup
    identity, _ = submit(s, capture(s, first=2, last=2))
    stop = []
    result = s.service.resume(identity, cancelled=lambda: bool(stop),
        progress=lambda done, total, phase: stop.append(True))
    assert result['status'] == 'complete' and result['completed'] == result['total'] == 1
    count = len(s.calls)
    assert s.service.resume(identity, cancelled=lambda: True)['status'] == 'complete'
    assert len(s.calls) == count


@pytest.mark.parametrize('stop', [False, True], ids=['source-deadline', 'source-cancel'])
def test_source_preflight_preserves_timeout_or_cancel_outcome(setup, monkeypatch, stop):
    from carr_library.reader.reading_desk import ReadingDesk
    s = setup
    identity, _ = submit(s, capture(s))
    interrupted = []
    def timeout(*args, **kwargs):
        interrupted.append(True)
        raise TimeoutError('Synthetic original-read deadline')
    monkeypatch.setattr(ReadingDesk, 'page', timeout)
    if stop:
        assert s.service.resume(identity, cancelled=lambda: bool(interrupted))['status'] == 'paused'
    else:
        with pytest.raises(TimeoutError, match='original-read deadline'):
            s.service.resume(identity)
    job = s.service.jobs.load(identity)
    assert job['status'] == ('paused' if stop else 'attention')
    assert job['receipts'] == [] and len(s.calls) == 1


def test_bilingual_view_explains_combined_limit_without_losing_supported_views():
    from carr_library.assistant.translation import split_text
    original = 'S' * 1_000_000
    chunks = list(split_text(original))
    job = {'status': 'complete', 'title': 'Large completed derivative',
           'source_text': original, 'chunks': chunks,
           'receipts': [{'text': 'T' * len(chunk)} for chunk in chunks]}
    assert len(TranslationService._reading_document(job, view='original').text) == 1_000_000
    assert len(TranslationService._reading_document(job, view='translation').text) == 1_000_000
    with pytest.raises(ValueError, match='Choose Original or Translation'):
        TranslationService._reading_document(job, view='bilingual')
    assert job['source_text'] == original and len(job['receipts']) == len(chunks)
