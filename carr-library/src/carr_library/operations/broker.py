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

from ._state import atomic_json, local_path, read_json, regular, sync_directory
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
        self._stopped = self._stopped or bool(self._cancelled())
        if self._stopped:
            raise ResourceCancelled('Operation cancelled; finish native worker cleanup before leaving the reservation.')
        if self._timeout is not None and time.monotonic() - self._started >= self._timeout:
            raise ResourceTimeout('Operation exceeded its cooperative execution time allowance.')

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
                        path.unlink()
                        sync_directory(path.parent)
                    # Other callers must not observe a live lock after its
                    # corresponding record has been removed.
                    self._lock.release()
            except ResourceTimeout:
                # Closing the native lock is sufficient to release ownership.
                # The next admission reaps the now-unlocked stale metadata.
                self.cleanup_pending = True
        finally:
            self._lock.release()
            self._released = True


class ResourceBroker:
    def __init__(self, directory, *, limits=None, policy_path=None, cancelled=lambda: False):
        if limits is not None and not isinstance(limits, ResourceLimits):
            raise TypeError('A typed resource policy is required.')
        self.root = local_path(directory)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        local_path(self.root, exists=True)
        self.leases_root = self.root / 'leases'
        self.leases_root.mkdir(mode=0o700, exist_ok=True)
        local_path(self.leases_root, exists=True)
        self.policy_path = local_path(policy_path or self.root / 'resources.json')
        binding = self.root / '.policy-location.json'
        if self.policy_path in (binding, self.root / '.admission.lock') or self.policy_path.is_relative_to(self.leases_root):
            raise ValueError('Resource policy must not replace coordinator or live-lease files.')
        self.policy_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._gate(time.monotonic() + 5, cancelled):
            location = {'schema': 'carr-resource-policy-location/1', 'path': str(self.policy_path)}
            if not os.path.lexists(binding):
                atomic_json(binding, location)
            elif _read(binding) != location:
                raise ResourceConflict('This runtime coordinator is already bound to another policy path.')
            if not os.path.lexists(self.policy_path):
                atomic_json(self.policy_path, {'schema': 'carr-resource-policy/1',
                            'limits': (limits or ResourceLimits()).to_dict()})
            elif limits is not None and self.limits != limits:
                raise ResourceConflict('A different resource policy already exists. Use configure() explicitly.')
            self.limits

    @classmethod
    def for_app(cls, root, *, limits=None, cancelled=lambda: False):
        root = local_path(root, exists=True)
        regular(root, directory=True)
        return cls(root / 'runtime' / 'resources', limits=limits,
                   policy_path=root / 'state' / 'operations' / 'resources.json', cancelled=cancelled)

    @property
    def limits(self):
        value = _read(self.policy_path)
        if (not isinstance(value, dict) or set(value) != {'schema', 'limits'}
                or value['schema'] != 'carr-resource-policy/1'):
            raise ResourceStateError('Unsupported saved resource policy.')
        try:
            return ResourceLimits.from_dict(value['limits'])
        except ValueError as exc:
            raise ResourceStateError('Saved resource limits are invalid.') from exc

    @contextmanager
    def _gate(self, deadline, cancelled):
        lock = NativeLock(self.root / '.admission.lock')
        attempted = False
        while True:
            if cancelled():
                raise ResourceCancelled('Resource admission was cancelled before starting heavy work.')
            if attempted and time.monotonic() >= deadline:
                raise ResourceTimeout('Timed out waiting for the shared resource coordinator.')
            attempted = True
            if lock.acquire():
                try:
                    if cancelled():
                        raise ResourceCancelled('Resource admission was cancelled before starting heavy work.')
                    yield
                finally:
                    lock.release()
                return
            if time.monotonic() >= deadline:
                raise ResourceTimeout('Timed out waiting for the shared resource coordinator.')
            time.sleep(min(_POLL_SECONDS, max(0, deadline - time.monotonic())))

    def _active(self):
        """Called under the coordinator lock; native lock ownership is decisive."""
        local_path(self.leases_root, exists=True)
        identities = set()
        with os.scandir(self.leases_root) as entries:
            for index, entry in enumerate(entries):
                if index >= _MAX_RECORDS:
                    raise ResourceStateError('Resource lease inventory exceeds its safety limit.')
                path = self.leases_root / entry.name
                regular(path)
                if entry.name.startswith('.pending-'):
                    # All publications in this private namespace hold _gate.
                    path.unlink()
                    continue
                if path.suffix not in ('.json', '.lock') or not _IDENTITY.fullmatch(path.stem):
                    raise ResourceStateError('Unexpected resource lease namespace entry.')
                identities.add(path.stem)
        result = []
        for identity in sorted(identities):
            path = self.leases_root / (identity + '.json')
            probe = NativeLock(path.with_suffix('.lock'))
            if probe.acquire():
                try:
                    if os.path.lexists(path):
                        regular(path)
                        path.unlink()
                finally:
                    probe.release()
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

    def configure(self, limits, *, timeout=5):
        if not isinstance(limits, ResourceLimits):
            raise TypeError('A typed resource policy is required.')
        deadline = time.monotonic() + _seconds(timeout, 'Policy wait timeout')
        with self._gate(deadline, lambda: False):
            active = self._active()
            if len(active) > limits.max_leases or any(self._usage(active)[name] > getattr(limits, name)
                                                     for name in ('cpu_slots', 'memory_bytes', 'disk_bytes')):
                raise ResourceConflict('New resource limits would invalidate existing reservations.')
            for record in active:
                try:
                    self._fits_policy(ResourceRequest.from_dict(record['request']), limits)
                except ResourceUnavailable as exc:
                    raise ResourceConflict('New resource limits would invalidate an existing reservation.') from exc
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
                        atomic_json(self.leases_root / (identity + '.json'), record)
                        lease = ResourceLease(self, record, lock, cancelled, run_timeout)
                    except BaseException:
                        lock.release()
                        raise
            if lease is None:
                if cancelled():
                    raise ResourceCancelled('Resource admission was cancelled while waiting.')
                if time.monotonic() >= deadline:
                    raise ResourceTimeout('Timed out waiting for ' + reason + '; heavy work was not started.')
                time.sleep(min(_POLL_SECONDS, max(0, deadline - time.monotonic())))
        try:
            lease.checkpoint()
            yield lease
        finally:
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
