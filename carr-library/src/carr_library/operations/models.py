"""Typed cooperative resource budgets, not measured hardware capacities."""
from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import re


GIB = 1024 ** 3
MIB = 1024 ** 2


class ResourceError(RuntimeError):
    """Admission failed without starting the requested heavy operation."""


class ResourceUnavailable(ResourceError):
    pass


class ResourceTimeout(ResourceError):
    pass


class ResourceCancelled(ResourceError):
    pass


class ResourceConflict(ResourceError):
    pass


class ResourceStateError(ResourceError):
    pass


def _count(value, name, *, minimum=0, maximum=2 ** 63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer between {minimum} and {maximum}.')
    return value


def _token(value, name):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,159}', value):
        raise ValueError(f'Invalid {name}; use a stable non-secret operation/device identifier.')
    return value


def _seconds(value, name, *, allow_none=False):
    if allow_none and value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 86400:
        raise ValueError(f'{name} must be a finite number of seconds between zero and 86400.')
    return float(value)


@dataclass(frozen=True)
class ResourceLimits:
    """Saved operator policy, not measured hardware capacity.

    Two default logical slots preserve existing two-thread audio/ASR jobs even
    on smaller hosts; this is a cooperative allowance, not a core-count claim.
    Explicitly selected lower policies are respected without retuning jobs.
    """
    cpu_slots: int = field(default_factory=lambda: max(2, min(4, (os.cpu_count() or 2) - 1)))
    memory_bytes: int = 4 * GIB
    disk_bytes: int = 16 * GIB
    min_free_disk_bytes: int = 256 * MIB
    gpu_devices: tuple = ()
    max_leases: int = 64

    def __post_init__(self):
        _count(self.cpu_slots, 'CPU slots', minimum=1, maximum=65536)
        _count(self.memory_bytes, 'Declared memory capacity')
        _count(self.disk_bytes, 'Declared disk capacity')
        _count(self.min_free_disk_bytes, 'Free disk reserve')
        _count(self.max_leases, 'Active reservation limit', minimum=1, maximum=4096)
        if not isinstance(self.gpu_devices, (tuple, list)) or len(self.gpu_devices) > 64:
            raise ValueError('GPU capacities must be a bounded sequence of device/byte pairs.')
        devices, names = [], set()
        for item in self.gpu_devices:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ValueError('A GPU capacity needs its exact device ID and declared memory bytes.')
            name, capacity = item
            _token(name, 'GPU device')
            if name != name.casefold() or name in names:
                raise ValueError('GPU device identifiers must be distinct and lowercase.')
            _count(capacity, 'Declared GPU memory capacity', minimum=1)
            names.add(name)
            devices.append((name, capacity))
        object.__setattr__(self, 'gpu_devices', tuple(sorted(devices)))

    def to_dict(self):
        return {'cpu_slots': self.cpu_slots, 'memory_bytes': self.memory_bytes,
                'disk_bytes': self.disk_bytes, 'min_free_disk_bytes': self.min_free_disk_bytes,
                'gpu_devices': [list(item) for item in self.gpu_devices], 'max_leases': self.max_leases}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls().to_dict()):
            raise ValueError('Unsupported resource limit fields.')
        return cls(**value)


@dataclass(frozen=True)
class ResourceRequest:
    cpu_slots: int = 1
    memory_bytes: int = 0
    gpu_device: str | None = None
    gpu_memory_bytes: int = 0
    disk_bytes: int = 0
    disk_path: Path | None = None
    devices: tuple = ()
    additional_disks: tuple = ()

    def __post_init__(self):
        _count(self.cpu_slots, 'Requested CPU slots', maximum=65536)
        for name in ('memory_bytes', 'gpu_memory_bytes', 'disk_bytes'):
            _count(getattr(self, name), name)
        if self.gpu_device is not None:
            _token(self.gpu_device, 'GPU device')
            if self.gpu_device != self.gpu_device.casefold() or self.gpu_memory_bytes == 0:
                raise ValueError('A GPU request needs a lowercase device ID and a positive declared memory budget.')
        elif self.gpu_memory_bytes:
            raise ValueError('GPU memory requires an explicitly selected GPU device.')
        if self.disk_path is not None:
            if not isinstance(self.disk_path, (str, Path)) or '\x00' in str(self.disk_path):
                raise ValueError('Invalid selected disk directory.')
            object.__setattr__(self, 'disk_path', Path(self.disk_path))
        if bool(self.disk_bytes) != (self.disk_path is not None):
            raise ValueError('A disk reservation needs both a positive byte budget and its selected directory.')
        if not isinstance(self.additional_disks, (tuple, list)) or len(self.additional_disks) > 8:
            raise ValueError('Additional disk reservations must be a bounded sequence of directory/byte pairs.')
        disks = []
        for item in self.additional_disks:
            if (not isinstance(item, (tuple, list)) or len(item) != 2
                    or not isinstance(item[0], (str, Path)) or '\x00' in str(item[0])):
                raise ValueError('Each additional disk reservation needs a directory and a byte budget.')
            _count(item[1], 'Additional disk bytes', minimum=1)
            disks.append((Path(item[0]), item[1]))
        object.__setattr__(self, 'additional_disks', tuple(disks))
        _count(self.total_disk_bytes, 'Total requested disk bytes')
        if not isinstance(self.devices, (tuple, list)) or len(self.devices) > 32:
            raise ValueError('Exclusive devices must be a bounded sequence of IDs.')
        devices = tuple(self.devices)
        for device in devices:
            _token(device, 'exclusive device')
            if device != device.casefold() or device.startswith('gpu:'):
                raise ValueError('Device IDs must be lowercase; reserve GPUs through gpu_device.')
        if len(set(devices)) != len(devices):
            raise ValueError('An exclusive device may only be requested once.')
        object.__setattr__(self, 'devices', tuple(sorted(devices)))
        if not (self.cpu_slots or self.memory_bytes or self.gpu_device or self.total_disk_bytes or self.devices):
            raise ValueError('A resource request must reserve at least one resource.')

    @property
    def disks(self):
        return (((self.disk_path, self.disk_bytes),) if self.disk_bytes else ()) + self.additional_disks

    @property
    def total_disk_bytes(self):
        return self.disk_bytes + sum(size for _, size in self.additional_disks)

    def to_dict(self):
        return {'cpu_slots': self.cpu_slots, 'memory_bytes': self.memory_bytes,
                'gpu_device': self.gpu_device, 'gpu_memory_bytes': self.gpu_memory_bytes,
                'disk_bytes': self.disk_bytes, 'disk_path': str(self.disk_path) if self.disk_path else None,
                'devices': list(self.devices),
                'additional_disks': [[str(path), size] for path, size in self.additional_disks]}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != set(cls().to_dict()):
            raise ValueError('Unsupported resource request fields.')
        return cls(**value)
