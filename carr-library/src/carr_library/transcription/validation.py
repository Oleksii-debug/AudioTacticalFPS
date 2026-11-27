"""Pure validation of inert job/receipt bytes; no paths here are opened."""
from hashlib import sha256
import math
from pathlib import PurePosixPath, PureWindowsPath
import re

from .files import canonical
from .models import (FORMATS, INPUT_SUFFIXES, PROTOCOL, TranscriptionBudget,
                     TranscriptionOptions, checksum, exact_keys, identity, positive)


REQUEST_BYTES = 8 * 1024**2
RESTORED_BYTES = 4096
RESTORED_NAME = 'restored.json'


def opaque_absolute_path(value):
    """Accept historical Windows/POSIX spelling without resolving or following it."""
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096
            or any(ord(c) < 32 for c in value) or value.startswith(('\\\\', '//'))
            or not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute())):
        raise ValueError('Invalid historical local path binding.')


def validate_request(value, job_id):
    identity(job_id)
    exact_keys(value, ('schema', 'protocol', 'id', 'data_root', 'workspace', 'profile_path',
                      'profile_sha256', 'model_id', 'model_revision', 'options', 'budget', 'sources'),
               'transcription request')
    if value['schema'] != 'resonance-transcription-request/1' or value['protocol'] != PROTOCOL or value['id'] != job_id:
        raise ValueError('Invalid saved transcription request identity.')
    for key in ('data_root', 'profile_path'):
        opaque_absolute_path(value[key])
    checksum(value['profile_sha256'])
    for key in ('workspace', 'model_id', 'model_revision'):
        if (not isinstance(value[key], str) or not 1 <= len(value[key]) <= 512
                or any(ord(c) < 32 for c in value[key])):
            raise ValueError('Invalid saved transcription scope or model identity.')
    if re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', value['model_revision']) is None:
        raise ValueError('Invalid saved exact model revision.')
    options = TranscriptionOptions.from_dict(value['options'])
    budget = TranscriptionBudget.from_dict(value['budget'])
    sources = value['sources']
    if type(sources) is not list or not 1 <= len(sources) <= budget.max_sources:
        raise ValueError('Invalid saved input list.')
    total = 0
    for number, row in enumerate(sources, 1):
        exact_keys(row, ('name', 'display_name', 'bytes', 'sha256'), 'source snapshot')
        if (not isinstance(row['name'], str) or PurePosixPath(row['name']).suffix not in INPUT_SUFFIXES
                or row['name'] != f'source-{number:05d}' + PurePosixPath(row['name']).suffix
                or not isinstance(row['display_name'], str) or not 1 <= len(row['display_name']) <= 512
                or any(ord(c) < 32 for c in row['display_name'])):
            raise ValueError('Invalid saved source locator.')
        checksum(row['sha256'])
        total += positive(row['bytes'], 'source byte count', budget.max_input_bytes)
    if total > budget.max_input_bytes:
        raise ValueError('Saved source snapshots exceed the approved budget.')
    return options, budget


def validate_receipt(value, request, request_raw):
    options, budget = validate_request(request, request['id'])
    exact_keys(value, ('schema', 'id', 'request_sha256', 'profile_sha256', 'quotation_authority',
        'accuracy_status', 'derivative_kind', 'sources', 'outputs', 'observed'), 'transcription receipt')
    if (value['schema'] != 'resonance-transcription-receipt/1' or value['id'] != request['id']
            or value['quotation_authority'] is not False or value['accuracy_status'] != 'not_qualified'
            or value['request_sha256'] != sha256(request_raw).hexdigest()
            or value['profile_sha256'] != request['profile_sha256']
            or value['derivative_kind'] != ('english_translation' if options.task == 'translate' else 'transcript')
            or canonical(value['sources']) != canonical(request['sources'])):
        raise ValueError('The receipt is not bound to this approved derivative request.')
    expected = set()
    for row in request['sources']:
        for fmt in FORMATS:
            if fmt == 'json' or fmt in options.formats:
                name = 'results/inputs/' + PurePosixPath(row['name']).stem + '.' + fmt
                expected.update((name, name + '.v43-output.json'))
    outputs = value['outputs']
    if type(outputs) is not list or len(outputs) != len(expected):
        raise ValueError('The receipt has an invalid output inventory.')
    for row in outputs:
        exact_keys(row, ('path', 'sha256', 'bytes'), 'receipt output')
        if not isinstance(row['path'], str) or row['path'] not in expected:
            raise ValueError('An output path does not belong to this transcript.')
        expected.remove(row['path'])
        checksum(row['sha256'])
        if type(row['bytes']) is not int or not 0 <= row['bytes'] <= budget.max_output_bytes:
            raise ValueError('An output exceeds its approved byte budget.')
    observed = value['observed']
    if type(observed) is not list or len(observed) != len(request['sources']):
        raise ValueError('Invalid completed source observation count.')
    for row, source in zip(observed, request['sources']):
        exact_keys(row, ('source', 'detected_language', 'duration', 'attempt', 'attempt_no',
                         'effective_settings'), 'source observation')
        if (row['source'] != source['name'] or not isinstance(row['detected_language'], str)
                or re.fullmatch('[a-z]{2,3}', row['detected_language']) is None
                or type(row['duration']) not in (int, float) or not math.isfinite(row['duration'])
                or not 0 <= row['duration'] <= 10**9 or type(row['attempt_no']) is not int
                or not 1 <= row['attempt_no'] <= 3
                or row['attempt'] not in ('original', 'beam1_no_previous', 'beam1_no_vad_no_previous')
                or type(row['effective_settings']) is not dict):
            raise ValueError('Invalid completed source observation.')
        settings = row['effective_settings']
        model = settings.get('model')
        if not isinstance(model, str) or re.fullmatch(r'[^/\\:\x00-\x1f]{1,512}', model) is None:
            raise ValueError('Invalid retained model selection.')
        original = options.worker_settings(model)
        candidates = [('original', original),
            ('beam1_no_previous', {**original, 'beam_size': 1, 'condition_on_previous_text': False}),
            ('beam1_no_vad_no_previous', {**original, 'beam_size': 1, 'condition_on_previous_text': False, 'vad_filter': False})]
        unique, seen = [], set()
        for name, candidate in candidates:
            encoded = canonical(candidate)
            if encoded not in seen:
                unique.append((name, candidate))
                seen.add(encoded)
        index = row['attempt_no'] - 1
        if (index >= len(unique) or unique[index][0] != row['attempt']
                or canonical(unique[index][1]) != canonical(settings)):
            raise ValueError('Retained decoder observation differs from the approved choices.')
    return outputs


def validate_restored(value, job_id, request_sha256, receipt_sha256=None):
    exact_keys(value, ('schema', 'id', 'request_sha256', 'receipt_sha256', 'requires_reapproval'),
               'restored transcription marker')
    if (value['schema'] != 'resonance-transcription-restored/1' or value['id'] != job_id
            or value['request_sha256'] != request_sha256 or value['requires_reapproval'] is not True):
        raise ValueError('The restored transcription marker does not bind its original request.')
    if value['receipt_sha256'] is not None:
        checksum(value['receipt_sha256'])
    if receipt_sha256 is not None and value['receipt_sha256'] != receipt_sha256:
        raise ValueError('The restored derivative marker does not bind its original receipt.')
    return value
