"""Real TUF snapshots, native lease exclusion and owned handoff processes.

All publisher keys, package bytes, runtime markers and setup execution adapters
are explicit fixtures. No production endpoint, Windows setup or acoustic claim.
"""
import base64
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from carr_library.library_backup import restore_backup
from carr_library.application import LibraryApp
from carr_library.security import update_handoff as handoff
from carr_library.security import update_host as supervisor
from carr_library.security._files import TrustError, atomic_write, json_bytes
from carr_library.security.updates import LocalRepositoryClient

from .conftest import Repository
from .test_managed_installation import managed, maintenance, _assert_original


@pytest.fixture
def configuration(tmp_path, repository):
    runtime, root, work, library = [tmp_path / name for name in ('external-host', 'installed', 'work', 'research')]
    for path in (runtime, root, work, library):
        path.mkdir()
    python, helper = runtime / 'python', runtime / 'maintenance.py'
    python.write_bytes(b'Explicit inert interpreter marker; never executed')
    helper.write_bytes(b'Explicit inert maintenance marker; never imported')
    files = {p.name: sha256(p.read_bytes()).hexdigest() for p in (python, helper)}
    sealed = json_bytes({'schema': 1, 'files': files})
    manifest = tmp_path / 'host-manifest.json'
    manifest.write_bytes(sealed)
    policy = repository.policy()
    publisher = asdict(policy)
    publisher['bootstrap'] = base64.b64encode(policy.bootstrap).decode('ascii')
    value = dict(schema=1, kind='carr-managed-update-host',
        runtime=dict(root=str(runtime), manifest=str(manifest), sha256=sha256(sealed).hexdigest(),
                     python='python', maintenance='maintenance.py'),
        installation=dict(root=str(root), work=str(work)), library=str(library), publisher=publisher,
        repository=dict(endpoint=None, target=repository.target_path, tls_ca_pem=None, timeout=60),
        anchor=dict(reference='carr/provider/update-test/' + '1' * 32, binding='2' * 64, profile_id='update-test'),
        close_timeout=3, operation_timeout=60)
    path = tmp_path / 'owner-configuration.json'
    raw = json_bytes(value)
    path.write_bytes(raw)
    return handoff.OwnerUpdateConfiguration(path, sha256(raw).hexdigest())


def _package(repository, cache):
    repository.publish(b'Only a signed synthetic target; never execute.')
    client = LocalRepositoryClient(cache, repository.policy())
    review = client.check(repository.root, repository.target_path)
    package = client.download(repository.root, repository.target_path, reviewed_sha256=review.sha256)
    return client, package


@pytest.mark.parametrize('consistent', [True, False])
def test_offline_handoff_reverifies_actual_signed_metadata_and_complete_target(tmp_path, consistent):
    repository = Repository(tmp_path / 'repo')
    if not consistent:
        repository.md['root'].signed.consistent_snapshot = False
        repository.bootstrap = repository.publish_root()
    repository.publish(b'Signed exact target for the fresh offline host.')
    if not consistent:
        for role in ('snapshot', 'targets'):
            (repository.root / 'metadata' / (role + '.json')).write_bytes(
                (repository.root / 'metadata' / ('1.' + role + '.json')).read_bytes())
        info = repository.md['targets'].signed.targets[repository.target_path]
        (repository.root / 'targets' / repository.target_path).write_bytes(
            (repository.root / 'targets' / (info.hashes['sha256'] + '.' + repository.target_path)).read_bytes())
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    release = client.check(repository.root, repository.target_path)
    package = client.download(repository.root, repository.target_path, reviewed_sha256=release.sha256)
    retained = handoff.capture_offline_repository(client, package, tmp_path / 'retained')
    isolated = LocalRepositoryClient(tmp_path / 'fresh-cache', repository.policy())
    copied = isolated.download(retained, repository.target_path, reviewed_sha256=release.sha256)
    assert copied.release == package.release and copied.path.read_bytes() == package.path.read_bytes()
    assert not list(retained.rglob('*.py')) and not (retained / '.venv').exists()
    next(iter((retained / 'targets').iterdir())).write_bytes(b'Changed after desktop approval')
    with pytest.raises(Exception):
        LocalRepositoryClient(tmp_path / 'third-cache', repository.policy()).download(
            retained, repository.target_path, reviewed_sha256=release.sha256)


@pytest.mark.parametrize('change', ['bytes', 'new-file', 'missing', 'hardlink', 'manifest'])
def test_exact_external_runtime_seal_refuses_every_unreviewed_payload_change(configuration, tmp_path, change):
    assert configuration.verify_runtime()['files'] == 2
    target = configuration.python
    if change == 'bytes':
        target.write_bytes(b'Changed executable marker')
    elif change == 'new-file':
        (configuration.runtime / 'unexpected.py').write_bytes(b'never load')
    elif change == 'missing':
        target.unlink()
    elif change == 'hardlink':
        os.link(target, tmp_path / 'same-marker')
    else:
        configuration.manifest_path.write_bytes(b'{}')
    with pytest.raises((TrustError, FileNotFoundError)):
        configuration.verify_runtime()


def test_same_byte_runtime_root_replacement_never_reuses_owner_configuration(configuration):
    root = configuration.runtime
    moved = root.with_name('retained-external-host')
    root.rename(moved)
    root.mkdir()
    for path in moved.iterdir():
        (root / path.name).write_bytes(path.read_bytes())
    with pytest.raises(TrustError, match='directory changed'):
        configuration.verify_runtime()
    assert all((root / path.name).read_bytes() == path.read_bytes() for path in moved.iterdir())


def test_owner_configuration_changes_are_refused_before_launch(configuration, monkeypatch):
    path = configuration.path
    value = json.loads(path.read_bytes())
    value['repository']['target'] = 'another.zip'
    path.write_bytes(json_bytes(value))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('Changed authority cannot launch.'))
    with pytest.raises(TrustError, match='authority changed'):
        configuration.check()


_CHILD = r'''
import hashlib, json, os, pathlib, sys, time
path, fault = pathlib.Path(sys.argv[1]), sys.argv[2]
request = json.loads((path / "request.json").read_bytes())
raw = json.dumps(request, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
value = {"schema": 1, "status": "ready", "request_sha256": hashlib.sha256(raw).hexdigest(),
         "pid": os.getpid(), "release": request["release"]}
if fault == "pid": value["pid"] += 1
if fault == "digest": value["request_sha256"] = "0" * 64
if fault == "status": value["status"] = "complete"
temp = path / "ready.partial"
temp.write_text(json.dumps(value), encoding="ascii")
temp.replace(path / "ready.json")
deadline = time.monotonic() + 4
while time.monotonic() < deadline:
    decision = path / "decision.json"
    if decision.exists():
        value = json.loads(decision.read_bytes())
        if value["action"] == "apply":
            (path / "fixture-applied").write_bytes(b"Only this synthetic child observed explicit commit.")
        break
    time.sleep(.01)
'''


@pytest.fixture
def owned_handoff(configuration, repository, tmp_path):
    owners = []
    def make(fault=''):
        _, package = _package(repository, tmp_path / ('cache-' + str(len(owners))))
        parent = configuration.work / 'handoffs'
        parent.mkdir(exist_ok=True)
        path = parent / ('%032x' % (len(owners) + 1))
        path.mkdir()
        request = dict(schema=1, nonce='3' * 32, created_at=int(time.time()),
            repository=str(path / 'repository'), release=asdict(package.release), restart=False,
            library_directories=[list(row) for row in handoff._directory_rows(configuration.library)],
            root_directories=[list(row) for row in handoff._directory_rows(configuration.root)])
        atomic_write(path / 'request.json', json_bytes(request))
        process = subprocess.Popen([sys.executable, '-I', '-B', '-c', _CHILD, str(path), fault],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        owner = handoff.UpdateHandoff(configuration, path, request, process)
        owners.append(owner)
        return owner
    yield make
    for owner in owners:
        if not owner.transferred:
            try:
                owner.abort()
            except TrustError:
                pass
        owner.process.wait(timeout=6)
        if owner.process.stderr is not None:
            owner.process.stderr.close()
        assert owner.process.poll() is not None


def test_real_supervisor_ready_never_means_apply_and_cancel_reaps_it(owned_handoff):
    owner = owned_handoff().wait_ready(timeout=5)
    assert owner.receipt()['status'] == 'PREPARED' and owner.process.poll() is None
    assert not (owner.path / 'fixture-applied').exists()
    owner.abort()
    assert owner.closed and owner.process.poll() == 0 and not owner.transferred
    assert not (owner.path / 'fixture-applied').exists()


@pytest.mark.parametrize('fault', ['pid', 'digest', 'status'])
def test_wrong_supervisor_ready_identity_is_refused_and_actual_child_is_reaped(owned_handoff, fault):
    owner = owned_handoff(fault)
    with pytest.raises(TrustError, match='exact reviewed update'):
        owner.wait_ready(timeout=5)
    assert owner.closed and owner.process.poll() is not None
    assert not (owner.path / 'fixture-applied').exists()


def test_only_actual_close_boolean_can_commit_the_exact_owned_handoff(owned_handoff):
    owner = owned_handoff().wait_ready(timeout=5)
    with pytest.raises(TrustError, match='actual approved desktop Close'):
        owner.commit_after_close(host_closed=False)
    assert not (owner.path / 'decision.json').exists()
    receipt = owner.commit_after_close(host_closed=True)
    owner.abort()  # A transferred supervisor must never be terminated by old UI cleanup.
    assert receipt['status'] == 'HANDED_OFF' and receipt['activation'] == 'NOT_ESTABLISHED'
    owner.process.wait(timeout=5)
    assert (owner.path / 'fixture-applied').is_file() and owner.transferred and not owner.closed


def test_visible_commit_survives_postpublication_error_without_terminating_host(owned_handoff, monkeypatch):
    owner = owned_handoff().wait_ready(timeout=5)
    original = handoff.atomic_write
    def publish(path, raw):
        original(path, raw)
        if Path(path).name == 'decision.json':
            raise OSError('Synthetic post-publication acknowledgment failure')
    monkeypatch.setattr(handoff, 'atomic_write', publish)
    with pytest.raises(OSError) as caught:
        owner.commit_after_close(host_closed=True)
    assert caught.value.update_handoff['status'] == 'HANDED_OFF'
    owner.abort()
    owner.process.wait(timeout=5)
    assert (owner.path / 'fixture-applied').is_file()


def test_uncertain_commit_retains_fixed_host_without_falsely_claiming_activation(owned_handoff, monkeypatch):
    owner = owned_handoff().wait_ready(timeout=5)
    def refuse(path, raw):
        raise OSError('Synthetic uncertainty before the decision could be observed')
    monkeypatch.setattr(handoff, 'atomic_write', refuse)
    with pytest.raises(OSError) as caught:
        owner.commit_after_close(host_closed=True)
    receipt = caught.value.update_handoff
    assert receipt['status'] == 'HANDOFF_UNCERTAIN' and receipt['activation'] == 'NOT_ESTABLISHED'
    owner.abort()
    owner.process.wait(timeout=6)
    assert not (owner.path / 'fixture-applied').exists()


def _configuration_for(managed):
    d = managed
    return SimpleNamespace(root=d.root, work=d.tx.work, library=d.app.root,
        policy=d.tx.policy, anchor=lambda: d.anchor, close_timeout=2, operation_timeout=60,
        check=lambda: None)


def _synthetic_adapter(monkeypatch, managed):
    def adapter(proxy, configuration, deadline, current):
        managed.adapter.maintenance = proxy
        return managed.adapter
    monkeypatch.setattr(supervisor, '_adapter', adapter)


def test_external_host_uses_actual_exclusive_lease_and_existing_complete_transaction(managed, monkeypatch, tmp_path):
    _synthetic_adapter(monkeypatch, managed)
    result, identity = supervisor._execute(_configuration_for(managed), maintenance,
        current=lambda: None, package=managed.package)
    assert result['status'] == 'complete' and identity['bundle_sha256'] == managed.hash_b
    assert managed.anchor.load().sequence == 2
    assert (managed.root / 'app/version.txt').read_bytes() == b'3.2.0'
    restored = LibraryApp(restore_backup(Path(result['backup']), tmp_path / 'restored',
                                       result['backup_sha256']))
    assert restored.search(managed.workspace, 'Ukrainian')[0].quote == 'Ukrainian'
    with maintenance.installation_lease(managed.root):
        assert managed.app.search(managed.workspace, 'Ukrainian')[0].quote == 'Ukrainian'


@pytest.mark.parametrize('fault', ['bootstrap', 'health', 'anchor'])
def test_external_host_failure_retains_exact_existing_rollback_and_backup(managed, monkeypatch, fault):
    _synthetic_adapter(monkeypatch, managed)
    if fault == 'anchor':
        managed.anchor.fail = True
    else:
        managed.adapter.failure = fault
    with pytest.raises((TrustError, RuntimeError)):
        supervisor._execute(_configuration_for(managed), maintenance,
            current=lambda: None, package=managed.package)
    _assert_original(managed)
    assert supervisor._phase(managed.tx.work) == 'rolled-back'
    assert list(managed.tx.work.glob('backup-*'))


def test_external_host_recovery_finishes_already_committed_b_without_rollback(managed, monkeypatch):
    _synthetic_adapter(monkeypatch, managed)
    original = maintenance.finish_upgrade
    monkeypatch.setattr(maintenance, 'finish_upgrade',
        lambda *a, **k: (_ for _ in ()).throw(OSError('Synthetic interrupted final marker')))
    with pytest.raises(OSError):
        supervisor._execute(_configuration_for(managed), maintenance,
            current=lambda: None, package=managed.package)
    assert managed.anchor.load().sequence == 2
    assert (managed.root / 'app/version.txt').read_bytes() == b'3.2.0'
    monkeypatch.setattr(maintenance, 'finish_upgrade', original)
    # Reuse the actual fixture adapter, but restore its original maintenance
    # delegate before the next real host operation supplies the new held lease.
    managed.adapter.maintenance = maintenance
    result, identity = supervisor._execute(_configuration_for(managed), maintenance,
        current=lambda: None, recover=True)
    assert result == 'complete' and identity['bundle_sha256'] == managed.hash_b
    assert maintenance.marker(managed.root)['state'] == 'complete'


def test_real_application_child_excludes_host_until_its_own_lease_is_released(managed, tmp_path):
    ready, release = tmp_path / 'ready', tmp_path / 'release'
    lease_path = Path(maintenance.lease_module.__file__)
    code = r'''
import importlib.util, pathlib, sys, time
name = "actual_update_lease_fixture"
spec = importlib.util.spec_from_file_location(name, sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)
root, ready, release = map(pathlib.Path, sys.argv[2:])
with module.installation_lease(root):
    ready.write_bytes(b"Actual shared native lease is held")
    until = time.monotonic() + 10
    while not release.exists():
        if time.monotonic() >= until: raise RuntimeError("Parent did not release the fixture")
        time.sleep(.01)
'''
    child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code, str(lease_path),
                              str(managed.root), str(ready), str(release)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists():
            assert child.poll() is None and time.monotonic() < deadline
            time.sleep(.01)
        with pytest.raises(TimeoutError, match='Close Resonance'):
            with supervisor._wait_for_installation(maintenance, managed.root,
                    current=lambda: None, deadline=time.monotonic() + .1):
                pytest.fail('A live application lease cannot be bypassed.')
        assert child.poll() is None and managed.adapter.started == 0
        _assert_original(managed)
        release.write_bytes(b'Let the real application child close normally')
        child.wait(timeout=5)
        with supervisor._wait_for_installation(maintenance, managed.root,
                current=lambda: None, deadline=time.monotonic() + 2) as owner:
            owner.check(managed.root, True)
    finally:
        if child.poll() is None:
            release.write_bytes(b'Release fixture during test cleanup')
            child.wait(timeout=5)
        if child.stderr is not None:
            child.stderr.close()
    assert child.returncode == 0


@pytest.mark.parametrize('location', ['runtime', 'root', 'work', 'library'])
def test_runtime_manifest_survives_only_outside_every_managed_payload(configuration, location, monkeypatch):
    selected = getattr(configuration, location) / 'nested-seal'
    selected.mkdir()
    seal = selected / 'runtime-seal.json'
    seal.write_bytes(configuration.manifest_path.read_bytes())
    value = json.loads(configuration.raw)
    value['runtime']['manifest'] = str(seal)
    encoded = json_bytes(value)
    configuration.path.write_bytes(encoded)
    monkeypatch.setattr(handoff.subprocess, 'Popen',
        lambda *a, **k: pytest.fail('Unsafe manifest placement must refuse before any host launch.'))
    with pytest.raises(TrustError, match='manifest must survive'):
        handoff.OwnerUpdateConfiguration(configuration.path, sha256(encoded).hexdigest())
    assert seal.read_bytes() == configuration.manifest_path.read_bytes()
    assert not (configuration.work / 'handoffs').exists()
