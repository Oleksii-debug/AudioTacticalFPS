"""Purpose-written synthetic peer for verified-file-job/1.

No supplied source, ASR implementation, model weights, or user recordings are
included. The fixture only exercises actual subprocess and filesystem contracts.
"""
import argparse
from hashlib import sha256
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import sys
import time
from uuid import uuid4


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return sha256(canonical(value)).hexdigest()


def identity(path):
    raw = Path(path).read_bytes()
    return {'sha256': sha256(raw).hexdigest(), 'size_bytes': len(raw)}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.stage-' + uuid4().hex)
    raw = canonical(value) + b'\n'
    with temporary.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return {'path': path.name, **identity(path)}


def runtime():
    # This fixture's declared dependency graph is deliberately finite and
    # synthetic. Production closure resolution belongs to the inert inspector.
    packages = []
    for name in sorted(('faster-whisper', 'ctranslate2', 'av', 'numpy', 'onnxruntime',
                        'tokenizers', 'huggingface-hub', 'packaging', 'synthetic-helper')):
        distribution = metadata.distribution(name)
        rows = [{'path': str(item), **identity(Path(distribution.locate_file(item)).resolve())}
                for item in sorted(distribution.files, key=str)
                if '__pycache__' not in item.parts and not str(item).endswith(('.pyc', '.pyo'))]
        packages.append({'name': name, 'version': distribution.version, 'sha256': digest(rows)})
    implementation = [{'name': path.name, **identity(path)} for path in (
        Path(__file__).resolve(), Path(__file__).with_name('recovery_support.py'),
        Path(sys.executable).resolve())]
    return {'python': sys.version, 'platform': platform.platform(),
            'machine': platform.machine(), 'packages': packages, 'implementation': implementation}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job-dir', required=True)
    root = Path(parser.parse_args().job_dir)
    config = json.loads((root / 'job_config.json').read_text(encoding='utf-8'))
    options = json.loads(Path(__file__).with_name('fixture-options.json').read_text(encoding='utf-8'))
    if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode and sys.flags.utf8_mode):
        raise RuntimeError('The new protocol fixture requires isolated, no-site, UTF-8 launch.')
    if not sys.pycache_prefix:
        raise RuntimeError('Existing runtime bytecode must not enter the isolated worker.')
    with Path(__file__).with_name('fixture-starts.log').open('a', encoding='utf-8') as stream:
        stream.write('started\n')
    sources = [path for folder in config['folders'] for path in sorted(Path(folder).iterdir()) if path.is_file()]
    settings = config['settings']
    results = Path(config['results_root']) / 'synthetic-result-namespace'
    results.mkdir(parents=True, exist_ok=True)
    model = Path(config['models_dir']) / settings['model']
    model_files = [{'path': path.relative_to(model).as_posix(), **identity(path)}
                   for path in sorted(model.rglob('*')) if path.is_file()]
    selected_model = {'sha256': digest(model_files), 'files': len(model_files),
                      'size_bytes': sum(row['size_bytes'] for row in model_files)}
    installed = runtime()
    run_id = uuid4().hex
    records, artifacts = [], []
    completed = None

    def status(state, done, result_state='running'):
        value = {'state': state, 'result_state': result_state, 'run_id': run_id,
                 'done': done, 'failed': 0, 'total': len(sources), 'current': '',
                 'results_dir': str(results), 'completion': completed}
        write(root / 'job_status.json', value)

    status('обробка', 0)
    for index, source in enumerate(sources):
        deadline = time.monotonic() + options.get('delay', 0)
        while time.monotonic() < deadline:
            if not options.get('ignore_stop') and ((root / 'stop.flag').exists() or (root / 'cancel.flag').exists()):
                status('зупинено', index, 'cancelled')
                return 130
            time.sleep(.01)
        item = {'group': source.parent.name + '__' + sha256(str(source.parent).encode()).hexdigest()[:12],
                'root': str(source.parent), 'path': str(source), 'relative': source.name}
        request = {'kind': 'whisper-transcribe-request', 'version': 1,
            'source': identity(source), 'model': selected_model, 'runtime': installed,
            'recognition': {name: settings[name] for name in (
                'task', 'translation_target', 'language', 'model', 'device', 'compute_type',
                'beam_size', 'vad_filter', 'multilingual', 'condition_on_previous_text')}}
        if options.get('wrong_helper'):
            request['runtime']['implementation'][1]['sha256'] = '0' * 64
        if options.get('missing_dependency'):
            request['runtime']['packages'] = [row for row in installed['packages'] if row['name'] != 'synthetic-helper']
        if options.get('wrong_source'):
            request['source']['sha256'] = '0' * 64
        key = digest(request)
        cache_path = root / 'logs/verified-cache-v1' / key[:2] / (key + '.json')
        reused = cache_path.exists()
        text = 'Synthetic verified protocol result.'
        result = {'text': text, 'segments': [
            {'id': 0, 'seek': 0, 'start': 0.0, 'end': 1.0, 'text': text, 'words': []}],
            'detected_language': 'en' if settings['language'] == 'auto' else settings['language'],
            'duration': 1.0, 'execution': {'device': settings['device'], 'compute_type': settings['compute_type']}}
        if options.get('invalid_timing'):
            result['segments'][0]['end'] = -1.0
        cache = {'kind': 'whisper-transcribe-result', 'version': 1, 'request': request,
                 'request_sha256': key, 'result': result, 'result_sha256': digest(result)}
        write(cache_path, cache)
        if not reused:
            with Path(__file__).with_name('fixture-inferences.log').open('a', encoding='utf-8') as stream:
                stream.write(source.parent.name + '\n')
        files = []
        for suffix in ('.txt', '.json'):
            target = results / item['group'] / ('source-' + key[:16] + suffix)
            target.parent.mkdir(parents=True, exist_ok=True)
            if suffix == '.txt':
                target.write_text(text + '\n', encoding='utf-8', newline='\n')
            else:
                write(target, {'kind': 'whisper-transcribe-export', 'version': 1,
                               'source': item, 'request': request, 'result': result})
            files.append({'path': target.relative_to(results).as_posix(), **identity(target)})
        receipt = {'kind': 'whisper-transcribe-export-receipt', 'version': 1,
                   'source': item, 'request_sha256': key,
                   'settings': {name: settings[name] for name in (
                       'output_mode', 'include_headers', 'output_txt', 'output_srt', 'output_vtt', 'output_json')},
                   'files': files}
        receipt_path = root / 'logs/export-receipts' / (digest(receipt) + '.json')
        if not options.get('omit_receipt'):
            write(receipt_path, receipt)
        if options.get('tamper_output'):
            (results / files[0]['path']).write_text('Changed after sealing.', encoding='utf-8')
        records.append({'source': item, 'request_sha256': key,
                        'receipt': {'path': receipt_path.relative_to(root).as_posix(), **identity(receipt_path)}
                        if receipt_path.exists() else {'path': receipt_path.relative_to(root).as_posix(),
                                                       'sha256': '0' * 64, 'size_bytes': 0},
                        'cache_reused': reused})
        artifacts.extend(files)
        status('обробка', index + 1)
        if options.get('fail_after') == index + 1:
            return 3

    completion = {'kind': 'whisper-transcribe-completion', 'version': 1,
                  'job_name': config['job_name'], 'run_id': run_id,
                  'files': sorted(artifacts, key=lambda row: row['path']), 'inputs': records}
    if options.get('extra_completion_file'):
        extra = results / 'unrelated.txt'
        extra.write_text('Not an accepted source export.', encoding='utf-8')
        completion['files'].append({'path': extra.name, **identity(extra)})
    if options.get('old_completion_run'):
        completion['run_id'] = '0' * 32
    if options.get('duplicate_completion_input') and completion['inputs']:
        completion['inputs'].append(completion['inputs'][0])
    if not options.get('omit_completion'):
        completed = write(root / 'completion.json', completion)
    status('завершено', len(sources), 'completed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
