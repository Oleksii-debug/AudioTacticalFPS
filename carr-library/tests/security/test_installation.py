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
    # Pending work and private voice configuration are not moved or retargeted
    # during an application-only update.
    retained = {}
    for name, data in (
        ('pending/synthetic-import.json', b'{"resume":"same-input-and-output-paths"}'),
        ('models/synthetic-voice/config.json', b'{"voice":"existing-local-voice"}'),
        ('audio-projects-retained/output.wav', b'existing-output-not-an-audio-fixture'),
    ):
        path = app.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        retained[path] = data
    # EntitlementStore has an explicitly configured owner root; it is not an
    # application LibraryApp sidecar. Keep this opaque preservation sentinel
    # outside the typed user-data backup, without treating it as a signed grant.
    protected = tmp_path / 'client-state' / 'entitlement.json'
    protected.parent.mkdir()
    protected.write_bytes(b'synthetic-owner-state-not-a-grant')
    retained[protected] = protected.read_bytes()
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
    assert not (restored.root / 'state' / 'trusted-client').exists()
    assert (tmp_path / 'client-state' / 'entitlement.json').read_bytes() == b'synthetic-owner-state-not-a-grant'
    assert anchor.load().sequence == 2
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


# Managed setup admission is deliberately separate from activation. These small
# genuine TUF targets contain synthetic binaries that are never executed.
def _managed_members(build=2):
    from carr_library.security._files import json_bytes
    from carr_library.storage.revisions import SCHEMAS
    files = {
        'runtime/python.exe': b'synthetic pinned runtime; never executed',
        'ResearchLibrary.exe': b'synthetic native launcher; never executed',
        'install_windows.py': b'raise AssertionError("admission must not execute payload")\n',
        'install.lock': b'# synthetic offline lock\n',
        'README.txt': ('Synthetic managed build ' + str(build)).encode(),
    }
    bundle_manifest = json_bytes(dict(schema=1, bundled_python=True, bundled_launcher=True,
                                     source_commit=str(build) * 40,
                                     files={name: sha256(raw).hexdigest() for name, raw in files.items()}))
    helpers = {name: b'raise AssertionError("admission must not import captured helpers")\n'
               for name in ('maintenance.py', 'installation_lease.py', 'bundle_verifier.py', 'windows_process.py')}
    setup = b'MZ synthetic setup; signature qualification is not represented'
    manifest = dict(schema=1, kind='carr-managed-setup', product='carr-test', channel='stable',
                    version=f'0.{build}.0', build=build, source_commit=str(build) * 40,
                    data_schema=max(SCHEMAS), bundle_manifest_sha256=sha256(bundle_manifest).hexdigest(),
                    setup_sha256=sha256(setup).hexdigest(), setup_bytes=len(setup),
                    helpers={name: sha256(raw).hexdigest() for name, raw in helpers.items()})
    return {
        **{'bundle/' + name: raw for name, raw in files.items()},
        'bundle/bundle-manifest.json': bundle_manifest,
        **{'helpers/' + name: raw for name, raw in helpers.items()},
        'ResonanceSetup.exe': setup,
        'managed-setup.json': json_bytes(manifest),
    }


def _managed_zip(members):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in members.items():
            archive.writestr(name, raw)
    return output.getvalue()


def _managed_signed(repository, cache, members, *, build=2, custom=None):
    repository.publish(_managed_zip(members), build=build, custom=custom)
    client = LocalRepositoryClient(cache, repository.policy())
    release = client.check(repository.root, repository.target_path)
    return client.download(repository.root, repository.target_path, reviewed_sha256=release.sha256)


@pytest.fixture
def managed_admission_fixture(tmp_path, repository, clock):
    from carr_library.security.managed_setup import authenticate_installed_package, review_managed_package
    original = _managed_signed(repository, tmp_path / 'managed-a', _managed_members(2))
    anchors = MemoryAnchor(Anchor(original.release.build, original.release.sha256, clock.now))
    repository.advance()
    incoming = _managed_signed(repository, tmp_path / 'managed-b', _managed_members(3), build=3)
    policy = repository.policy(current_build=2)
    installed = authenticate_installed_package(original.path, policy, anchors)
    reviewed = review_managed_package(incoming, policy)
    return policy, anchors, installed, reviewed


def test_managed_admission_authenticates_independent_a_and_b_without_mutating_program_or_anchor(
        managed_admission_fixture, tmp_path):
    from carr_library.security.managed_setup import admit_managed_update, revalidate_managed_admission
    policy, anchors, installed, reviewed = managed_admission_fixture
    original_anchor = anchors.load()
    before = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    result = admit_managed_update(installed, reviewed, policy, anchors,
                                  reviewed_sha256=reviewed.package.release.sha256)
    assert result.installed.setup.build == 2 and result.incoming.setup.build == 3
    assert result.installed.setup.setup_sha256 == sha256(_managed_members(2)['ResonanceSetup.exe']).hexdigest()
    assert revalidate_managed_admission(result) == result
    assert anchors.load() == original_anchor
    assert {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()} == before
    assert not any(path.name in ('bootstrap-owner.json', 'managed-install.ini') for path in tmp_path.rglob('*'))


def test_managed_committed_b_recovery_needs_no_old_a_or_downgrade_permission(
        managed_admission_fixture, repository, tmp_path):
    from carr_library.security.managed_setup import authenticate_installed_package
    policy, anchors, installed, reviewed = managed_admission_fixture
    committed = Anchor(3, reviewed.package.release.sha256, anchors.load().max_utc)
    anchors.compare_and_set(anchors.load(), committed)
    installed.path.unlink()
    current_policy = replace(policy, current_build=3)
    recovered = authenticate_installed_package(reviewed.package.path, current_policy, anchors)
    assert recovered.setup.build == 3 and recovered.anchor == committed
    repository.advance()
    repository.publish(_managed_zip(_managed_members(2)), build=2)
    client = LocalRepositoryClient(tmp_path / 'no-downgrade', current_policy)
    with pytest.raises(TrustError, match='advance.*rollback'):
        client.check(repository.root, repository.target_path)
    assert anchors.load() == committed
    assert not installed.path.exists()


@pytest.mark.parametrize('anchor', [Anchor(), Anchor(2, '0' * 64), Anchor(3, '0' * 64)])
def test_managed_local_receipt_cannot_provision_or_replace_protected_authority(
        managed_admission_fixture, anchor):
    from carr_library.security.managed_setup import authenticate_installed_package
    policy, _, installed, _ = managed_admission_fixture
    anchors = MemoryAnchor(anchor)
    with pytest.raises(TrustError, match='anchor'):
        authenticate_installed_package(installed.path, policy, anchors)
    assert anchors.load() == anchor


def test_managed_matching_package_digest_still_requires_anchor_build_match(managed_admission_fixture):
    from carr_library.security.managed_setup import authenticate_installed_package
    policy, anchors, installed, _ = managed_admission_fixture
    anchors.value = Anchor(3, installed.anchor.digest, installed.anchor.max_utc)
    with pytest.raises(TrustError, match='build differs'):
        authenticate_installed_package(installed.path, policy, anchors)


def test_managed_anchor_changed_during_real_package_read_refuses_authentication(managed_admission_fixture):
    from carr_library.security.managed_setup import authenticate_installed_package
    policy, anchors, installed, reviewed = managed_admission_fixture
    count = 0
    def changed():
        nonlocal count
        count += 1
        if count == 3:
            anchors.value = Anchor(3, reviewed.package.release.sha256, installed.anchor.max_utc)
        return False
    with pytest.raises(TrustError, match='identity changed during'):
        authenticate_installed_package(installed.path, policy, anchors, cancelled=changed)
    assert count >= 3 and anchors.load().sequence == 3


@pytest.mark.parametrize('change', ['incoming', 'installed', 'anchor', 'owner', 'review'])
def test_managed_admission_freshly_rechecks_bytes_anchor_owner_and_review(managed_admission_fixture, change):
    from carr_library.security.managed_setup import admit_managed_update
    policy, anchors, installed, reviewed = managed_admission_fixture
    checksum = reviewed.package.release.sha256
    if change == 'incoming':
        raw = reviewed.package.path.read_bytes()
        reviewed.package.path.write_bytes(b'x' + raw[1:])
    elif change == 'installed':
        raw = installed.path.read_bytes()
        installed.path.write_bytes(b'x' + raw[1:])
    elif change == 'anchor':
        anchors.value = Anchor(3, checksum, installed.anchor.max_utc)
    elif change == 'owner':
        anchors = MemoryAnchor(installed.anchor)
    else:
        checksum = '0' * 64
    before = anchors.load()
    with pytest.raises(TrustError):
        admit_managed_update(installed, reviewed, policy, anchors, reviewed_sha256=checksum)
    assert anchors.load() == before


def test_managed_capability_replacement_cannot_rebind_paths_or_signed_descriptions(
        managed_admission_fixture, tmp_path):
    from carr_library.security.managed_setup import ManagedAdmission, ReviewedManagedPackage
    policy, anchors, installed, reviewed = managed_admission_fixture
    with pytest.raises(TrustError, match='live protected'):
        replace(installed, path=tmp_path / 'different.zip')
    with pytest.raises(TrustError, match='live protected'):
        replace(installed, _store=MemoryAnchor(anchors.load()))
    with pytest.raises(TrustError, match='TUF capability'):
        replace(reviewed, setup=replace(reviewed.setup, build=50))
    with pytest.raises(TrustError, match='TUF capability'):
        ReviewedManagedPackage(reviewed.package, reviewed.setup, policy)
    with pytest.raises(TrustError, match='admission boundary'):
        ManagedAdmission(installed, reviewed)


def test_managed_installed_capability_cannot_be_used_as_incoming_tuf_authority(managed_admission_fixture):
    from carr_library.security.managed_setup import admit_managed_update, review_managed_package
    policy, anchors, installed, reviewed = managed_admission_fixture
    with pytest.raises(TrustError, match='TUF-verified'):
        review_managed_package(installed, policy)
    with pytest.raises(TrustError, match='capabilities'):
        admit_managed_update(installed, installed, policy, anchors,
                             reviewed_sha256=installed.anchor.digest)
    assert anchors.load() == installed.anchor


def test_managed_revalidation_refuses_commit_of_b_after_reviewed_a(managed_admission_fixture):
    from carr_library.security.managed_setup import admit_managed_update, revalidate_managed_admission
    policy, anchors, installed, reviewed = managed_admission_fixture
    admission = admit_managed_update(installed, reviewed, policy, anchors,
                                    reviewed_sha256=reviewed.package.release.sha256)
    anchors.compare_and_set(installed.anchor, Anchor(3, reviewed.package.release.sha256,
                                                   installed.anchor.max_utc))
    with pytest.raises(TrustError, match='identity changed'):
        revalidate_managed_admission(admission)
    assert anchors.load().sequence == 3


@pytest.mark.parametrize('field,value', [
    ('product', 'other-product'), ('channel', 'other-channel'), ('version', '0.9.0'),
    ('source_commit', '9' * 40), ('build', 3), ('data_schema', 999),
    ('setup_bytes', True), ('schema', True),
])
def test_managed_even_signed_package_requires_exact_context_and_typed_manifest(repository, tmp_path, field, value):
    from carr_library.security.managed_setup import review_managed_package
    from carr_library.security._files import json_bytes
    members = _managed_members()
    manifest = json.loads(members['managed-setup.json'])
    manifest[field] = value
    members['managed-setup.json'] = json_bytes(manifest)
    package = _managed_signed(repository, tmp_path / 'cache', members)
    with pytest.raises(TrustError):
        review_managed_package(package, repository.policy())


@pytest.mark.parametrize('member', ['ResonanceSetup.exe', 'helpers/maintenance.py', 'bundle/README.txt'])
def test_managed_even_signed_container_cannot_substitute_declared_setup_helper_or_payload(
        repository, tmp_path, member):
    from carr_library.security.managed_setup import review_managed_package
    members = _managed_members()
    members[member] = b'changed ' + members[member]
    package = _managed_signed(repository, tmp_path / 'cache', members)
    with pytest.raises(TrustError, match='length|checksum'):
        review_managed_package(package, repository.policy())


@pytest.mark.parametrize('name', [
    '../outside', 'helpers/extra.py', 'bundle/.venv/pyvenv.cfg', 'bundle/portable-update.ini',
    'bundle/managed-install.ini', 'bundle/owned-files.json', 'bundle/bootstrap-owner.json',
    'Bundle/README.txt', 'bundle/README.txt/child', 'bundle/runtime/python.exe:stream',
])
def test_managed_fixed_layout_refuses_traversal_state_aliases_and_extra_commands(repository, tmp_path, name):
    from carr_library.security.managed_setup import review_managed_package
    members = _managed_members()
    members[name] = b'untrusted'
    package = _managed_signed(repository, tmp_path / 'cache', members)
    with pytest.raises(TrustError):
        review_managed_package(package, repository.policy())
    assert not (tmp_path / 'outside').exists()


def test_managed_missing_declared_file_and_invalid_bundle_manifest_are_refused(repository, tmp_path):
    from carr_library.security.managed_setup import review_managed_package
    members = _managed_members()
    del members['bundle/README.txt']
    package = _managed_signed(repository, tmp_path / 'missing', members)
    with pytest.raises(TrustError, match='missing'):
        review_managed_package(package, repository.policy())
    repository.advance()
    members = _managed_members()
    members['bundle/bundle-manifest.json'] = b'{"schema":1}'
    package = _managed_signed(repository, tmp_path / 'manifest', members)
    with pytest.raises(TrustError, match='manifest checksum'):
        review_managed_package(package, repository.policy())


def test_managed_signed_link_and_duplicate_members_remain_refused(repository, tmp_path):
    from carr_library.security.managed_setup import review_managed_package
    for mode in ('link', 'duplicate'):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, raw in _managed_members().items():
                archive.writestr(name, raw)
            info = zipfile.ZipInfo('linked-member' if mode == 'link' else 'managed-setup.json')
            if mode == 'link':
                info.create_system = 3
                info.external_attr = 0o120777 << 16
                archive.writestr(info, b'outside')
            else:
                with pytest.warns(UserWarning, match='Duplicate name'):
                    archive.writestr(info, b'{}')
        repository.advance()
        package = signed_package(repository, tmp_path / mode, stream.getvalue())
        with pytest.raises(TrustError, match='unlinked|Duplicate'):
            review_managed_package(package, repository.policy())


def test_managed_package_limits_apply_before_payload_processing(repository, tmp_path):
    from carr_library.security.managed_setup import review_managed_package
    package = _managed_signed(repository, tmp_path / 'cache', _managed_members())
    for policy in (repository.policy(max_package_bytes=package.release.length - 1),
                   repository.policy(max_files=2), repository.policy(max_unpacked_bytes=32)):
        with pytest.raises(TrustError, match='budget'):
            review_managed_package(package, policy)


def test_managed_requested_cancellation_never_advances_anchor_or_extracts_payload(
        managed_admission_fixture, tmp_path):
    from carr_library.security.managed_setup import admit_managed_update
    from carr_library.security.updates import UpdateCancelled
    policy, anchors, installed, reviewed = managed_admission_fixture
    before = {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    with pytest.raises(UpdateCancelled):
        admit_managed_update(installed, reviewed, policy, anchors,
                             reviewed_sha256=reviewed.package.release.sha256, cancelled=lambda: True)
    assert anchors.load() == installed.anchor
    assert {path: path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()} == before


def test_managed_retained_authentication_rejects_real_file_link(managed_admission_fixture, tmp_path):
    from carr_library.security.managed_setup import authenticate_installed_package
    policy, anchors, installed, _ = managed_admission_fixture
    alias = tmp_path / 'retained-hardlink.zip'
    os.link(installed.path, alias)
    with pytest.raises(TrustError, match='unlinked'):
        authenticate_installed_package(alias, policy, anchors)
    assert anchors.load() == installed.anchor


def test_managed_old_genuine_tuf_package_cannot_replay_the_protected_build(
        managed_admission_fixture, repository, tmp_path):
    from carr_library.security.managed_setup import admit_managed_update, review_managed_package
    policy, anchors, installed, _ = managed_admission_fixture
    repository.advance()
    package = _managed_signed(repository, tmp_path / 'historical-a', _managed_members(2))
    historical_review = review_managed_package(package, policy)
    with pytest.raises(TrustError, match='advance the protected'):
        admit_managed_update(installed, historical_review, policy, anchors,
                             reviewed_sha256=package.release.sha256)
    assert anchors.load() == installed.anchor


def test_managed_review_uses_private_verified_snapshot_and_admission_rechecks_live_bytes(
        managed_admission_fixture, monkeypatch):
    from carr_library.security import managed_setup
    policy, anchors, installed, reviewed = managed_admission_fixture
    original = managed_setup._inspect
    changed = []
    def replace_download_after_snapshot(snapshot, checked_policy, cancelled):
        assert checked_policy == policy
        raw = reviewed.package.path.read_bytes()
        reviewed.package.path.write_bytes(b'x' + raw[1:])
        changed.append(True)
        return original(snapshot, checked_policy, cancelled)
    monkeypatch.setattr(managed_setup, '_inspect', replace_download_after_snapshot)
    still_original = managed_setup.review_managed_package(reviewed.package, policy)
    assert changed == [True] and still_original == reviewed
    monkeypatch.setattr(managed_setup, '_inspect', original)
    with pytest.raises(TrustError, match='checksum'):
        managed_setup.admit_managed_update(installed, still_original, policy, anchors,
                                          reviewed_sha256=reviewed.package.release.sha256)
    assert anchors.load() == installed.anchor


def test_managed_admission_cannot_rebind_an_equal_valued_capability_to_another_owner(
        managed_admission_fixture):
    from carr_library.security.managed_setup import (
        admit_managed_update, authenticate_installed_package, revalidate_managed_admission,
    )
    policy, anchors, installed, reviewed = managed_admission_fixture
    admission = admit_managed_update(installed, reviewed, policy, anchors,
                                    reviewed_sha256=reviewed.package.release.sha256)
    other_owner = MemoryAnchor(anchors.load())
    other_installed = authenticate_installed_package(installed.path, policy, other_owner)
    # Both are independently valid and semantically equal. They are different
    # owner capabilities, so the existing review must not migrate between them.
    assert other_installed == installed and other_installed is not installed
    assert other_installed._store is other_owner and admission.installed._store is anchors
    with pytest.raises(TrustError, match='admission boundary'):
        replace(admission, installed=other_installed)
    assert revalidate_managed_admission(admission).installed._store is anchors
    explicit_other_review = admit_managed_update(
        other_installed, reviewed, policy, other_owner,
        reviewed_sha256=reviewed.package.release.sha256)
    assert revalidate_managed_admission(explicit_other_review).installed._store is other_owner
    other_owner.value = Anchor(3, reviewed.package.release.sha256, other_owner.load().max_utc)
    with pytest.raises(TrustError, match='identity changed'):
        revalidate_managed_admission(explicit_other_review)
    assert revalidate_managed_admission(admission).installed._store is anchors
    assert anchors.load() == installed.anchor


def test_managed_admission_retains_exact_incoming_review_capability(managed_admission_fixture):
    from carr_library.security.managed_setup import admit_managed_update, review_managed_package
    policy, anchors, installed, reviewed = managed_admission_fixture
    admission = admit_managed_update(installed, reviewed, policy, anchors,
                                    reviewed_sha256=reviewed.package.release.sha256)
    separate_review = review_managed_package(reviewed.package, policy)
    assert separate_review == admission.incoming and separate_review is not admission.incoming
    with pytest.raises(TrustError, match='admission boundary'):
        replace(admission, incoming=separate_review)
    assert admission.incoming.package is reviewed.package
