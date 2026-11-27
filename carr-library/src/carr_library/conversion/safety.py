"""Reuse local path and process boundaries for standalone reading derivatives.

No document parser or independent import queue lives here. The existing native
process supervisor owns child lifetime, cancellation and bounded diagnostics.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat
import sys

from ..audio.engines import _run, EngineUnavailable
from ..audio.jobs import _sync_directory as sync_directory
from ..research.export_file import _filename
from ..sync.archive import local_path as _local_path, _open_regular, _identity
from .models import ConversionError


def local_path(value, *, root=None, exists=False):
    path = _local_path(value)
    for part in path.parts[1:]:
        _filename(part)
    if exists and not path.exists():
        raise FileNotFoundError('The selected conversion path is unavailable.')
    if root is not None and not path.is_relative_to(root):
        raise ValueError('The path is outside the selected conversion directory.')
    return path


def regular(path):
    path = local_path(path, exists=True)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Conversion requires ordinary files without hard links.')
    return info


@dataclass(frozen=True)
class ProcessBudget:
    timeout: float
    max_output_bytes: int
    reserve_disk_bytes: int = 16 * 1024 * 1024
    max_staging_files: int = 128


@dataclass(frozen=True)
class ProcessResult:
    stderr: bytes


def run_process(args, *, budget, cancelled, watch=(), env=None, cwd=None):
    if (not isinstance(args, (list, tuple)) or not args
            or any(not isinstance(arg, str) or '\x00' in arg for arg in args)
            or not Path(args[0]).is_absolute()):
        raise ValueError('A resolved document worker argument vector is required.')
    watched = tuple(Path(path) for path in watch)
    if len(watched) > 8:
        raise ValueError('Too many conversion worker outputs.')

    def stopped():
        if cancelled():
            return True
        total, count = 0, 0
        pending = list(watched)
        while pending:
            path = pending.pop()
            try:
                local_path(path, exists=True)
                count += 1
                if count > budget.max_staging_files:
                    raise ConversionError('Document worker exceeded its staging file budget.')
                info = path.lstat()
                if getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise ConversionError('Document worker output cannot use redirected paths.')
                if stat.S_ISDIR(info.st_mode):
                    with os.scandir(path) as entries:
                        for entry in entries:
                            pending.append(Path(entry.path))
                            if len(pending) + count > budget.max_staging_files:
                                raise ConversionError('Document worker exceeded its staging file budget.')
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    total += info.st_size
                else:
                    raise ConversionError('Document worker outputs must be ordinary files.')
            except FileNotFoundError:
                continue  # A trusted worker may finish and remove its staging.
        if total > budget.max_output_bytes:
            raise ConversionError('Document worker exceeded its output byte budget.')
        if watched and shutil.disk_usage(watched[0].parent).free < budget.reserve_disk_bytes:
            raise ConversionError('Document conversion stopped to preserve free disk space.')
        return False

    try:
        errors = _run(args, cancelled=stopped, timeout=budget.timeout,
                      env=env, cwd=cwd, return_stderr=True)
    except EngineUnavailable as exc:
        # Native/parser diagnostics may include document text or local paths.
        raise ConversionError('The isolated document reader failed. Check the source format, '
                              'selected encoding and resource budget.') from exc
    stopped()
    return ProcessResult(errors)


def worker_command(request):
    # As in ingest.worker: ignore CWD/PYTHONPATH, load only this trusted package.
    root = str(Path(__file__).resolve().parents[2])
    bootstrap = ("import sys,runpy; root,request=sys.argv[1:]; "
                 "sys.path.insert(0,root); sys.argv=['conversion-worker',request]; "
                 "runpy.run_module('carr_library.conversion.worker',run_name='__main__')")
    return [str(Path(sys.executable).absolute()), '-I', '-B', '-X', 'utf8',
            '-c', bootstrap, root, str(request)]


def read_bytes(path, maximum, check=lambda: None):
    stream, before = _open_regular(path)
    if before.st_size > maximum:
        stream.close()
        raise ConversionError('Conversion file exceeds its read budget.')
    pieces, count = [], 0
    with stream:
        while block := stream.read(min(1024 * 1024, maximum - count + 1)):
            check()
            count += len(block)
            if count > maximum:
                raise ConversionError('Conversion file grew beyond its read budget.')
            pieces.append(block)
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ConversionError('Conversion file changed while reading.')
    if _identity(regular(path)) != _identity(before):
        raise ConversionError('Conversion file changed while reading.')
    check()
    return b''.join(pieces)
