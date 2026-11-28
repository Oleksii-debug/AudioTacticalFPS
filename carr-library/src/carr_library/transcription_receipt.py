"""Verify a local transcription's byte-bound completion receipt.

This module neither launches an engine nor treats a receipt as proof of a
successful process exit. The supervisor must separately require exit code zero.
Only the selected input copies and declared result files are opened; original
source-provenance paths remain inert, hash-bound job metadata.
"""
from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
import unicodedata

from .audio.model_manifest import _read_json_snapshot, digest_file, local_path
from .audio.models import canonical, digest
from .audio.paths import filename_component


PROTOCOL = 'supplied-whisper-v43-hardened-1'
MAX_INPUTS = 1000
MAX_OUTPUTS = 4000
MAX_RECEIPT_BYTES = 16 * 1024 * 1024
OUTPUT_SETTINGS_KEYS = ('output_mode', 'include_headers', 'output_txt',
                        'output_srt', 'output_vtt', 'output_json')
DEFAULT_SETTINGS = MappingProxyType(dict(
    output_mode='separate', task='transcribe', translation_target='en',
    language='auto', model='small', device='cpu', compute_type='int8',
    beam_size=1, parallel_files=1, vad_filter=True, include_headers=False,
    output_txt=True, output_srt=False, output_vtt=False, output_json=False,
    multilingual=False, condition_on_previous_text=True))
_BOOL_SETTINGS = frozenset(('vad_filter', 'include_headers', 'output_txt', 'output_srt',
                           'output_vtt', 'output_json', 'multilingual', 'condition_on_previous_text'))
# Public Whisper tokenizer language identifiers; no language/model download.
_LANGUAGES = frozenset(('en zh de es ru ko fr ja pt tr pl ca nl ar sv it id hi fi vi he uk el ms cs ro '
    'da hu ta no th ur hr bg lt la mi ml cy sk te fa lv bn sr az sl kn et mk br eu is hy ne mn bs kk '
    'sq sw gl mr pa si km sn yo so af oc ka be tg sd gu am yi lo uz fo ht ps tk nn mt sa lb my bo tl '
    'mg as tt haw ln ha ba jw su yue').split())
_INPUT_KEYS = frozenset(('path', 'root', 'relative', 'bytes', 'sha256'))
_RECEIPT_INPUT_KEYS = _INPUT_KEYS | {'effective_settings', 'model_sha256', 'runtime_sha256'}
_OUTPUT_KEYS = frozenset(('path', 'format', 'source_indexes', 'bytes', 'sha256'))
_PAYLOAD_KEYS = frozenset(('run_id', 'job_root', 'job_config_sha256', 'results_root',
                           'output_settings', 'inputs', 'outputs'))


class TranscriptionReceiptError(ValueError):
    """Completion could not be established; messages contain no source paths."""


def _reject(message):
    raise TranscriptionReceiptError(message) from None


def _fields(value, keys, label):
    if type(value) is not dict or set(value) != set(keys):
        _reject(f'The {label} has unknown or missing fields.')
    return value


def _hex(value, length, label):
    if type(value) is not str or re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is None:
        _reject(f'The {label} is invalid.')
    return value


def _text(value, label, maximum=4096):
    if (type(value) is not str or not value or len(value) > maximum
            or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in value)):
        _reject(f'The {label} is invalid.')
    try:
        value.encode('utf-8')
    except UnicodeError:
        _reject(f'The {label} is not valid Unicode.')
    return value


def validate_settings(value):
    """Validate known settings and return all 17 captured, independent values."""
    if type(value) is not dict or set(value) - set(DEFAULT_SETTINGS):
        _reject('Transcription settings contain unknown fields.')
    settings = dict(DEFAULT_SETTINGS)
    settings.update(value)
    choices = dict(output_mode=('separate', 'one_per_folder', 'one_whole_job'),
                   task=('transcribe', 'translate'), translation_target=('en',),
                   device=('cpu', 'cuda', 'auto'), compute_type=('int8', 'auto', 'float16', 'float32'))
    for name, allowed in choices.items():
        if type(settings[name]) is not str or settings[name] not in allowed:
            _reject(f'The transcription {name} setting is invalid.')
    language = settings['language']
    if type(language) is not str or language not in _LANGUAGES | {'auto'}:
        _reject('The transcription language setting is invalid.')
    model = _text(settings['model'], 'model setting', 64)
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', model) is None:
        _reject('The model setting must be one admitted local model directory name.')
    try:
        filename_component(model)
    except ValueError:
        _reject('The model setting must be one local directory name.')
    for name, maximum in (('beam_size', 10), ('parallel_files', 20)):
        if type(settings[name]) is not int or not 1 <= settings[name] <= maximum:
            _reject(f'The transcription {name} setting is invalid.')
    for name in _BOOL_SETTINGS:
        if type(settings[name]) is not bool:
            _reject(f'The transcription {name} setting must be boolean.')
    if not any(settings['output_' + format] for format in ('txt', 'srt', 'vtt', 'json')):
        _reject('Select at least one transcription output format.')
    return settings



def canonical_digest(value, *, maximum=64 * 1024 * 1024):
    """Return the shared canonical JSON hash without following any file paths."""
    if type(maximum) is not int or not 1 <= maximum <= 64 * 1024 * 1024:
        _reject('The record byte budget is invalid.')
    try:
        encoded = canonical(value).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _reject('The integrity record is not finite canonical JSON.')
    if len(encoded) > maximum:
        _reject('The integrity record exceeds its canonical byte budget.')
    return hashlib.sha256(encoded).hexdigest()


def validate_record(value, kind, *, maximum=64 * 1024 * 1024):
    """Validate a pure worker integrity envelope; payload semantics stay typed.

    Backup uses this for inert historical records. This function never opens a
    path, rewrites an absolute binding, or admits an archived execution.
    """
    if type(kind) is not str or re.fullmatch(r'[a-z][a-z0-9-]{0,63}', kind) is None:
        _reject('The integrity record kind is invalid.')
    value = _fields(value, ('schema', 'payload', 'sha256'), 'integrity envelope')
    if value['schema'] != 'supplied-whisper-v43-integrity/1/' + kind:
        _reject('The integrity record schema differs from its required kind.')
    if type(value['payload']) is not dict:
        _reject('The integrity record payload must be an object.')
    expected = _hex(value['sha256'], 64, 'integrity payload digest')
    if canonical_digest(value['payload'], maximum=maximum) != expected:
        _reject('The integrity record payload differs from its captured digest.')
    return value['payload']


def _relative(value, label):
    value = _text(value, label)
    relative = PurePosixPath(value)
    if (relative.is_absolute() or relative.as_posix() != value or '\\' in value
            or any(part in ('', '.', '..') for part in value.split('/'))):
        _reject(f'The {label} must be a canonical relative path.')
    try:
        for part in relative.parts:
            filename_component(part)
    except ValueError:
        _reject(f'The {label} contains an unsafe filename.')
    return relative


def _path(value, label):
    value = _text(value, label, 32768)
    if not Path(value).is_absolute():
        _reject(f'The {label} must be an absolute local path.')
    try:
        return local_path(value)
    except (OSError, ValueError):
        _reject(f'The {label} is unavailable or is not an ordinary local path.')


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _regular(path, label):
    try:
        path = local_path(path)
        info = path.stat()
    except (OSError, ValueError):
        _reject(f'The {label} is unavailable or changed during verification.')
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        _reject(f'The {label} must be a regular file without links.')
    return info


def _directory(value, label, watched):
    path = _path(value, label)
    try:
        info = path.stat()
    except OSError:
        _reject(f'The {label} is unavailable.')
    if not stat.S_ISDIR(info.st_mode):
        _reject(f'The {label} must be a directory.')
    identity = info.st_dev, info.st_ino, info.st_mode
    if path in watched and watched[path][0] != identity:
        _reject(f'The {label} changed during verification.')
    watched[path] = identity, label
    return path


def _byte_count(value, label, *, empty=False):
    if type(value) is not int or not (0 if empty else 1) <= value <= (1 << 63) - 1:
        _reject(f'The {label} byte count is invalid.')
    return value


def _json_snapshot(path, label, watched):
    before = _regular(path, label)
    try:
        value, checksum = _read_json_snapshot(path, max_bytes=MAX_RECEIPT_BYTES)
    except (OSError, ValueError, UnicodeError, RecursionError):
        _reject(f'The {label} is not a bounded, unambiguous JSON document.')
    if _stamp(_regular(path, label)) != _stamp(before):
        _reject(f'The {label} changed during verification.')
    watched[path] = _stamp(before), label
    return value, checksum


def _verify_file(path, size, checksum, label, watched, cancelled):
    before = _regular(path, label)
    if before.st_size != size:
        _reject(f'The {label} byte count changed.')
    try:
        current = digest_file(path, max_bytes=size, cancelled=cancelled)
    except InterruptedError:
        raise
    except (OSError, ValueError):
        _reject(f'The {label} could not be verified.')
    if current != checksum or _stamp(_regular(path, label)) != _stamp(before):
        _reject(f'The {label} differs from its captured bytes.')
    watched[path] = _stamp(before), label
    return before.st_dev, before.st_ino


def _alias(path):
    return unicodedata.normalize('NFC', str(path)).casefold()


def verify_completion(job_dir, config, status, *, expected_config_sha256,
                      expected_runtime_sha256, expected_model_sha256,
                      cancelled=lambda: False, verify_runtime_current=True):
    """Validate a completed receipt; the caller separately requires exit zero.

    Returned output paths remain relative to the validated results root. No
    input provenance path is followed or copied into this summary or errors.
    A changed live setting is not silently admitted as the captured request.
    Read-only reopening may omit a removed external runtime only after the
    caller verifies its saved accepted exit and immutable job definition.
    """
    if type(verify_runtime_current) is not bool:
        _reject('The current-runtime verification option must be boolean.')
    if not callable(cancelled):
        _reject('A completion cancellation callback is required.')

    def stopped():
        try:
            value = cancelled()
        except Exception:
            _reject('The completion cancellation state is unavailable.')
        if type(value) is not bool:
            _reject('The completion cancellation state must be boolean.')
        return value

    def check_cancelled():
        if stopped():
            raise InterruptedError('Transcription completion verification cancelled.')

    check_cancelled()
    for value, label in ((expected_config_sha256, 'expected configuration digest'),
                         (expected_runtime_sha256, 'expected runtime digest'),
                         (expected_model_sha256, 'expected model digest')):
        _hex(value, 64, label)
    files, directories = {}, {}
    try:
        selected_job = str(Path(job_dir).absolute())
    except (TypeError, ValueError):
        _reject('Choose a local transcription job directory.')
    job = _directory(selected_job, 'job directory', directories)
    config_path, receipt_path = job / 'job_config.json', job / 'logs/completion-v1.json'
    disk_config, config_sha = _json_snapshot(config_path, 'job configuration', files)
    required = {'run_id', 'runtime_manifest', 'runtime_manifest_sha256', 'settings',
                'folders', 'results_root', 'inputs'}
    if type(disk_config) is not dict or not required <= set(disk_config) or type(config) is not dict:
        _reject('The job configuration is missing its captured authority fields.')
    try:
        same_config = canonical(disk_config) == canonical(config)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _reject('The captured job configuration is invalid.')
    if config_sha != expected_config_sha256 or not same_config:
        _reject('The job configuration differs from the captured request.')
    config = disk_config  # Use the byte-bound snapshot, not a mutable caller object.
    run_id = _hex(config['run_id'], 32, 'job run identity')
    settings = validate_settings(config['settings'])
    output_settings = {name: settings[name] for name in OUTPUT_SETTINGS_KEYS}
    if (type(status) is not dict or status.get('protocol') != PROTOCOL
            or status.get('run_id') != run_id or status.get('state') not in ('completed', 'завершено')):
        _reject('The status does not confirm this hardened transcription run.')
    completion_sha = _hex(status.get('completion_sha256'), 64, 'status completion digest')
    if config['runtime_manifest_sha256'] != expected_runtime_sha256:
        _reject('The captured runtime differs from the admitted runtime.')
    runtime_reference = _text(config['runtime_manifest'], 'runtime manifest', 32768)
    if not Path(runtime_reference).is_absolute():
        _reject('The runtime manifest must have an absolute recorded location.')
    runtime_path = Path(runtime_reference)
    if verify_runtime_current:
        runtime_path = _path(runtime_reference, 'runtime manifest')
        runtime_info = _regular(runtime_path, 'runtime manifest')
        if runtime_info.st_size > MAX_RECEIPT_BYTES:
            _reject('The runtime manifest exceeds its verification budget.')
        _verify_file(runtime_path, runtime_info.st_size, expected_runtime_sha256,
                     'runtime manifest', files, stopped)
    requested_results = _directory(config['results_root'], 'requested result directory', directories)
    folders = config['folders']
    if type(folders) is not list or not 1 <= len(folders) <= MAX_INPUTS:
        _reject('The selected folder inventory is invalid.')
    folder_paths, folder_aliases = {}, set()
    for value in folders:
        path = _directory(value, 'selected input root', directories)
        if _alias(path) in folder_aliases:
            _reject('The selected input roots contain duplicate paths.')
        folder_aliases.add(_alias(path))
        folder_paths[value] = path
    inputs = config['inputs']
    if type(inputs) is not list or not 1 <= len(inputs) <= MAX_INPUTS:
        _reject('The selected input inventory is invalid.')
    source_paths, source_inodes, source_aliases = [], set(), set()
    for index, row in enumerate(inputs):
        check_cancelled()
        label = f'input {index + 1}'
        _fields(row, _INPUT_KEYS, label)
        if type(row['root']) is not str or row['root'] not in folder_paths:
            _reject(f'The {label} is outside the selected input roots.')
        relative = _relative(row['relative'], label + ' relative name')
        path = _path(row['path'], label)
        if path != folder_paths[row['root']].joinpath(*relative.parts):
            _reject(f'The {label} does not match its selected relative name.')
        if _alias(path) in source_aliases:
            _reject('The selected inputs contain duplicate paths.')
        source_aliases.add(_alias(path))
        source_paths.append(path)
        size = _byte_count(row['bytes'], label)
        checksum = _hex(row['sha256'], 64, label + ' digest')
        source_inodes.add(_verify_file(path, size, checksum, label, files, stopped))
    envelope, actual_receipt_sha = _json_snapshot(receipt_path, 'completion receipt', files)
    _fields(envelope, ('schema', 'payload', 'sha256'), 'completion envelope')
    if envelope['schema'] != PROTOCOL or actual_receipt_sha != completion_sha:
        _reject('The completion receipt does not match the final status.')
    payload = _fields(envelope['payload'], _PAYLOAD_KEYS, 'completion payload')
    payload_sha = _hex(envelope['sha256'], 64, 'payload digest')
    try:
        matches = digest(payload) == payload_sha
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _reject('The completion payload is invalid.')
    if not matches or payload['run_id'] != run_id or payload['job_config_sha256'] != config_sha:
        _reject('The completion payload does not match this captured run.')
    if _directory(payload['job_root'], 'receipt job directory', directories) != job:
        _reject('The receipt belongs to a different job directory.')
    result_root = _directory(payload['results_root'], 'receipt result directory', directories)
    if not result_root.is_relative_to(requested_results):
        _reject('The receipt result directory is outside the selected scope.')
    _fields(payload['output_settings'], OUTPUT_SETTINGS_KEYS, 'receipt output settings')
    if canonical(payload['output_settings']) != canonical(output_settings):
        _reject('The output settings differ from the captured request.')
    received_inputs = payload['inputs']
    if type(received_inputs) is not list or len(received_inputs) != len(inputs):
        _reject('The completion input inventory differs from the selected inputs.')
    for index, (row, expected) in enumerate(zip(received_inputs, inputs)):
        _fields(row, _RECEIPT_INPUT_KEYS, f'receipt input {index + 1}')
        _fields(row['effective_settings'], DEFAULT_SETTINGS, 'effective input settings')
        if (canonical({name: row[name] for name in _INPUT_KEYS}) != canonical(expected)
                or validate_settings(row['effective_settings']) != settings
                or row['model_sha256'] != expected_model_sha256
                or row['runtime_sha256'] != expected_runtime_sha256):
            _reject(f'The receipt input {index + 1} differs from its captured request or engine.')
    outputs = payload['outputs']
    if type(outputs) is not list or not 1 <= len(outputs) <= MAX_OUTPUTS:
        _reject('The completion output inventory is invalid.')
    coverage, output_aliases, output_inodes, validated = Counter(), set(), set(), []
    reserved = {_alias(path) for path in (*source_paths, config_path, receipt_path,
                                         runtime_path, job / 'job_status.json')}
    for index, row in enumerate(outputs):
        check_cancelled()
        label = f'output {index + 1}'
        _fields(row, _OUTPUT_KEYS, label)
        relative = _relative(row['path'], label + ' relative name')
        format = row['format']
        if (type(format) is not str or format not in ('txt', 'srt', 'vtt', 'json')
                or relative.suffix.casefold() != '.' + format):
            _reject(f'The {label} format is invalid.')
        indices = row['source_indexes']
        if (type(indices) is not list or not 1 <= len(indices) <= len(inputs)
                or any(type(i) is not int or not 0 <= i < len(inputs) for i in indices)
                or indices != sorted(set(indices))):
            _reject(f'The {label} source indices are invalid.')
        path = _path(str(result_root.joinpath(*relative.parts)), label)
        if not path.is_relative_to(result_root) or _alias(path) in reserved:
            _reject(f'The {label} is outside its result scope or aliases a selected source.')
        if _alias(path) in output_aliases:
            _reject('The outputs contain duplicate or aliased paths.')
        output_aliases.add(_alias(path))
        size = _byte_count(row['bytes'], label, empty=True)
        checksum = _hex(row['sha256'], 64, label + ' digest')
        inode = _verify_file(path, size, checksum, label, files, stopped)
        if inode in source_inodes or inode in output_inodes:
            _reject('The outputs alias a selected source or another result file.')
        output_inodes.add(inode)
        coverage[format, tuple(indices)] += 1
        validated.append(dict(path=row['path'], format=format, source_indexes=list(indices),
                              bytes=size, sha256=checksum))
    expected_coverage = Counter()
    for format in ('txt', 'srt', 'vtt', 'json'):
        if not settings['output_' + format]:
            continue
        groups = [(index,) for index in range(len(inputs))]
        if format == 'txt' and settings['output_mode'] == 'one_whole_job':
            groups = [tuple(range(len(inputs)))]
        elif format == 'txt' and settings['output_mode'] == 'one_per_folder':
            selected = {}
            for index, row in enumerate(inputs):
                selected.setdefault(row['root'], []).append(index)
            groups = [tuple(group) for group in selected.values()]
        expected_coverage.update((format, group) for group in groups)
    if coverage != expected_coverage:
        _reject('The result formats or source coverage are incomplete or unexpected.')
    # Detect changes after an earlier file was hashed while later files were read.
    for path, (identity, label) in directories.items():
        check_cancelled()
        try:
            current = local_path(path).stat()
        except (OSError, ValueError):
            _reject(f'The {label} changed during verification.')
        if (current.st_dev, current.st_ino, current.st_mode) != identity:
            _reject(f'The {label} changed during verification.')
    for path, (identity, label) in files.items():
        check_cancelled()
        if _stamp(_regular(path, label)) != identity:
            _reject(f'The {label} changed during verification.')
    return dict(protocol=PROTOCOL, run_id=run_id, job_root=str(job), results_root=str(result_root),
                completion_sha256=actual_receipt_sha, job_config_sha256=config_sha,
                runtime_manifest_sha256=expected_runtime_sha256, model_sha256=expected_model_sha256,
                input_count=len(inputs), output_count=len(validated),
                output_settings=dict(output_settings), outputs=validated,
                runtime_current_verified=verify_runtime_current)
