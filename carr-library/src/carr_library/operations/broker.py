"""Cross-process cooperative admission over existing job stores and native locks.

Reservations are estimates declared by trusted adapters. No process is killed,
no PID is taken as proof of liveness, and elapsed time never revokes a live lock.
"""
from contextlib import contextmanager
from dataclasses import replace
import os
from pathlib import Path
import re
import shutil
import time
from uuid import uuid4

from ..durable_state import atomic_json, local_path, read_json, regular, sync_directory
from ._locks import NativeLock
from .models import (ResourceLimits, ResourceRequest, ResourceError, ResourceCancelled,
                     ResourceTimeout, ResourceUnavailable, ResourceConflict, ResourceStateError,
                     _seconds, _token)


_IDENTITY = re.compile(r'[0-9a-f]{32}')
_POLL_SECONDS = .05
_MAX_RECORDS = 8192
_MAX_RECORD_BYTES = 32768
_PROCESS_ID, _PROCESS_TOKEN = os.getpid(), uuid4().hex


def _process_token():
    global _PROCESS_ID, _PROCESS_TOKEN
    if _PROCESS_ID != os.getpid():
        _PROCESS_ID, _PROCESS_TOKEN = os.getpid(), uuid4().hex
    return _PROCESS_TOKEN


def _read(path):
    if regular(path).st_size > _MAX_RECORD_BYTES:
        raise ResourceStateError('Resource policy or lease exceeds its bounded record size.')
    try:
        return read_json(path)
    except (ValueError, UnicodeError) as exc:
        raise ResourceStateError('Resource policy or lease is invalid; admission stopped.') from exc


def _directory_identity(path):
    value = regular(local_path(path, exists=True), directory=True)
    return value.st_dev, value.st_ino


def _release_reservation_lock(lock):
    """Release one unique lease lock and its inert file while _gate is held.

    UUID reservation names are allocated only under the coordinator gate.
    No cooperating owner or waiter can acquire this identity between release
    and deletion. The shared admission lock must never use this helper.
    """
    if not lock.held:
        raise ResourceStateError('Only an owned reservation lock can be retired.')
    try:
        before = regular(lock.path)
    finally:
        # Always close the descriptor, including a changed-path failure.
        # NativeLock validates ownership; Windows may remove the file here.
        lock.release()
    if os.path.lexists(lock.path):
        after = regular(lock.path)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ResourceStateError('A released reservation lock was replaced; it was not removed.')
        lock.path.unlink()
        sync_directory(lock.path.parent)


class ResourceLease:
    def __init__(self, broker, record, lock, cancelled, run_timeout):
        self.broker, self.record, self._lock = broker, record, lock
        self.id, self.operation_id = record['id'], record['operation_id']
        self.request = ResourceRequest.from_dict(record['request'])
        self._cancelled, self._stopped = cancelled, False
        self._started, self._timeout = time.monotonic(), run_timeout
        self._pid, self._released = os.getpid(), False
        self.cleanup_pending = False

    def cancelled(self):
        """A stop signal only. Resources stay held until the worker is cleaned up."""
        if self._released or self._pid != os.getpid():
            return True
        self._stopped = self._stopped or bool(self._cancelled())
        return self._stopped or (self._timeout is not None and time.monotonic() - self._started >= self._timeout)

    def checkpoint(self):
        if self._pid != os.getpid() or self._released or not self._lock.held:
            raise ResourceStateError('This resource reservation is not held by the current process.')
        self.broker._check_scope()
        self._stopped = self._stopped or bool(self._cancelled())
        if self._stopped:
            raise ResourceCancelled('Operation cancelled; finish native worker cleanup before leaving the reservation.')
        self.broker._check_scope()
        if self._timeout is not None and time.monotonic() - self._started >= self._timeout:
            raise ResourceTimeout('Operation exceeded its cooperative execution time allowance.')

    def settle_cleanup(self, error):
        """Keep this native lease until the existing worker owner is quiescent.

        This synchronous boundary runs on an operation worker, never the GUI.
        It does not kill processes or replace the canonical CleanupOwner.
        """
        from ..audio.models import AudioCleanupPending
        if not isinstance(error, AudioCleanupPending):
            raise TypeError('An owned pending-cleanup error is required.')
        if self._pid != os.getpid() or self._released or not self._lock.held:
            raise ResourceStateError('Pending worker cleanup requires its still-owned reservation.')
        owner, interrupted = error.cleanup_owner, None
        while True:
            try:
                if owner.quiescent_event.is_set():
                    break
                owner.close()
                if not owner.quiescent_event.is_set():
                    owner.quiescent_event.wait(.25)
            except (KeyboardInterrupt, SystemExit) as exc:
                if interrupted is None:
                    interrupted = exc
        if interrupted is not None:
            raise interrupted

    def release(self):
        if self._released:
            return
        if self._pid != os.getpid():
            raise ResourceStateError('A child process cannot release its parent reservation.')
        try:
            try:
                with self.broker._gate(time.monotonic() + 5, lambda: False):
                    path = self.broker.leases_root / (self.id + '.json')
                    if os.path.lexists(path):
                        value = _read(path)
                        if value != self.record:
                            raise ResourceStateError('Reservation identity changed before release.')
                        self.broker._check_scope()
                        path.unlink()
                        sync_directory(path.parent)
                    # Other callers must not observe a live lock after its
                    # corresponding record has been removed.
                    self.broker._release_lock(self._lock, retire=True)
            except ResourceTimeout:
                # Closing the native lock is sufficient to release ownership.
                # The next admission reaps the now-unlocked stale metadata.
                self.cleanup_pending = True
            except ResourceStateError:
                self.cleanup_pending = True
                raise
        finally:
            try:
                self.broker._release_lock(self._lock)
            except ResourceStateError:
                self.cleanup_pending = True
                raise
            finally:
                if not self._lock.held:
                    self._released = True


class ResourceBroker:
    def __init__(self, directory, *, limits=None, policy_path=None, cancelled=lambda: False,
                 _application_policy=False, require_existing_policy=False):
        if type(require_existing_policy) is not bool:
            raise TypeError('The existing resource policy requirement must be boolean.')
        if limits is not None and not isinstance(limits, ResourceLimits):
            raise TypeError('A typed resource policy is required.')
        if type(_application_policy) is not bool:
            raise TypeError('The application policy mode must be explicit.')
        self.root = local_path(directory)
        self._scope_directories = ()
        if _application_policy:
            application_root = self.root.parent.parent
            self._scope_directories = ((application_root, _directory_identity(application_root)),)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        local_path(self.root, exists=True)
        self.leases_root = self.root / 'leases'
        self.leases_root.mkdir(mode=0o700, exist_ok=True)
        local_path(self.leases_root, exists=True)
        self.policy_path = local_path(policy_path or self.root / 'resources.json')
        if _application_policy and self.policy_path != (
                self.root.parent.parent / 'state' / 'operations' / 'resources.json'):
            raise ResourceConflict('Application resources require the canonical policy location.')
        binding = self.root / '.policy-location.json'
        if self.policy_path in (binding, self.root / '.admission.lock') or self.policy_path.is_relative_to(self.leases_root):
            raise ValueError('Resource policy must not replace coordinator or live-lease files.')
        self.policy_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        scope = dict(self._scope_directories)
        for selected in (self.root, self.leases_root, self.policy_path.parent):
            scope[selected] = _directory_identity(selected)
        self._scope_directories = tuple(scope.items())
        with self._gate(time.monotonic() + 5, cancelled):
            location = ({'schema': 'carr-application-resource-policy/1',
                         'path': 'state/operations/resources.json'} if _application_policy else
                        {'schema': 'carr-resource-policy-location/1', 'path': str(self.policy_path)})
            binding_exists = os.path.lexists(binding)
            previous = _read(binding) if binding_exists else None
            if not binding_exists:
                atomic_json(binding, location)
            elif previous != location:
                # Runtime coordination is not part of typed backup. If an owner
                # copies a complete library directory, the application policy
                # remains relative to that library; never open the old root.
                # Migrate only the older exact application-policy shape while
                # no reservation in THIS coordinator holds a native lock.
                legacy = (type(previous) is dict
                          and set(previous) == {'schema', 'path'}
                          and previous['schema'] == 'carr-resource-policy-location/1'
                          and isinstance(previous['path'], str))
                legacy_path = Path(previous['path']) if legacy else None
                if (not _application_policy or not legacy
                        or not legacy_path.is_absolute()
                        or legacy_path.parts[-3:] != ('state', 'operations', 'resources.json')
                        or self._active()):
                    raise ResourceConflict('This runtime coordinator is already bound to another policy path.')
                atomic_json(binding, location, replace=True)
            if not os.path.lexists(self.policy_path):
                if require_existing_policy:
                    raise ResourceStateError('The retained resource policy is missing; '
                                             'automatic defaults are not permitted for restored work.')
                atomic_json(self.policy_path, {'schema': 'carr-resource-policy/1',
                            'limits': (limits or ResourceLimits()).to_dict()})
            elif limits is not None and self.limits != limits:
                raise ResourceConflict('A different resource policy already exists. Use configure() explicitly.')
            self.limits

    @classmethod
    def for_app(cls, root, *, limits=None, cancelled=lambda: False, require_existing_policy=False):
        root = local_path(root, exists=True)
        regular(root, directory=True)
        return cls(root / 'runtime' / 'resources', limits=limits,
                   policy_path=root / 'state' / 'operations' / 'resources.json', cancelled=cancelled,
                   _application_policy=True, require_existing_policy=require_existing_policy)

    def _check_scope(self):
        # Resolve each maximal captured branch once in this check.  local_path
        # still rejects linked ancestors; every captured identity stays fresh.
        for path, _expected in self._scope_directories:
            if any(other != path and other.is_relative_to(path)
                   for other, _identity in self._scope_directories):
                continue
            try:
                if local_path(path, exists=True) != path:
                    raise ValueError('The resource directory path changed.')
            except (OSError, ValueError) as error:
                raise ResourceStateError(
                    'The selected resource directory changed; reopen the selected library before retrying.') from error
        for path, expected in self._scope_directories:
            try:
                value = regular(path, directory=True)
                current = value.st_dev, value.st_ino
            except (OSError, ValueError) as error:
                raise ResourceStateError(
                    'The selected resource directory changed; reopen the selected library before retrying.') from error
            if current != expected:
                raise ResourceStateError(
                    'The selected resource directory changed; reopen the selected library before retrying.')

    def _release_lock(self, lock, *, retire=False):
        try:
            self._check_scope()
        except ResourceStateError:
            # The worker is already quiescent at this boundary. Keep metadata
            # inert in its original directory; never enter a substituted path.
            lock.close_owned_descriptor()
            raise
        if retire:
            _release_reservation_lock(lock)
        else:
            lock.release()

    @property
    def limits(self):
        self._check_scope()
        value = _read(self.policy_path)
        self._check_scope()
        if (not isinstance(value, dict) or set(value) != {'schema', 'limits'}
                or value['schema'] != 'carr-resource-policy/1'):
            raise ResourceStateError('Unsupported saved resource policy.')
        try:
            return ResourceLimits.from_dict(value['limits'])
        except ValueError as exc:
            raise ResourceStateError('Saved resource limits are invalid.') from exc

    @contextmanager
    def _gate(self, deadline, cancelled):
        self._check_scope()
        lock = NativeLock(self.root / '.admission.lock')
        attempted = False
        while True:
            if cancelled():
                raise ResourceCancelled('Resource admission was cancelled before starting heavy work.')
            self._check_scope()
            if attempted and time.monotonic() >= deadline:
                raise ResourceTimeout('Timed out waiting for the shared resource coordinator.')
            attempted = True
            if lock.acquire():
                try:
                    self._check_scope()
                    if cancelled():
                        raise ResourceCancelled('Resource admission was cancelled before starting heavy work.')
                    self._check_scope()
                    yield
                finally:
                    self._release_lock(lock)
                return
            if time.monotonic() >= deadline:
                raise ResourceTimeout('Timed out waiting for the shared resource coordinator.')
            time.sleep(min(_POLL_SECONDS, max(0, deadline - time.monotonic())))

    def _active(self):
        """Called under the coordinator lock; native lock ownership is decisive."""
        self._check_scope()
        local_path(self.leases_root, exists=True)
        identities = set()
        with os.scandir(self.leases_root) as entries:
            for index, entry in enumerate(entries):
                if index >= _MAX_RECORDS:
                    raise ResourceStateError('Resource lease inventory exceeds its safety limit.')
                self._check_scope()
                path = self.leases_root / entry.name
                regular(path)
                if entry.name.startswith('.pending-'):
                    # All publications in this private namespace hold _gate.
                    self._check_scope()
                    path.unlink()
                    continue
                if path.suffix not in ('.json', '.lock') or not _IDENTITY.fullmatch(path.stem):
                    raise ResourceStateError('Unexpected resource lease namespace entry.')
                identities.add(path.stem)
        result = []
        for identity in sorted(identities):
            self._check_scope()
            path = self.leases_root / (identity + '.json')
            probe = NativeLock(path.with_suffix('.lock'))
            if probe.acquire():
                try:
                    self._check_scope()
                    if os.path.lexists(path):
                        regular(path)
                        path.unlink()
                finally:
                    self._release_lock(probe, retire=True)
                continue
            if not os.path.lexists(path):
                raise ResourceStateError('A live resource lock has no valid reservation record.')
            value = _read(path)
            expected = {'schema', 'id', 'operation_id', 'owner_pid', 'owner_token',
                        'created', 'request', 'disk_volumes'}
            if (not isinstance(value, dict) or set(value) != expected
                    or value['schema'] != 'carr-resource-lease/1' or value['id'] != identity
                    or type(value['owner_pid']) is not int or value['owner_pid'] < 1
                    or not isinstance(value['owner_token'], str) or not _IDENTITY.fullmatch(value['owner_token'])
                    or type(value['created']) not in (int, float) or not 0 < value['created'] < 10 ** 12):
                raise ResourceStateError('Invalid live resource reservation identity.')
            try:
                _token(value['operation_id'], 'operation identity')
                request = ResourceRequest.from_dict(value['request'])
                if any(not path.is_absolute() for path, _ in request.disks):
                    raise ValueError('A saved disk directory must be absolute.')
                if (not isinstance(value['disk_volumes'], list)
                        or len(value['disk_volumes']) != len(request.disks)
                        or any(not isinstance(volume, str) or not re.fullmatch(r'[0-9]+', volume)
                               for volume in value['disk_volumes'])):
                    raise ValueError('Invalid disk volume binding.')
            except ValueError as exc:
                raise ResourceStateError('Invalid live resource reservation request.') from exc
            result.append(value)
        self._check_scope()
        return result

    @staticmethod
    def _fits_policy(request, limits):
        for name in ('cpu_slots', 'memory_bytes', 'disk_bytes'):
            amount = request.total_disk_bytes if name == 'disk_bytes' else getattr(request, name)
            if amount > getattr(limits, name):
                raise ResourceUnavailable(f'Requested {name} exceeds the saved cooperative resource policy.')
        if request.gpu_device is not None:
            maximum = dict(limits.gpu_devices).get(request.gpu_device)
            if maximum is None:
                raise ResourceUnavailable('Selected GPU has no explicit configured capacity; no device fallback was performed.')
            if request.gpu_memory_bytes > maximum:
                raise ResourceUnavailable('Requested GPU memory exceeds its configured capacity.')

    @staticmethod
    def _usage(active):
        return {name: sum(ResourceRequest.from_dict(item['request']).total_disk_bytes
                          if name == 'disk_bytes' else item['request'][name] for item in active)
                for name in ('cpu_slots', 'memory_bytes', 'disk_bytes')}

    def _reason(self, request, volumes, active, limits):
        self._fits_policy(request, limits)
        if len(active) >= limits.max_leases:
            return 'active reservation slots'
        usage = self._usage(active)
        for name in usage:
            amount = request.total_disk_bytes if name == 'disk_bytes' else getattr(request, name)
            if usage[name] + amount > getattr(limits, name):
                return name
        devices = set(request.devices)
        for item in active:
            other = item['request']
            if request.gpu_device is not None and other['gpu_device'] == request.gpu_device:
                return 'exclusive GPU device'
            if devices.intersection(other['devices']):
                return 'exclusive audio or capture device'
        needed, representatives = {}, {}
        for (path, amount), volume in zip(request.disks, volumes):
            regular(local_path(path, exists=True), directory=True)
            if str(path.stat().st_dev) != volume:
                raise ResourceStateError('The selected disk volume changed during admission.')
            needed[volume] = needed.get(volume, 0) + amount
            representatives[volume] = path
        for volume, amount in needed.items():
            committed = sum(size for item in active
                            for (_, size), used_volume in zip(ResourceRequest.from_dict(item['request']).disks,
                                                              item['disk_volumes']) if used_volume == volume)
            if shutil.disk_usage(representatives[volume]).free < committed + amount + limits.min_free_disk_bytes:
                return 'actual free disk reserve'
        return None

    def configure(self, limits, *, timeout=5, expected_limits=None, cancelled=lambda: False):
        if not isinstance(limits, ResourceLimits):
            raise TypeError('A typed resource policy is required.')
        if expected_limits is not None and not isinstance(expected_limits, ResourceLimits):
            raise TypeError('The reviewed previous resource policy must be typed.')
        if not callable(cancelled):
            raise TypeError('Policy admission requires a cancellation callback.')
        deadline = time.monotonic() + _seconds(timeout, 'Policy wait timeout')
        with self._gate(deadline, cancelled):
            if expected_limits is not None and self.limits != expected_limits:
                raise ResourceConflict('Resource allowances changed. Refresh and review them before saving.')
            active = self._active()
            if len(active) > limits.max_leases or any(self._usage(active)[name] > getattr(limits, name)
                                                     for name in ('cpu_slots', 'memory_bytes', 'disk_bytes')):
                raise ResourceConflict('New resource limits would invalidate existing reservations.')
            for record in active:
                try:
                    self._fits_policy(ResourceRequest.from_dict(record['request']), limits)
                except ResourceUnavailable as exc:
                    raise ResourceConflict('New resource limits would invalidate an existing reservation.') from exc
            if cancelled():
                raise ResourceCancelled('Saving resource allowances was cancelled before publication.')
            self._check_scope()
            atomic_json(self.policy_path, {'schema': 'carr-resource-policy/1', 'limits': limits.to_dict()}, replace=True)
        return limits

    @contextmanager
    def reserve(self, request, *, operation_id, cancelled=lambda: False, timeout=60, run_timeout=None):
        if not isinstance(request, ResourceRequest) or not callable(cancelled):
            raise TypeError('Admission requires a typed request and a cancellation callback.')
        _token(operation_id, 'operation identity')
        timeout = _seconds(timeout, 'Admission wait timeout')
        run_timeout = _seconds(run_timeout, 'Execution timeout', allow_none=True)
        if cancelled():
            raise ResourceCancelled('Resource admission was cancelled before starting heavy work.')
        disks, volumes = [], []
        for incoming, amount in request.disks:
            path = local_path(incoming, exists=True)
            regular(path, directory=True)
            disks.append((path, amount))
            volumes.append(str(path.stat().st_dev))
        if disks:
            offset = int(request.disk_path is not None)
            request = replace(request, disk_path=disks[0][0] if offset else None,
                              additional_disks=tuple(disks[offset:]))
        deadline, lease, attempted = time.monotonic() + timeout, None, False
        try:
            while lease is None:
                if cancelled():
                    raise ResourceCancelled('Resource admission was cancelled while waiting.')
                if attempted and time.monotonic() >= deadline:
                    raise ResourceTimeout('Timed out waiting for shared resources; heavy work was not started.')
                attempted = True
                with self._gate(deadline, cancelled):
                    limits, active = self.limits, self._active()
                    if any(item['operation_id'] == operation_id for item in active):
                        raise ResourceConflict('This operation already holds a resource reservation.')
                    reason = self._reason(request, volumes, active, limits)
                    if reason is None:
                        self._check_scope()
                        identity = uuid4().hex
                        record = {'schema': 'carr-resource-lease/1', 'id': identity, 'operation_id': operation_id,
                                  'owner_pid': os.getpid(), 'owner_token': _process_token(), 'created': time.time(),
                                  'request': request.to_dict(), 'disk_volumes': volumes}
                        lock = NativeLock(self.leases_root / (identity + '.lock'))
                        if not lock.acquire():
                            raise ResourceConflict('Reservation identity is already locked.')
                        try:
                            if cancelled():
                                raise ResourceCancelled('Resource admission was cancelled before publication.')
                            self._check_scope()
                            atomic_json(self.leases_root / (identity + '.json'), record)
                            lease = ResourceLease(self, record, lock, cancelled, run_timeout)
                        except BaseException:
                            self._release_lock(lock, retire=True)
                            raise
                if lease is None:
                    if cancelled():
                        raise ResourceCancelled('Resource admission was cancelled while waiting.')
                    if time.monotonic() >= deadline:
                        raise ResourceTimeout('Timed out waiting for ' + reason + '; heavy work was not started.')
                    time.sleep(min(_POLL_SECONDS, max(0, deadline - time.monotonic())))
            lease.checkpoint()
            yield lease
        except BaseException as error:
            from ..audio.models import AudioCleanupPending
            if lease is not None and isinstance(error, AudioCleanupPending):
                lease.settle_cleanup(error)
            raise
        finally:
            if lease is not None:
                lease.release()

    def snapshot(self, *, timeout=5, cancelled=lambda: False):
        with self._gate(time.monotonic() + _seconds(timeout, 'Snapshot timeout'), cancelled):
            active, limits = self._active(), self.limits
            return {'schema': 'carr-resource-snapshot/1', 'limits': limits.to_dict(),
                    'usage': self._usage(active), 'leases': active,
                    'gpu_devices_reserved': sorted(item['request']['gpu_device'] for item in active
                                                   if item['request']['gpu_device'] is not None),
                    'exclusive_devices': sorted(device for item in active for device in item['request']['devices']),
                    'measurement': 'declared cooperative reservations, not measured CPU/RAM/VRAM usage'}

    def capabilities(self):
        return {'schema': 'carr-resource-capabilities/1',
                'coordination': 'native cross-process file locks', 'pid_based_reaping': False,
                'scope': 'cooperating callers using this same broker directory',
                'cpu_policy': 'declared slots; no affinity or operating-system CPU enforcement',
                'memory_policy': 'declared estimates; no physical RAM/VRAM measurement or memory sandbox',
                'disk_policy': 'declared peak bytes plus queried free bytes per selected filesystem',
                'devices': 'exclusive canonical IDs supplied by actual device adapters; no device enumeration',
                'crash_recovery': 'unlocked reservations reclaimed on the next admission or snapshot',
                'orphan_native_workers_managed': False, 'network_sandbox': False,
                'execution_cancellation': 'cooperative; live reservations are never expired or forcibly revoked',
                'configured_limits': self.limits.to_dict()}
