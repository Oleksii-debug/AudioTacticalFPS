"""Host bindings for the supplied-worker bridge; reuse the selected process runner.

No recognizer, audio model registry, provider database or second queue lives here.
The isolated-site bootstrap is the reviewed canonical helper used by PR140.
"""
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import shutil
import stat

from .audio.engines import _run
from .audio.jobs import RenderCancelled as AudioCancelled
from .durable_state import MAX_JSON_BYTES, _unique, local_path, regular

AudioError = RuntimeError


def finite(value, label, minimum=None, maximum=None):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or minimum is not None and value < minimum
            or maximum is not None and value > maximum):
        raise ValueError('Invalid ' + label + '.')
    return value


@dataclass(frozen=True)
class AudioBudget:
    timeout: float = 86400
    max_output_bytes: int = 512 * 1024**2
    max_staging_files: int = 10000
    reserve_disk_bytes: int = 64 * 1024**2


def run_process(args, *, budget, cancelled=lambda: False, watch=(), env=None, cwd=None):
    """Add the canonical staging budget to the existing bounded PR99 runner."""
    finite(budget.timeout, 'transcription time allowance', .05, 365 * 86400)
    if not args or not Path(args[0]).is_absolute():
        raise ValueError('A transcription worker requires an absolute executable.')
    watched = tuple(Path(path) for path in watch)
    def check():
        stopped = cancelled()
        total, count, pending = 0, 0, list(watched)
        while pending:
            path = pending.pop()
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise AudioError('Transcription staging must not contain links.')
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:
                    for entry in entries:
                        count += 1
                        if count > budget.max_staging_files:
                            raise AudioError('Transcription exceeded its staging file budget.')
                        pending.append(Path(entry.path))
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > budget.max_output_bytes:
                    raise AudioError('Transcription exceeded its output disk budget.')
            else:
                raise AudioError('Transcription staging must contain regular files.')
        if watched and shutil.disk_usage(watched[0].parent).free < budget.reserve_disk_bytes:
            raise AudioError('Transcription stopped to preserve the free disk reserve.')
        return stopped
    try:
        return _run(args, cancelled=check, timeout=budget.timeout, env=env, cwd=cwd)
    except InterruptedError as exc:
        raise AudioCancelled('Transcription was cancelled; saved worker results remain available.') from exc
    except TimeoutError as exc:
        raise AudioError('Transcription exceeded its time allowance; saved worker results remain available.') from exc


def _open_worker_status(path):
    """Capture bounded status bytes and close the OS handle before returning.

    Legacy Windows replacement can reject even a delete-shared open target.
    Keep parsing and observers on an in-memory snapshot; the isolated producer
    also retries brief Windows sharing conflicts during the bounded read.
    """
    from io import BytesIO
    path = local_path(path, exists=True)
    if regular(path).st_size > MAX_JSON_BYTES:
        raise ValueError('State exceeds its configured JSON budget.')
    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink not in (0, 1)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('State must use regular local files, without links.')
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('State grew beyond its JSON budget.')
    return BytesIO(raw)


def read_worker_status(path):
    """Use the existing bounded JSON contract for a replaceable status file."""
    with _open_worker_status(path) as snapshot:
        raw = snapshot.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError('State grew beyond its JSON budget.')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite state number.')))


def data_directory(selected):
    if selected:
        return Path(selected)
    from platformdirs import user_data_path
    from .preferences import selected_directory
    return selected_directory() or user_data_path('Universal Research Library', appauthor=False, roaming=False)


def report_progress(task, done, total, phase):
    """Keep the host's monotonic work count while final receipt checks are deferred."""
    snapshot = task.snapshot()
    task.update(completed=max(snapshot.completed, done),
                total=max(snapshot.completed, total), phase=phase)


def require_completed(result):
    """Translate the saved service outcome to the existing background-job contract."""
    if result['status'] == 'stopped':
        raise InterruptedError('Transcription stopped. Saved worker results remain available for explicit resume.')
    if result['status'] != 'completed':
        raise RuntimeError(result.get('last_error') or 'Transcription did not complete every requested export; review remaining inputs.')
    return result


_SITE_BOOTSTRAP = r'''
import pathlib, sys, sysconfig
executable = pathlib.Path(sys.executable).absolute()
prefix = executable.parent.parent if executable.parent.name in ('bin','Scripts') else executable.parent
if (prefix / 'pyvenv.cfg').is_file():
    package_path = prefix / ('Lib/site-packages' if sys.platform == 'win32' else 'lib/python%d.%d/site-packages' % sys.version_info[:2])
    sys.path.insert(0,str(package_path))
else:
    for key in ('purelib','platlib'):
        directory = sysconfig.get_path(key)
        if directory not in sys.path:
            sys.path.append(directory)
'''
