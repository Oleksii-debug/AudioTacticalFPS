"""Actual local snapshot transfers; no mocked filesystem or backup engine."""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup
from carr_library.sync.vault import Vault, VaultBusy, VaultConflict, VaultError
import carr_library.sync.vault as module


def payload(root):
    root.mkdir()
    (root / 'папка').mkdir()
    (root / 'папка' / 'empty').mkdir()
    (root / 'папка' / 'дані.bin').write_bytes(bytes(range(256)) * 24 + b'last block')
    (root / 'empty.txt').write_bytes(b'')
    (root / 'manifest.json').write_text('{"version":1,"test":"opaque payload"}\n', encoding='utf-8')
    return root


def tree(root):
    return {path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
            for path in root.rglob('*')}


def chunks(vault):
    return sorted(path for path in (vault.root / 'chunks').glob('*/*') if path.is_file())


def test_actual_library_backup_transfer_restore_search_and_quote(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Наукова бібліотека')
    text = 'Exact original source — наука, dôkaz, evidence.\nSecond line.'
    app.add_text(workspace, 'Portable source', text)
    backup = tmp_path / 'prepared backup'
    original_token = app.backup(backup)
    original_tree = tree(backup)
    vault = Vault(tmp_path / 'portable vault', chunk_bytes=1024)

    receipt = vault.publish(backup, expected_head=None, label='Research checkpoint')
    assert receipt.snapshot_id == receipt.manifest_sha256 == receipt.transfer_token == vault.head()
    assert receipt.file_count == sum(data is not None for data in original_tree.values())
    assert receipt.total_bytes == sum(len(data) for data in original_tree.values() if data is not None)
    assert str(backup) not in json.dumps(asdict(receipt))
    assert vault.list_snapshots() == (receipt,)
    copied = vault.materialize(receipt.snapshot_id, tmp_path / 'transported backup',
                              expected_token=receipt.transfer_token)
    assert tree(copied) == original_tree == tree(backup)
    restored = restore_backup(copied, tmp_path / 'restored library', original_token)
    reopened = LibraryApp(restored)
    result = reopened.search(workspace, 'Exact original source')[0]
    assert result.verified is True
    assert reopened.copy(workspace, result.ref, result.revision, 'quote') == 'Exact original source'


def test_opaque_tree_unicode_empty_directories_and_deduplicated_bytes(tmp_path):
    source = payload(tmp_path / 'prepared')
    (source / 'second.bin').write_bytes((source / 'папка' / 'дані.bin').read_bytes())
    vault = Vault(tmp_path / 'vault', chunk_bytes=1024)
    events = []
    receipt = vault.publish(source, expected_head=None, on_progress=events.append)
    assert events[-1].files_completed == events[-1].files_total == 4
    assert events[-1].bytes_completed == receipt.total_bytes
    assert events[-1].chunks_reused > 0
    restored = vault.materialize(receipt.snapshot_id, tmp_path / 'new', expected_token=receipt.transfer_token)
    assert tree(restored) == tree(source)


def test_cancelled_publish_keeps_verified_chunks_for_reopened_resume(tmp_path):
    source = payload(tmp_path / 'prepared')
    vault = Vault(tmp_path / 'vault', chunk_bytes=1024)
    cancel = Event()
    def progress(event):
        if event.chunks_written:
            cancel.set()
    with pytest.raises(InterruptedError):
        vault.publish(source, expected_head=None, cancelled=cancel.is_set, on_progress=progress)
    assert vault.head() is None
    assert vault.list_snapshots() == ()
    kept = {path: path.read_bytes() for path in chunks(vault)}
    assert kept
    restarted = Vault(vault.root, chunk_bytes=1024)
    events = []
    receipt = restarted.publish(source, expected_head=None, on_progress=events.append)
    assert events[-1].chunks_reused >= 1
    assert all(path.read_bytes() == data for path, data in kept.items())
    assert tree(restarted.materialize(receipt.snapshot_id, tmp_path / 'copy',
                expected_token=receipt.transfer_token)) == tree(source)


@pytest.mark.parametrize('phase', ['chunk', 'manifest'])
def test_process_exit_releases_lock_and_resumes_durable_verified_chunks(tmp_path, phase):
    source = payload(tmp_path / 'prepared')
    vault = Vault(tmp_path / 'vault', chunk_bytes=1024)
    script = r'''
import os, sys
from pathlib import Path
import carr_library.sync.vault as module
assert Path(module.__file__).resolve() == Path(sys.argv[4]).resolve(), 'Wrong installed/source Vault origin'
vault = module.Vault(Path(sys.argv[1]), chunk_bytes=1024)
if sys.argv[3] == 'manifest':
    original = module._write_file
    def interrupted(path, raw, **kwargs):
        original(path, raw, **kwargs)
        if path.parent.name == 'snapshots':
            os._exit(73)
    module._write_file = interrupted
def progress(event):
    if sys.argv[3] == 'chunk' and event.chunks_written:
        os._exit(73)
vault.publish(Path(sys.argv[2]), expected_head=None, on_progress=progress)
raise AssertionError('crash boundary was not reached')
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, str(vault.root), str(source), phase,
                             str(Path(module.__file__).resolve())],
                            capture_output=True, timeout=15, check=False)
    assert result.returncode == 73, result.stderr.decode('utf-8', errors='replace')
    restarted = Vault(vault.root, chunk_bytes=1024)
    assert restarted.head() is None
    retained = chunks(restarted)
    assert retained
    for path in retained:
        assert sha256(path.read_bytes()).hexdigest() == path.parent.name + path.name
    events = []
    receipt = restarted.publish(source, expected_head=None, on_progress=events.append)
    assert events[-1].chunks_reused > 0
    assert tree(restarted.materialize(receipt.snapshot_id, tmp_path / 'restored',
                expected_token=receipt.transfer_token)) == tree(source)


def test_parent_compare_conflicts_preserve_current_generation(tmp_path):
    source = payload(tmp_path / 'prepared')
    vault = Vault(tmp_path / 'vault', chunk_bytes=1024)
    first = vault.publish(source, expected_head=None)
    with pytest.raises(VaultConflict):
        vault.publish(source, expected_head=None)
    second = vault.publish(source, expected_head=first.snapshot_id)
    assert second.parent == first.snapshot_id
    assert second.snapshot_id != first.snapshot_id
    with pytest.raises(VaultConflict):
        vault.publish(source, expected_head=first.snapshot_id)
    assert vault.head() == second.snapshot_id
    assert vault.list_snapshots() == (first, second)
    assert tree(vault.materialize(first.snapshot_id, tmp_path / 'old version',
                expected_token=first.transfer_token)) == tree(source)


def test_concurrent_writer_cannot_publish_or_evict_inflight_generation(tmp_path):
    source = payload(tmp_path / 'prepared')
    first = Vault(tmp_path / 'vault', chunk_bytes=1024)
    second = Vault(first.root, chunk_bytes=1024)
    reached, release = Event(), Event()
    output, errors = [], []
    def progress(event):
        if event.chunks_written and not reached.is_set():
            reached.set()
            assert release.wait(5), 'test writer release timed out'
    def run():
        try:
            output.append(first.publish(source, expected_head=None, on_progress=progress))
        except BaseException as exc:
            errors.append(exc)
    worker = Thread(target=run)
    worker.start()
    try:
        assert reached.wait(5), 'test writer did not reach first checkpoint'
        with pytest.raises(VaultBusy):
            second.publish(source, expected_head=None)
        assert second.head() is None
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    assert second.head() == output[0].snapshot_id
    successor = second.publish(source, expected_head=output[0].snapshot_id)
    assert successor.parent == output[0].snapshot_id


def test_corrupted_chunk_rejects_restore_and_reuse_without_overwrite(tmp_path):
    source = payload(tmp_path / 'prepared')
    vault = Vault(tmp_path / 'vault', chunk_bytes=1024)
    receipt = vault.publish(source, expected_head=None)
    victim = chunks(vault)[0]
    corrupt = b'X' * victim.stat().st_size
    victim.write_bytes(corrupt)
    with pytest.raises(VaultError, match='integrity'):
        vault.materialize(receipt.snapshot_id, tmp_path / 'rejected', expected_token=receipt.transfer_token)
    assert not (tmp_path / 'rejected').exists()
    with pytest.raises(VaultError, match='integrity'):
        vault.publish(source, expected_head=receipt.snapshot_id)
    assert victim.read_bytes() == corrupt
    assert vault.head() == receipt.snapshot_id


def test_manifest_tampering_and_missing_independent_token_fail_before_publication(tmp_path):
    source = payload(tmp_path / 'prepared')
    vault = Vault(tmp_path / 'vault')
    receipt = vault.publish(source, expected_head=None)
    target = tmp_path / 'rejected'
    with pytest.raises(TypeError):
        vault.materialize(receipt.snapshot_id, target)
    with pytest.raises(VaultError, match='token'):
        vault.materialize(receipt.snapshot_id, target, expected_token='0' * 64)
    manifest = vault.root / 'snapshots' / (receipt.snapshot_id + '.json')
    manifest.write_bytes(manifest.read_bytes().replace(b'Portable checkpoint', b'Untrusted checkpoint'))
    with pytest.raises(VaultError, match='token'):
        vault.materialize(receipt.snapshot_id, target, expected_token=receipt.transfer_token)
    assert not target.exists()


def test_cancelled_materialize_does_not_expose_partial_directory(tmp_path):
    source = payload(tmp_path / 'prepared')
    vault = Vault(tmp_path / 'vault', chunk_bytes=1024)
    receipt = vault.publish(source, expected_head=None)
    cancel = Event()
    with pytest.raises(InterruptedError):
        vault.materialize(receipt.snapshot_id, tmp_path / 'new', expected_token=receipt.transfer_token,
                          cancelled=cancel.is_set, on_progress=lambda event: cancel.set())
    assert not (tmp_path / 'new').exists()
    assert not list(tmp_path.glob('.carr-snapshot-*'))
    assert vault.head() == receipt.snapshot_id
    assert tree(vault.materialize(receipt.snapshot_id, tmp_path / 'new',
                expected_token=receipt.transfer_token)) == tree(source)


@pytest.mark.parametrize('change', ['file', 'tree'])
def test_changed_prepared_tree_cannot_be_committed(tmp_path, change):
    source = payload(tmp_path / 'prepared')
    vault = Vault(tmp_path / 'vault', chunk_bytes=1024)
    changed = False
    def progress(event):
        nonlocal changed
        if not changed and event.bytes_completed:
            changed = True
            if change == 'file':
                (source / 'manifest.json').write_bytes(b'changed source')
            else:
                (source / 'unexpected.txt').write_bytes(b'new file')
    with pytest.raises(VaultError, match='changed'):
        vault.publish(source, expected_head=None, on_progress=progress)
    assert vault.head() is None
    assert vault.list_snapshots() == ()


def test_existing_destination_and_racing_directory_are_never_overwritten(tmp_path, monkeypatch):
    source = payload(tmp_path / 'prepared')
    vault = Vault(tmp_path / 'vault')
    receipt = vault.publish(source, expected_head=None)
    target = tmp_path / 'new'
    target.mkdir()
    (target / 'keep').write_bytes(b'original')
    with pytest.raises(FileExistsError):
        vault.materialize(receipt.snapshot_id, target, expected_token=receipt.transfer_token)
    assert tree(target) == {'keep': b'original'}
    target = tmp_path / 'racing target'
    original = module._publish_directory
    def race(stage, destination, **kwargs):
        destination.mkdir()
        (destination / 'keep').write_bytes(b'concurrent')
        return original(stage, destination, **kwargs)
    monkeypatch.setattr(module, '_publish_directory', race)
    with pytest.raises(FileExistsError):
        vault.materialize(receipt.snapshot_id, target, expected_token=receipt.transfer_token)
    assert tree(target) == {'keep': b'concurrent'}
    assert not list(tmp_path.glob('.carr-snapshot-*'))


def test_failure_before_atomic_publication_leaves_destination_absent(tmp_path, monkeypatch):
    source = payload(tmp_path / 'prepared')
    vault = Vault(tmp_path / 'vault')
    receipt = vault.publish(source, expected_head=None)
    def fail(stage, destination, **kwargs):
        raise OSError('injected publication failure')
    monkeypatch.setattr(module, '_publish_directory', fail)
    with pytest.raises(OSError, match='injected'):
        vault.materialize(receipt.snapshot_id, tmp_path / 'new', expected_token=receipt.transfer_token)
    assert not (tmp_path / 'new').exists()
    assert not list(tmp_path.glob('.carr-snapshot-*'))


@pytest.mark.parametrize('link', ['symbolic', 'hard'])
def test_linked_payload_file_is_rejected(tmp_path, link):
    source = payload(tmp_path / 'prepared')
    external = tmp_path / 'private'
    external.write_bytes(b'not authorized')
    if link == 'symbolic':
        os.symlink(external, source / 'linked')
    else:
        os.link(external, source / 'linked')
    vault = Vault(tmp_path / 'vault')
    with pytest.raises(VaultError):
        vault.publish(source, expected_head=None)
    assert vault.head() is None and not chunks(vault)
    assert external.read_bytes() == b'not authorized'


def test_linked_ancestor_and_directory_are_rejected(tmp_path):
    source = payload(tmp_path / 'prepared')
    alias = tmp_path / 'alias'
    os.symlink(tmp_path, alias, target_is_directory=True)
    with pytest.raises(VaultError):
        Vault(alias / 'vault')
    vault = Vault(tmp_path / 'vault')
    os.symlink(tmp_path, source / 'linked directory', target_is_directory=True)
    with pytest.raises(VaultError):
        vault.publish(source, expected_head=None)


@pytest.mark.parametrize('path', ['https://host/data', 'file:///tmp/data', '//server/share',
                                  '\\\\server\\share', 'relative', '/tmp/../escape',
                                  'C:remote\\name', '/tmp/bad\x00name'])
def test_nonlocal_ambiguous_or_traversal_roots_are_rejected(path):
    with pytest.raises(VaultError):
        Vault(path)


@pytest.mark.parametrize('path', ['../escape', '/absolute', 'C:\\escape', '.', 'NUL',
                                  'nested//file', 'nested/../file', 'file.',
                                  'COM¹.txt', 'LPT²', 'CON .txt', 'nested/LPT³.log'])
def test_even_independently_hashed_manifest_cannot_escape_destination(tmp_path, path):
    source = payload(tmp_path / 'prepared')
    vault = Vault(tmp_path / 'vault')
    receipt = vault.publish(source, expected_head=None)
    original = vault.root / 'snapshots' / (receipt.snapshot_id + '.json')
    value = json.loads(original.read_bytes())
    value['files'][0]['path'] = path
    raw = module._canonical(value)
    identity = sha256(raw).hexdigest()
    (vault.root / 'snapshots' / (identity + '.json')).write_bytes(raw)
    with pytest.raises(VaultError):
        vault.materialize(identity, tmp_path / 'new', expected_token=identity)
    assert not (tmp_path / 'new').exists()


def test_resource_budgets_and_unrelated_files_are_preserved(tmp_path):
    source = payload(tmp_path / 'prepared')
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'unrelated.bin').write_bytes(b'keep')
    vault = Vault(root, max_total_bytes=1)
    with pytest.raises(VaultError, match='budget'):
        vault.publish(source, expected_head=None)
    assert (root / 'unrelated.bin').read_bytes() == b'keep'
    assert not chunks(vault) and vault.head() is None
    vault = Vault(root, max_files=1)
    with pytest.raises(VaultError, match='budget'):
        vault.publish(source, expected_head=None)
    assert (root / 'unrelated.bin').read_bytes() == b'keep'


def test_path_bytes_are_bounded_during_inventory_before_chunk_writes(tmp_path):
    source = tmp_path / 'prepared'
    source.mkdir()
    for index in range(24):
        (source / (f'{index:02d}-' + 'a' * 220)).write_bytes(b'opaque')
    vault = Vault(tmp_path / 'vault', max_manifest_bytes=4096)
    with pytest.raises(VaultError, match='paths.*budget'):
        vault.publish(source, expected_head=None)
    assert vault.head() is None and not chunks(vault)


def test_exclusive_file_publication_on_the_actual_platform(tmp_path, monkeypatch):
    from carr_library import file_publication
    destination = tmp_path / 'receipt.json'
    calls = []
    operation = file_publication.publish_directory_no_replace
    def exclusive(source, target):
        calls.append('native-exclusive-rename')
        return operation(source, target)
    monkeypatch.setattr(file_publication, 'publish_directory_no_replace', exclusive)
    module._write_file(destination, b'complete receipt')
    assert destination.read_bytes() == b'complete receipt'
    assert destination.stat().st_nlink == 1
    with pytest.raises(FileExistsError):
        module._write_file(destination, b'must not replace')
    assert destination.read_bytes() == b'complete receipt'
    assert calls == ['native-exclusive-rename'] * 2
    assert not list(tmp_path.glob('.snapshot-*.tmp'))

def test_unknown_files_are_not_deleted_by_listing_or_resume(tmp_path):
    source = payload(tmp_path / 'prepared')
    vault = Vault(tmp_path / 'vault')
    (vault.root / 'snapshots' / 'private-note').write_bytes(b'keep')
    (vault.root / 'chunks' / 'unrelated').write_bytes(b'keep too')
    receipt = vault.publish(source, expected_head=None)
    assert vault.list_snapshots() == (receipt,)
    assert (vault.root / 'snapshots' / 'private-note').read_bytes() == b'keep'
    assert (vault.root / 'chunks' / 'unrelated').read_bytes() == b'keep too'


def test_selected_root_replacement_is_rejected(tmp_path):
    vault = Vault(tmp_path / 'vault')
    vault.root.rename(tmp_path / 'original-vault')
    Vault(tmp_path / 'vault')
    with pytest.raises(VaultError, match='changed'):
        vault.head()


def test_nested_source_vault_and_destination_are_refused(tmp_path):
    source = payload(tmp_path / 'prepared')
    vault = Vault(source / 'vault')
    with pytest.raises(VaultError, match='separate'):
        vault.publish(source, expected_head=None)
    standalone = Vault(tmp_path / 'standalone')
    receipt = standalone.publish(source / 'папка', expected_head=None)
    with pytest.raises(VaultError, match='separate'):
        standalone.materialize(receipt.snapshot_id, standalone.root / 'output',
                               expected_token=receipt.transfer_token)


def test_empty_opaque_payload_and_progress_failure_do_not_confuse_completion(tmp_path):
    source = tmp_path / 'empty'
    source.mkdir()
    vault = Vault(tmp_path / 'vault')
    receipt = vault.publish(source, expected_head=None)
    assert receipt.total_bytes == 0 and receipt.file_count == 0
    assert tree(vault.materialize(receipt.snapshot_id, tmp_path / 'copy',
                expected_token=receipt.transfer_token)) == {}
    source = payload(tmp_path / 'prepared')
    def fail(event):
        raise RuntimeError('progress observer failed')
    with pytest.raises(RuntimeError, match='observer'):
        vault.publish(source, expected_head=receipt.snapshot_id, on_progress=fail)
    assert vault.head() == receipt.snapshot_id


def test_open_existing_never_creates_or_adopts_missing_metadata(tmp_path):
    absent = tmp_path / 'absent'
    with pytest.raises((OSError, ValueError)):
        Vault.open_existing(absent)
    assert not absent.exists()
    unknown = tmp_path / 'unknown'
    unknown.mkdir()
    (unknown / 'keep').write_bytes(b'owner data')
    with pytest.raises((OSError, ValueError)):
        Vault.open_existing(unknown)
    assert tree(unknown) == {'keep': b'owner data'}
    original = Vault(tmp_path / 'known')
    marker = original.root / 'vault.json'
    before = marker.read_bytes(), marker.stat().st_mtime_ns
    opened = Vault.open_existing(original.root)
    assert opened.head() is None
    assert (marker.read_bytes(), marker.stat().st_mtime_ns) == before


def test_post_materialization_sync_failure_reports_visible_complete_destination(tmp_path, monkeypatch):
    source = payload(tmp_path / 'prepared')
    vault = Vault(tmp_path / 'vault')
    receipt = vault.publish(source, expected_head=None)
    target = tmp_path / 'visible'
    sync = module._sync_directory
    def fail_after_publication(path):
        if Path(path) == target.parent and target.exists():
            raise OSError('Directory sync unavailable after rename')
        return sync(path)
    monkeypatch.setattr(module, '_sync_directory', fail_after_publication)
    with pytest.raises(module.VaultMaterializedError) as error:
        vault.materialize(receipt.snapshot_id, target, expected_token=receipt.transfer_token)
    assert error.value.destination == target
    assert error.value.transfer_token == receipt.transfer_token
    assert tree(target) == tree(source)
