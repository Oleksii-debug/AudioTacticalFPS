"""Selected native converters inside the existing conversion worker.

This module is not a new process broker or document reader framework. The
caller uses the existing run_process Windows Job and keeps its one broker lease,
selected core readers and source staging until native cleanup is complete.
"""
from hashlib import sha256
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryFile
import time

from ._selected_files import (checked_directory, file_identity, regular_file,
                              verified_reader, digest_stream)
from .models import ConversionError, ConversionUnavailable


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError('Selected document adapter exceeded its time allowance.')
    return value


def _executable(selection, deadline):
    if (not isinstance(selection, dict) or set(selection) != {'path', 'bytes', 'sha256'}
            or not isinstance(selection['path'], str) or '\x00' in selection['path']
            or type(selection['bytes']) is not int or not 0 < selection['bytes'] <= 256 * 1024 * 1024
            or not isinstance(selection['sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', selection['sha256'])):
        raise ConversionError('Invalid selected converter identity.')
    path = Path(selection['path'])
    if not path.is_absolute():
        raise ConversionError('Selected converter path must be absolute.')
    checked_directory(path.parent)
    info = regular_file(path)
    if info.st_nlink != 1 or info.st_size != selection['bytes']:
        raise ConversionError('The selected converter identity changed.')
    def check():
        _remaining(deadline)
        return False
    with verified_reader(path, expected_identity=file_identity(info)) as stream:
        checksum = digest_stream(stream, info.st_size, check)
    if checksum != selection['sha256']:
        raise ConversionError('The selected converter bytes changed.')
    return path


def _pandoc(source, data, budget, deadline, selection):
    if not re.match(rb'^\s*\{\\rtf[0-9]+(?:\s|\\)', data[:256]):
        raise ConversionError('The selected RTF source has no valid RTF header.')
    executable = _executable(selection, deadline)
    output = source.parent / 'pandoc.txt'
    if os.path.lexists(output):
        raise ConversionError('Private converter output already exists.')
    args = [str(executable), '--sandbox', '--from=rtf', '--to=plain', '--wrap=none',
            '--output=' + output.name, source.name, '+RTS',
            '-M' + str(min(1024 * 1024**2, budget.memory_bytes // 2) // (1024 * 1024)) + 'M', '-RTS']
    # Deliberately inherit the current worker's POSIX group / Windows Job.
    # A nested new session would escape the outer Cancel/timeout owner.
    environment = {key: value for key, value in os.environ.items()
                   if key in ('PATH', 'SYSTEMROOT', 'WINDIR', 'LANG', 'LD_LIBRARY_PATH')}
    environment.update(TMPDIR=str(source.parent), TEMP=str(source.parent), TMP=str(source.parent))
    with TemporaryFile() as stdout, TemporaryFile() as stderr:
        process = subprocess.Popen(args, cwd=source.parent, env=environment,
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, shell=False,
            start_new_session=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            while True:
                _remaining(deadline)
                if any(os.fstat(stream.fileno()).st_size > 256 * 1024 for stream in (stdout, stderr)):
                    raise ConversionError('Selected converter diagnostics exceeded their byte budget.')
                if os.path.lexists(output) and regular_file(output).st_size > budget.max_text_bytes:
                    raise ConversionError('Selected converter output exceeded its text budget.')
                if process.poll() is not None:
                    break
                time.sleep(.02)
            if process.returncode:
                raise ConversionUnavailable('Selected Pandoc failed its fixed sandboxed RTF conversion.')
            had_warnings = os.fstat(stderr.fileno()).st_size > 0
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
    _executable(selection, deadline)
    info = regular_file(output)
    if info.st_nlink != 1 or not 0 < info.st_size <= budget.max_text_bytes:
        raise ConversionError('Selected converter returned an invalid text size.')
    with verified_reader(output, expected_identity=file_identity(info)) as stream:
        raw = stream.read(budget.max_text_bytes + 1)
    if len(raw) != info.st_size:
        raise ConversionError('Selected converter output changed.')
    text = raw.decode('utf-8', errors='strict')
    if not text.strip() or '\x00' in text:
        raise ConversionError('Selected converter returned invalid reading text.')
    notices = ['sandboxed_plain_text_layout_and_printed_pages_not_retained']
    if had_warnings:
        notices.append('converter_reported_warnings_review_source_fidelity')
    record = dict(raw_text=text, physical_index=None, status='review_required',
        logical_locator={'section': 1, 'converter_sha256': selection['sha256'], 'sandbox': True})
    return 'selected-pandoc-rtf-sandbox', '\n\n', [record], notices



def selected_records(source, data, suffix, budget, deadline, selection):
    from .office import OFFICE_SUFFIXES, office_records
    from .calibre import CALIBRE_SUFFIXES, calibre_records
    if suffix in CALIBRE_SUFFIXES:
        if (type(selection) is not dict or set(selection) != {'kind', 'runtime'}
                or selection['kind'] != 'calibre-book'):
            raise ConversionUnavailable('This book requires the explicitly selected Calibre core identity.')
        return calibre_records(source, data, suffix, budget, deadline, selection['runtime'])
    if suffix in OFFICE_SUFFIXES:
        if (type(selection) is not dict or set(selection) != {'kind', 'runtime'}
                or selection['kind'] != 'libreoffice'):
            raise ConversionUnavailable('Legacy conversion requires the explicitly selected LibreOffice identity.')
        return office_records(source, data, suffix, budget, deadline, selection['runtime'])
    if suffix == '.rtf':
        if (type(selection) is not dict or set(selection) != {'kind', 'executable'}
                or selection['kind'] != 'pandoc-rtf'):
            raise ConversionUnavailable('RTF requires the explicitly selected converter identity.')
        return _pandoc(source, data, budget, deadline, selection['executable'])
    raise ConversionUnavailable('Unsupported selected conversion adapter.')
