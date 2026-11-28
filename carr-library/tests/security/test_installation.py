from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import zipfile

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.speech.source import read_source_range
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.reader.reading_state import ReadingState
from carr_library.security._files import TrustError
from carr_library.security.anchors import Anchor
from carr_library.security.installation import UpdateTransaction, unpack
from carr_library.security.updates import LocalRepositoryClient, Release, VerifiedPackage
from .conftest import MemoryAnchor, SyntheticBundleAdapter, bundle, idle


@pytest.fixture
def setup_update(tmp_path, repository, clock):
    app = LibraryApp(tmp_path / 'research')
    workspace = app.create_workspace('Populated library')
    book = app.add_text(workspace, 'Exact source', 'Original quotation. Незмінний текст.\fSecond page.')
    topic = app.create_topic(workspace, 'Notes')
    hit, = app.search(workspace, 'Original')
    evidence = app.save_hit(workspace, topic, hit.ref, hit.revision, 'Retain this separate note.')
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    state = ReadingState(app.root)
    state.remember(page.location)
    bookmark = state.add_bookmark(page.location, 'Return to this exact source')
    reading = read_source_range(ReadingDesk(app, workspace, book['edition']), 1, 1).reading_document()
    ReaderDraftStore(app.root, slot='update-saved-copy').save(ReaderResume(reading))
    draft = ReadingDocument.from_dict(reading.to_dict())
    draft.replace(0, len(draft.text), 'Explicit spoken derivative.')
    ReaderDraftStore(app.root, slot='update-editable-draft').save(ReaderResume(draft, cursor=3))
    # Pending work, private voice configuration and protected-state document
    # are not moved or retargeted during an application-only update.
    retained = {}
    for name, data in (
        ('pending/synthetic-import.json', b'{"resume":"same-input-and-output-paths"}'),
        ('models/synthetic-voice/config.json', b'{"voice":"existing-local-voice"}'),
        ('state/trusted-client/entitlement.json', b'synthetic-owner-state-not-a-grant'),
        ('audio-projects-retained/output.wav', b'existing-output-not-an-audio-fixture'),
    ):
        path = app.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        retained[path] = data
    policy = repository.policy()
    raw_a = bundle('A')
    # Initial A is a locally created, trusted test installation, not an update.
    installed = tmp_path / 'program'
    with zipfile.ZipFile(io.BytesIO(raw_a)) as fixture:
        fixture.extractall(installed)
    repository.publish(bundle('B'))
    client = LocalRepositoryClient(tmp_path / 'cache', policy)
    review = client.check(repository.root, repository.target_path)
    package = client.download(repository.root, repository.target_path, reviewed_sha256=review.sha256)
    anchor = MemoryAnchor(Anchor(1, sha256(raw_a).hexdigest(), clock.now))
    adapter = SyntheticBundleAdapter()
    work = tmp_path / 'updates'
    transaction = UpdateTransaction(app, installed, work, policy, anchor, adapter, idle_lease=idle, clock=clock)
    return app, workspace, topic, bookmark, reading, retained, package, transaction, anchor, adapter


def assert_preserved(data):
    app, workspace, topic, bookmark, reading, retained, *_ = data
    assert app.evidence(workspace, topic)[0].note == 'Retain this separate note.'
    assert app.search(workspace, 'Original')[0].quote == 'Original'
    assert ReadingDesk(app, workspace, bookmark.location.edition).page(1).raw_text == reading.source_text
    assert ReadingState(app.root).bookmarks(workspace, bookmark.location.edition) == (bookmark,)
    saved = ReaderDraftStore(app.root, slot='update-saved-copy').load().document
    resume = ReaderDraftStore(app.root, slot='update-editable-draft').load()
    draft = resume.document
    assert saved.source_text == reading.source_text
    assert draft.source_text == reading.source_text
    assert saved.source_anchor == draft.source_anchor == reading.source_anchor
    assert saved.source_anchor['quotation_authority'] is False
    assert draft.text == 'Explicit spoken derivative.' and resume.cursor == 3
    assert draft.undo() and draft.text == reading.source_text
    for path, raw in retained.items():
        assert path.read_bytes() == raw


def test_signed_a_to_b_real_stage_backup_activation_and_checked_restore_preserve_library(setup_update, tmp_path):
    app, workspace, topic, bookmark, reading, retained, package, transaction, anchor, adapter = setup_update
    result = transaction.apply(package)
    assert result['status'] == 'complete'
    assert anchor.load().sequence == 2
    assert (transaction.installed / 'version.txt').read_text() == 'B'
    assert (Path(result['previous']) / 'version.txt').read_text() == 'A'
    assert adapter.prepared == adapter.checked == 1
    assert adapter.activated_path == transaction.installed
    assert_preserved(setup_update)
    restored = LibraryApp(restore_backup(Path(result['backup']), tmp_path / 'restored', result['backup_sha256']))
    assert restored.evidence(workspace, topic)[0].note == 'Retain this separate note.'
    assert ReadingState(restored.root).bookmarks(workspace, bookmark.location.edition) == (bookmark,)
    restored_draft = ReaderDraftStore(restored.root, slot='update-editable-draft').load()
    assert restored_draft.document.text == 'Explicit spoken derivative.'
    assert restored_draft.document.source_text == reading.source_text
    assert restored_draft.document.source_anchor == reading.source_anchor
    assert restored_draft.cursor == 3
    with pytest.raises(TrustError, match='rollback or replay'):
        transaction.apply(package)


def test_failed_health_restores_a_and_keeps_checked_backup(setup_update):
    *_, package, transaction, anchor, adapter = setup_update
    adapter.fail = True
    with pytest.raises(TrustError, match='health'):
        transaction.apply(package)
    assert (transaction.installed / 'version.txt').read_text() == 'A'
    assert anchor.load().sequence == 1
    record = json.loads(transaction.journal.read_bytes())
    assert record['phase'] == 'rolled-back'
    assert (transaction.work / record['failed'] / 'version.txt').read_text() == 'B'
    assert (transaction.work / record['backup'] / 'manifest.json').exists()
    assert_preserved(setup_update)


@pytest.mark.parametrize('after', ['old-moved', 'new-active'])
def test_process_death_between_renames_recovers_a_without_touching_data(setup_update, monkeypatch, after):
    from carr_library.security import installation
    *_, package, transaction, anchor, adapter = setup_update
    original = installation.os.rename
    class PowerLoss(BaseException):
        pass
    def interrupted(source, destination):
        original(source, destination)
        if (after == 'old-moved' and Path(source) == transaction.installed) or (
                after == 'new-active' and Path(destination) == transaction.installed):
            raise PowerLoss()
    monkeypatch.setattr(installation.os, 'rename', interrupted)
    with pytest.raises(PowerLoss):
        transaction.apply(package)
    monkeypatch.undo()
    restarted = UpdateTransaction(transaction.app, transaction.installed, transaction.work,
        transaction.policy, anchor, adapter, idle_lease=idle, clock=transaction.clock)
    assert restarted.recover() == 'rolled-back'
    assert (transaction.installed / 'version.txt').read_text() == 'A'
    assert anchor.load().sequence == 1
    assert_preserved(setup_update)


def test_crash_after_protected_commit_finalizes_b_instead_of_rolling_back(setup_update, monkeypatch):
    *_, package, transaction, anchor, adapter = setup_update
    original = transaction._write
    def interrupted(record, phase):
        if phase == 'complete':
            raise OSError('synthetic final journal failure')
        return original(record, phase)
    monkeypatch.setattr(transaction, '_write', interrupted)
    with pytest.raises(OSError):
        transaction.apply(package)
    assert anchor.load().sequence == 2
    monkeypatch.undo()
    assert transaction.recover() == 'complete'
    assert (transaction.installed / 'version.txt').read_text() == 'B'
    assert_preserved(setup_update)


def test_protected_store_failure_rolls_back_without_losing_data(setup_update):
    *_, package, transaction, anchor, adapter = setup_update
    anchor.fail = True
    with pytest.raises(TrustError, match='protected-store'):
        transaction.apply(package)
    assert (transaction.installed / 'version.txt').read_text() == 'A'
    assert anchor.load().sequence == 1
    assert_preserved(setup_update)


def test_active_work_refuses_update_before_any_stage_or_backup(setup_update):
    *_, package, transaction, anchor, adapter = setup_update
    @contextmanager
    def busy():
        raise TrustError('An active render is running.')
        yield
    transaction.idle_lease = busy
    with pytest.raises(TrustError, match='active render'):
        transaction.apply(package)
    assert list(transaction.work.glob('stage-*')) == []
    assert adapter.prepared == adapter.checked == 0
    assert_preserved(setup_update)


def test_changed_package_and_redirected_recovery_paths_are_rejected(setup_update):
    *_, package, transaction, anchor, adapter = setup_update
    original = package.path.read_bytes()
    package.path.write_bytes(b'x' * len(original))
    with pytest.raises(TrustError, match='checksum'):
        transaction.apply(package)
    package.path.write_bytes(original)
    transaction.apply(package)
    record = json.loads(transaction.journal.read_bytes())
    record['backup'] = '../research'
    transaction.journal.write_text(json.dumps(record))
    with pytest.raises(TrustError, match='paths'):
        transaction.recover()
    assert_preserved(setup_update)


@pytest.mark.parametrize('name', ['../outside', '/absolute', 'C:/alternate', 'safe/../outside',
                                  'CON.txt', 'a//b', 'a/./b', 'trailing.'])
def test_even_signed_archive_cannot_escape_staging(repository, tmp_path, name):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr(name, b'bad')
    raw = stream.getvalue()
    package = signed_package(repository, tmp_path / 'cache', raw)
    with pytest.raises(TrustError):
        unpack(package, tmp_path / 'stage', repository.policy())
    assert not (tmp_path / 'outside').exists()


def test_budget_and_link_members_rejected(repository, tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        info = zipfile.ZipInfo('link')
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        archive.writestr(info, b'../outside')
    raw = stream.getvalue()
    package = signed_package(repository, tmp_path / 'cache-link', raw)
    with pytest.raises(TrustError, match='linked'):
        unpack(package, tmp_path / 'stage', repository.policy())
    raw = bundle()
    package = signed_package(repository, tmp_path / 'cache-budget', raw)
    with pytest.raises(TrustError, match='entry budget'):
        unpack(package, tmp_path / 'stage', repository.policy(max_files=2))


def signed_package(repository, cache, raw):
    repository.publish(raw)
    client = LocalRepositoryClient(cache, repository.policy())
    review = client.check(repository.root, repository.target_path)
    return client.download(repository.root, repository.target_path, reviewed_sha256=review.sha256)


def test_plain_checksum_cannot_masquerade_as_a_tuf_verified_package(tmp_path):
    release = Release('carr-test', 'stable', '0.2', 2, 'a' * 40, 4, 'release.zip', 1, 'b' * 64, '')
    with pytest.raises(TrustError, match='TUF client'):
        VerifiedPackage(release, tmp_path / 'unverified.zip')


def test_forged_zip_directory_count_is_bounded_before_zipinfo_allocation(repository, tmp_path, monkeypatch):
    import struct
    from carr_library.security import installation
    raw = bytearray(bundle())
    end = raw.rfind(b'PK\x05\x06')
    struct.pack_into('<H', raw, end + 8, 10001)
    struct.pack_into('<H', raw, end + 10, 10001)
    package = signed_package(repository, tmp_path / 'cache', bytes(raw))
    def must_not_allocate(*args, **kwargs):
        raise AssertionError('ZipFile allocated entries before the budget check.')
    monkeypatch.setattr(installation.zipfile, 'ZipFile', must_not_allocate)
    with pytest.raises(TrustError, match='entry budget'):
        unpack(package, tmp_path / 'stage', repository.policy())


def test_aliased_parent_directories_are_refused_even_when_signed(repository, tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('Folder/first.txt', b'first')
        archive.writestr('folder/second.txt', b'second')
    package = signed_package(repository, tmp_path / 'cache', stream.getvalue())
    with pytest.raises(TrustError, match='aliases'):
        unpack(package, tmp_path / 'stage', repository.policy())


def test_failed_preupdate_backup_never_activates_candidate(setup_update, monkeypatch):
    from carr_library import library_backup
    *_, package, transaction, anchor, adapter = setup_update
    def unavailable(*args, **kwargs):
        raise OSError('synthetic backup disk full')
    monkeypatch.setattr(library_backup, 'create_backup', unavailable)
    with pytest.raises(OSError, match='disk full'):
        transaction.apply(package)
    assert (transaction.installed / 'version.txt').read_text() == 'A'
    assert adapter.activated_path is None
    assert anchor.load().sequence == 1
    assert_preserved(setup_update)



def test_cancel_during_prepared_journal_prevents_first_program_rename(setup_update, monkeypatch):
    from carr_library.security.updates import UpdateCancelled
    *_, package, transaction, anchor, adapter = setup_update
    original = transaction._write
    stopped = False
    def stop_after_durable_preparation(record, phase):
        nonlocal stopped
        original(record, phase)
        if phase == 'prepared':
            stopped = True
    monkeypatch.setattr(transaction, '_write', stop_after_durable_preparation)
    with pytest.raises(UpdateCancelled, match='cancelled'):
        transaction.apply(package, cancelled=lambda: stopped)
    record = json.loads(transaction.journal.read_bytes())
    assert record['phase'] == 'rolled-back'
    assert (transaction.installed / 'version.txt').read_bytes() == b'A'
    assert not (transaction.installed.parent / record['previous']).exists()
    assert (transaction.work / record['stage'] / 'version.txt').read_bytes() == b'B'
    assert anchor.load().sequence == 1 and adapter.checked == 0
    assert (transaction.work / record['backup'] / 'manifest.json').is_file()
    assert_preserved(setup_update)

    # Only the live owner can attest that no first rename occurred. Its durable
    # terminal phase lets a restarted owner recover and retry without guessing
    # which program happens to occupy the installed pathname.
    cancelled_record = transaction.journal.read_bytes()
    restarted = UpdateTransaction(transaction.app, transaction.installed, transaction.work,
        transaction.policy, anchor, adapter, idle_lease=idle, clock=transaction.clock)
    assert restarted.recover() == 'rolled-back'
    assert restarted.journal.read_bytes() == cancelled_record
    assert (restarted.installed / 'version.txt').read_bytes() == b'A'
    assert anchor.load().sequence == 1 and adapter.checked == 0
    result = restarted.apply(package)
    assert result['status'] == 'complete'
    assert (restarted.installed / 'version.txt').read_bytes() == b'B'
    assert (Path(result['previous']) / 'version.txt').read_bytes() == b'A'
    assert anchor.load().sequence == 2 and adapter.checked == 1
    assert json.loads(restarted.journal.read_bytes())['transaction'] != record['transaction']
    assert (transaction.work / record['stage'] / 'version.txt').read_bytes() == b'B'
    assert (transaction.work / record['backup'] / 'manifest.json').is_file()
    assert_preserved(setup_update)


def test_cancel_terminal_journal_failure_is_not_reported_as_recovered(setup_update, monkeypatch):
    *_, package, transaction, anchor, adapter = setup_update
    original = transaction._write
    stopped = False

    def stop_then_fail_terminal_write(record, phase):
        nonlocal stopped
        if phase == 'rolled-back':
            raise OSError('Synthetic cancellation journal publication failure')
        original(record, phase)
        if phase == 'prepared':
            stopped = True

    monkeypatch.setattr(transaction, '_write', stop_then_fail_terminal_write)
    with pytest.raises(OSError, match='cancellation journal publication failure'):
        transaction.apply(package, cancelled=lambda: stopped)
    raw = transaction.journal.read_bytes()
    record = json.loads(raw)
    assert record['phase'] == 'prepared'
    assert (transaction.installed / 'version.txt').read_bytes() == b'A'
    assert not (transaction.installed.parent / record['previous']).exists()
    assert anchor.load().sequence == 1 and adapter.checked == 0
    assert (transaction.work / record['stage'] / 'version.txt').read_bytes() == b'B'
    assert (transaction.work / record['backup'] / 'manifest.json').is_file()
    # A later process has no live-owner proof of the cancelled no-rename path.
    # Preserve the strict missing-prior rejection instead of inferring identity.
    with pytest.raises(TrustError, match='Previous installation is unavailable'):
        transaction.recover()
    assert transaction.journal.read_bytes() == raw
    assert_preserved(setup_update)


def test_missing_previous_installation_cannot_be_reported_as_rolled_back(setup_update, monkeypatch):
    import shutil
    *_, package, transaction, anchor, adapter = setup_update
    original = transaction._write
    class PowerLoss(BaseException):
        pass
    def stop_after_new_program(record, phase):
        original(record, phase)
        if phase == 'new-active':
            raise PowerLoss()
    monkeypatch.setattr(transaction, '_write', stop_after_new_program)
    with pytest.raises(PowerLoss):
        transaction.apply(package)
    raw = transaction.journal.read_bytes()
    record = json.loads(raw)
    assert record['phase'] == 'new-active'
    previous = transaction.installed.parent / record['previous']
    assert (previous / 'version.txt').read_bytes() == b'A'
    shutil.rmtree(previous)
    assert (transaction.installed / 'version.txt').read_bytes() == b'B'
    assert anchor.load().sequence == 1
    with pytest.raises(TrustError, match='Previous installation is unavailable'):
        transaction.recover()
    assert transaction.journal.read_bytes() == raw
    assert (transaction.installed / 'version.txt').read_bytes() == b'B'
    assert not (transaction.work / record['failed']).exists()
    assert (transaction.work / record['backup'] / 'manifest.json').is_file()
    assert_preserved(setup_update)
