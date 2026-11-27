"""Allowlisted diagnostics built from the existing isolated installation self-test."""
from datetime import datetime, timezone
import importlib.metadata
import json
import re
import sqlite3
import struct
import sys

from . import __version__


CHECKS = (
    'original import', 'scoped search', 'exact quotation', 'workspace isolation',
    'saved research', 'reopen persistence', 'academic reading list',
    'bibliography export', 'backup and restore',
)
PACKAGES = (
    'universal-research-library', 'wxPython', 'pypdfium2', 'lxml', 'Pillow',
    'onnxruntime', 'tokenizers', 'numpy', 'quickjs-ng',
)
OMITTED = (
    'document text and research notes', 'book, workspace and topic names',
    'user paths and filenames', 'provider endpoints and account identifiers',
    'credentials and environment variables', 'raw error messages and logs',
)
_FAILURES = frozenset({'none', 'filesystem', 'database', 'missing-dependency', 'check-failed'})
_VERSION = re.compile(r'[0-9][0-9A-Za-z.+!_-]{0,63}\Z')


def _version(value):
    return (value if isinstance(value, str) and
            (value in ('NOT_INSTALLED', 'UNAVAILABLE') or _VERSION.fullmatch(value)) else 'UNAVAILABLE')


def _check_cancelled(cancelled):
    if cancelled():
        raise InterruptedError('Diagnostics cancelled; no report was published.')


def redacted_report(result, versions, *, failure='none'):
    """Project only fixed fields; never scrub or forward arbitrary log messages."""
    if failure not in _FAILURES:
        failure = 'check-failed'
    raw = result if type(result) is dict else {}
    observed = raw.get('checks')
    observed = observed if type(observed) in (list, tuple) else ()
    observed = {entry for entry in observed if type(entry) is str and entry in CHECKS}
    complete = raw.get('status') == 'PASS' and observed == set(CHECKS) and failure == 'none'
    status = 'PASS' if complete else 'FAILED'
    versions = versions if type(versions) is dict else {}
    report = {
        'schema': 'resonance-redacted-diagnostics/1',
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'application_version': _version(__version__),
        'system': {
            'platform': sys.platform if sys.platform in ('win32', 'linux', 'darwin') else 'other',
            'python': '.'.join(str(value) for value in sys.version_info[:3]),
            'pointer_bits': struct.calcsize('P') * 8,
            'sqlite': _version(sqlite3.sqlite_version),
        },
        'packages': {name: _version(versions.get(name, 'NOT_INSTALLED')) for name in PACKAGES},
        'installation_check': {
            'status': status,
            'failure_category': failure if failure != 'none' or complete else 'check-failed',
            'checks': [{'name': name, 'status': 'PASS' if complete else 'NOT_RUN'} for name in CHECKS],
            'input': 'isolated synthetic library; no user library scan',
        },
        'native_gui': 'NOT_RUN',
        'windows_nvda': 'NOT_RUN',
        'model_inference': 'NOT_RUN',
        'omitted': list(OMITTED),
    }
    return (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            + '\n').encode('utf-8')


def collect_diagnostics(*, cancelled=lambda: False):
    """Run in the shared background operation; cancellation discards its result.

    The existing small synthetic self-test is not interruptible inside an
    individual SQLite operation. Check cancellation before and after it and
    while collecting package metadata; never report it as stopped prematurely.
    """
    _check_cancelled(cancelled)
    versions = {}
    for name in PACKAGES:
        _check_cancelled(cancelled)
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = 'NOT_INSTALLED'
        except Exception:
            versions[name] = 'UNAVAILABLE'
    from .__main__ import self_test
    result, failure = None, 'none'
    try:
        _check_cancelled(cancelled)
        result = self_test()
    except InterruptedError:
        raise
    except ImportError:
        failure = 'missing-dependency'
    except sqlite3.Error:
        failure = 'database'
    except OSError:
        failure = 'filesystem'
    except Exception:
        failure = 'check-failed'
    _check_cancelled(cancelled)
    return redacted_report(result, versions, failure=failure)
