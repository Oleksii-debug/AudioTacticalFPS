"""Adversarial file receipts; synthetic bytes are not speech-quality evidence."""
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

from carr_library.transcription.models import settings, worker_settings
from carr_library.transcription.verified_protocol import (
    DifferentRecipe, PROTOCOL, ReceiptReader, ROOT_DISTRIBUTIONS, decode,
    model_identity, relative_name, result_directory,
)


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return sha256(wire(value)).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = wire(value) + b'\n'
    path.write_bytes(raw)
    return {'sha256': sha256(raw).hexdigest(), 'size_bytes': len(raw)}


def identity(path):
    raw = path.read_bytes()
    return {'sha256': sha256(raw).hexdigest(), 'size_bytes': len(raw)}


@pytest.fixture
def sample(tmp_path):
    worker = tmp_path / 'worker'
    results = worker / 'results/run'
    results.mkdir(parents=True)
    source = tmp_path / 'retained/source.wav'
    source.parent.mkdir()
    source.write_bytes(b'Purpose-written source identity; not acoustic input.')
    source_identity = identity(source)
    options = worker_settings(settings(), 'synthetic-model')
    profile = {'protocol': PROTOCOL,
        'model_files': {name: {'sha256': sha256(name.encode()).hexdigest(), 'size': len(name)}
                        for name in ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt')},
        'verified_packages': [{'name': name, 'version': '1.0.0',
            'sha256': sha256(name.encode()).hexdigest()}
            for name in sorted(ROOT_DISTRIBUTIONS | {'synthetic-helper'})],
        'verified_implementation': [{'name': name, 'sha256': sha256(name.encode()).hexdigest(),
            'size_bytes': len(name)} for name in ('worker.py', 'recovery_support.py', 'python.exe')]}
    inputs = {str(source): {'id': 'source-one', 'sha256': source_identity['sha256'], 'size': source_identity['size_bytes']}}
    locator = {'group': 'retained__0123456789ab', 'root': str(source.parent),
               'path': str(source), 'relative': source.name}
    model_rows = [{'path': name, 'sha256': row['sha256'], 'size_bytes': row['size']}
                  for name, row in sorted(profile['model_files'].items())]
    request = {'kind': 'whisper-transcribe-request', 'version': 1, 'source': source_identity,
        'model': {'sha256': digest(model_rows), 'files': len(model_rows),
                  'size_bytes': sum(row['size_bytes'] for row in model_rows)},
        'runtime': {'python': 'Synthetic Python diagnostic', 'platform': 'Synthetic platform',
                    'machine': 'Synthetic architecture', 'packages': deepcopy(profile['verified_packages']),
                    'implementation': deepcopy(profile['verified_implementation'])},
        'recognition': {key: options[key] for key in (
            'task', 'translation_target', 'language', 'model', 'device', 'compute_type',
            'beam_size', 'vad_filter', 'multilingual', 'condition_on_previous_text')}}
    result = {'text': 'Synthetic transcript.', 'segments': [
        {'id': 0, 'seek': 0, 'start': 0.0, 'end': 1.0, 'text': 'Synthetic transcript.', 'words': []}],
        'detected_language': 'en', 'duration': 1.0,
        'execution': {'device': 'cpu', 'compute_type': 'int8'}}
    data = {'worker': worker, 'results': results, 'source': source, 'inputs': inputs,
            'options': options, 'profile': profile, 'locator': locator,
            'request': request, 'result': result}
    data['reader'] = lambda: ReceiptReader(worker, results, inputs, options, profile)
    return data


def seal(sample):
    root, results, request, result = (sample[name] for name in ('worker', 'results', 'request', 'result'))
    key = digest(request)
    cache = {'kind': 'whisper-transcribe-result', 'version': 1,
             'request': request, 'request_sha256': key, 'result': result, 'result_sha256': digest(result)}
    cache_path = root / 'logs/verified-cache-v1' / key[:2] / (key + '.json')
    write(cache_path, cache)
    txt = results / 'source.txt'
    txt.write_bytes((result['text'] + '\n').encode('utf-8'))
    structured = results / 'source.json'
    write(structured, {'kind': 'whisper-transcribe-export', 'version': 1,
                       'source': sample['locator'], 'request': request, 'result': result})
    files = [{'path': path.name, **identity(path)} for path in (txt, structured)]
    receipt = {'kind': 'whisper-transcribe-export-receipt', 'version': 1,
        'source': sample['locator'], 'request_sha256': key, 'files': files,
        'settings': {name: sample['options'][name] for name in (
            'output_mode', 'include_headers', 'output_txt', 'output_srt', 'output_vtt', 'output_json')}}
    receipt_path = root / 'logs/export-receipts' / (digest(receipt) + '.json')
    record = {'path': receipt_path.relative_to(root).as_posix(), **write(receipt_path, receipt)}
    manifest = {'kind': 'whisper-transcribe-completion', 'version': 1, 'job_name': 'Synthetic job',
                'run_id': 'a' * 32, 'files': files,
                'inputs': [{'source': sample['locator'], 'request_sha256': key,
                            'receipt': record, 'cache_reused': False}]}
    status = {'state': 'завершено', 'result_state': 'completed', 'run_id': 'a' * 32,
              'done': 1, 'failed': 0, 'total': 1, 'results_dir': str(results),
              'completion': {'path': 'completion.json', **write(root / 'completion.json', manifest)}}
    return {'receipt': record, 'receipt_value': receipt, 'receipt_path': receipt_path,
            'cache': cache, 'cache_path': cache_path, 'manifest': manifest, 'status': status}


def reseal_completion(sample, sealed):
    sealed['status']['completion'] = {'path': 'completion.json',
        **write(sample['worker'] / 'completion.json', sealed['manifest'])}


def test_exact_file_union_and_full_identity_are_accepted(sample):
    sealed = seal(sample)
    reader = sample['reader']()
    checkpoint = reader.read_checkpoint(sealed['receipt'])
    assert checkpoint.result['text'] == 'Synthetic transcript.'
    assert checkpoint.execution == {'device': 'cpu', 'compute_type': 'int8'}
    assert tuple(reader.checkpoints()) == (checkpoint,)
    accepted, record = reader.completion(sealed['status'], run_id='a' * 32, job_name='Synthetic job')
    assert accepted == (checkpoint,) and record == sealed['status']['completion']
    assert result_directory(sample['worker'], sealed['status']) == sample['results']


@pytest.mark.parametrize('mutation', [
    'source_hash', 'source_size', 'model_hash', 'model_count_bool', 'request_version_bool',
    'language', 'beam_bool', 'worker_hash', 'helper_hash', 'python_hash',
    'missing_transitive', 'extra_package', 'package_version', 'package_hash',
])
def test_resigned_wrong_recipe_is_never_admitted(sample, mutation):
    request = sample['request']
    if mutation == 'source_hash':
        request['source']['sha256'] = '0' * 64
    elif mutation == 'source_size':
        request['source']['size_bytes'] += 1
    elif mutation == 'model_hash':
        request['model']['sha256'] = '0' * 64
    elif mutation == 'model_count_bool':
        request['model']['files'] = True
    elif mutation == 'request_version_bool':
        request['version'] = True
    elif mutation == 'language':
        request['recognition']['language'] = 'uk'
    elif mutation == 'beam_bool':
        request['recognition']['beam_size'] = True
    elif mutation.endswith('_hash') and mutation.split('_')[0] in ('worker', 'helper', 'python'):
        index = ('worker', 'helper', 'python').index(mutation.split('_')[0])
        request['runtime']['implementation'][index]['sha256'] = '0' * 64
    elif mutation == 'missing_transitive':
        request['runtime']['packages'] = [row for row in request['runtime']['packages'] if row['name'] != 'synthetic-helper']
    elif mutation == 'extra_package':
        request['runtime']['packages'].append({'name': 'zzz-unreviewed', 'version': '1.0.0', 'sha256': '0' * 64})
    elif mutation == 'package_version':
        request['runtime']['packages'][0]['version'] = '2.0.0'
    elif mutation == 'package_hash':
        request['runtime']['packages'][0]['sha256'] = '0' * 64
    sealed = seal(sample)
    with pytest.raises(ValueError):
        sample['reader']().read_checkpoint(sealed['receipt'])
    with pytest.raises(ValueError):
        sample['reader']().completion(sealed['status'])


@pytest.mark.parametrize('mutation', ['id_bool', 'seek_negative', 'end_negative',
    'text_mismatch', 'word_outside_segment', 'execution_device', 'execution_type', 'unknown_result_field'])
def test_resigned_malformed_result_is_rejected(sample, mutation):
    result = sample['result']
    if mutation == 'id_bool':
        result['segments'][0]['id'] = True
    elif mutation == 'seek_negative':
        result['segments'][0]['seek'] = -1
    elif mutation == 'end_negative':
        result['segments'][0]['end'] = -1
    elif mutation == 'text_mismatch':
        result['segments'][0]['text'] = 'Another transcript.'
    elif mutation == 'word_outside_segment':
        result['segments'][0]['words'] = [{'start': 0.0, 'end': 2.0, 'word': 'invalid'}]
    elif mutation == 'execution_device':
        result['execution']['device'] = 'cuda'
    elif mutation == 'execution_type':
        result['execution']['compute_type'] = 'float32'
    elif mutation == 'unknown_result_field':
        result['unreviewed'] = True
    sealed = seal(sample)
    with pytest.raises(ValueError):
        sample['reader']().read_checkpoint(sealed['receipt'])


@pytest.mark.parametrize('mutation', ['extra_file', 'missing_file', 'duplicate_file',
    'duplicate_input', 'wrong_run', 'wrong_job', 'locator_root', 'reuse_nonboolean'])
def test_resigned_completion_must_exactly_bind_all_files_and_inputs(sample, mutation):
    sealed = seal(sample)
    manifest = sealed['manifest']
    if mutation == 'extra_file':
        extra = sample['results'] / 'extra.txt'
        extra.write_bytes(b'Unrelated output with a valid digest.')
        manifest['files'].append({'path': extra.name, **identity(extra)})
    elif mutation == 'missing_file':
        manifest['files'].pop()
    elif mutation == 'duplicate_file':
        manifest['files'].append(deepcopy(manifest['files'][0]))
    elif mutation == 'duplicate_input':
        manifest['inputs'].append(deepcopy(manifest['inputs'][0]))
    elif mutation == 'wrong_run':
        manifest['run_id'] = 'b' * 32
    elif mutation == 'wrong_job':
        manifest['job_name'] = 'Other job'
    elif mutation == 'locator_root':
        manifest['inputs'][0]['source'] = {**manifest['inputs'][0]['source'], 'root': str(sample['worker'])}
    elif mutation == 'reuse_nonboolean':
        manifest['inputs'][0]['cache_reused'] = 1
    reseal_completion(sample, sealed)
    with pytest.raises(ValueError):
        sample['reader']().completion(sealed['status'], run_id='a' * 32, job_name='Synthetic job')


@pytest.mark.parametrize('mutation', ['done_bool', 'total_wrong', 'failed_bool', 'state_partial',
    'no_completion', 'stale_run'])
def test_terminal_status_cannot_substitute_counters_for_completion(sample, mutation):
    sealed = seal(sample)
    status = sealed['status']
    if mutation == 'done_bool':
        status['done'] = True
    elif mutation == 'total_wrong':
        status['total'] = 2
    elif mutation == 'failed_bool':
        status['failed'] = False
    elif mutation == 'state_partial':
        status['result_state'] = 'partial'
    elif mutation == 'no_completion':
        status['completion'] = None
    elif mutation == 'stale_run':
        status['run_id'] = 'b' * 32
    with pytest.raises(ValueError):
        sample['reader']().completion(status, run_id='a' * 32)


def test_recognition_cache_and_loose_text_alone_grant_no_admission(sample):
    sealed = seal(sample)
    sealed['receipt_path'].unlink()
    assert list(sample['reader']().checkpoints()) == []
    with pytest.raises(FileNotFoundError):
        sample['reader']().completion(sealed['status'])


@pytest.mark.parametrize('target', ['source', 'txt', 'receipt', 'cache', 'completion'])
def test_changed_actual_bytes_are_rejected(sample, target):
    sealed = seal(sample)
    path = {'source': sample['source'], 'txt': sample['results'] / 'source.txt',
            'receipt': sealed['receipt_path'], 'cache': sealed['cache_path'],
            'completion': sample['worker'] / 'completion.json'}[target]
    raw = path.read_bytes()
    path.write_bytes(b'X' + raw[1:])
    with pytest.raises((ValueError, UnicodeError)):
        sample['reader']().completion(sealed['status'])


def test_same_length_source_edit_with_restored_mtime_is_still_detected(sample):
    sealed = seal(sample)
    path = sample['source']
    before, raw = path.stat(), path.read_bytes()
    path.write_bytes(b'X' + raw[1:])
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match='source bytes changed'):
        sample['reader']().completion(sealed['status'])


def test_linked_output_is_rejected_on_windows_and_posix_without_skips(sample, tmp_path):
    sealed = seal(sample)
    os.link(sample['results'] / 'source.txt', tmp_path / 'second-name.txt')
    with pytest.raises(ValueError, match='regular local files'):
        sample['reader']().completion(sealed['status'])


def test_empty_transcript_remains_a_valid_sealed_result(sample):
    sample['result'].update(text='', segments=[])
    sealed = seal(sample)
    assert sample['reader']().completion(sealed['status'])[0][0].result['text'] == ''


@pytest.mark.parametrize('path', ['../outside.txt', '/absolute.txt', 'C:/drive.txt',
    'sub\\escape.txt', '.', 'folder//file.txt', 'NUL.txt', 'name.', 'name ', 'a/../b.txt'])
def test_artifact_paths_are_portable_and_confined(path):
    with pytest.raises(ValueError):
        relative_name(path)


def test_duplicate_json_keys_and_nonfinite_values_are_not_accepted():
    for raw in (b'{"version":1,"version":1}', b'{"number":NaN}', b'{"number":Infinity}'):
        with pytest.raises(ValueError):
            decode(raw)


def test_incomplete_model_pack_is_rejected_before_a_receipt_can_be_read(sample):
    del sample['profile']['model_files']['vocabulary.txt']
    with pytest.raises(ValueError, match='model inventory|incomplete'):
        sample['reader']()


def test_result_namespace_must_stay_inside_configured_output_root(sample):
    with pytest.raises(ValueError):
        result_directory(sample['worker'], {'results_dir': str(sample['source'].parent)})


def test_checkpoint_scan_uses_actual_link_count_not_cached_windows_direntry_fields(sample, monkeypatch):
    from types import SimpleNamespace
    from carr_library.transcription import verified_protocol as module
    sealed = seal(sample)
    folder = sample['worker'] / 'logs/export-receipts'
    with os.scandir(folder) as entries:
        cached = [SimpleNamespace(name=entry.name, path=entry.path,
            stat=lambda **kwargs: SimpleNamespace(st_mode=0o100600, st_nlink=0, st_file_attributes=0))
            for entry in entries]
    class Scan:
        def __enter__(self): return iter(cached)
        def __exit__(self, *args): return False
    monkeypatch.setattr(module.os, 'scandir', lambda path: Scan())
    checkpoints = tuple(sample['reader']().checkpoints())
    assert len(checkpoints) == 1 and checkpoints[0].source_id == 'source-one'
