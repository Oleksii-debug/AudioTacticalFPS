"""Actual signed targets and retained bytes; a checkpoint never grants installation."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256, sha512
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import threading

import pytest
import carr_library
from tuf.api.exceptions import RepositoryError

from carr_library import durable_state
from carr_library.security._files import TrustError, json_bytes
from carr_library.security.download_state import PausedDownload
from carr_library.security.updates import LocalRepositoryClient, require_verified_package


def selected(repository, tmp_path, *, length=512 * 1024):
    raw = (bytes(range(256)) * ((length + 255) // 256))[:length]
    info = repository.publish(raw)
    policy = repository.policy(max_package_bytes=max(1024**2, length))
    client = LocalRepositoryClient(tmp_path / 'cache', policy)
    review = client.check(repository.root, repository.target_path)
    original = repository.root / 'targets' / (info.hashes['sha256'] + '.' + repository.target_path)
    return raw, client, review, original


def paused(repository, tmp_path, *, length=512 * 1024, after=65536):
    raw, client, review, original = selected(repository, tmp_path, length=length)
    stop = threading.Event()
    reports = []
    def progress(done, total):
        reports.append((done, total))
        if done >= after:
            stop.set()
    result = client.download(repository.root, review.target, reviewed_sha256=review.sha256,
                             paused=stop.is_set, progress=progress)
    assert type(result) is PausedDownload and result.state == 'paused'
    assert 0 < result.received < len(raw)
    roots = list(client.downloads.glob('.paused-*'))
    assert len(roots) == 1
    folder = roots[0]
    record = json.loads((folder / 'checkpoint.json').read_bytes())
    assert record['received'] == result.received
    assert (folder / 'package.part').read_bytes() == raw[:result.received]
    assert record['prefix_sha256'] == sha256(raw[:result.received]).hexdigest()
    assert not list(client.downloads.glob('*.zip'))
    return raw, client, review, original, result, folder, reports


def test_pause_restart_and_resume_use_real_tuf_and_exact_retained_source_offset(repository, tmp_path, monkeypatch):
    raw, client, review, original, partial, folder, reports = paused(repository, tmp_path)
    cached = {p.name: p.read_bytes() for p in folder.iterdir()}
    reopened = LocalRepositoryClient(client.cache, client.policy)
    assert reopened.check(repository.root, review.target) == review
    assert reopened.paused_download(repository.root, review) == partial
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == cached
    with pytest.raises(TrustError, match='verified package'):
        require_verified_package(partial)
    calls, positions = [], []
    actual = durable_state._open_json_file
    class Observed:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__(); return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def seek(self, offset, *args):
            positions.append(offset)
            return self.stream.seek(offset, *args)
        def read(self, count=-1):
            value = self.stream.read(count)
            calls.append(len(value))
            return value
    def opened(path):
        stream = actual(path)
        return Observed(stream) if Path(path) == original else stream
    monkeypatch.setattr(durable_state, '_open_json_file', opened)
    package = reopened.download(repository.root, review.target, reviewed_sha256=review.sha256,
                                resume=True, paused=lambda: False)
    assert require_verified_package(package) is package
    assert package.path.read_bytes() == raw and package.release == review
    assert package.path.stat().st_nlink == 1
    assert positions == [partial.received] and sum(calls) == len(raw) - partial.received
    assert not folder.exists() and original.read_bytes() == raw
    assert all(total == len(raw) and 0 <= done <= total for done, total in reports)


def test_a_new_download_never_silently_overwrites_an_existing_paused_fragment(repository, tmp_path):
    _raw, client, review, _original, _partial, folder, _reports = paused(repository, tmp_path)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    with pytest.raises(TrustError, match='Resume or Cancel'):
        client.download(repository.root, review.target, reviewed_sha256=review.sha256,
                        paused=lambda: False)
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert not list(client.downloads.glob('*.zip'))


@pytest.mark.parametrize('mutation', ['bytes', 'replacement', 'linked', 'record-field', 'record-count',
                                    'record-source', 'record-release', 'oversized-record'])
def test_tampered_partial_cannot_become_package_authority(repository, tmp_path, mutation):
    raw, client, review, original, partial, folder, _reports = paused(repository, tmp_path)
    path, record_path = folder / 'package.part', folder / 'checkpoint.json'
    record = json.loads(record_path.read_bytes())
    if mutation == 'bytes':
        path.write_bytes(b'x' * partial.received)
    elif mutation == 'replacement':
        new = folder / 'replacement'
        new.write_bytes(path.read_bytes())
        os.replace(new, path)
    elif mutation == 'linked':
        os.link(path, tmp_path / 'foreign-alias')
    elif mutation == 'record-field':
        record['instruction'] = 'install'
    elif mutation == 'record-count':
        record['received'] = True
    elif mutation == 'record-source':
        record['binding']['target'] = 'other.zip'
    elif mutation == 'record-release':
        record['release']['notes'] = 'unreviewed metadata'
    elif mutation == 'oversized-record':
        record_path.write_bytes(b' ' * 32769)
    if mutation.startswith('record-'):
        record_path.write_bytes(json_bytes(record))
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    with pytest.raises((TrustError, OSError, RepositoryError)):
        client.download(repository.root, review.target, reviewed_sha256=review.sha256,
                        resume=True, paused=lambda: False)
    assert not list(client.downloads.glob('*.zip'))
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert original.read_bytes() == raw


def test_recomputed_local_checkpoint_hash_cannot_replace_signed_full_target_hash(repository, tmp_path):
    raw, client, review, original, partial, folder, _reports = paused(repository, tmp_path)
    path, record_path = folder / 'package.part', folder / 'checkpoint.json'
    path.write_bytes(b'x' * partial.received)
    record = json.loads(record_path.read_bytes())
    record['prefix_sha256'] = sha256(path.read_bytes()).hexdigest()
    record_path.write_bytes(json_bytes(record))
    with pytest.raises(RepositoryError):
        client.download(repository.root, review.target, reviewed_sha256=review.sha256,
                        resume=True, paused=lambda: False)
    assert not list(client.downloads.glob('*.zip'))
    assert original.read_bytes() == raw


def test_every_tuf_hash_is_verified_after_resume_not_only_the_ui_sha256(repository, tmp_path):
    raw, client, review, original, _partial, _folder, _reports = paused(repository, tmp_path)
    target = repository.md['targets'].signed.targets[review.target]
    target.hashes['sha512'] = '0' * 128
    assert sha512(raw).hexdigest() != target.hashes['sha512']
    repository.advance(); repository.flush()
    with pytest.raises(RepositoryError):
        client.download(repository.root, review.target, reviewed_sha256=review.sha256,
                        resume=True, paused=lambda: False)
    assert not list(client.downloads.glob('*.zip')) and original.read_bytes() == raw


@pytest.mark.parametrize('mutation', ['expired', 'changed-target', 'wrong-signed-channel'])
def test_resume_reauthenticates_current_signed_metadata_before_consuming_partial(repository, tmp_path, mutation):
    raw, client, review, original, _partial, folder, _reports = paused(repository, tmp_path)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    repository.advance()
    if mutation == 'expired':
        repository.md['timestamp'].signed.expires = datetime.now(timezone.utc) - timedelta(seconds=1)
        repository.flush()
    else:
        repository.publish(raw if mutation == 'wrong-signed-channel' else b'changed',
                           build=3, custom={'channel': 'other'} if mutation == 'wrong-signed-channel' else None)
    with pytest.raises((TrustError, RepositoryError)):
        client.download(repository.root, review.target, reviewed_sha256=review.sha256,
                        resume=True, paused=lambda: False)
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert not list(client.downloads.glob('*.zip'))
    assert original.read_bytes() == raw


def test_cancel_removes_only_bound_partial_and_never_removes_other_cache_or_source(repository, tmp_path):
    raw, client, review, original, _partial, folder, _reports = paused(repository, tmp_path)
    unrelated = client.downloads / 'separate-user-file.txt'
    unrelated.write_bytes(b'keep')
    assert client.cancel_download(repository.root, review) is True
    assert not folder.exists() and unrelated.read_bytes() == b'keep'
    assert original.read_bytes() == raw
    assert client.cancel_download(repository.root, review) is False


def test_unknown_partial_entry_blocks_recursive_cleanup(repository, tmp_path):
    _raw, client, review, _original, _partial, folder, _reports = paused(repository, tmp_path)
    foreign = folder / 'do-not-remove'
    foreign.write_bytes(b'foreign')
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    with pytest.raises(TrustError, match='unknown'):
        client.cancel_download(repository.root, review)
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before


def test_uncheckpointed_tail_is_discarded_only_on_same_inode_after_verified_prefix(repository, tmp_path):
    raw, client, review, original, partial, folder, _reports = paused(repository, tmp_path)
    path = folder / 'package.part'
    identity = path.stat().st_ino
    with path.open('ab') as stream:
        stream.write(b'uncheckpointed crash tail')
    before = path.read_bytes()
    assert client.paused_download(repository.root, review) == partial
    assert path.read_bytes() == before, 'Inspection must not truncate or resume.'
    result = client.download(repository.root, review.target, reviewed_sha256=review.sha256,
                             resume=True, paused=lambda: False)
    assert result.path.read_bytes() == raw and original.read_bytes() == raw
    assert not folder.exists() and identity > 0


def test_low_disk_does_not_open_a_target_or_replace_the_retained_fragment(repository, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from carr_library.security import updates
    _raw, client, review, _original, _partial, folder, _reports = paused(repository, tmp_path)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    monkeypatch.setattr(updates.shutil, 'disk_usage', lambda _path: SimpleNamespace(free=0))
    with pytest.raises(TrustError, match='space'):
        client.download(repository.root, review.target, reviewed_sha256=review.sha256,
                        resume=True, paused=lambda: False)
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert not list(client.downloads.glob('*.zip'))


def test_cancel_during_actual_prefix_verification_does_not_create_a_verified_package(repository, tmp_path):
    _raw, client, review, _original, _partial, folder, _reports = paused(repository, tmp_path, after=131072)
    cancelled = threading.Event()
    from carr_library.security import download_state
    actual = download_state._write_stream
    class Interrupting:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def read(self, size):
            block = self.stream.read(size)
            cancelled.set()
            return block
    def opened(*args, **kwargs):
        return Interrupting(actual(*args, **kwargs))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(download_state, '_write_stream', opened)
        from carr_library.security.updates import UpdateCancelled
        with pytest.raises(UpdateCancelled):
            client.download(repository.root, review.target, reviewed_sha256=review.sha256,
                            resume=True, paused=lambda: False, cancelled=cancelled.is_set)
    assert not list(client.downloads.glob('*.zip'))
    assert not folder.exists(), 'Confirmed cancellation discards only its selected fragment.'


def _selected_package():
    root = Path(carr_library.__file__).resolve().parent
    checkout = Path(__file__).resolve().parents[2] / 'src/carr_library'
    allowed = [str(root)]
    if root == checkout.resolve():
        allowed.append(str((Path(sysconfig.get_paths()['purelib']) / 'carr_library').resolve()))
    inventory = {str(path.relative_to(root)).replace(os.sep, '/'): sha256(path.read_bytes()).hexdigest()
                 for path in root.rglob('*.py')}
    return {'roots': allowed, 'files': inventory}


def test_actual_parent_owned_crash_resumes_only_last_durable_prefix(repository, tmp_path):
    raw, client, review, original = selected(repository, tmp_path, length=2 * 1024**2)
    selection = _selected_package()
    bootstrap = tmp_path / 'selected-bootstrap.json'
    bootstrap.write_bytes(repository.bootstrap)
    child = r'''
from hashlib import sha256
import json, os, sys
from pathlib import Path
import carr_library
selection = json.loads(sys.argv[1])
root = Path(carr_library.__file__).resolve().parent
assert str(root) in selection['roots']
actual = {str(p.relative_to(root)).replace(os.sep, '/'): sha256(p.read_bytes()).hexdigest()
          for p in root.rglob('*.py')}
assert actual == selection['files'], 'child source bytes differ before repository use'
from carr_library.security.updates import LocalRepositoryClient, UpdatePolicy
bootstrap = Path(sys.argv[3]).read_bytes()
policy = UpdatePolicy('carr-test', 'stable', bootstrap, sha256(bootstrap).hexdigest(), 1,
                      max_package_bytes=2*1024**2, max_unpacked_bytes=2*1024**2, max_files=100)
client = LocalRepositoryClient(Path(sys.argv[4]), policy)
release = client.check(Path(sys.argv[2]), 'release.zip')
def observed(received, total):
    if received >= 1024**2 + 65536:
        os._exit(73)
client.download(Path(sys.argv[2]), release.target, reviewed_sha256=release.sha256,
                paused=lambda: False, progress=observed)
raise AssertionError('actual crash boundary was not reached')
'''
    process = subprocess.Popen([sys.executable, '-I', '-B', '-c', child,
        json.dumps(selection, sort_keys=True), str(repository.root), str(bootstrap), str(client.cache)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail('Owned crash child exceeded its bound: ' + repr((stdout, stderr)))
    finally:
        if process.poll() is None:
            process.kill(); process.communicate(timeout=5)
    assert process.returncode == 73, (stdout, stderr)
    assert stdout == stderr == b''
    partial = client.paused_download(repository.root, review)
    assert partial.state == 'interrupted' and partial.received == 1024**2
    result = client.download(repository.root, review.target, reviewed_sha256=review.sha256,
                             resume=True, paused=lambda: False)
    assert require_verified_package(result) is result
    assert result.path.read_bytes() == raw and original.read_bytes() == raw
    assert not tuple(client.downloads.glob('.paused-*'))



@pytest.mark.parametrize('error_type', [OSError, KeyboardInterrupt])
@pytest.mark.parametrize('mutation', ['unchanged', 'different-bytes', 'equal-replacement', 'removed'])
def test_verified_publication_survives_only_cleanup_failure_with_exact_saved_inode(
        repository, tmp_path, monkeypatch, error_type, mutation):
    from carr_library.security import download_state
    raw, client, review, original = selected(repository, tmp_path)
    destination = client.downloads / (review.sha256 + '.zip')
    observed = []
    def failed_cleanup(transfer):
        observed.append(destination.stat().st_ino)
        assert destination.read_bytes() == raw
        assert destination.stat().st_nlink == 1
        if mutation == 'different-bytes':
            destination.write_bytes(b'x' * len(raw))
        elif mutation == 'equal-replacement':
            replacement = client.downloads / 'replacement'
            replacement.write_bytes(raw)
            os.replace(replacement, destination)
        elif mutation == 'removed':
            destination.unlink()
        raise error_type('owned fragment cleanup was not confirmed')
    monkeypatch.setattr(download_state.TargetDownload, 'discard', failed_cleanup)
    if mutation == 'unchanged':
        result = client.download(repository.root, review.target, reviewed_sha256=review.sha256,
                                 paused=lambda: False)
        assert require_verified_package(result) is result
        assert result.path.read_bytes() == raw and result.path.stat().st_ino == observed[0]
        assert 'exact verified package is present' in client.download_notice
        assert 'cleanup was not confirmed' in client.download_notice
        assert tuple(client.downloads.glob('.paused-*')), 'The notice must describe the actual remaining fragment.'
    else:
        with pytest.raises((TrustError, OSError, RepositoryError)):
            client.download(repository.root, review.target, reviewed_sha256=review.sha256,
                            paused=lambda: False)
        assert client.download_notice == ''
        if mutation == 'different-bytes':
            assert destination.read_bytes() == b'x' * len(raw)
        elif mutation == 'equal-replacement':
            assert destination.read_bytes() == raw and destination.stat().st_ino != observed[0]
        else:
            assert not destination.exists()
    assert len(observed) == 1 and original.read_bytes() == raw
