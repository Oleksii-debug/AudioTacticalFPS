"""Selected legacy formats on the existing conversion, process and export owners.

The complete external reading folder is the publication unit. Its exact
original, UTF-8 reading copy and inert fidelity report commit together through
the existing atomic no-replace directory publisher. It is not a native catalog
source or an execution-authorizing saved runtime profile.
"""
from contextlib import ExitStack
from dataclasses import asdict, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from ..audio.cleanup import pending_cleanup
from ..audio.models import AudioBudget, AudioCancelled, AudioCleanupPending
from ..audio.process import run_process
from ..data_paths import guard_data_directory
from ..durable_state import local_path, regular, read_json_snapshot, sync_directory
from ..file_publication import publish_directory_no_replace
from ..operations import ResourceBroker, ResourceRequest, ResourceCancelled
from ..speech._io import local_file
from ._selected_files import (checked_directory, file_identity, regular_file,
                              verified_reader, digest_file, digest_stream)
from .calibre import CALIBRE_SUFFIXES
from .office import OFFICE_SUFFIXES
from .models import ConversionError, ConversionUnavailable, ConversionResult
from .selected_worker import MAX_SOURCE_BYTES, MAX_TEXT_BYTES, MAX_REPORT_BYTES, MAX_RECORDS

SELECTED_SUFFIXES = frozenset((*CALIBRE_SUFFIXES, *OFFICE_SUFFIXES, '.rtf'))


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Document conversion cancelled before publication.')


def _scope(service):
    service.guard()
    if service.state_root is None:
        raise ConversionUnavailable('Select the current library before using an optional document converter.')
    for path, identity in service._selected_directories:
        info = regular(local_path(path, exists=True), directory=True)
        if (info.st_dev, info.st_ino) != identity:
            raise ConversionError('The selected library, input or output directory changed.')
    service.guard()


def _selection(service, suffix, cancelled):
    if suffix in CALIBRE_SUFFIXES:
        if service.calibre is None:
            raise ConversionUnavailable('Select the installed Calibre ebook-convert executable explicitly.')
        return {'kind': 'calibre-book', 'runtime': service.calibre.snapshot(cancelled=cancelled)}
    if suffix in OFFICE_SUFFIXES:
        if service.office is None:
            raise ConversionUnavailable('Select the installed LibreOffice program/soffice executable explicitly.')
        return {'kind': 'libreoffice', 'runtime': service.office.snapshot(cancelled=cancelled)}
    if suffix == '.rtf':
        if service.pandoc is None:
            raise ConversionUnavailable('Select an installed Pandoc executable explicitly for RTF.')
        path = local_file(service.pandoc)
        info = regular_file(path)
        parent = regular(path.parent, directory=True)
        if (file_identity(info) != service._pandoc_identity
                or (parent.st_dev, parent.st_ino) != service._pandoc_parent_identity
                or not 0 < info.st_size <= 256 * 1024**2):
            raise ConversionError('The selected Pandoc executable changed.')
        return {'kind': 'pandoc-rtf', 'executable': {'path': str(path), 'bytes': info.st_size,
                'sha256': digest_file(path, cancelled=cancelled,
                                      expected_identity=service._pandoc_identity)}}
    raise ConversionUnavailable('No optional converter is selected for this format.')


def _held_core(service, suffix, selection, cancelled):
    held = ExitStack()
    try:
        if suffix in CALIBRE_SUFFIXES:
            held.enter_context(service.calibre.lease(selection['runtime'], cancelled=cancelled))
        elif suffix in OFFICE_SUFFIXES:
            held.enter_context(service.office.lease(selection['runtime'], cancelled=cancelled))
        else:
            held.enter_context(verified_reader(service.pandoc,
                               expected_identity=service._pandoc_identity))
            if digest_file(service.pandoc, cancelled=cancelled,
                           expected_identity=service._pandoc_identity) != selection['executable']['sha256']:
                raise ConversionError('The selected Pandoc bytes changed.')
        return held
    except BaseException:
        held.close()
        raise


def _worker_command(request, digest):
    package_root = Path(__file__).resolve().parents[2]
    roots = [str(package_root)]
    if (package_root.parent / 'bootstrap/vendor/cairn_extractors/hasher.py').is_file():
        roots.append(str(package_root.parent))
    bootstrap = ("import sys,runpy; request,digest=sys.argv[1:3]; "
                 "sys.path[:0]=sys.argv[3:]; sys.argv=['carr_library.conversion.worker',request,digest]; "
                 "runpy.run_module('carr_library.conversion.worker',run_name='__main__')")
    executable = str(Path(sys.executable).absolute())
    environment = {name: os.environ[name] for name in ('SYSTEMROOT', 'WINDIR', 'PATH', 'LANG')
                   if name in os.environ}
    if (os.name == 'nt' and sys.prefix != sys.base_prefix
            and getattr(sys, '_base_executable', None)
            and os.path.normcase(sys._base_executable) != os.path.normcase(executable)):
        environment['__PYVENV_LAUNCHER__'] = executable
        executable = sys._base_executable
    environment.update(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
                       TMPDIR=str(request.parent), TEMP=str(request.parent), TMP=str(request.parent))
    return [executable, '-I', '-B', '-X', 'utf8', '-c', bootstrap, str(request), digest, *roots], environment


def _validate_details(value, text, budget):
    if (type(value) is not dict or set(value) != {'engine', 'sections', 'metadata', 'warnings'}
            or type(value['engine']) is not str or not 0 < len(value['engine']) <= 200
            or type(value['sections']) is not list
            or not 1 <= len(value['sections']) <= min(MAX_RECORDS, budget.max_sections)
            or type(value['metadata']) is not dict
            or set(value['metadata']) != {'separator', 'selected_conversion'}
            or value['metadata']['selected_conversion'] is not True
            or value['metadata']['separator'] not in ('\n\n', '\n\f\n')
            or type(value['warnings']) is not list or len(value['warnings']) > 100
            or any(type(item) is not str or len(item) > 256 for item in value['warnings'])):
        raise ConversionError('The selected conversion worker returned invalid provenance.')
    separator, previous = value['metadata']['separator'], 0
    expected = {'label', 'start', 'end', 'physical_index', 'printed_label',
                'printed_label_verified', 'status', 'warnings', 'logical_locator', 'text_sha256'}
    for number, section in enumerate(value['sections'], 1):
        if (type(section) is not dict or set(section) != expected
                or section['label'] != 'Logical section ' + str(number)
                or type(section['start']) is not int or type(section['end']) is not int
                or section['start'] != previous + (len(separator) if number > 1 else 0)
                or not section['start'] <= section['end'] <= len(text)
                or section['physical_index'] is not None or section['printed_label'] is not None
                or section['printed_label_verified'] is not False or section['status'] != 'review_required'
                or type(section['warnings']) is not list or len(section['warnings']) > 100
                or any(type(item) is not str or len(item) > 256 for item in section['warnings'])
                or type(section['logical_locator']) is not dict
                or type(section['text_sha256']) is not str
                or re.fullmatch('[0-9a-f]{64}', section['text_sha256']) is None):
            raise ConversionError('The selected conversion worker returned an invalid logical section.')
        if number > 1 and text[previous:section['start']] != separator:
            raise ConversionError('The selected conversion text separators changed.')
        if sha256(text[section['start']:section['end']].encode('utf-8')).hexdigest() != section['text_sha256']:
            raise ConversionError('The selected conversion section bytes changed.')
        previous = section['end']
    if previous != len(text):
        raise ConversionError('The selected conversion provenance does not cover all reading text.')
    return value


def extract_selected(service, source, output, *, cancelled=lambda: False):
    if not callable(cancelled):
        raise ValueError('A conversion cancellation callback is required.')
    _cancel(cancelled)
    _scope(service)
    source = service._input(source)
    suffix = source.suffix.casefold()
    if suffix not in SELECTED_SUFFIXES:
        raise ConversionUnavailable('This source has no selected optional conversion adapter.')
    if suffix in CALIBRE_SUFFIXES | OFFICE_SUFFIXES and os.name != 'nt':
        raise ConversionUnavailable('Selected Calibre and LibreOffice conversion currently requires the owned Windows Job route.')
    if service.root.is_relative_to(service.state_root.parent):
        raise PermissionError('Choose a reading-copy folder outside the research library.')
    target, _ = service._output(output)
    destination = target.with_name(target.stem + '-files')
    if len(destination.name) > 200:
        raise ConversionError('Choose a shorter reading-copy name.')
    checked_directory(guard_data_directory(destination.parent, purpose='reading conversion output'))
    if os.path.lexists(destination):
        raise FileExistsError('The reading-copy folder already exists. Choose another name.')
    parent_info = regular(destination.parent, directory=True)
    parent_identity = parent_info.st_dev, parent_info.st_ino
    before = regular_file(source)
    if before.st_size > min(MAX_SOURCE_BYTES, service.budget.max_source_bytes):
        raise ConversionError('The selected native document exceeds its 64 MiB source allowance.')
    identity = file_identity(before)
    selected_identity = dict(service._selected_input_files).get(source)
    if selected_identity is not None and selected_identity != identity:
        raise ConversionError('An explicitly selected conversion input changed before admission.')
    if not .1 <= service.budget.timeout <= 600:
        raise ConversionError('Selected document conversion requires a .1–600 second allowance.')
    deadline = monotonic() + service.budget.timeout
    completed = [None]

    def check():
        _cancel(cancelled)
        _scope(service)
        if monotonic() >= deadline:
            raise TimeoutError('The selected document conversion time budget expired.')
        if file_identity(regular_file(source)) != identity:
            raise ConversionError('The original document changed during conversion.')
        parent = regular(local_path(destination.parent, exists=True), directory=True)
        if (parent.st_dev, parent.st_ino) != parent_identity:
            raise ConversionError('The selected reading-copy destination changed.')
        return False

    broker = ResourceBroker.for_app(service.state_root.parent, cancelled=check)
    disk_bytes = (3 * min(MAX_SOURCE_BYTES, service.budget.max_source_bytes)
                  + 2 * min(MAX_TEXT_BYTES, service.budget.max_text_bytes)
                  + 2 * MAX_REPORT_BYTES + 80 * 1024**2)
    request = ResourceRequest(cpu_slots=1, memory_bytes=service.budget.memory_bytes,
                              disk_bytes=disk_bytes, disk_path=service.root)
    try:
        with broker.reserve(request, operation_id='document-conversion:' + uuid4().hex,
                            cancelled=check, timeout=min(60, service.budget.timeout),
                            run_timeout=service.budget.timeout) as lease:
            def admitted():
                check()
                lease.checkpoint()
                return False
            admitted()
            temporary = TemporaryDirectory(prefix='.pending-conversion-', dir=destination.parent)
            stage = Path(temporary.name)
            work, bundle = stage / 'work', stage / 'result'
            try:
                work.mkdir(mode=0o700)
                bundle.mkdir(mode=0o700)
                snapshot = work / ('source' + suffix)
                with verified_reader(source, expected_identity=identity) as original:
                    digest, count = sha256(), 0
                    with snapshot.open('xb') as copied:
                        while block := original.read(min(1024 * 1024, before.st_size - count + 1)):
                            admitted()
                            count += len(block)
                            if count > before.st_size:
                                raise ConversionError('The selected source grew while it was copied.')
                            copied.write(block)
                            digest.update(block)
                        copied.flush()
                        os.fsync(copied.fileno())
                    if count != before.st_size:
                        raise ConversionError('The selected source was truncated.')
                    source_hash = digest.hexdigest()
                    selection = _selection(service, suffix, admitted)
                    message = {'operation': 'selected', 'source': str(snapshot),
                        'output': str(work / 'reading.txt'), 'report': str(work / 'details.json'),
                        'budget': asdict(service.budget), 'adapter': selection,
                        'source_sha256': source_hash, 'source_size': count}
                    raw = json.dumps(message, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')
                    if len(raw) > 65536:
                        raise ConversionError('The selected conversion request exceeds its byte allowance.')
                    request_path = work / 'request.json'
                    with request_path.open('xb') as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    command, environment = _worker_command(request_path, sha256(raw).hexdigest())
                    with _held_core(service, suffix, selection, admitted):
                        admitted()
                        remaining = deadline - monotonic()
                        if remaining < .05:
                            raise TimeoutError('The selected document conversion time budget expired before worker admission.')
                        try:
                            run_process(command, cwd=work, env=environment, cancelled=admitted,
                                budget=AudioBudget(timeout=remaining,
                                    max_output_bytes=disk_bytes, max_log_bytes=256 * 1024,
                                    reserve_disk_bytes=16 * 1024**2, max_inputs=1,
                                    threads=1, max_staging_files=4096),
                                watch=(work,), owned_windows_job=os.name == 'nt')
                        except AudioCleanupPending as error:
                            # Do not release a selected native file or the copied
                            # source while any owned converter descendant is live.
                            lease.settle_cleanup(error)
                            raise
                    admitted()
                    reading = work / 'reading.txt'
                    text_info = regular_file(reading)
                    if not 0 < text_info.st_size <= min(MAX_TEXT_BYTES, service.budget.max_text_bytes):
                        raise ConversionError('The selected conversion returned an invalid text size.')
                    with verified_reader(reading, expected_identity=file_identity(text_info)) as stream:
                        text_raw = stream.read(text_info.st_size + 1)
                    if len(text_raw) != text_info.st_size:
                        raise ConversionError('The selected conversion text changed.')
                    text = text_raw.decode('utf-8', errors='strict')
                    if not text.strip() or '\x00' in text:
                        raise ConversionError('The selected conversion returned no valid reading text.')
                    details, _ = read_json_snapshot(work / 'details.json', max_bytes=MAX_REPORT_BYTES)
                    details = _validate_details(details, text, service.budget)
                    if digest_file(snapshot, cancelled=admitted) != source_hash:
                        raise ConversionError('The private original copy changed during conversion.')
                    reading_name, original_name = target.name, 'original' + suffix
                    receipt = {'schema': 'carr-reading-derivative/1', 'source_name': source.name,
                        'source_format': suffix, 'source_sha256': source_hash, 'source_size': count,
                        'text_sha256': sha256(text_raw).hexdigest(), 'text_size': len(text_raw),
                        'reading_name': reading_name, 'retained_original_name': original_name,
                        'retained_original_sha256': source_hash,
                        'verified_for_quotation': False, 'original_modified': False,
                        'printed_pages_reconstructed': False, 'native_catalog_source': False,
                        'elapsed_seconds': round(service.budget.timeout - max(0, deadline - monotonic()), 3),
                        **details}
                    receipt_raw = (json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                                              separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
                    if len(receipt_raw) > MAX_REPORT_BYTES:
                        raise ConversionError('The conversion fidelity report exceeds its allowance.')
                    os.rename(snapshot, bundle / original_name)
                    os.rename(reading, bundle / reading_name)
                    with (bundle / 'provenance.json').open('xb') as stream:
                        stream.write(receipt_raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    for path in (bundle / original_name, bundle / reading_name):
                        with path.open('r+b') as stream:
                            os.fsync(stream.fileno())
                    sync_directory(bundle)

                    def publication_check():
                        admitted()
                        if digest_file(source, cancelled=admitted, expected_identity=identity) != source_hash:
                            raise ConversionError('The original changed before reading-copy publication.')
                        if os.path.lexists(destination):
                            raise FileExistsError('The reading-copy folder already exists. Choose another name.')
                    publication_check()
                    info = regular(bundle, directory=True)
                    bundle_identity = info.st_dev, info.st_ino
                    result = ConversionResult(destination / reading_name, receipt,
                        destination / 'provenance.json', destination / original_name,
                        bundle_identity, sha256(receipt_raw).hexdigest())
                    publish_directory_no_replace(bundle, destination)
                    completed[0] = result  # The single directory rename is the commit.
                    # No late cancellation check can rewrite this actual result.
                    try:
                        sync_directory(destination.parent)
                    except OSError:
                        result = replace(result, notices=(
                            'The complete reading-copy folder is saved; directory durability could not be confirmed. Do not retry over it.',))
                        completed[0] = result
                return result
            finally:
                try:
                    temporary.cleanup()
                except Exception as error:
                    pending = pending_cleanup(temporary.cleanup,
                        message='Reading-copy staging cleanup remains pending; its resource reservation is retained.')
                    if completed[0] is not None:
                        pending.completed_conversion = completed[0]
                    raise pending from error
    except BaseException as error:
        if completed[0] is not None:
            error.completed_conversion = completed[0]
            if isinstance(error, (InterruptedError, ResourceCancelled, AudioCancelled)):
                notice = ConversionError('The complete reading-copy folder is saved; a later scope or cleanup check needs attention. Do not repeat the conversion.')
                notice.completed_conversion = completed[0]
                raise notice from error
        if isinstance(error, (ResourceCancelled, AudioCancelled)):
            cancelled_error = InterruptedError('Document conversion cancelled before publication.')
            if completed[0] is not None:
                cancelled_error.completed_conversion = completed[0]
            raise cancelled_error from error
        raise


def read_result(result, *, cancelled=lambda: False, guard=lambda: None):
    """Verify a live returned reading-folder receipt; never execute saved paths."""
    if type(result) is not ConversionResult or result.original_path is None:
        raise ConversionError('Choose a complete selected-format reading-copy result.')
    _cancel(cancelled); guard()
    folder = checked_directory(result.path.parent)
    info = regular(folder, directory=True)
    if (result.bundle_identity != (info.st_dev, info.st_ino)
            or result.report_path != folder / 'provenance.json'
            or result.original_path.parent != folder
            or type(result.receipt_sha256) is not str
            or re.fullmatch('[0-9a-f]{64}', result.receipt_sha256) is None):
        raise ConversionError('The selected reading-copy folder changed.')
    report, digest = read_json_snapshot(result.report_path, max_bytes=MAX_REPORT_BYTES)
    expected_report = (json.dumps(result.report, ensure_ascii=False, sort_keys=True,
                                  separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
    if digest != result.receipt_sha256 or sha256(expected_report).hexdigest() != digest:
        raise ConversionError('The reading-copy fidelity report changed.')
    if (type(report) is not dict or report.get('schema') != 'carr-reading-derivative/1'
            or report.get('native_catalog_source') is not False
            or report.get('verified_for_quotation') is not False
            or report.get('reading_name') != result.path.name
            or report.get('retained_original_name') != result.original_path.name
            or report.get('retained_original_sha256') != report.get('source_sha256')
            or type(report.get('source_size')) is not int
            or not 0 < report['source_size'] <= MAX_SOURCE_BYTES
            or type(report.get('text_size')) is not int
            or not 0 < report['text_size'] <= MAX_TEXT_BYTES):
        raise ConversionError('The reading-copy folder has invalid provenance or unexpected files.')
    names = set()
    with os.scandir(folder) as entries:
        for entry in entries:
            if len(names) >= 3:
                raise ConversionError('The reading-copy folder has unexpected files.')
            names.add(entry.name)
    if names != {result.path.name, result.original_path.name, 'provenance.json'}:
        raise ConversionError('The reading-copy folder has unexpected files.')
    def check():
        _cancel(cancelled); guard()
        current = regular(checked_directory(folder), directory=True)
        if (current.st_dev, current.st_ino) != result.bundle_identity:
            raise ConversionError('The selected reading-copy folder changed.')
        return False
    for path, size, expected in ((result.original_path, report['source_size'], report['source_sha256']),
                                (result.path, report['text_size'], report['text_sha256'])):
        if regular_file(path).st_size != size or digest_file(path, cancelled=check) != expected:
            raise ConversionError('The reading copy or its retained original changed.')
    with verified_reader(result.path) as stream:
        raw = stream.read(report['text_size'] + 1)
    if len(raw) != report['text_size'] or sha256(raw).hexdigest() != report['text_sha256']:
        raise ConversionError('The reading copy changed before use.')
    text = raw.decode('utf-8', errors='strict')
    if not text.strip() or '\x00' in text:
        raise ConversionError('The reading copy contains invalid text.')
    check()
    return text


def import_result(result, library, workspace, *, cancelled=lambda: False, guard=lambda: None):
    """Import only the verified reading TXT through the existing LibraryApp path.

    The external original/report are deliberately not invented catalog assets
    or copied-source authority. The caller displays that boundary before consent.
    """
    if not callable(cancelled) or not callable(guard):
        raise ValueError('Current reading-copy import callbacks are required.')
    if type(result) is not ConversionResult or result.original_path is None:
        raise ConversionError('Choose a complete selected-format reading folder.')
    guard()
    root = local_path(library.root, exists=True)
    info = regular(root, directory=True)
    root_identity = info.st_dev, info.st_ino
    folder = checked_directory(result.path.parent)
    files = tuple((path, file_identity(regular_file(path))) for path in
                  (result.path, result.report_path, result.original_path))
    def check():
        _cancel(cancelled)
        guard()
        current = regular(local_path(root, exists=True), directory=True)
        if local_path(library.root, exists=True) != root or (current.st_dev, current.st_ino) != root_identity:
            raise ConversionError('The selected library changed before reading-copy import.')
        current_folder = regular(checked_directory(folder), directory=True)
        if (current_folder.st_dev, current_folder.st_ino) != result.bundle_identity:
            raise ConversionError('The verified reading-copy folder changed during import.')
        if any(file_identity(regular_file(path)) != identity for path, identity in files):
            raise ConversionError('A verified reading-copy component changed during import.')
        return False
    check()
    returned = False
    rows = None
    try:
        with ExitStack() as held:
            # Preserve every exact returned component while the importer obtains
            # its own retained reading-text snapshot and publication receipt.
            for path, identity in files:
                held.enter_context(verified_reader(path, expected_identity=identity))
            read_result(result, cancelled=check, guard=guard)
            check()
            from ..extract.page_ocr import Config
            rows = library.import_files(workspace, (result.path,),
                config=Config(), cancelled=check)
            returned = True  # These are actual returned results, never inferred files.
        return rows
    except BaseException as error:
        if returned:
            error.completed_import = rows
        raise
