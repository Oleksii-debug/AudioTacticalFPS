"""Fresh scope authority with bounded repeated ancestor work.

These use real directories, SQLite-free engine settings and native leases.  The
lstat counter measures only repeated filesystem checks, not speech throughput.
All old broker/voice owner/managed-authority suites remain required separately.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys
from threading import Event

import pytest

import carr_library.audio.voice_settings as settings_module
import carr_library.integrations._dordiai_authority as authority_module
import carr_library.operations.broker as broker_module
from carr_library.audio.voice_ownership import capture_voice_ownership, hold_voice_ownership
from carr_library.audio.voice_settings import VoiceSettingsStore
from carr_library.integrations._dordiai_authority import DordiaiAuthority
from carr_library.operations import (
    ResourceBroker, ResourceCancelled, ResourceLimits, ResourceRequest, ResourceStateError,
)
from carr_library.runtime_settings import load_settings, save_settings


@pytest.fixture
def scopes(tmp_path):
    base = tmp_path.resolve() / 'nested' / 'captured' / 'local' / 'scope'
    library, runtime = base / 'library', base / 'runtime'
    state = library / 'state'
    state.mkdir(parents=True)
    (runtime / 'models' / 'voices').mkdir(parents=True)
    value = load_settings(state)
    value['dordiai'] = {
        'enabled': True, 'runtime_directory': str(runtime),
        'python_executable': sys.executable, 'expected_worker_sha256': 'a' * 64,
    }
    save_settings(state, value)
    row = load_settings(state)['dordiai']
    broker = ResourceBroker.for_app(library, limits=ResourceLimits(cpu_slots=1))
    store = VoiceSettingsStore(library)
    authority = DordiaiAuthority(runtime, configuration=row, state_root=state)
    return library, runtime, broker, store, authority


def _files(root):
    return tuple((path.relative_to(root).as_posix(), path.read_bytes())
                 for path in sorted(root.rglob('*')) if path.is_file())


def _replace_same_bytes(path, moved):
    previous = _files(path)
    before = path.stat()
    path.rename(moved)
    shutil.copytree(moved, path)
    after = path.stat()
    assert (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
    assert _files(path) == previous
    return previous


def _after_walk(monkeypatch, module, leaf, action):
    original = module.local_path
    observed = []
    def checked(path, **kwargs):
        result = original(path, **kwargs)
        if result == leaf and not observed:
            observed.append(result)
            action()
        return result
    monkeypatch.setattr(module, 'local_path', checked)
    return observed


def _link_directory(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        if os.name != 'nt' or getattr(error, 'winerror', None) != 1314:
            raise
        # Directory junctions do not need the optional symlink privilege.
        # Both are actual Windows reparse points rejected by regular().
        command = Path(os.environ['SystemRoot']) / 'System32' / 'cmd.exe'
        result = subprocess.run([str(command), '/d', '/c', 'mklink', '/J',
                                 str(link), str(target)], capture_output=True,
                                check=False, timeout=10)
        assert result.returncode == 0, (result.stdout, result.stderr)
    assert link.is_dir()


@pytest.mark.parametrize('component', ['broker', 'settings', 'runtime', 'state'])
def test_fresh_scope_checks_reduce_real_ancestor_walks_without_caching(scopes, monkeypatch, component):
    _library, runtime, broker, store, authority = scopes
    # Compare the previous complete lexical walk for every captured directory
    # with the current check over the same real identities.  No clock fixture
    # or metadata result is substituted.
    if component == 'broker':
        expected = broker._scope_directories
        def previous():
            for path, identity in expected:
                assert broker_module._directory_identity(path) == identity
        current = broker._check_scope
    elif component == 'settings':
        expected = ((store.root, store._root_identity),
                    (store.state_root, store._state_identity),
                    (store.path.parent, store._audio_identity))
        def previous():
            store.scope_guard()
            for path, identity in expected:
                assert settings_module._identity(path) == identity
        current = store.check
    elif component == 'runtime':
        authority = DordiaiAuthority(runtime)
        expected = authority._directories
        def previous():
            for path, identity in expected:
                assert authority_module._directory(path) == (path, identity)
            try:
                authority_module._directory(authority.packages)
            except FileNotFoundError:
                pass
            else:
                pytest.fail('Unexpected owner namespace in the real baseline.')
        current = authority._check_base
    else:
        expected = authority._state_directories
        def previous():
            for path, identity in expected:
                assert authority_module._directory(path) == (path, identity)
        current = authority._check_settings_scope
    actual_lstat = Path.lstat
    def observe(call):
        reads = []
        def lstat(path, *args, **kwargs):
            result = actual_lstat(path, *args, **kwargs)
            reads.append(Path(path))
            return result
        with monkeypatch.context() as scoped:
            scoped.setattr(Path, 'lstat', lstat)
            call()
        return reads
    before = observe(previous)
    first = observe(current)
    second = observe(current)
    assert 0 < len(first) < len(before)
    assert len(second) == len(first), 'A later call must perform fresh filesystem checks.'
    assert {path for path, _ in expected} <= set(first)
    assert {path for path, _ in expected} <= set(second)
    assert not (runtime / 'models' / 'voices' / 'packages').exists()


@pytest.mark.parametrize('scope', ['application', 'coordinator', 'leases', 'policy'])
@pytest.mark.parametrize('boundary', ['before-check', 'after-lexical-walk'])
def test_broker_rechecks_both_branches_and_each_captured_identity(
        scopes, tmp_path, monkeypatch, scope, boundary):
    library, _runtime, broker, _store, _authority = scopes
    target = {'application': library, 'coordinator': broker.root,
              'leases': broker.leases_root, 'policy': broker.policy_path.parent}[scope]
    original = _files(target)
    changed = []
    def replace():
        changed.append(_replace_same_bytes(target, tmp_path / 'retired-broker-scope'))
    if boundary == 'before-check':
        replace()
    else:
        leaf = broker.policy_path.parent if scope == 'policy' else broker.leases_root
        walked = _after_walk(monkeypatch, broker_module, leaf, replace)
    with pytest.raises(ResourceStateError, match='selected resource directory changed'):
        broker._check_scope()
    assert changed == [original]
    if boundary == 'after-lexical-walk':
        assert walked == [leaf]
    with pytest.raises(ResourceStateError):
        with broker.reserve(ResourceRequest(), operation_id='must-not-rebind', timeout=0):
            pytest.fail('Replaced coordination scope admitted work.')
    assert _files(target) == original


@pytest.mark.parametrize('scope', ['library', 'state', 'audio'])
@pytest.mark.parametrize('boundary', ['before-check', 'after-lexical-walk'])
def test_voice_settings_rechecks_each_captured_identity_after_path_resolution(
        scopes, tmp_path, monkeypatch, scope, boundary):
    library, _runtime, _broker, store, _authority = scopes
    target = {'library': library, 'state': store.state_root, 'audio': store.path.parent}[scope]
    original = _files(target)
    changed = []
    def replace():
        changed.append(_replace_same_bytes(target, tmp_path / 'retired-settings-scope'))
    if boundary == 'before-check':
        replace()
    else:
        walked = _after_walk(monkeypatch, settings_module, store.path.parent, replace)
    with pytest.raises(ValueError, match='changed'):
        store.check()
    assert changed == [original]
    if boundary == 'after-lexical-walk':
        assert walked == [store.path.parent]
    with pytest.raises(ValueError):
        store.read()
    assert _files(target) == original


@pytest.mark.parametrize('scope', ['runtime', 'models', 'references', 'library', 'state', 'audio'])
@pytest.mark.parametrize('boundary', ['before-check', 'after-lexical-walk'])
def test_authority_rechecks_all_runtime_and_state_identities_after_path_resolution(
        scopes, tmp_path, monkeypatch, scope, boundary):
    library, runtime, _broker, store, authority = scopes
    target = {'runtime': runtime, 'models': runtime / 'models', 'references': authority.references,
              'library': library, 'state': store.state_root, 'audio': store.path.parent}[scope]
    original = _files(target)
    changed = []
    def replace():
        changed.append(_replace_same_bytes(target, tmp_path / 'retired-authority-scope'))
    if boundary == 'before-check':
        replace()
    else:
        leaf = authority.references if scope in {'runtime', 'models', 'references'} else store.path.parent
        walked = _after_walk(monkeypatch, authority_module, leaf, replace)
    with pytest.raises(ValueError, match='changed'):
        authority._check_base()
    assert authority._invalid and changed == [original]
    if boundary == 'after-lexical-walk':
        assert walked == [leaf]
    with pytest.raises(ValueError):
        with authority.operation(timeout=0):
            pytest.fail('A revoked scope was reused as voice authority.')
    assert authority._depth == 0 and not authority._gate.locked()
    assert authority._stack is None and authority._lease is None
    assert _files(target) == original


@pytest.mark.parametrize('component', ['broker', 'settings', 'runtime', 'state'])
def test_actual_linked_scope_refuses_even_when_it_reaches_the_original_directory(
        scopes, tmp_path, component):
    _library, _runtime, broker, store, authority = scopes
    if component == 'broker':
        target, check, error = broker.policy_path.parent, broker._check_scope, ResourceStateError
    elif component == 'settings':
        target, check, error = store.state_root, store.check, ValueError
    elif component == 'runtime':
        target, check, error = authority.runtime, authority._check_base, ValueError
    else:
        target, check, error = store.state_root, authority._check_base, ValueError
    before = _files(target)
    identity = target.stat()
    moved = tmp_path / 'original-linked-scope'
    target.rename(moved)
    try:
        _link_directory(target, moved)
        reached = target.stat()
        assert (reached.st_dev, reached.st_ino) == (identity.st_dev, identity.st_ino)
        with pytest.raises(error):
            check()
        assert _files(moved) == before
    finally:
        if os.path.lexists(target):
            if os.name == 'nt' and not target.is_symlink():
                target.rmdir()
            else:
                target.unlink()
        moved.rename(target)
    assert _files(target) == before


def test_voice_settings_guard_cancellation_precedes_fresh_path_reads(scopes, monkeypatch):
    library, _runtime, _broker, _store, _authority = scopes
    stop = Event()
    original_error = InterruptedError('Synthetic current-library cancellation.')
    def guard():
        if stop.is_set():
            raise original_error
    store = VoiceSettingsStore(library, scope_guard=guard)
    raw = store.path.read_bytes()
    stop.set()
    monkeypatch.setattr(settings_module, 'local_path',
                        lambda *args, **kwargs: pytest.fail('Cancelled scope read another path.'))
    with pytest.raises(InterruptedError) as caught:
        store.check()
    assert caught.value is original_error
    assert store.path.read_bytes() == raw


def test_resource_cancellation_keeps_the_real_existing_reservation(scopes):
    _library, _runtime, broker, _store, _authority = scopes
    with broker.reserve(ResourceRequest(), operation_id='existing-scope-owner', timeout=0) as owner:
        names = tuple(sorted(path.name for path in broker.leases_root.iterdir()))
        before = tuple((path.name, path.read_bytes()) for path in sorted(broker.leases_root.glob('*.json')))
        with pytest.raises(ResourceCancelled):
            with broker.reserve(ResourceRequest(), operation_id='cancelled-scope-waiter',
                                cancelled=lambda: True, timeout=0):
                pytest.fail('Cancelled resource admission entered its body.')
        owner.checkpoint()
        assert owner._lock.held and not owner._released
        assert tuple(sorted(path.name for path in broker.leases_root.iterdir())) == names
        assert tuple((path.name, path.read_bytes()) for path in sorted(broker.leases_root.glob('*.json'))) == before
    assert not owner._lock.held


@pytest.mark.parametrize('boundary', ['entry', 'held'])
def test_authority_cancellation_keeps_the_actual_package_lease_until_unwind(scopes, boundary):
    _library, runtime, _broker, _store, _old_authority = scopes
    packages = runtime / 'models' / 'voices' / 'packages'
    packages.mkdir()
    (packages / '.voice-packages.lock').write_bytes(b'0')
    authority = DordiaiAuthority(runtime)
    ticket = capture_voice_ownership(runtime)
    stopped = Event()
    if boundary == 'entry':
        stopped.set()
    captured = []
    with pytest.raises(InterruptedError):
        with authority.operation(cancelled=stopped, timeout=0):
            assert boundary == 'held'
            captured.append(authority._lease)
            with pytest.raises(TimeoutError):
                with hold_voice_ownership(ticket, timeout=0):
                    pytest.fail('Another owner entered during a live authority scope.')
            stopped.set()
            authority.checkpoint()
    assert authority._lease is None and authority._stack is None
    assert authority._depth == 0 and not authority._gate.locked()
    if captured:
        assert not captured[0].held
    with hold_voice_ownership(ticket, timeout=0) as next_owner:
        next_owner.checkpoint()
    assert (packages / '.voice-packages.lock').read_bytes() == b'0'
    assert not (packages / 'voice-packages.json').exists()


def test_settings_with_no_captured_audio_directory_preserves_passive_behavior(tmp_path):
    library = tmp_path / 'passive-library'
    (library / 'state').mkdir(parents=True)
    store = VoiceSettingsStore(library)
    assert store._audio_identity is None
    assert store.check() == library.resolve()
    assert not (library / 'state' / 'audio').exists()
    # Previously uncreated audio remains unbound until the existing settings
    # publication explicitly creates it; check() itself creates nothing.
    (library / 'state' / 'audio').mkdir()
    assert store.check() == library.resolve()
    assert tuple((library / 'state' / 'audio').iterdir()) == ()


@pytest.mark.parametrize('change', ['appeared', 'disappeared'])
def test_fresh_scope_optimization_preserves_owner_namespace_presence_guard(scopes, change):
    _library, runtime, _broker, _store, authority = scopes
    packages = runtime / 'models' / 'voices' / 'packages'
    if change == 'disappeared':
        packages.mkdir()
        (packages / '.voice-packages.lock').write_bytes(b'0')
        authority = DordiaiAuthority(runtime)
        (packages / '.voice-packages.lock').unlink()
        packages.rmdir()
    else:
        packages.mkdir()
    before = _files(runtime)
    with pytest.raises(ValueError, match='ownership changed'):
        authority._check_base()
    assert authority._invalid and _files(runtime) == before


def test_broker_checks_disjoint_explicit_policy_branch_without_recreating_it(tmp_path):
    coordinator = tmp_path / 'coordinator' / 'deep' / 'runtime'
    policy = tmp_path / 'separate' / 'policy' / 'resources.json'
    broker = ResourceBroker(coordinator, policy_path=policy, limits=ResourceLimits(cpu_slots=1))
    broker._check_scope()
    before = policy.read_bytes()
    moved = tmp_path / 'retired-explicit-policy'
    policy.parent.rename(moved)
    with pytest.raises(ResourceStateError):
        broker._check_scope()
    assert not policy.parent.exists()
    assert (moved / policy.name).read_bytes() == before
