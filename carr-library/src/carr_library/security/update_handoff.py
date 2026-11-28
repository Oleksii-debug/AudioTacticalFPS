"""Owner-provisioned update authority and an explicit desktop-to-host handoff.

The maintenance runtime is an independently sealed, already installed runtime
outside the replaceable application. This module neither copies a live venv nor
imports code from an update target. Before commit the launching process owns and
reaps the one supervisor. After commit the fixed supervisor owns the transaction
and its existing native process families until completion or recovery.
"""
from __future__ import annotations

import atexit
import base64
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
import uuid

from ._files import (TrustError, atomic_write, directory, file_info, json_bytes,
                     json_value, locked, read_bytes)
from .updates import LocalRepositoryClient, UpdatePolicy, require_verified_package, relative_path


def _digest(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise TrustError('An exact owner-provisioned SHA256 is required.')
    return value


def _directory_rows(path):
    path = directory(path)
    return tuple((str(p), p.stat().st_dev, p.stat().st_ino)
                 for p in (*reversed(path.parents), path))


def _same_directories(rows):
    if _directory_rows(rows[-1][0]) != tuple(tuple(row) for row in rows):
        raise TrustError('The captured update directory changed.')


def _file_stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink


def _hash_file(path, *, maximum, cancelled=lambda: False):
    before = file_info(path)
    if before.st_size > maximum:
        raise TrustError('A trusted host file exceeds its configured byte allowance.')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    digest, used = sha256(), 0
    with os.fdopen(descriptor, 'rb') as stream:
        if _file_stamp(os.fstat(descriptor)) != _file_stamp(before):
            raise TrustError('The trusted host file changed while opening.')
        while block := stream.read(1024 * 1024):
            if cancelled():
                raise InterruptedError('Update preparation cancelled.')
            used += len(block)
            if used > maximum:
                raise TrustError('A trusted host file grew beyond its byte allowance.')
            digest.update(block)
        if _file_stamp(os.fstat(descriptor)) != _file_stamp(before):
            raise TrustError('The trusted host file changed during verification.')
    if used != before.st_size or _file_stamp(file_info(path)) != _file_stamp(before):
        raise TrustError('The trusted host path changed during verification.')
    return digest.hexdigest(), used


def _supervisor_environment(venv_python=None):
    """Keep CPython's explicit venv route, never an inherited launcher override."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(('PYTHON', 'PIP'))
                   and key.upper() not in ('__PYVENV_LAUNCHER__', 'VIRTUAL_ENV')}
    environment.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', PIP_NO_INDEX='1')
    if venv_python is not None:
        environment['__PYVENV_LAUNCHER__'] = str(venv_python)
    return environment


class OwnerUpdateConfiguration:
    """Data-only configuration, selected with an independently supplied digest."""
    def __init__(self, path, expected_sha256):
        self.path = Path(path).absolute()
        self.sha256 = _digest(expected_sha256)
        self.raw = read_bytes(self.path, 65536)
        if sha256(self.raw).hexdigest() != self.sha256:
            raise TrustError('The owner update configuration changed.')
        value = json_value(self.raw, maximum=65536)
        fields = {'schema', 'kind', 'runtime', 'installation', 'library', 'publisher',
                  'repository', 'anchor', 'close_timeout', 'operation_timeout'}
        if (type(value) is not dict or set(value) != fields or type(value['schema']) is not int
                or value['schema'] != 1 or value['kind'] != 'carr-managed-update-host'):
            raise TrustError('Unsupported owner update configuration.')
        self.value = value
        runtime, installation = value['runtime'], value['installation']
        if (type(runtime) is not dict or set(runtime) != {'root', 'manifest', 'sha256', 'python', 'maintenance'}
                or type(installation) is not dict or set(installation) != {'root', 'work'}):
            raise TrustError('Invalid trusted maintenance runtime.')
        self.runtime = directory(runtime['root'])
        self.manifest_path = Path(runtime['manifest']).absolute()
        self.manifest_sha256 = _digest(runtime['sha256'])
        self.python = self.runtime.joinpath(*relative_path(runtime['python']))
        self.maintenance = self.runtime.joinpath(*relative_path(runtime['maintenance']))
        if self.python.name.casefold() not in ('python.exe', 'python3', 'python') or self.maintenance.name != 'maintenance.py':
            raise TrustError('Use the fixed installed Python and reviewed maintenance entrypoint.')
        file_info(self.python)
        file_info(self.maintenance)
        self.root = directory(installation['root'])
        self.work = directory(installation['work'])
        self.library = directory(value['library'])
        paths = (self.runtime, self.root, self.work, self.library)
        if any(left == right or left.is_relative_to(right) or right.is_relative_to(left)
               for i, left in enumerate(paths) for right in paths[i + 1:]):
            raise TrustError('Runtime, installation, work and library directories must be separate.')
        if any(self.path.is_relative_to(parent) for parent in paths):
            raise TrustError('Owner configuration must survive application replacement and work cleanup.')
        if any(self.manifest_path.is_relative_to(parent) for parent in paths):
            raise TrustError('The runtime manifest must survive application replacement and work cleanup outside every managed payload.')
        if self.work.stat().st_dev != self.root.stat().st_dev:
            raise TrustError('Managed recovery requires a work directory on the installation filesystem.')
        self.bindings = tuple(_directory_rows(p) for p in (self.path.parent, self.runtime,
            self.manifest_path.parent, self.root, self.work, self.library))
        publisher = value['publisher']
        policy_fields = {'product', 'channel', 'bootstrap', 'bootstrap_sha256', 'current_build',
                         'data_schema', 'max_package_bytes', 'max_unpacked_bytes', 'max_files'}
        if type(publisher) is not dict or set(publisher) != policy_fields:
            raise TrustError('Invalid independent publisher policy.')
        try:
            bootstrap = base64.b64decode(publisher['bootstrap'], validate=True)
        except (ValueError, TypeError):
            raise TrustError('Invalid independently provisioned TUF root.') from None
        self.policy = UpdatePolicy(**{**publisher, 'bootstrap': bootstrap})
        repository = value['repository']
        if type(repository) is not dict or set(repository) != {'endpoint', 'target', 'tls_ca_pem', 'timeout'}:
            raise TrustError('Invalid owner repository configuration.')
        relative_path(repository['target'])
        self.endpoint = repository['endpoint']
        self.target = repository['target']
        if self.endpoint is not None:
            from .update_http_worker import _endpoint
            _endpoint(self.endpoint)
        anchor = value['anchor']
        if type(anchor) is not dict or set(anchor) != {'reference', 'binding', 'profile_id'}:
            raise TrustError('Invalid protected update anchor configuration.')
        from .anchors import WindowsVaultAnchor
        self.anchor_arguments = dict(anchor, product=self.policy.product)
        WindowsVaultAnchor(**self.anchor_arguments)  # validates identity; never provisions a credential
        for field, minimum, maximum in (('close_timeout', 1, 300), ('operation_timeout', 60, 7200)):
            number = value[field]
            if type(number) is not int or not minimum <= number <= maximum:
                raise TrustError('Invalid maintenance handoff deadline.')
        self.close_timeout, self.operation_timeout = value['close_timeout'], value['operation_timeout']
        self.check()

    def check(self):
        for rows in self.bindings:
            _same_directories(rows)
        if read_bytes(self.path, 65536) != self.raw:
            raise TrustError('The owner update authority changed after review.')

    def _runtime_inventory(self):
        self.check()
        raw = read_bytes(self.manifest_path, 8 * 1024**2)
        if sha256(raw).hexdigest() != self.manifest_sha256:
            raise TrustError('The trusted maintenance runtime manifest changed.')
        manifest = json_value(raw, maximum=8 * 1024**2)
        if (type(manifest) is not dict or set(manifest) != {'schema', 'files'}
                or type(manifest['schema']) is not int or manifest['schema'] != 1
                or type(manifest['files']) is not dict or not 1 <= len(manifest['files']) <= 100000):
            raise TrustError('Invalid trusted maintenance runtime inventory.')
        expected = {}
        for name, digest in manifest['files'].items():
            relative_path(name)
            expected[name] = _digest(digest)
        required = {self.python.relative_to(self.runtime).as_posix(),
                    self.maintenance.relative_to(self.runtime).as_posix()}
        if not required.issubset(expected):
            raise TrustError('The maintenance executable or helper is not sealed.')
        return raw, expected

    def verify_runtime(self, *, cancelled=lambda: False):
        raw, expected = self._runtime_inventory()
        observed, directories, total = {}, {}, 0
        pending = [self.runtime]
        while pending:
            parent = pending.pop()
            if cancelled():
                raise InterruptedError('Update runtime verification cancelled.')
            rows = _directory_rows(parent)
            directories[str(parent)] = rows[-1]
            for path in parent.iterdir():
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400:
                    pending.append(path)
                    if len(pending) + len(directories) > 100000:
                        raise TrustError('The maintenance directory inventory exceeds its bound.')
                    continue
                name = path.relative_to(self.runtime).as_posix()
                if name not in expected or name in observed:
                    raise TrustError('The maintenance runtime contains an unsealed payload.')
                digest, size = _hash_file(path, maximum=4 * 1024**3, cancelled=cancelled)
                total += size
                if total > 32 * 1024**3 or digest != expected[name]:
                    raise TrustError('The maintenance runtime payload differs from its owner seal.')
                observed[name] = digest
            _same_directories(rows)
        if observed != expected:
            raise TrustError('The maintenance runtime is missing a sealed payload.')
        self.check()
        if read_bytes(self.manifest_path, 8 * 1024**2) != raw:
            raise TrustError('The maintenance runtime manifest changed while verifying.')
        return {'sha256': self.manifest_sha256, 'files': len(observed), 'bytes': total}

    def _supervisor_launch(self, *, cancelled=lambda: False):
        """Bind the actual interpreter PID while retaining the sealed venv."""
        if cancelled():
            raise InterruptedError('Update handoff cancelled before supervisor launch.')
        raw, expected = self._runtime_inventory()
        manifest_stamp = _file_stamp(file_info(self.manifest_path))
        captured = {}

        def capture(path, maximum):
            if not path.is_relative_to(self.runtime):
                raise TrustError('The supervisor interpreter must remain inside its sealed runtime.')
            name = path.relative_to(self.runtime).as_posix()
            if name not in expected:
                raise TrustError('The supervisor launch path is not in its sealed runtime inventory.')
            rows = _directory_rows(path.parent)
            stamp = _file_stamp(file_info(path))
            digest, _ = _hash_file(path, maximum=maximum, cancelled=cancelled)
            if digest != expected[name] or _file_stamp(file_info(path)) != stamp:
                raise TrustError('The sealed supervisor launch input changed.')
            _same_directories(rows)
            captured[path] = (maximum, stamp, rows, expected[name])

        capture(self.python, 4 * 1024**3)
        executable, venv_python = self.python, None
        candidates = ()
        if os.name == 'nt':
            candidates = tuple(path for path in
                (self.python.parent / 'pyvenv.cfg', self.python.parent.parent / 'pyvenv.cfg')
                if path.is_relative_to(self.runtime))
        present = tuple(os.path.lexists(path) for path in candidates)
        selected = [path for path, exists in zip(candidates, present) if exists]
        if len(selected) > 1:
            raise TrustError('The supervisor venv configuration is ambiguous.')
        if selected:
            configuration = selected[0]
            capture(configuration, 4096)
            encoded = read_bytes(configuration, 4096)
            if sha256(encoded).hexdigest() != captured[configuration][3]:
                raise TrustError('The sealed supervisor venv configuration changed.')
            try:
                lines = encoded.decode('utf-8', errors='strict').splitlines()
            except UnicodeError as error:
                raise TrustError('The sealed supervisor venv configuration is not UTF-8.') from error
            homes = []
            for line in lines:
                key, separator, value = line.partition('=')
                if separator and key.strip().casefold() == 'home':
                    homes.append(value.strip())
            if len(homes) != 1 or not homes[0] or not Path(homes[0]).is_absolute():
                raise TrustError('The sealed supervisor venv needs one absolute Python home.')
            home = Path(homes[0])
            if not home.is_relative_to(self.runtime):
                raise TrustError('The supervisor Python home must remain inside its sealed runtime.')
            executable = directory(home) / 'python.exe'
            if executable == self.python:
                raise TrustError('The supervisor venv cannot redirect to its own launcher.')
            capture(executable, 4 * 1024**3)
            venv_python = self.python

        def current():
            if cancelled():
                raise InterruptedError('Update handoff cancelled before supervisor launch.')
            self.check()
            if (_file_stamp(file_info(self.manifest_path)) != manifest_stamp
                    or read_bytes(self.manifest_path, 8 * 1024**2) != raw
                    or tuple(os.path.lexists(path) for path in candidates) != present):
                raise TrustError('The sealed supervisor launch authority changed.')
            for path, (maximum, stamp, rows, expected_digest) in captured.items():
                _same_directories(rows)
                if _file_stamp(file_info(path)) != stamp:
                    raise TrustError('The sealed supervisor launch identity changed.')
                digest, _ = _hash_file(path, maximum=maximum, cancelled=cancelled)
                if digest != expected_digest:
                    raise TrustError('The sealed supervisor launch bytes changed.')
            # Cancellation callbacks may have touched an earlier input while a
            # later one was read. Preserve every original identity at admission.
            for path, (_, stamp, rows, _) in captured.items():
                _same_directories(rows)
                if _file_stamp(file_info(path)) != stamp:
                    raise TrustError('The sealed supervisor launch identity changed.')
            if (_file_stamp(file_info(self.manifest_path)) != manifest_stamp
                    or read_bytes(self.manifest_path, 8 * 1024**2) != raw
                    or tuple(os.path.lexists(path) for path in candidates) != present):
                raise TrustError('The sealed supervisor launch authority changed.')
            self.check()

        current()
        return executable, _supervisor_environment(venv_python), current

    def client(self):
        repository = self.value['repository']
        return ConfiguredRepositoryClient(self.work / 'download-cache', self.policy,
            endpoint=self.endpoint, timeout=repository['timeout'], ca_pem=repository['tls_ca_pem'])

    def anchor(self):
        from .anchors import WindowsVaultAnchor
        return WindowsVaultAnchor(**self.anchor_arguments)


class ConfiguredRepositoryClient:
    """One TUF policy, explicit offline import, and an optional configured site."""
    def __init__(self, cache, policy, *, endpoint=None, timeout=1800, ca_pem=None):
        self.policy, self.endpoint = policy, endpoint
        root = directory(cache, create=True)
        self.local = LocalRepositoryClient(root / 'offline', policy)
        self.online = None
        if endpoint is not None:
            from .update_transport import HttpsRepositoryClient
            self.online = HttpsRepositoryClient(root / 'online', policy, endpoint=endpoint,
                                                timeout=timeout, ca_pem=ca_pem)

    def _client(self, repository):
        if type(repository) is str and repository.startswith(('https:', 'http:')):
            if self.online is None or repository != self.endpoint:
                raise TrustError('The selected website is not configured for this installation.')
            return self.online
        return self.local

    def check(self, repository, target, *, cancelled=lambda: False):
        return self._client(repository).check(repository, target, cancelled=cancelled)

    def download(self, repository, target, *, reviewed_sha256, cancelled=lambda: False, progress=None):
        client = self._client(repository)
        options = dict(reviewed_sha256=reviewed_sha256, cancelled=cancelled)
        if client is self.online:
            options['progress'] = progress
        return client.download(repository, target, **options)



def _copy_snapshot_file(source, target, expected, *, maximum, cancelled):
    """Copy one exact ordinary source; a snapshot never contains a live venv."""
    before = file_info(source)
    if before.st_size > maximum:
        raise TrustError('The reviewed snapshot exceeds its byte allowance.')
    descriptor = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    total, digest = 0, sha256()
    with os.fdopen(descriptor, 'rb') as stream, target.open('xb') as output:
        if _file_stamp(os.fstat(descriptor)) != _file_stamp(before):
            raise TrustError('The reviewed snapshot source changed while opening.')
        while block := stream.read(1024 * 1024):
            if cancelled():
                raise InterruptedError('Snapshot preparation cancelled.')
            total += len(block)
            if total > maximum:
                raise TrustError('The reviewed snapshot source grew.')
            digest.update(block)
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
        if _file_stamp(os.fstat(descriptor)) != _file_stamp(before):
            raise TrustError('The reviewed snapshot source changed while reading.')
    if (total != before.st_size or digest.hexdigest() != expected
            or _file_stamp(file_info(source)) != _file_stamp(before)):
        raise TrustError('The reviewed snapshot source identity changed.')


def capture_offline_repository(client, package, destination, *, cancelled=lambda: False):
    """Retain the actual authenticated metadata for a network-free handoff.

    TUF verifies this independent copy again in the external host. Only private
    metadata and one exact target are copied; no update code is imported.
    """
    from tuf.api.metadata import Metadata
    from .updates import _metadata_cache
    require_verified_package(package)
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    metadata = destination / 'metadata'
    targets = destination / 'targets'
    metadata.mkdir()
    targets.mkdir()
    with locked(client.cache):
        _metadata_cache(client.metadata)
        roots = sorted((client.metadata / 'root_history').iterdir(),
                       key=lambda path: int(path.name.split('.', 1)[0]))
        if not roots:
            raise TrustError('No authenticated TUF root history is available.')
        last = None
        for path in roots:
            # TUF's one exact root.json alias can make its latest history file
            # a two-link file on Windows; no other linked history is admitted.
            before = path.lstat()
            alias = client.metadata / 'root.json'
            if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
                    or getattr(before, 'st_file_attributes', 0) & 0x400
                    or before.st_size > 512000 or before.st_nlink not in (1, 2)
                    or before.st_nlink == 2 and (alias.is_symlink() or not os.path.samefile(alias, path))):
                raise TrustError('The authenticated root history changed.')
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(descriptor, 'rb') as stream:
                if _file_stamp(os.fstat(descriptor)) != _file_stamp(before):
                    raise TrustError('The authenticated root changed while opening.')
                raw = stream.read(512001)
                if len(raw) > 512000 or _file_stamp(os.fstat(descriptor)) != _file_stamp(before):
                    raise TrustError('The authenticated root changed while copying.')
            if _file_stamp(path.lstat()) != _file_stamp(before):
                raise TrustError('The authenticated root path changed.')
            atomic_write(metadata / path.name, raw)
            last = Metadata.from_bytes(raw).signed
        target_info = None
        for role, maximum in (('timestamp', 16384), ('snapshot', 2_000_000), ('targets', 5_000_000)):
            raw = read_bytes(client.metadata / (role + '.json'), maximum)
            parsed = Metadata.from_bytes(raw).signed
            name = (role + '.json' if role == 'timestamp' or not last.consistent_snapshot
                    else f'{parsed.version}.{role}.json')
            atomic_write(metadata / name, raw)
            if role == 'targets':
                target_info = parsed.targets.get(package.release.target)
        _metadata_cache(client.metadata)
        if target_info is None or target_info.hashes.get('sha256') != package.release.sha256:
            raise TrustError('The cached signed target differs from the reviewed package.')
        target_path = package.release.target
        if last.consistent_snapshot:
            directory_part, separator, basename = target_path.rpartition('/')
            digest = next(iter(target_info.hashes.values()))
            target_path = directory_part + separator + digest + '.' + basename
        target = targets.joinpath(*relative_path(target_path))
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_snapshot_file(package.path, target, package.release.sha256,
                            maximum=package.release.length, cancelled=cancelled)
    return destination

_PREPARED = set()
_PREPARED_LOCK = threading.Lock()


def _forget(owner):
    with _PREPARED_LOCK:
        _PREPARED.discard(owner)


@atexit.register
def _cancel_uncommitted_handoffs():
    with _PREPARED_LOCK:
        owners = tuple(_PREPARED)
    for owner in owners:
        if not owner.transferred:
            try:
                owner.abort()
            except Exception:
                # The fixed supervisor never applies without a commit. Its
                # finite ready deadline still expires if process exit wins.
                pass


class UpdateHandoff:
    """Explicit ownership transfer; a committed helper is not claimed reaped."""
    def __init__(self, configuration, path, request, process):
        self.configuration, self.path, self.request, self.process = configuration, path, request, process
        self.digest = sha256(json_bytes(request)).hexdigest()
        self.directories = _directory_rows(path)
        self._ownership_lock = threading.RLock()
        self.transferred = False
        self.closed = False
        with _PREPARED_LOCK:
            _PREPARED.add(self)

    def _check(self):
        self.configuration.check()
        _same_directories(self.directories)
        if read_bytes(self.path / 'request.json', 32768) != json_bytes(self.request):
            raise TrustError('The reviewed update handoff changed.')

    def wait_ready(self, *, cancelled=lambda: False, timeout=300):
        deadline = time.monotonic() + timeout
        ready = self.path / 'ready.json'
        try:
            while time.monotonic() < deadline:
                if cancelled():
                    raise InterruptedError('Update preparation cancelled.')
                self._check()
                if os.path.lexists(ready):
                    result = json_value(read_bytes(ready, 16384), maximum=16384)
                    expected = {'schema': 1, 'status': 'ready', 'request_sha256': self.digest,
                                'pid': self.process.pid, 'release': self.request['release']}
                    returncode = self.process.poll()
                    if result != expected or returncode is not None:
                        raise TrustError('The maintenance host did not retain this exact reviewed update. '
                            f'Ready identity matches: {result == expected}; direct process returncode: {returncode!r}.')
                    return self
                if self.process.poll() is not None:
                    raise TrustError('The maintenance host refused preparation. Review its retained status.')
                time.sleep(.05)
            raise TimeoutError('The maintenance host did not become ready within its bound.')
        except BaseException:
            self.abort()
            raise

    @contextmanager
    def desktop_close_guard(self):
        """Serialize desktop finalizers with its one explicit Close decision."""
        with self._ownership_lock:
            if self.closed or self.transferred:
                raise TrustError('This prepared update no longer belongs to the desktop.')
            yield

    def commit_after_close(self, *, host_closed):
        with self._ownership_lock:
            if host_closed is not True or self.closed or self.transferred:
                raise TrustError('Apply requires the actual approved desktop Close handoff.')
            self._check()
            if self.process.poll() is not None:
                raise TrustError('The prepared update expired; check the update again.')
            raw = json_bytes({'schema': 1, 'action': 'apply', 'request_sha256': self.digest,
                              'nonce': self.request['nonce']})
            path = self.path / 'decision.json'
            try:
                atomic_write(path, raw)
            except BaseException as error:
                # A publication attempt may have become visible before a later
                # fsync/readback failure. Never kill this helper when acceptance
                # cannot be disproved: only its fixed COMMIT protocol can start
                # setup, and an absent decision expires without any child.
                try:
                    visible = read_bytes(path, 4096) == raw
                except Exception:
                    visible = False
                self.transferred = True
                _forget(self)
                receipt = self.receipt()
                if not visible:
                    receipt['status'] = 'HANDOFF_UNCERTAIN'
                error.update_handoff = receipt
                raise
            self.transferred = True
            _forget(self)
            return self.receipt()

    def receipt(self):
        return {'schema': 1, 'status': 'HANDED_OFF' if self.transferred else 'PREPARED',
                'request_sha256': self.digest, 'pid': self.process.pid,
                'report': str(self.path / 'result.json'), 'installation': str(self.configuration.root),
                'release': self.request['release'], 'activation': 'NOT_ESTABLISHED'}

    def abort(self):
        with self._ownership_lock:
            if self.closed or self.transferred:
                return
            failure = None
            try:
                if self.process.poll() is None:
                    try:
                        self._check()
                        atomic_write(self.path / 'decision.json', json_bytes({
                            'schema': 1, 'action': 'cancel', 'request_sha256': self.digest,
                            'nonce': self.request['nonce']}))
                    except Exception as error:
                        failure = error
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        # Before commit this fixed supervisor uses only the retained
                        # offline repository and starts no installer or restart child.
                        # Terminate this actual owned process even if paths changed.
                        self.process.terminate()
                        self.process.wait(timeout=5)
            finally:
                if self.process.poll() is not None:
                    self.closed = True
                    _forget(self)
            if failure is not None:
                raise failure


class ManagedUpdateHandoffService:
    def __init__(self, configuration):
        if type(configuration) is not OwnerUpdateConfiguration:
            raise TrustError('Use an independently verified owner update configuration.')
        self.configuration = configuration

    def prepare(self, repository, package, *, restart=False, cancelled=lambda: False):
        require_verified_package(package)
        config = self.configuration
        if type(restart) is not bool:
            raise TrustError('Restart requires an explicit choice.')
        config.check()
        if (package.release.product, package.release.channel, package.release.target) != (
                config.policy.product, config.policy.channel, config.target):
            raise TrustError('The package does not belong to this configured update target.')
        deadline = time.monotonic() + 300
        def stopped():
            return cancelled() or time.monotonic() >= deadline
        config.verify_runtime(cancelled=stopped)
        if stopped():
            raise InterruptedError('Update handoff cancelled before launch.')
        client = config.client()
        fresh = client.download(repository, config.target, reviewed_sha256=package.release.sha256,
                                cancelled=stopped)
        if fresh.release != package.release:
            raise TrustError('The complete signed release changed; review it again before Apply.')
        parent = directory(config.work / 'handoffs', create=True)
        path = parent / uuid.uuid4().hex
        path.mkdir(mode=0o700)
        retained = capture_offline_repository(client._client(repository), fresh,
                                             path / 'repository', cancelled=stopped)
        request = {'schema': 1, 'nonce': uuid.uuid4().hex, 'created_at': int(time.time()),
                   'repository': str(retained), 'release': asdict(package.release),
                   'restart': restart, 'library_directories': [list(row) for row in _directory_rows(config.library)],
                   'root_directories': [list(row) for row in _directory_rows(config.root)]}
        raw = json_bytes(request)
        request_path = path / 'request.json'
        atomic_write(request_path, raw)
        executable, environment, launch_current = config._supervisor_launch(cancelled=stopped)
        config.check()
        process = subprocess.Popen([str(executable), '-I', '-B', '-m',
            'carr_library.security.update_host', '--configuration', str(config.path),
            '--configuration-sha256', config.sha256, '--request', str(request_path),
            '--request-sha256', sha256(raw).hexdigest()], cwd=str(config.runtime), env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=os.name == 'posix',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        owner = UpdateHandoff(config, path, request, process)
        try:
            launch_current()
            return owner.wait_ready(cancelled=stopped, timeout=max(.05, deadline - time.monotonic()))
        except BaseException:
            owner.abort()
            raise
