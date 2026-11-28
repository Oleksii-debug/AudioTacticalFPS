"""Fixed external supervisor for an explicitly reviewed standard-setup update.

This module runs from the separately sealed maintenance runtime, never from the
proposed release. Before the desktop's durable COMMIT it starts no child.
After COMMIT it retains the existing native installation lease and transaction
through installer settlement, backup, health, rollback or recorded recovery.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from ._files import TrustError, atomic_write, directory, file_info, json_bytes, json_value, locked, read_bytes
from .update_handoff import OwnerUpdateConfiguration, _directory_rows, _same_directories, _digest
from .updates import LocalRepositoryClient, Release

_BUSY = 'Close Resonance and its running jobs before repairing or uninstalling.'


def _load_maintenance(configuration):
    configuration.verify_runtime()
    here, executable = Path(__file__).absolute(), Path(sys.executable).absolute()
    if not here.is_relative_to(configuration.runtime) or executable != configuration.python:
        raise TrustError('The updater must run in the explicitly sealed external runtime.')
    path = configuration.maintenance
    name = '_carr_sealed_update_maintenance'
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise TrustError('The sealed maintenance helper cannot be loaded.')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if (Path(module.HERE) != path.parent
            or set(module.HELPERS) != {'maintenance.py', 'installation_lease.py', 'bundle_verifier.py',
                'managed_installation.py', 'windows_job.py', 'cleanup.py', 'models.py', 'process.py'}
            or any(Path(value).parent != path.parent for value in module.HELPERS.values())):
        raise TrustError('The maintenance helper escaped its fixed sealed inventory.')
    configuration.check()
    return module


def _request(configuration, path, expected_sha256):
    path = Path(path).absolute()
    if (path.name != 'request.json' or not re.fullmatch('[0-9a-f]{32}', path.parent.name)
            or path.parent.parent != configuration.work / 'handoffs'):
        raise TrustError('Select the exact private update handoff.')
    directories = _directory_rows(path.parent)
    raw = read_bytes(path, 32768)
    if sha256(raw).hexdigest() != _digest(expected_sha256):
        raise TrustError('The requested update handoff changed.')
    value = json_value(raw, maximum=32768)
    fields = {'schema', 'nonce', 'created_at', 'repository', 'release', 'restart',
              'library_directories', 'root_directories'}
    if (type(value) is not dict or set(value) != fields
            or type(value['schema']) is not int or value['schema'] != 1
            or type(value['nonce']) is not str or not re.fullmatch('[0-9a-f]{32}', value['nonce'])
            or type(value['created_at']) is not int
            or not 0 <= time.time() - value['created_at'] <= 600
            or type(value['restart']) is not bool
            or type(value['release']) is not dict or set(value['release']) != set(Release.__dataclass_fields__)
            or value['repository'] != str(path.parent / 'repository')):
        raise TrustError('Invalid or expired desktop update request.')
    if (value['library_directories'] != [list(row) for row in _directory_rows(configuration.library)]
            or value['root_directories'] != [list(row) for row in _directory_rows(configuration.root)]):
        raise TrustError('The update request names changed installation or research directories.')
    release = Release(**value['release'])
    if (release.product, release.channel, release.target) != (
            configuration.policy.product, configuration.policy.channel, configuration.target):
        raise TrustError('The update request belongs to another publisher or target.')

    def current():
        configuration.check()
        _same_directories(directories)
        _same_directories(value['library_directories'])
        _same_directories(value['root_directories'])
        if read_bytes(path, 32768) != raw:
            raise TrustError('The reviewed update request changed during handoff.')
    current()
    return value, current


def _decision(configuration, path, request, digest, current):
    deadline = time.monotonic() + configuration.close_timeout
    target = path / 'decision.json'
    while time.monotonic() < deadline:
        current()
        if os.path.lexists(target):
            value = json_value(read_bytes(target, 4096), maximum=4096)
            expected = {'schema': 1, 'action': 'apply', 'request_sha256': digest, 'nonce': request['nonce']}
            if value == {**expected, 'action': 'cancel'}:
                return False
            if value != expected:
                raise TrustError('The desktop did not commit this exact update request.')
            return True
        time.sleep(.05)
    return False


@contextmanager
def _wait_for_installation(maintenance, root, *, current, deadline):
    """Wait only on the existing native lease; never replace its lock protocol."""
    while True:
        current()
        if time.monotonic() >= deadline:
            raise TimeoutError('Close Resonance and its jobs before applying this prepared update.')
        context = maintenance.installation_lease(root, exclusive=True)
        try:
            owner = context.__enter__()
        except RuntimeError as error:
            if str(error) != _BUSY:
                raise
            time.sleep(.05)
            continue
        break
    try:
        current()
        owner.check(root, True)
        yield owner
    finally:
        context.__exit__(*sys.exc_info())


class _HeldMaintenance:
    """Borrow the one already acquired actual lease inside the old transaction."""
    def __init__(self, original, root, owner, current):
        self.original, self.root, self.owner, self.current = original, root, owner, current

    def __getattr__(self, name):
        return getattr(self.original, name)

    @contextmanager
    def installation_lease(self, root, *, exclusive=False, owner=None):
        if Path(root) != self.root or exclusive is not True or owner is not None:
            raise TrustError('The updater cannot acquire or relabel another installation lease.')
        self.current()
        self.owner.check(self.root, True)
        yield self.owner
        self.owner.check(self.root, True)


def _phase(work):
    """A bounded observation, never an inferred rollback or success."""
    path = work / 'managed-activation.json'
    if not os.path.lexists(path):
        return 'NOT_RECORDED'
    try:
        value = json_value(read_bytes(path, 16384), maximum=16384)
        phase = value.get('phase') if type(value) is dict else None
        return phase if phase in {'prepared', 'activating', 'health-checked', 'complete', 'rolled-back'} else 'INVALID'
    except Exception:
        return 'UNREADABLE'


def _adapter(maintenance, configuration, deadline, current):
    from .managed_installation import WindowsManagedSetupAdapter

    class BoundedAdapter(WindowsManagedSetupAdapter):
        def _remaining(self):
            current()
            remaining = deadline - time.monotonic()
            if remaining < 1:
                raise TimeoutError('The explicit update operation deadline was reached.')
            return max(1, min(1800, int(remaining)))

        def activate(self, *args, **kwargs):
            self.timeout = self.bundle.timeout = self._remaining()
            return super().activate(*args, **kwargs)

        def health(self, *args, **kwargs):
            self.bundle.timeout = self._remaining()
            return super().health(*args, **kwargs)

        # restore intentionally keeps the existing exact rollback and cleanup
        # path. A deadline cannot release native ownership or strand live setup.
    return BoundedAdapter(maintenance, timeout=min(1800, configuration.operation_timeout))


def _execute(configuration, maintenance, *, current, package=None, recover=False):
    from ..application import LibraryApp
    from .managed_installation import ManagedUpdateTransaction
    deadline = time.monotonic() + configuration.operation_timeout

    def cancelled():
        current()
        return time.monotonic() >= deadline

    @contextmanager
    def idle():
        # This fresh supervisor has no desktop, executor or queued library jobs.
        # The actual installation lease excludes all guarded app entrypoints;
        # the unchanged transaction owns the library backup/writer boundary.
        current()
        if cancelled():
            raise TimeoutError('The update operation deadline was reached.')
        yield

    with _wait_for_installation(maintenance, configuration.root, current=current,
                               deadline=min(deadline, time.monotonic() + configuration.close_timeout)) as owner:
        current()
        app = LibraryApp(configuration.library)
        current()
        proxy = _HeldMaintenance(maintenance, configuration.root, owner, current)
        adapter = _adapter(proxy, configuration, deadline, current)
        transaction = ManagedUpdateTransaction(app, configuration.root, configuration.work,
            configuration.policy, configuration.anchor(), adapter, idle_lease=idle)
        result = transaction.recover() if recover else transaction.apply(package, cancelled=cancelled)
        current()
        # The normal transaction already verifies native installer families,
        # complete health, exact ownership and the protected anchor before this.
        identity = None
        if result == 'complete' or isinstance(result, dict) and result.get('status') == 'complete':
            marker = adapter.verify_current(configuration.root)
            identity = {key: marker[key] for key in ('bundle_sha256', 'setup_sha256', 'owner_sha256')}
        return result, identity


def _restart(configuration, maintenance, identity):
    """Start only the new verified standard launcher after the exclusive lease."""
    with maintenance.installation_lease(configuration.root):
        configuration.check()
        from .managed_installation import WindowsManagedSetupAdapter
        marker = WindowsManagedSetupAdapter(maintenance).verify_current(configuration.root)
        if identity is None or any(marker[key] != identity[key] for key in identity):
            raise TrustError('The completed installation changed before restart.')
        launcher = configuration.root / 'app' / 'ResearchLibrary.exe'
        file_info(launcher)
        # verify_current in the completed transaction checked this exact manifest.
        raw = read_bytes(launcher.parent / 'bundle-manifest.json', 16 * 1024**2)
        expected = json_value(raw, maximum=16 * 1024**2)['files'].get('ResearchLibrary.exe')
        from .update_handoff import _hash_file
        if expected is None or _hash_file(launcher, maximum=32 * 1024**2)[0] != expected:
            raise TrustError('The verified restart launcher changed.')
        environment = {k: v for k, v in os.environ.items()
                       if not k.upper().startswith(('PYTHON', 'PIP'))}
        process = subprocess.Popen([str(launcher), '--data-dir', str(configuration.library),
            '--update-configuration', str(configuration.path), '--update-configuration-sha256', configuration.sha256],
            cwd=str(launcher.parent), env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
        # Explicitly requested application restart is an ownership handoff, not
        # an installer child claimed reaped by this supervisor.
        return {'status': 'STARTED_NOT_QUALIFIED', 'pid': process.pid}


def run_request(configuration, request_path, request_sha256):
    request, current = _request(configuration, request_path, request_sha256)
    path = Path(request_path).parent
    report = {'schema': 1, 'status': 'NOT_APPLIED', 'request_sha256': request_sha256,
              'release': request['release'], 'transaction_phase': 'NOT_RECORDED',
              'restart': {'status': 'NOT_REQUESTED'}, 'data_retargeted': False}
    with locked(path, '.host.lock'):
        try:
            maintenance = _load_maintenance(configuration)
            current()
            # Offline only: no network worker or setup child exists before COMMIT.
            client = LocalRepositoryClient(path / 'verified', configuration.policy)
            package = client.download(request['repository'], configuration.target,
                reviewed_sha256=request['release']['sha256'], cancelled=lambda: False)
            if asdict(package.release) != request['release']:
                raise TrustError('The current signed metadata differs from the desktop review.')
            maintenance.marker(configuration.root)
            configuration.anchor().load()
            current()
            atomic_write(path / 'ready.json', json_bytes({
                'schema': 1, 'status': 'ready', 'request_sha256': request_sha256,
                'pid': os.getpid(), 'release': request['release']}))
            if not _decision(configuration, path, request, request_sha256, current):
                report['status'] = 'CANCELLED_BEFORE_APPLY'
                return report
            current()
            configuration.verify_runtime()
            # Expiry or a changed retained target after READY is still refused.
            package = client.download(request['repository'], configuration.target,
                reviewed_sha256=request['release']['sha256'], cancelled=lambda: False)
            if asdict(package.release) != request['release']:
                raise TrustError('The signed release changed before activation.')
            report['transaction'], report['installation_identity'] = _execute(
                configuration, maintenance, current=current, package=package)
            report['status'] = 'COMPLETE'
            report['transaction_phase'] = _phase(configuration.work)
            if request['restart']:
                try:
                    report['restart'] = _restart(configuration, maintenance, report['installation_identity'])
                except Exception as error:
                    report['restart'] = {'status': 'REFUSED', 'error_type': type(error).__name__}
            return report
        except BaseException as error:
            report['status'] = 'FAILED'
            report['error_type'] = type(error).__name__
            report['transaction_phase'] = _phase(configuration.work)
            # The actual journal, not a generic catch, states committed or
            # rolled-back truth. Pending state is preserved for explicit recovery.
            raise
        finally:
            raw = json_bytes(report)
            if len(raw) > 32768:
                raise TrustError('The maintenance result exceeds its report bound.')
            current()
            atomic_write(path / 'result.json', raw)


def recover(configuration):
    configuration.verify_runtime()
    maintenance = _load_maintenance(configuration)
    result, identity = _execute(configuration, maintenance, current=configuration.check, recover=True)
    return {'schema': 1, 'status': result, 'transaction_phase': _phase(configuration.work),
            'installation_identity': identity,
            'data_retargeted': False, 'restart': 'NOT_REQUESTED'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--configuration', required=True, type=Path)
    parser.add_argument('--configuration-sha256', required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--request', type=Path)
    mode.add_argument('--recover', action='store_true')
    parser.add_argument('--request-sha256')
    args = parser.parse_args(argv)
    if (args.request is None) != (args.request_sha256 is None):
        parser.error('A request path requires its exact independently supplied digest.')
    try:
        configuration = OwnerUpdateConfiguration(args.configuration, args.configuration_sha256)
        result = recover(configuration) if args.recover else run_request(
            configuration, args.request, args.request_sha256)
        print(json_bytes(result).decode('ascii'))
        return 0
    except Exception as error:
        print('Update host refused: ' + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
