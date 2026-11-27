"""Pure bindings for the existing assistant's supplied-transcription step.

Saved jobs/templates describe reviewed inputs; these validators never open
runtime paths or grant execution permission to restored state.
"""
from hashlib import sha256
import json
import re
from uuid import NAMESPACE_URL, uuid5

from .models import canonical
from ..transcription_receipt import PROTOCOL, validate_settings
from ..transcription_state import relative_path

TOOL = 'audio.transcribe'
RECEIPT_NAME = 'transcription-export.json'
NOTICE = ('Verified retained transcript exports. The saved transcript remains a '
          'derived reading source and does not grant quotation authority.')


def job_identity(assistant_id, index):
    return uuid5(NAMESPACE_URL, 'carr-assistant:' + assistant_id + ':' + str(index) + ':transcription').hex


def job_title(identity):
    return 'Assistant transcription ' + identity


def validate_arguments(arguments):
    from .validation import _output_name, _path, _sha
    required = {'path', 'name', 'runtime_manifest', 'runtime_manifest_sha256', 'settings'}
    if type(arguments) is not dict or set(arguments) != required:
        raise ValueError('Transcription needs an input, new export name, explicit runtime manifest/hash and all settings.')
    _path(arguments['path'])
    _path(arguments['runtime_manifest'])
    _output_name(arguments['name'])
    relative_path(arguments['name'])
    _sha(arguments['runtime_manifest_sha256'])
    if (type(arguments['settings']) is not dict
            or validate_settings(arguments['settings']) != arguments['settings']):
        raise ValueError('Review all seventeen transcription settings explicitly.')
    return arguments


def definition_digest(value):
    return sha256(canonical(value).encode('utf-8')).hexdigest()


def export_receipt(identity, summary):
    return dict(schema='carr-transcription-export/1', job_id=identity, protocol=summary['protocol'],
        run_id=summary['run_id'], completion_sha256=summary['completion_sha256'],
        job_config_sha256=summary['job_config_sha256'],
        runtime_manifest_sha256=summary['runtime_manifest_sha256'],
        model_sha256=summary['model_sha256'], historical=True, runtime_current_verified=False,
        outputs=[dict(row) for row in summary['outputs']])


def receipt_bytes(receipt):
    # Exact serialization used by the existing transcription export publisher.
    return json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode('utf-8')


def artifacts(intent):
    from .validation import _path
    receipt = intent['transcription_export']
    root = _path(intent['transcription']['destination'])
    raw = receipt_bytes(receipt)
    return [dict(path=str(root / RECEIPT_NAME), size=len(raw), sha256=sha256(raw).hexdigest()),
            *[dict(path=str(root.joinpath('files', *relative_path(row['path']).parts)),
                   size=row['bytes'], sha256=row['sha256']) for row in receipt['outputs']]]


def completed_result(intent):
    binding, receipt = intent['transcription'], intent['transcription_export']
    return dict(status='complete', job_id=binding['job_id'], source_sha256=binding['sha256'],
        runtime_manifest_sha256=binding['runtime_manifest_sha256'], model_sha256=binding['model_sha256'],
        definition_sha256=intent['definition_sha256'], directory=binding['destination'],
        completion_sha256=receipt['completion_sha256'], job_config_sha256=receipt['job_config_sha256'],
        historical=True, runtime_current_verified=False, notice=NOTICE, artifacts=artifacts(intent))


def validate_checkpoint(record, index, arguments, checkpoint):
    from .validation import _integer, _path, _sha
    scope, intent = record['plan']['scope'], checkpoint['intent']
    if set(intent) - {'transcription', 'definition_sha256', 'transcription_export'}:
        raise ValueError('Invalid transcription step intent.')
    if not intent:
        if checkpoint['result'] is not None or checkpoint['status'] == 'complete':
            raise ValueError('Transcription completion requires its saved input intent.')
        return
    binding = intent.get('transcription')
    fields = {'job_id', 'source', 'size', 'sha256', 'runtime_manifest_sha256',
              'model_sha256', 'settings', 'destination'}
    if type(binding) is not dict or set(binding) != fields:
        raise ValueError('Invalid transcription source binding.')
    if (binding['job_id'] != job_identity(record['id'], index)
            or _path(binding['source']) != _path(arguments['path'])
            or binding['runtime_manifest_sha256'] != arguments['runtime_manifest_sha256']
            or binding['settings'] != arguments['settings']
            or _path(binding['destination']) != _path(scope['output_root']) / arguments['name']):
        raise ValueError('Transcription intent differs from the reviewed plan.')
    _integer(binding['size'], 'transcription input size', maximum=scope['max_bytes'])
    for key in ('sha256', 'runtime_manifest_sha256', 'model_sha256'):
        _sha(binding[key])
    if 'definition_sha256' in intent:
        _sha(intent['definition_sha256'])
    if 'transcription_export' not in intent:
        if checkpoint['result'] is not None:
            raise ValueError('A transcript result requires its export intent.')
        return
    if 'definition_sha256' not in intent:
        raise ValueError('An export requires the exact prepared transcription definition.')
    receipt = intent['transcription_export']
    fields = {'schema', 'job_id', 'protocol', 'run_id', 'completion_sha256', 'job_config_sha256',
              'runtime_manifest_sha256', 'model_sha256', 'historical', 'runtime_current_verified', 'outputs'}
    if (type(receipt) is not dict or set(receipt) != fields
            or receipt['schema'] != 'carr-transcription-export/1'
            or receipt['job_id'] != binding['job_id'] or receipt['protocol'] != PROTOCOL
            or receipt['historical'] is not True or receipt['runtime_current_verified'] is not False
            or receipt['runtime_manifest_sha256'] != binding['runtime_manifest_sha256']
            or receipt['model_sha256'] != binding['model_sha256']
            or type(receipt['run_id']) is not str or not re.fullmatch('[0-9a-f]{32}', receipt['run_id'])):
        raise ValueError('Invalid transcription export intent.')
    for key in ('completion_sha256', 'job_config_sha256'):
        _sha(receipt[key])
    rows = receipt['outputs']
    chosen = {name for name in ('txt', 'srt', 'vtt', 'json') if arguments['settings']['output_' + name]}
    if type(rows) is not list or len(rows) != len(chosen):
        raise ValueError('Transcription exports must cover every selected format for the approved input.')
    paths, formats = set(), set()
    for row in rows:
        if type(row) is not dict or set(row) != {'path', 'format', 'source_indexes', 'bytes', 'sha256'}:
            raise ValueError('Invalid transcription export entry.')
        path = relative_path(row['path'])
        if (type(row['format']) is not str or row['format'] not in chosen or row['format'] in formats or path in paths
                or row['source_indexes'] != [0] or any(type(i) is not int for i in row['source_indexes'])
                or path.suffix != '.' + row['format']):
            raise ValueError('Transcription export changed its format or input coverage.')
        formats.add(row['format'])
        paths.add(path)
        _integer(row['bytes'], 'transcription export size', minimum=0, maximum=scope['max_bytes'])
        _sha(row['sha256'])
    expected = artifacts(intent)
    if len(expected) > scope['max_files'] or sum(row['size'] for row in expected) > scope['max_bytes']:
        raise ValueError('Transcription exports exceed the approved artifact budget.')
    if checkpoint['result'] is not None and checkpoint['result'] != completed_result(intent):
        raise ValueError('The transcript result differs from its reviewed input and verified export.')
