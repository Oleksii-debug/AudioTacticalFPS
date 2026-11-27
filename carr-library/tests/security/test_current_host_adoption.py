"""Current-host boundaries; synthetic authorities never activate a product."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
import carr_library

from carr_library.audio import engines
from carr_library.security import installation, updates
from carr_library.security._files import TrustError
from carr_library.security.anchors import Anchor, AnchorUnavailable, WindowsVaultAnchor
from carr_library.security.updates import LocalRepositoryClient
from .conftest import REFERENCE, bundle
from .test_canonical_boundary import anchor_fixture
from .test_publication_integrity import release_fixture


def test_private_status_works_without_optional_crypto_network_or_background_workers(tmp_path):
    code = """import importlib.abc, pathlib, sys, threading
sys.path.insert(0, sys.argv[1])
class NoOptionalCrypto(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'cryptography','tuf','securesystemslib'}:
            raise AssertionError('Private default tried to load optional crypto')
sys.meta_path.insert(0, NoOptionalCrypto())
def no_network(event, args):
    if event in {'socket.connect','socket.bind','socket.getaddrinfo','subprocess.Popen'}:
        raise AssertionError('Private default tried external activity')
sys.addaudithook(no_network)
before = {thread.ident for thread in threading.enumerate()}
from carr_library.security.__main__ import main
assert main(['status']) == 0
assert {thread.ident for thread in threading.enumerate()} == before
assert not tuple(pathlib.Path.cwd().iterdir())
"""
    result = subprocess.run([sys.executable, '-I', '-B', '-c', code,
        str(Path(carr_library.__file__).resolve().parent.parent)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'WAITING_INPUT' in result.stdout and 'Private perpetual' in result.stdout
    assert 'No signed update source' in result.stdout


@pytest.mark.parametrize('field', ['product', 'profile_id', 'binding'])
def test_current_vault_target_separates_product_profile_and_client_root(field):
    vault, valid = anchor_fixture()
    original, calls = dict(vault.values), len(vault.calls)
    values = dict(product='carr-test', profile_id='trust-test', binding='1' * 64)
    values[field] = '2' * 64 if field == 'binding' else 'other'
    different = WindowsVaultAnchor(REFERENCE, values.pop('binding'), vault=vault, **values)
    assert different.vault_binding != valid.vault_binding
    with pytest.raises(AnchorUnavailable):
        different.compare_and_set(Anchor(), Anchor(1, 'a' * 64, 1))
    assert vault.values == original
    assert vault.calls[calls:] == [('get', REFERENCE, different.vault_binding)]
    assert valid.load() == Anchor()


def test_real_process_death_after_tuf_publication_leaves_one_link_and_reusable_package(
        repository, tmp_path):
    raw = bundle('B')
    repository.publish(raw)
    cache = tmp_path / 'crash-cache'
    code = """import hashlib, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from carr_library.security import updates
policy = updates.UpdatePolicy('carr-test','stable',bytes.fromhex(sys.argv[4]),
    hashlib.sha256(bytes.fromhex(sys.argv[4])).hexdigest(),1,
    max_package_bytes=1024**2,max_unpacked_bytes=2*1024**2,max_files=100)
client = updates.LocalRepositoryClient(sys.argv[3],policy)
release = client.check(sys.argv[2],'release.zip')
publish = updates.publish_noreplace
def crash_after_move(source,destination):
    publish(source,destination)
    os._exit(73)
updates.publish_noreplace = crash_after_move
client.download(sys.argv[2],'release.zip',reviewed_sha256=release.sha256)
raise AssertionError('Actual crash boundary was not reached')
"""
    process = subprocess.run([sys.executable, '-I', '-B', '-c', code,
        str(Path(carr_library.__file__).resolve().parent.parent), str(repository.root),
        str(cache), repository.bootstrap.hex()], capture_output=True, timeout=30)
    assert process.returncode == 73, process.stderr.decode('utf-8', 'replace')
    client = LocalRepositoryClient(cache, repository.policy())
    published = client.downloads / (sha256(raw).hexdigest() + '.zip')
    assert published.read_bytes() == raw and published.stat().st_nlink == 1
    assert not list(cache.rglob('package.zip'))
    package = client.download(repository.root, repository.target_path,
                              reviewed_sha256=sha256(raw).hexdigest())
    assert package.path == published and published.stat().st_nlink == 1


def test_tuf_publication_preserves_a_concurrent_destination(repository, tmp_path, monkeypatch):
    raw = bundle('B')
    repository.publish(raw)
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    publish = updates.publish_noreplace
    competing = []
    def race(source, destination):
        destination.write_bytes(b'Another operation owns these exact bytes.')
        competing.append(destination)
        return publish(source, destination)
    monkeypatch.setattr(updates, 'publish_noreplace', race)
    with pytest.raises(FileExistsError):
        client.download(repository.root, repository.target_path,
                        reviewed_sha256=sha256(raw).hexdigest())
    assert len(competing) == 1
    assert competing[0].read_bytes() == b'Another operation owns these exact bytes.'
    assert competing[0].stat().st_nlink == 1


@pytest.mark.parametrize('code', [0, 23], ids=['success', 'failure'])
def test_installer_uses_actual_owned_child_and_preserves_exit_status(tmp_path, code):
    assert installation._owned_process(
        [sys.executable, '-I', '-B', '-c', 'import sys; raise SystemExit(int(sys.argv[1]))', str(code)],
        cwd=tmp_path, env=installation.WindowsBundleAdapter._environment(), timeout=30) == code


def test_installer_timeout_cleans_the_actual_started_child(tmp_path, monkeypatch):
    ready, children = tmp_path / 'ready', []
    original = engines._start_process
    def start(arguments, **options):
        child = original(arguments, **options)
        children.append(child)
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            assert ready.read_bytes() == b'ready'
            return child
        except BaseException:
            engines._stop(child)
            raise
    monkeypatch.setattr(installation, '_start_process', start)
    with pytest.raises(TrustError, match='time budget'):
        installation._owned_process(
            [sys.executable, '-I', '-B', '-c',
             "from pathlib import Path; import sys,time; Path(sys.argv[1]).write_bytes(b'ready'); time.sleep(60)",
             str(ready)], cwd=tmp_path, env=installation.WindowsBundleAdapter._environment(), timeout=.1)
    assert len(children) == 1 and children[0].poll() is not None


@pytest.mark.parametrize('failure', ['native', 'self-test', 'version', None],
                         ids=['native-check-failed', 'self-test-failed', 'wrong-version', 'verified'])
def test_health_requires_current_native_checks_self_test_and_exact_version(
        tmp_path, monkeypatch, failure):
    root, release, _ = release_fixture(tmp_path)
    executable = root / '.venv' / 'Scripts' / 'python.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'Inert synthetic executable; never execute.')
    (root / '.venv' / '.installation-complete').write_bytes(b'')
    calls = []
    def probe(arguments, **options):
        assert options['cwd'] == root
        calls.append(arguments)
        if arguments[5:7] == ['-m', 'carr_library.native_checks']:
            assert arguments[7:] == ['--bundle-root', str(root)]
            return int(failure == 'native')
        if arguments[5:7] == ['-m', 'carr_library']:
            assert arguments[7:] == ['--self-test']
            return int(failure == 'self-test')
        assert arguments[5] == '-c' and arguments[-1] == release.version
        return int(failure == 'version')
    monkeypatch.setattr(installation, '_owned_process', probe)
    from .conftest import installer_verify
    assert installation.WindowsBundleAdapter(installer_verify()).health(root, release) is (failure is None)
    assert len(calls) == {'native': 1, 'self-test': 2, 'version': 3, None: 3}[failure]


def test_installer_child_environment_cannot_inherit_report_or_key_log_paths(monkeypatch):
    monkeypatch.setenv('FFREPORT', 'file=private-output')
    monkeypatch.setenv('SSLKEYLOGFILE', 'private-key-output')
    monkeypatch.setenv('PYTHONPATH', 'foreign-code')
    monkeypatch.setenv('PIP_INDEX_URL', 'https://not-a-configured-authority.invalid')
    environment = installation.WindowsBundleAdapter._environment()
    assert all(name not in environment for name in ('FFREPORT','SSLKEYLOGFILE','PYTHONPATH','PIP_INDEX_URL'))
    assert environment['PIP_NO_INDEX'] == '1'
