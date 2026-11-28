"""Passive remote-transcript receipts; reading never authorizes network access.

The formats reuse the established retained transcription job folders and
serializers. No credential, active Authorization, executable or network client
is loaded while validating a saved job or a copied backup.
"""
from hashlib import sha256
import json
import math
from pathlib import PurePosixPath
import re

from ..durable_state import local_path, regular, read_json, digest_file
from ..assistant.models import canonical
from ..assistant.profiles import ProviderProfile
from ..assistant.transcription_provider import (PROTOCOL, OPERATION, MODEL,
    MAX_INPUT_BYTES, MAX_RESPONSE_BYTES, MAX_WORDS, MAX_TEXT_CHARACTERS, request)
from .transcription_values import encoded_outputs, validated_result

JOB_VERSION = 2
RECEIPT_SCHEMA = 'resonance-remote-transcription-receipt/1'
ID = r'[0-9a-f]{32}'
HEX = r'[0-9a-f]{64}'
SOURCE = r'001-[0-9a-f]{32}\.[a-z0-9]{2,5}'
MAX_RESULT_BYTES = 16 * 1024**2
REVIEW_REQUIRED = 'An explicit new confirmation is required for every remote call; stored jobs carry no active consent.'


def fingerprint(value):
    return sha256(canonical(value).encode('utf-8')).hexdigest()


def fields(value, required):
    if type(value) is not dict or set(value) != set(required):
        raise ValueError('Invalid remote transcription fields.')


def checksum(value):
    if type(value) is not str or not re.fullmatch(HEX, value):
        raise ValueError('Invalid remote transcription checksum.')
    return value


def identifier(value):
    if type(value) is not str or not re.fullmatch(ID, value):
        raise ValueError('Invalid remote transcription identity.')
    return value


def finite(value, maximum=30 * 86400):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError('Invalid remote transcription numeric value.')


def text(value, maximum, *, empty=False):
    if type(value) is not str or '\x00' in value or len(value) > maximum or (not empty and not value):
        raise ValueError('Invalid remote transcription text.')
    value.encode('utf-8', 'strict')


def source(value):
    fields(value, ('filename', 'original_name', 'size_bytes', 'sha256'))
    if type(value['filename']) is not str or not re.fullmatch(SOURCE, value['filename']):
        raise ValueError('Invalid remote transcription retained source name.')
    text(value['original_name'], 4096)
    checksum(value['sha256'])
    if type(value['size_bytes']) is not int or not 0 < value['size_bytes'] <= MAX_INPUT_BYTES:
        raise ValueError('Invalid remote transcription source size.')
    return value


def snapshot(profile):
    return {**profile.to_dict(), 'credential_ref': None, 'enabled': False}


def remote_binding(value, audio):
    fields(value, ('profile', 'profile_sha256', 'request_sha256', 'approval_sha256',
                   'authorization_id', 'operation_id', 'restored', 'consent', 'payload', 'max_cost'))
    profile = ProviderProfile(**value['profile'])
    if profile.enabled or profile.credential_ref is not None or value['profile'] != snapshot(profile):
        raise ValueError('Saved transcription profiles cannot carry credentials or active settings.')
    for key in ('profile_sha256', 'request_sha256', 'approval_sha256'):
        checksum(value[key])
    identifier(value['operation_id'])
    identifier(value['authorization_id'])
    if type(value['restored']) is not bool or value['consent'] != REVIEW_REQUIRED:
        raise ValueError('Saved transcription consent must remain inactive.')
    # Imported lazily for the pure Decimal validator; this never opens a vault.
    from ..assistant.providers import money
    if type(value['max_cost']) is not str or str(money(value['max_cost'])) != value['max_cost']:
        raise ValueError('Invalid explicit remote cost bound.')
    expected_audio = {'sha256': audio['sha256'], 'size_bytes': audio['size_bytes'],
                      'suffix': PurePosixPath(audio['filename']).suffix}
    if type(value['payload']) is not dict or value['payload'].get('audio') != expected_audio:
        raise ValueError('The remote request differs from its retained recording.')
    _, body, _ = request(profile, value['payload'])
    if fingerprint(body) != value['request_sha256']:
        raise ValueError('The saved remote request changed.')
    return value


def job(value, *, identity=None, workspace=None):
    fields(value, ('version', 'protocol', 'id', 'workspace', 'title', 'created', 'updated',
                   'status', 'sources', 'settings', 'done', 'failed', 'receipts', 'artifacts',
                   'run_context', 'last_error', 'remote'))
    if type(value['version']) is not int or value['version'] != JOB_VERSION or value['protocol'] != PROTOCOL:
        raise ValueError('Unsupported remote transcription job schema.')
    identifier(value['id'])
    if identity is not None and value['id'] != identity:
        raise ValueError('Remote transcription identity changed.')
    text(value['workspace'], 200)
    if workspace is not None and value['workspace'] != workspace:
        raise ValueError('Remote transcription belongs to another workspace.')
    text(value['title'], 200)
    for key in ('created', 'updated'):
        finite(value[key], 10**12)
    text(value['last_error'], 8192, empty=True)
    if (type(value['sources']) is not list or len(value['sources']) != 1
            or value['run_context'] is not None):
        raise ValueError('Remote transcription requires one retained source and no local worker identity.')
    audio = source(value['sources'][0])
    binding = remote_binding(value['remote'], audio)
    if value['settings'] != {'language': binding['payload']['language'],
                            'model': MODEL, 'max_attempts': 1, 'word_timestamps': True}:
        raise ValueError('Remote transcription settings changed.')
    if value['status'] not in ('ready', 'running', 'completed', 'failed', 'stopped'):
        raise ValueError('Invalid remote transcription status.')
    if (type(value['done']) is not int or type(value['failed']) is not int
            or value['done'] not in (0, 1) or value['failed'] not in (0, 1)
            or type(value['receipts']) is not list or type(value['artifacts']) is not list):
        raise ValueError('Invalid remote transcription completion counters.')
    if value['status'] != 'completed':
        if value['done'] != 0 or value['receipts'] or value['artifacts']:
            raise ValueError('Incomplete remote transcription cannot expose completed results.')
    else:
        if value['done'] != 1 or value['failed'] != 0 or len(value['receipts']) != 1 or len(value['artifacts']) != 4:
            raise ValueError('A completed remote transcription must retain one receipt and all four exports.')
        if not re.fullmatch(r'results/' + HEX + r'\.result\.json', value['receipts'][0]):
            raise ValueError('Invalid remote transcription receipt route.')
        for row in value['artifacts']:
            artifact(row, audio['filename'])
        if {row['format'] for row in value['artifacts']} != {'txt', 'srt', 'vtt', 'json'}:
            raise ValueError('Remote transcript exports are incomplete or duplicated.')
    return value


def artifact(row, name):
    fields(row, ('path', 'size_bytes', 'sha256', 'source', 'format', 'recognition_identity'))
    if (row['source'] != name or row['format'] not in ('txt', 'srt', 'vtt', 'json')
            or row['path'] != name + '.' + row['format']
            or type(row['size_bytes']) is not int or not 0 <= row['size_bytes'] <= MAX_RESULT_BYTES):
        raise ValueError('Invalid remote transcription artifact.')
    checksum(row['sha256'])
    checksum(row['recognition_identity'])


def receipt(value, *, saved_job=None):
    fields(value, ('schema', 'state', 'job', 'workspace', 'source', 'profile',
                   'profile_sha256', 'request_sha256', 'approval_sha256', 'authorization_id', 'operation_id',
                   'response_sha256', 'response_size_bytes', 'transcript', 'provenance',
                   'artifacts', 'recognition_identity', 'local', 'actual_cost'))
    if value['schema'] != RECEIPT_SCHEMA or value['state'] != 'complete':
        raise ValueError('Invalid remote transcription receipt.')
    identifier(value['job'])
    identifier(value['operation_id'])
    identifier(value['authorization_id'])
    text(value['workspace'], 200)
    audio = source(value['source'])
    profile = ProviderProfile(**value['profile'])
    if (value['profile'] != snapshot(profile) or profile.adapter != 'elevenlabs'
            or profile.model != MODEL or profile.capabilities != (OPERATION,) or profile.local):
        raise ValueError('Remote receipts cannot retain active provider configuration.')
    for key in ('profile_sha256', 'request_sha256', 'approval_sha256', 'response_sha256',
                'recognition_identity'):
        checksum(value[key])
    if (value['local'] is not False or value['actual_cost'] is not None
            or type(value['response_size_bytes']) is not int
            or not 0 < value['response_size_bytes'] <= MAX_RESPONSE_BYTES):
        raise ValueError('Remote transcript receipt misstates its provider boundary.')
    transcript = value['transcript']
    if type(transcript) is not dict or set(transcript) != {'language', 'duration', 'segments', 'text'}:
        raise ValueError('Invalid retained remote transcript.')
    if (len(transcript.get('segments', ())) > 1
            or len(transcript.get('text', '')) > MAX_TEXT_CHARACTERS):
        raise ValueError('Remote transcript exceeds its bounded synchronous contract.')
    normalized = validated_result(transcript, transcript['text'])
    if normalized != transcript or sum(len(s['words']) for s in normalized['segments']) > MAX_WORDS:
        raise ValueError('Remote transcript normalization changed.')
    provenance = value['provenance']
    fields(provenance, ('protocol', 'provider', 'model', 'endpoint', 'request_sha256',
                        'response_sha256', 'source_sha256', 'provider_transcript_id',
                        'provider_text_sha256', 'detected_language_probability',
                        'duration_basis', 'timestamp_source', 'text_normalization'))
    if (provenance['protocol'] != PROTOCOL or provenance['provider'] != profile.id
            or provenance['model'] != MODEL or provenance['endpoint'] != profile.endpoint
            or provenance['request_sha256'] != value['request_sha256']
            or provenance['response_sha256'] != value['response_sha256']
            or provenance['source_sha256'] != audio['sha256']):
        raise ValueError('Remote transcript provenance differs from its receipt.')
    checksum(provenance['provider_text_sha256'])
    finite(provenance['detected_language_probability'], 1)
    reference = provenance['provider_transcript_id']
    if reference is not None and (type(reference) is not str or not re.fullmatch('[A-Za-z0-9_.:-]{1,256}', reference)):
        raise ValueError('Invalid provider transcript reference.')
    if (provenance['duration_basis'] not in (
            'provider reported audio duration',
            'end of provider reported word timeline; source duration not measured')
            or provenance['timestamp_source'] != 'provider reported; not independently aligned'
            or provenance['text_normalization'] != 'existing transcription model trims outer segment whitespace'):
        raise ValueError('Remote transcript cannot claim measured local timing or rewritten text.')
    base = {key: content for key, content in value.items() if key not in ('artifacts', 'recognition_identity')}
    if fingerprint(base) != value['recognition_identity']:
        raise ValueError('Remote recognition identity changed.')
    if type(value['artifacts']) is not list or len(value['artifacts']) != 4:
        raise ValueError('Remote transcript exports are incomplete.')
    encoded = encoded_outputs(normalized, provenance)
    expected = [{'path': audio['filename'] + '.' + kind, 'size_bytes': len(raw),
                 'sha256': sha256(raw).hexdigest(), 'source': audio['filename'],
                 'format': kind, 'recognition_identity': value['recognition_identity']}
                for kind, raw in encoded.items()]
    if value['artifacts'] != expected:
        raise ValueError('Remote transcript exports do not match their structured text and timestamps.')
    if saved_job is not None:
        job(saved_job)
        binding = saved_job['remote']
        if (value['job'] != saved_job['id'] or value['workspace'] != saved_job['workspace']
                or value['source'] != saved_job['sources'][0]
                or any(value[key] != binding[key] for key in (
                    'profile', 'profile_sha256', 'request_sha256', 'approval_sha256', 'authorization_id', 'operation_id'))
                or value['artifacts'] != saved_job['artifacts']):
            raise ValueError('Remote result belongs to another job, recording or reviewed request.')
    return value


def make_receipt(saved_job, result):
    job(saved_job)
    binding, audio = saved_job['remote'], saved_job['sources'][0]
    if (type(result) is not dict or result.get('provider') != binding['profile']['id']
            or result.get('model') != MODEL or result.get('operation_id') != binding['operation_id']
            or result.get('request_sha256') != binding['request_sha256']
            or result.get('local') is not False or result.get('actual_cost') is not None
            or result.get('artifacts') != []):
        raise ValueError('The gateway result changed the approved remote request.')
    provenance = {key: result[key] for key in ('provider_transcript_id', 'provider_text_sha256',
        'detected_language_probability', 'duration_basis', 'timestamp_source', 'text_normalization')}
    provenance.update(protocol=PROTOCOL, provider=result['provider'], model=MODEL,
        endpoint=binding['profile']['endpoint'], request_sha256=binding['request_sha256'],
        response_sha256=result['output_sha256'], source_sha256=audio['sha256'])
    value = dict(schema=RECEIPT_SCHEMA, state='complete', job=saved_job['id'],
        workspace=saved_job['workspace'], source=audio, profile=binding['profile'],
        profile_sha256=binding['profile_sha256'], request_sha256=binding['request_sha256'],
        approval_sha256=binding['approval_sha256'], authorization_id=binding['authorization_id'],
        operation_id=binding['operation_id'],
        response_sha256=result['output_sha256'], response_size_bytes=result['size'],
        transcript=result['transcript'], provenance=provenance, local=False, actual_cost=None)
    identity = fingerprint(value)
    encoded = encoded_outputs(result['transcript'], provenance)
    value.update(recognition_identity=identity, artifacts=[
        dict(path=audio['filename'] + '.' + kind, size_bytes=len(raw), sha256=sha256(raw).hexdigest(),
             source=audio['filename'], format=kind, recognition_identity=identity)
        for kind, raw in encoded.items()])
    receipt(value)
    return value, encoded


def verify_usage(directory, saved_job, value):
    """Retained results require the exact completed existing usage reservation."""
    binding = saved_job['remote']
    root = directory.parents[4]
    expected = root / 'state' / 'transcription' / sha256(saved_job['workspace'].encode('utf-8')).hexdigest() / 'jobs' / saved_job['id']
    if directory != expected:
        raise ValueError('Remote transcript has no exact library-scoped usage route.')
    ledger = read_json(local_path(root / 'state' / 'providers' / 'usage' /
        (binding['authorization_id'] + '.usage.json'), root=root, exists=True), maximum=131072)
    fields(ledger, ('schema', 'authorization', 'currency', 'operations'))
    if (ledger['schema'] != 'carr-provider-usage/1' or ledger['authorization'] != binding['approval_sha256']
            or ledger['currency'] != 'USD' or type(ledger['operations']) is not dict
            or set(ledger['operations']) != {binding['operation_id']}):
        raise ValueError('The exact remote request usage evidence is unavailable.')
    row = ledger['operations'][binding['operation_id']]
    expected_usage = dict(submitted_audio_bytes=saved_job['sources'][0]['size_bytes'],
        transcript_sha256=sha256(value['transcript']['text'].encode('utf-8')).hexdigest(),
        provider_timestamp_count=sum(len(segment['words']) for segment in value['transcript']['segments']))
    expected_row = dict(profile=binding['profile']['id'], model=MODEL, operation=OPERATION,
        profile_sha256=binding['profile_sha256'], request_sha256=binding['request_sha256'],
        reserved_cost=binding['max_cost'], cost_kind='user-approved upper bound',
        actual_cost=None, status='complete', output_sha256=value['response_sha256'], usage=expected_usage)
    if row != expected_row:
        raise ValueError('Remote transcript differs from its completed provider usage receipt.')


def completed_job(saved_job, value):
    """A pure replay candidate; caller must verify every retained byte first."""
    result = json.loads(canonical(saved_job))
    result.update(status='completed', done=1, failed=0, artifacts=value['artifacts'],
                  receipts=['results/' + fingerprint(value) + '.result.json'], last_error='')
    receipt(value, saved_job=result)
    return result


def verify_files(directory, saved_job, *, cancelled=lambda: False, names=None):
    """Same pure source/result checks for ordinary read and copied backup.

    A crash after the immutable results directory was committed but before the
    small job checkpoint changed can be verified without repeating the call.
    This returns that candidate; it never writes or reconstructs Authorization.
    """
    from ..transcription import TranscriptionService
    job(saved_job)
    validator = object.__new__(TranscriptionService)
    TranscriptionService._sources(validator, directory, saved_job, cancelled)
    expected = {'job.json', 'input/' + saved_job['sources'][0]['filename']}
    result_dir = directory / 'results'
    recovered = saved_job
    if result_dir.exists():
        regular(local_path(result_dir, root=directory, exists=True), directory=True)
        children = []
        for child in result_dir.iterdir():
            if len(children) >= 5:
                raise ValueError('Remote transcript result inventory changed.')
            regular(child)
            children.append(child)
        receipts = [child for child in children if re.fullmatch(HEX + r'\.result\.json', child.name)]
        if len(receipts) != 1:
            raise ValueError('A remote result directory requires one exact receipt.')
        value = read_json(receipts[0], maximum=MAX_RESULT_BYTES)
        recovered = completed_job(saved_job, value)
        verify_usage(directory, saved_job, value)
        receipt_name = recovered['receipts'][0]
        if receipt_name != 'results/' + receipts[0].name:
            raise ValueError('The saved remote receipt filename changed.')
        if saved_job['status'] == 'completed' and (
                saved_job['artifacts'] != recovered['artifacts']
                or saved_job['receipts'] != recovered['receipts']):
            raise ValueError('The completed remote transcript changed.')
        expected.add(receipt_name)
        for row in value['artifacts']:
            if cancelled():
                raise InterruptedError('Remote transcript verification was cancelled.')
            name = 'results/' + row['path']
            target = local_path(directory / name, root=directory, exists=True)
            if regular(target).st_size != row['size_bytes'] or digest_file(target, cancelled=cancelled) != row['sha256']:
                raise ValueError('A remote transcript artifact changed.')
            expected.add(name)
        if {'results/' + child.name for child in children} != expected - {
                'job.json', 'input/' + saved_job['sources'][0]['filename']}:
            raise ValueError('Remote transcript results contain undeclared files.')
    elif saved_job['status'] == 'completed':
        raise ValueError('The completed remote transcript lost its result directory.')
    if names is not None:
        for name in names:
            if name in expected:
                continue
            if re.fullmatch(r'edits/' + ID + r'\.json', name):
                edit = read_json(local_path(directory / name, root=directory, exists=True))
                original = next((row for row in recovered['artifacts'] if row['format'] == 'txt'), None)
                if (original is None or edit.get('job') != saved_job['id']
                        or edit.get('source') != saved_job['sources'][0]['filename']
                        or edit.get('source_sha256') != saved_job['sources'][0]['sha256']
                        or edit.get('recognition_identity') != original['recognition_identity']
                        or edit.get('original_text_sha256') != original['sha256']):
                    raise ValueError('Remote transcript edit lost its original receipt binding.')
                continue
            raise ValueError('A remote transcription job contains undeclared worker or result data.')
        if not expected <= set(names):
            raise ValueError('Remote transcription inventory is incomplete.')
    return recovered
