"""Allowlisted local diagnostics with an exact preview and create-only export.

No source text, library database, job journal, transcript, credential, environment,
provider configuration, log file or device name is read for this report. The
existing resource broker supplies only declared cooperative reservation counts.
A report is a historical snapshot, not a qualification or hardware guarantee.
"""
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import struct
import sys

from . import __version__
from .durable_state import local_path, regular
from .operations.broker import ResourceBroker
from .operations.models import ResourceCancelled, ResourceError, ResourceTimeout
from .research.export_file import (ExportDestination, capture_export_destination,
                                   write_binary_export)

SCHEMA = 'carr-redacted-diagnostics/1'
MAX_REPORT_BYTES = 16 * 1024
_MESSAGES = {
    'CANCELLED': 'Diagnostic work was cancelled.',
    'LIBRARY_CHANGED': 'The library folder changed. Open a new diagnostic preview.',
    'INVALID_PREVIEW': 'The diagnostic preview is invalid. Prepare a new preview.',
    'PREVIEW_CHANGED': 'The preview changed. Review the current report before saving.',
    'DESTINATION_UNAVAILABLE': 'Choose a new file outside the library in an existing local folder.',
    'DESTINATION_EXISTS': 'That file already exists. Choose a new filename.',
    'EXPORT_FAILED': 'The report could not be saved. The selected destination was not overwritten.',
    'CANCELLATION_UNAVAILABLE': 'Diagnostic cancellation state is unavailable.',
}
_RESOURCE_KEYS = {'cpu_slots', 'memory_bytes', 'disk_bytes',
                  'min_free_disk_bytes', 'max_leases', 'gpu_device_count'}
_USAGE_KEYS = {'cpu_slots', 'memory_bytes', 'disk_bytes', 'active_reservations',
               'reserved_gpu_count', 'exclusive_device_count'}
_RESOURCE_ERRORS = {'RESOURCE_TIMEOUT', 'RESOURCE_UNAVAILABLE', 'RESOURCE_STATE_INVALID'}


class DiagnosticError(ValueError):
    def __init__(self, code):
        self.code = code if code in _MESSAGES else 'INVALID_PREVIEW'
        super().__init__(_MESSAGES[self.code])


class DiagnosticCancelled(InterruptedError):
    def __init__(self):
        self.code = 'CANCELLED'
        super().__init__(_MESSAGES[self.code])


def error_notice(error):
    """Never copy an exception message, path or traceback into a report or UI."""
    if isinstance(error, DiagnosticCancelled):
        return _MESSAGES['CANCELLED']
    if type(error) is DiagnosticError:
        return _MESSAGES[error.code]
    return _MESSAGES['EXPORT_FAILED']


def _check(cancelled):
    if not callable(cancelled):
        raise DiagnosticError('CANCELLATION_UNAVAILABLE')
    try:
        value = cancelled()
    except (DiagnosticError, DiagnosticCancelled):
        raise
    except Exception:
        raise DiagnosticError('CANCELLATION_UNAVAILABLE') from None
    if type(value) is not bool:
        raise DiagnosticError('CANCELLATION_UNAVAILABLE')
    if value:
        raise DiagnosticCancelled()


def _binding(root):
    try:
        root = local_path(root, exists=True)
        paths = (*reversed(root.parents), root)
        identities = []
        for path in paths:
            info = regular(path, directory=True)
            identities.append((info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)))
        return root, tuple(identities)
    except (OSError, ValueError, TypeError):
        raise DiagnosticError('LIBRARY_CHANGED') from None


@dataclass(frozen=True)
class DiagnosticContext:
    _root: Path = field(repr=False)
    _identity: tuple = field(repr=False)


def bind_library(root, *, cancelled=lambda: False):
    """Capture the chosen ordinary folder before queueing diagnostic work."""
    _check(cancelled)
    selected, identity = _binding(root)
    _check(cancelled)
    return DiagnosticContext(selected, identity)


def check_context(context, *, cancelled=lambda: False):
    _check(cancelled)
    if type(context) is not DiagnosticContext:
        raise DiagnosticError('LIBRARY_CHANGED')
    if _binding(context._root) != (context._root, context._identity):
        raise DiagnosticError('LIBRARY_CHANGED')
    _check(cancelled)
    return context


@dataclass(frozen=True)
class DiagnosticReport:
    payload: bytes
    _root: Path = field(repr=False)
    _identity: tuple = field(repr=False)

    @property
    def text(self):
        return self.payload.decode('ascii')

    @property
    def sha256(self):
        return sha256(self.payload).hexdigest()


def _number(value, *, maximum=2 ** 63 - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise DiagnosticError('INVALID_PREVIEW')
    return value


def _runtime():
    return {
        'product_version': __version__,
        'python': list(sys.version_info[:3]),
        'implementation': sys.implementation.name if sys.implementation.name in ('cpython', 'pypy') else 'other',
        'sqlite': list(sqlite3.sqlite_version_info),
        'system': ('windows' if sys.platform == 'win32' else
                   'linux' if sys.platform.startswith('linux') else
                   'macos' if sys.platform == 'darwin' else 'other'),
        'pointer_bits': struct.calcsize('P') * 8,
    }


def _fts_capability():
    try:
        with closing(sqlite3.connect(':memory:')) as db:
            db.execute('CREATE VIRTUAL TABLE diagnostic_probe USING fts5(value)')
        return 'available'
    except sqlite3.Error:
        return 'unavailable'


def _resources(root, cancelled):
    # Do not initialize a resource policy merely to prepare a report. These are
    # the exact existing broker paths; no recursive user-directory scan occurs.
    directory = root / 'runtime' / 'resources'
    required = ((directory, True), (directory / 'leases', True),
                (directory / '.policy-location.json', False),
                (root / 'state' / 'operations' / 'resources.json', False))
    try:
        for path, is_directory in required:
            _check(cancelled)
            local_path(path, exists=True)
            regular(path, directory=is_directory)
    except (DiagnosticError, DiagnosticCancelled):
        raise
    except FileNotFoundError:
        return {'state': 'not_configured'}
    except (OSError, ValueError):
        return {'state': 'unavailable', 'error_code': 'RESOURCE_STATE_INVALID'}
    try:
        _check(cancelled)
        broker = ResourceBroker.for_app(root, cancelled=cancelled)
        snapshot = broker.snapshot(timeout=1, cancelled=cancelled)
        _check(cancelled)
        if type(snapshot) is not dict or snapshot.get('schema') != 'carr-resource-snapshot/1':
            raise ValueError('Invalid snapshot.')
        limits, usage = snapshot['limits'], snapshot['usage']
        if type(limits) is not dict or type(usage) is not dict:
            raise ValueError('Invalid resource counters.')
        selected_limits = {key: _number(limits[key]) for key in _RESOURCE_KEYS - {'gpu_device_count'}}
        selected_limits['gpu_device_count'] = _number(len(limits['gpu_devices']), maximum=64)
        selected_usage = {key: _number(usage[key]) for key in ('cpu_slots', 'memory_bytes', 'disk_bytes')}
        selected_usage.update(
            active_reservations=_number(len(snapshot['leases']), maximum=4096),
            reserved_gpu_count=_number(len(snapshot['gpu_devices_reserved']), maximum=4096),
            exclusive_device_count=_number(len(snapshot['exclusive_devices']), maximum=65536))
        return {'state': 'available', 'limits': selected_limits, 'usage': selected_usage,
                'measurement': 'declared_cooperative_reservations'}
    except (DiagnosticError, DiagnosticCancelled):
        raise
    except ResourceCancelled:
        raise DiagnosticCancelled() from None
    except ResourceTimeout:
        return {'state': 'unavailable', 'error_code': 'RESOURCE_TIMEOUT'}
    except (ResourceError, OSError):
        return {'state': 'unavailable', 'error_code': 'RESOURCE_UNAVAILABLE'}
    except (ValueError, TypeError, KeyError):
        return {'state': 'unavailable', 'error_code': 'RESOURCE_STATE_INVALID'}


def _encode(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode('ascii')


def _validate_payload(payload):
    if type(payload) is not bytes or not 1 <= len(payload) <= MAX_REPORT_BYTES:
        raise DiagnosticError('INVALID_PREVIEW')
    try:
        value = json.loads(payload)
        expected = {'schema', 'captured_utc', 'runtime', 'capabilities', 'resources',
                    'filesystem', 'privacy'}
        if type(value) is not dict or set(value) != expected or value['schema'] != SCHEMA:
            raise ValueError()
        if value['runtime'] != _runtime():
            raise ValueError()
        captured = value['captured_utc']
        if type(captured) is not str or re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', captured) is None:
            raise ValueError()
        if value['capabilities'] not in (
                {'sqlite_fts5': 'available', 'models_and_devices': 'not_probed'},
                {'sqlite_fts5': 'unavailable', 'models_and_devices': 'not_probed'}):
            raise ValueError()
        if value['privacy'] != {'scope': 'technical_allowlist_only', 'user_content_read': False,
                                'identifiers_and_paths_included': False, 'network_calls': False}:
            raise ValueError()
        filesystem = value['filesystem']
        if type(filesystem) is not dict or set(filesystem) != {'state', 'total_bytes', 'free_bytes'}:
            raise ValueError()
        if filesystem['state'] != 'observed_selected_filesystem':
            raise ValueError()
        if _number(filesystem['free_bytes']) > _number(filesystem['total_bytes']):
            raise ValueError()
        resources = value['resources']
        if type(resources) is not dict:
            raise ValueError()
        if resources.get('state') == 'not_configured':
            if set(resources) != {'state'}:
                raise ValueError()
        elif resources.get('state') == 'unavailable':
            if set(resources) != {'state', 'error_code'} or resources['error_code'] not in _RESOURCE_ERRORS:
                raise ValueError()
        elif resources.get('state') == 'available':
            if set(resources) != {'state', 'limits', 'usage', 'measurement'}:
                raise ValueError()
            if resources['measurement'] != 'declared_cooperative_reservations':
                raise ValueError()
            if type(resources['limits']) is not dict or set(resources['limits']) != _RESOURCE_KEYS:
                raise ValueError()
            if type(resources['usage']) is not dict or set(resources['usage']) != _USAGE_KEYS:
                raise ValueError()
            for number in (*resources['limits'].values(), *resources['usage'].values()):
                _number(number)
        else:
            raise ValueError()
        # Reject duplicate keys, alternate encodings and arbitrary extra text.
        if _encode(value) != payload:
            raise ValueError()
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise DiagnosticError('INVALID_PREVIEW') from None
    return value


def check_report(report, *, cancelled=lambda: False):
    _check(cancelled)
    if type(report) is not DiagnosticReport:
        raise DiagnosticError('INVALID_PREVIEW')
    _validate_payload(report.payload)
    if _binding(report._root) != (report._root, report._identity):
        raise DiagnosticError('LIBRARY_CHANGED')
    _check(cancelled)
    return report


def prepare_report(context, *, cancelled=lambda: False):
    check_context(context, cancelled=cancelled)
    selected, identity = context._root, context._identity
    def stopped():
        check_context(context, cancelled=cancelled)
        return False
    resources = _resources(selected, stopped)
    _check(cancelled)
    try:
        disk = shutil.disk_usage(selected)
        filesystem = {'state': 'observed_selected_filesystem',
                      'total_bytes': _number(disk.total), 'free_bytes': _number(disk.free)}
    except (OSError, ValueError):
        raise DiagnosticError('LIBRARY_CHANGED') from None
    value = {
        'schema': SCHEMA,
        'captured_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'runtime': _runtime(),
        'capabilities': {'sqlite_fts5': _fts_capability(), 'models_and_devices': 'not_probed'},
        'resources': resources,
        'filesystem': filesystem,
        'privacy': {'scope': 'technical_allowlist_only', 'user_content_read': False,
                    'identifiers_and_paths_included': False, 'network_calls': False},
    }
    report = DiagnosticReport(_encode(value), selected, identity)
    return check_report(report, cancelled=cancelled)


def prepare_destination(report, path, *, cancelled=lambda: False):
    check_report(report, cancelled=cancelled)
    def stopped():
        check_report(report, cancelled=cancelled)
        return False
    try:
        approval = capture_export_destination(
            path, private_root=report._root, cancelled=stopped,
            _reject_existing=FileExistsError('Existing diagnostic destination.'))
    except FileExistsError:
        raise DiagnosticError('DESTINATION_EXISTS') from None
    except (DiagnosticError, DiagnosticCancelled):
        raise
    except (OSError, ValueError, TypeError):
        raise DiagnosticError('DESTINATION_UNAVAILABLE') from None
    check_report(report, cancelled=cancelled)
    if not approval.require_absent:
        raise DiagnosticError('DESTINATION_EXISTS')
    return approval


def export_report(report, approval, *, expected_sha256, cancelled=lambda: False):
    check_report(report, cancelled=cancelled)
    if type(expected_sha256) is not str or expected_sha256 != report.sha256:
        raise DiagnosticError('PREVIEW_CHANGED')
    if type(approval) is not ExportDestination or not approval.require_absent:
        raise DiagnosticError('DESTINATION_UNAVAILABLE')
    def stopped():
        check_report(report, cancelled=cancelled)
        return False
    try:
        return write_binary_export(
            approval.requested, report.payload, private_root=report._root,
            require_absent=True, approval=approval, cancelled=stopped)
    except (DiagnosticError, DiagnosticCancelled):
        raise
    except (OSError, ValueError, TypeError):
        raise DiagnosticError('EXPORT_FAILED') from None
