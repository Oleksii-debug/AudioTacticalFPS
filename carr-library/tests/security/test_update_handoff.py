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
(path / "fixture-runtime.json").write_text(json.dumps({
    "pid": os.getpid(), "executable": sys.executable, "prefix": sys.prefix,
    "base_executable": sys._base_executable, "isolated": sys.flags.isolated,
    "user_site_disabled": sys.flags.no_user_site,
    "launcher_override_present": "__PYVENV_LAUNCHER__" in os.environ,
    "pythonpath_present": "PYTHONPATH" in os.environ,
    "virtual_env_present": "VIRTUAL_ENV" in os.environ}), encoding="ascii")
raw = json.dumps(request, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
value = {"schema": 1, "status": "ready", "request_sha256": hashlib.sha256(raw).hexdigest(),
         "pid": os.getpid(), "release": request["release"]}
if fault == "pid": value["pid"] += 1
if fault == "digest": value["request_sha256"] = "0" * 64
if fault == "status": value["status"] = "complete"
temp = path / "ready.partial"
temp.write_text(json.dumps(value), encoding="ascii")
temp.replace(path / "ready.json")
if fault == "exit": sys.exit(0)
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
    def make(fault='', *, python=None):
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
        selected = Path(sys.executable) if python is None else Path(python)
        # The production authority selector proves the sealed home separately.
        # This fixture uses the actual test interpreter as its explicit runtime.
        executable = Path(sys._base_executable) if os.name == 'nt' else selected
        environment = handoff._supervisor_environment(selected if os.name == 'nt' else None)
        process = subprocess.Popen([str(executable), '-I', '-B', '-c', _CHILD, str(path), fault],
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
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


@pytest.mark.parametrize('change', ['none', 'interpreter-after-verification', 'completed-identity', 'configuration'])
def test_restart_uses_only_exact_owned_installed_entrypoint_and_current_library(
        managed, monkeypatch, tmp_path, change):
    # Real managed inventory and native lease; only the final new application
    # process and registration are explicit fixtures, never a native restart claim.
    import carr_library.security.managed_installation as managed_module
    monkeypatch.setattr(managed_module, 'WindowsRegistration', lambda: managed.registration)
    configuration = _configuration_for(managed)
    configuration.path = tmp_path / 'independent-ready-host.json'
    configuration.path.write_bytes(b'Explicit inert owner configuration marker')
    configuration.sha256 = sha256(configuration.path.read_bytes()).hexdigest()
    marker = maintenance.marker(managed.root)
    identity = {key: marker[key] for key in ('bundle_sha256', 'setup_sha256', 'owner_sha256')}
    original = maintenance.owned
    calls, reads = [], []
    expected_python = managed.root / 'app/.venv/Scripts/python.exe'

    def current_owned(*args, **kwargs):
        value = original(*args, **kwargs)
        reads.append(True)
        if change == 'interpreter-after-verification' and len(reads) == 2:
            expected_python.write_bytes(b'Changed after the complete installation inventory check')
        return value
    monkeypatch.setattr(maintenance, 'owned', current_owned)
    if change == 'completed-identity':
        identity['owner_sha256'] = '0' * 64
    if change == 'configuration':
        configuration.check = lambda: (_ for _ in ()).throw(TrustError('Explicit configuration changed'))
    monkeypatch.setenv('CARR_PROGRAM_DIR', str(tmp_path / 'external-host'))
    monkeypatch.setenv('PYTHONPATH', str(tmp_path / 'untrusted-import-path'))
    monkeypatch.setenv('__PYVENV_LAUNCHER__', 'untrusted-interpreter-selection')

    def launch(command, **options):
        calls.append((command, options))
        assert command == [str(expected_python), '-I', '-B', '-X', 'utf8', '-m', 'carr_library',
            '--data-dir', str(managed.app.root), '--update-configuration', str(configuration.path),
            '--update-configuration-sha256', configuration.sha256]
        assert options['cwd'] == str(managed.root / 'app')
        assert options['env']['CARR_PROGRAM_DIR'] == str(managed.root / 'app')
        assert not any(key.upper().startswith(('PYTHON', 'PIP')) for key in options['env'])
        assert '__PYVENV_LAUNCHER__' not in options['env']
        assert options['close_fds'] is True and options['stdin'] == subprocess.DEVNULL
        with pytest.raises(RuntimeError):
            with maintenance.installation_lease(managed.root, exclusive=True):
                pytest.fail('Restart admission released its actual shared installation lease.')
        return SimpleNamespace(pid=23456)
    monkeypatch.setattr(supervisor.subprocess, 'Popen', launch)
    if change == 'none':
        assert supervisor._restart(configuration, maintenance, identity) == {
            'status': 'STARTED_NOT_QUALIFIED', 'pid': 23456}
        assert len(calls) == 1 and len(reads) == 2
        _assert_original(managed)
    else:
        with pytest.raises(TrustError, match='changed'):
            supervisor._restart(configuration, maintenance, identity)
        assert calls == []
        assert managed.app.search(managed.workspace, 'Ukrainian')[0].quote == 'Ukrainian'
    with maintenance.installation_lease(managed.root, exclusive=True):
        pass


def _sealed_venv_configuration(configuration, *, change=None, omit=()):
    runtime = configuration.runtime
    launcher = runtime / 'app/.venv/Scripts/python.exe'
    base = runtime / 'app/runtime/python.exe'
    cfg = runtime / 'app/.venv/pyvenv.cfg'
    for path in (launcher, base):
        path.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_bytes(b'Explicit inert launcher; authority tests never execute this marker')
    base.write_bytes(b'Explicit inert sealed base interpreter; never executed')
    cfg.write_text('home = ' + str(base.parent) + '\ninclude-system-site-packages = false\n',
                   encoding='utf-8')
    if change is not None:
        change(launcher, base, cfg)
    files = {path.relative_to(runtime).as_posix(): sha256(path.read_bytes()).hexdigest()
             for path in runtime.rglob('*') if path.is_file()}
    for path in omit:
        del files[path]
    sealed = json_bytes({'schema': 1, 'files': files})
    configuration.manifest_path.write_bytes(sealed)
    value = json.loads(configuration.raw)
    value['runtime'].update(python=launcher.relative_to(runtime).as_posix(),
                            sha256=sha256(sealed).hexdigest())
    raw = json_bytes(value)
    configuration.path.write_bytes(raw)
    return handoff.OwnerUpdateConfiguration(configuration.path, sha256(raw).hexdigest())


def _windows_launch_boundary(monkeypatch):
    # Exercise Windows route selection on both OSes, without replacing any real
    # path/file/descriptor primitives or pretending marker bytes are executable.
    monkeypatch.setattr(handoff, 'os', SimpleNamespace(**{**vars(os), 'name': 'nt'}))


def test_sealed_venv_launch_binds_current_base_cfg_launcher_and_ignores_ambient_overrides(
        configuration, monkeypatch):
    configured = _sealed_venv_configuration(configuration)
    configured.verify_runtime()
    _windows_launch_boundary(monkeypatch)
    monkeypatch.setenv('__PYVENV_LAUNCHER__', 'Untrusted ambient interpreter')
    monkeypatch.setenv('PYTHONHOME', 'Untrusted ambient library')
    monkeypatch.setenv('PYTHONPATH', 'Untrusted ambient imports')
    monkeypatch.setenv('VIRTUAL_ENV', 'Untrusted ambient venv')
    executable, environment, current = configured._supervisor_launch()
    assert executable == configured.runtime / 'app/runtime/python.exe'
    assert environment['__PYVENV_LAUNCHER__'] == str(configured.python)
    assert 'PYTHONHOME' not in environment and 'PYTHONPATH' not in environment
    assert 'VIRTUAL_ENV' not in environment and environment['PIP_NO_INDEX'] == '1'
    assert environment['PYTHONNOUSERSITE'] == '1' and environment['PYTHONDONTWRITEBYTECODE'] == '1'
    current()
    assert configured.verify_runtime()['sha256'] == configured.manifest_sha256
    assert not (configured.work / 'handoffs').exists()


@pytest.mark.parametrize('fault', [
    'external-home', 'missing-home', 'duplicate-home', 'relative-home',
    'ambiguous-cfg', 'unsealed-base', 'unsealed-cfg',
])
def test_sealed_venv_launch_refuses_unowned_or_ambiguous_interpreter_authority(
        configuration, tmp_path, monkeypatch, fault):
    outside = tmp_path / 'unowned-runtime'
    outside.mkdir()
    canary = outside / 'python.exe'
    canary.write_bytes(b'Unowned interpreter must never be read or started')
    def change(launcher, base, cfg):
        if fault == 'external-home':
            cfg.write_text('home = ' + str(outside) + '\n', encoding='utf-8')
        elif fault == 'missing-home':
            cfg.write_text('include-system-site-packages = false\n', encoding='utf-8')
        elif fault == 'duplicate-home':
            cfg.write_text('home = ' + str(base.parent) + '\nHOME = ' + str(base.parent) + '\n',
                           encoding='utf-8')
        elif fault == 'relative-home':
            cfg.write_text('home = ../runtime\n', encoding='utf-8')
        elif fault == 'ambiguous-cfg':
            (launcher.parent / 'pyvenv.cfg').write_bytes(cfg.read_bytes())
    omit = (() if fault not in ('unsealed-base', 'unsealed-cfg') else
            ('app/runtime/python.exe',) if fault == 'unsealed-base' else ('app/.venv/pyvenv.cfg',))
    configured = _sealed_venv_configuration(configuration, change=change, omit=omit)
    _windows_launch_boundary(monkeypatch)
    original = handoff._hash_file
    def hashed(path, **options):
        assert Path(path) != canary, 'Outside executable bytes are never admitted.'
        return original(path, **options)
    monkeypatch.setattr(handoff, '_hash_file', hashed)
    with pytest.raises(TrustError, match='sealed|ambiguous|home'):
        configured._supervisor_launch()
    assert canary.read_bytes() == b'Unowned interpreter must never be read or started'
    assert not (configured.work / 'handoffs').exists()


@pytest.mark.parametrize('change', [
    'base-bytes', 'base-replaced', 'cfg-replaced', 'launcher-bytes',
    'manifest-replaced', 'base-parent-replaced', 'late-manifest-bytes', 'late-cfg-appearance',
])
def test_sealed_venv_launch_rechecks_each_real_identity_after_review(
        configuration, monkeypatch, change):
    configured = _sealed_venv_configuration(configuration)
    _windows_launch_boundary(monkeypatch)
    executable, _, current = configured._supervisor_launch()
    cfg = configured.runtime / 'app/.venv/pyvenv.cfg'
    target = (cfg if change == 'cfg-replaced' else configured.manifest_path
              if change == 'manifest-replaced' else configured.python
              if change == 'launcher-bytes' else executable)
    original = target.read_bytes()
    if change == 'base-parent-replaced':
        previous = executable.parent.with_name('previous-runtime')
        executable.parent.rename(previous)
        executable.parent.mkdir()
        executable.write_bytes(original)
    elif change.endswith('replaced'):
        previous = target.with_name(target.name + '.previous')
        target.rename(previous)
        target.write_bytes(original)
        assert target.stat().st_ino != previous.stat().st_ino
    elif change.startswith('late-'):
        original_hash = handoff._hash_file
        def hashed(path, **options):
            value = original_hash(path, **options)
            if Path(path) == executable:
                if change == 'late-manifest-bytes':
                    configured.manifest_path.write_bytes(configured.manifest_path.read_bytes() + b'\n')
                else:
                    (configured.python.parent / 'pyvenv.cfg').write_bytes(cfg.read_bytes())
            return value
        monkeypatch.setattr(handoff, '_hash_file', hashed)
    else:
        target.write_bytes(original + b' Changed after the actual captured review.')
    with pytest.raises(TrustError, match='changed'):
        current()
    assert not (configured.work / 'handoffs').exists()
    if change.endswith('replaced'):
        assert (previous / executable.name if change == 'base-parent-replaced'
                else previous).read_bytes() == original


@pytest.mark.parametrize('mode', ['return-true', 'original-exception'])
def test_sealed_venv_launch_cancellation_preserves_original_control_and_starts_no_child(
        configuration, monkeypatch, mode):
    configured = _sealed_venv_configuration(configuration)
    _windows_launch_boundary(monkeypatch)
    original = InterruptedError('Exact explicit cancellation object')
    def stopped():
        if mode == 'original-exception':
            raise original
        return True
    monkeypatch.setattr(handoff, 'file_info',
        lambda *args, **kwargs: pytest.fail('Precancelled launch must not open its runtime.'))
    with pytest.raises(InterruptedError) as caught:
        configured._supervisor_launch(cancelled=stopped)
    if mode == 'original-exception':
        assert caught.value is original
    assert not (configured.work / 'handoffs').exists()


@pytest.mark.parametrize('action', ['cancel', 'apply'])
def test_real_temporary_venv_preserves_direct_pid_installed_prefix_and_handoff_ownership(
        owned_handoff, tmp_path, monkeypatch, action):
    import venv
    selected_root = tmp_path / 'actual-venv'
    venv.EnvBuilder(with_pip=False, symlinks=False).create(selected_root)
    selected = selected_root / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    monkeypatch.setenv('__PYVENV_LAUNCHER__', str(tmp_path / 'wrong-python'))
    monkeypatch.setenv('PYTHONHOME', str(tmp_path / 'wrong-library'))
    monkeypatch.setenv('PYTHONPATH', str(tmp_path / 'wrong-imports'))
    monkeypatch.setenv('VIRTUAL_ENV', str(tmp_path / 'wrong-environment'))
    owner = owned_handoff(python=selected).wait_ready(timeout=5)
    observed = json.loads((owner.path / 'fixture-runtime.json').read_bytes())
    assert observed['pid'] == owner.process.pid
    assert Path(observed['executable']) == selected
    assert Path(observed['prefix']) == selected_root
    assert observed['isolated'] == observed['user_site_disabled'] == 1
    assert observed['launcher_override_present'] is False
    assert observed['pythonpath_present'] is False and observed['virtual_env_present'] is False
    assert owner.process.poll() is None and not (owner.path / 'fixture-applied').exists()
    if action == 'cancel':
        owner.abort()
        assert owner.closed and owner.process.returncode == 0 and not owner.transferred
        assert not (owner.path / 'fixture-applied').exists()
    else:
        receipt = owner.commit_after_close(host_closed=True)
        owner.abort()
        assert owner.transferred and receipt['activation'] == 'NOT_ESTABLISHED'
        owner.process.wait(timeout=5)
        assert owner.process.returncode == 0 and (owner.path / 'fixture-applied').is_file()


@pytest.mark.parametrize('fault', ['pid', 'exit'])
def test_ready_refusal_keeps_exact_identity_and_live_process_diagnostics(
        owned_handoff, fault):
    owner = owned_handoff(fault)
    if fault == 'exit':
        owner.process.wait(timeout=5)
    with pytest.raises(TrustError, match='exact reviewed update') as caught:
        owner.wait_ready(timeout=5)
    message = str(caught.value)
    if fault == 'pid':
        assert 'Ready identity matches: False; direct process returncode: None.' in message
    else:
        assert 'Ready identity matches: True; direct process returncode: 0.' in message
    assert owner.closed and owner.process.poll() is not None
    assert not (owner.path / 'fixture-applied').exists()


def test_changed_launch_binding_after_real_spawn_reaps_only_precommit_owner(
        configuration, repository, tmp_path, monkeypatch):
    configured = _sealed_venv_configuration(configuration)
    _, package = _package(repository, tmp_path / 'selected-real-package')
    original_popen = subprocess.Popen
    processes = []
    _windows_launch_boundary(monkeypatch)
    def launch(command, **options):
        assert command[0] == str(configured.runtime / 'app/runtime/python.exe')
        assert options['env']['__PYVENV_LAUNCHER__'] == str(configured.python)
        request = Path(command[command.index('--request') + 1])
        selected = Path(sys.executable)
        executable = Path(sys._base_executable) if os.name == 'nt' else selected
        environment = handoff._supervisor_environment(selected if os.name == 'nt' else None)
        child = original_popen([str(executable), '-I', '-B', '-c', _CHILD, str(request.parent), ''],
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        processes.append((child, request.parent))
        (configured.runtime / 'app/runtime/python.exe').write_bytes(b'Changed after actual Popen')
        return child
    monkeypatch.setattr(handoff.subprocess, 'Popen', launch)
    try:
        with pytest.raises(TrustError, match='launch identity changed|launch bytes changed'):
            handoff.ManagedUpdateHandoffService(configured).prepare(repository.root, package)
        assert len(processes) == 1
        child, path = processes[0]
        assert child.poll() == 0
        assert json.loads((path / 'decision.json').read_bytes())['action'] == 'cancel'
        assert not (path / 'fixture-applied').exists()
    finally:
        for child, path in processes:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
            if child.stderr is not None:
                child.stderr.close()
