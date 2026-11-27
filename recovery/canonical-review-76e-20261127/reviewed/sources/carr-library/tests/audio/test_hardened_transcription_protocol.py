"""Authored protocol records; no private worker source or model is distributed."""
import copy
import hashlib
import json
from pathlib import Path
import runpy

import pytest

from carr_library.audio import transcription as asr
from carr_library.audio import transcription_protocol as protocol
from carr_library.durable_state import atomic_json


# An authored protocol peer, not the private supplied worker or an ASR model.
# Its child performs a dependency import and emits literal fixture text only.
SPAWN_WORKER = r'''
import hashlib, json, multiprocessing, os, platform, socket, sys
from pathlib import Path
import faster_whisper

def digest(value):
    return hashlib.sha256((json.dumps(value, ensure_ascii=False, sort_keys=True,
        indent=2, allow_nan=False) + '\n').encode('utf-8')).hexdigest()

def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def child(connection):
    denied = False
    try:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        denied = True
    connection.send({'child_pid': os.getpid(), 'parent_pid': os.getppid(),
        'marker': faster_whisper.marker, 'network_denied': denied,
        'isolated': sys.flags.isolated, 'no_site': sys.flags.no_site,
        'utf8': sys.flags.utf8_mode, 'no_bytecode': sys.dont_write_bytecode,
        'text': 'Тестовий запис'})
    connection.close()

class Worker:
    def __init__(self, job):
        self.job = job
    def run(self):
        context = multiprocessing.get_context('spawn')
        receive, send = context.Pipe()
        process = context.Process(target=child, args=(send,))
        process.start()
        send.close()
        try:
            if not receive.poll(10):
                raise RuntimeError('The authored spawn child did not answer.')
            proof = receive.recv()
            process.join(10)
            assert process.exitcode == 0 and proof['parent_pid'] == os.getpid()
        finally:
            receive.close()
            if process.is_alive():
                process.terminate()
                process.join(5)
        template = json.loads((Path(faster_whisper.__file__).parent.parent /
            'hardened-fixture.json').read_text(encoding='utf-8'))
        config = json.loads((self.job / 'job_config.json').read_text(encoding='utf-8'))
        settings = config['settings']
        source = next(Path(config['folders'][0]).iterdir())
        assert source.read_text(encoding='utf-8') == proof['text']
        source_record = {'path': str(source), 'sha256': file_hash(source), 'bytes': source.stat().st_size}
        cache, status, terminal = (template[name] for name in ('cache', 'status', 'terminal'))
        identity, result = cache['identity'], cache['result']
        identity['source'] = source_record
        identity['recognition'] = {name: settings[name] for name in identity['recognition']}
        runtime = identity['runtime']
        runtime.update(python=sys.version, platform=platform.platform(),
            executable_sha256=file_hash(sys.executable), worker_sha256=file_hash(__file__))
        runtime['sha256'] = digest({name: value for name, value in runtime.items() if name != 'sha256'})
        model = Path(config['models_dir']) / settings['model']
        files = [{'path': path.relative_to(model).as_posix(), 'sha256': file_hash(path),
                  'bytes': path.stat().st_size} for path in sorted(model.rglob('*')) if path.is_file()]
        identity['model'] = {'files': files, 'sha256': digest(files)}
        result.update(source=source_record, settings=identity['recognition'],
            warnings=['Authored spawn proof: ' + json.dumps(proof, ensure_ascii=False, sort_keys=True)])
        cache.update(cache_key=digest(identity), result_sha256=digest(result))
        results = self.job / 'results'
        results.mkdir()
        artifacts = []
        for format, text in template['exports'].items():
            if not settings['output_' + format]:
                continue
            raw = (json.dumps(result, ensure_ascii=False) + '\n').encode('utf-8') if format == 'json' else text.encode('utf-8')
            path = results / ('record.part.1.' + format)
            path.write_bytes(raw)
            artifacts.append({'path': path.name, 'format': format, 'bytes': len(raw),
                              'sha256': hashlib.sha256(raw).hexdigest()})
        status.update(job_id=config['job_id'], results_dir=str(results))
        terminal.update(job_id=config['job_id'], results_dir=str(results), artifacts=artifacts)
        terminal['files'][0].update(source=source_record, cache_key=cache['cache_key'], artifacts=artifacts)
        def write(path, value):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8'))
        write(self.job / 'logs' / 'cache-v1' / cache['cache_key'][:2] / (cache['cache_key'] + '.json'), cache)
        write(self.job / 'job_status.json', status)
        write(self.job / 'job_result.json', terminal)
        return terminal
'''


@pytest.fixture
def receipt_case(tmp_path):
    stage = tmp_path / 'stage'
    stage.mkdir()
    source = stage / 'input' / 'source.wav'
    source.parent.mkdir()
    source.write_bytes(b'Authored test input, not acoustic inference.')
    item = {'file': 'source.wav', 'bytes': source.stat().st_size,
            'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
    settings = asr.TranscriptionSettings(language='uk').worker('small')
    model = tmp_path / 'models' / 'small'
    model.mkdir(parents=True)
    (model / 'model.bin').write_bytes(b'Authored fixture, never loaded.')
    model_sha = hashlib.sha256((model / 'model.bin').read_bytes()).hexdigest()
    manifest = {'python_sha256': 'a' * 64, 'models_directory': str(model.parent),
                'model': 'small', 'model_files': {'model.bin': model_sha}}
    runtime = {'python': 'Fixture Python', 'platform': 'Fixture OS',
               'executable_sha256': manifest['python_sha256'], 'worker_sha256': asr.HARDENED_SHA256,
               'packages': [{'name': name, 'version': '0.0.0', 'sha256': 'b' * 64}
                            for name in protocol.DISTRIBUTIONS]}
    runtime['sha256'] = protocol.worker_digest(runtime)
    actual = {'device': 'cpu', 'compute_type': 'int8', 'effective_parallel_files': 1}
    recognition = {name: settings[name] for name in protocol.RECOGNITION_KEYS}
    source_record = {'path': str(source), 'sha256': item['sha256'], 'bytes': item['bytes']}
    files = [{'path': 'model.bin', 'sha256': model_sha, 'bytes': (model / 'model.bin').stat().st_size}]
    identity = {'protocol': asr.HARDENED_PROTOCOL, 'source': source_record, 'runtime': runtime,
                'model': {'files': files, 'sha256': protocol.worker_digest(files)},
                'actual_runtime': actual, 'recognition': recognition}
    result = {'source': source_record, 'selected_language': 'uk', 'detected_language': 'uk',
              'settings': recognition, 'attempt': 'requested-settings', 'attempt_no': 1,
              'actual_runtime': actual, 'duration': 1.0, 'text': 'Тестовий запис',
              'segments': [{'start': 0.0, 'end': 1.0, 'text': 'Тестовий запис'}], 'warnings': []}
    status = {'schema_version': 1, 'protocol': asr.HARDENED_PROTOCOL, 'job_id': 'c' * 64,
              'run_id': 'd' * 32, 'state': 'завершено', 'phase': 'terminal',
              'result_state': 'succeeded', 'done': 1, 'total': 1, 'failed': 0,
              'active_count': 0, 'effective_parallel_files': 1, 'results_dir': str(stage / 'results')}
    cache = {'schema_version': 1, 'identity': identity, 'result': result}
    record = {'source': source_record, 'state': 'succeeded', 'error': None,
              'recognition_reused': False, 'warnings': []}
    terminal = {key: value for key, value in status.items() if key not in ('result_state', 'phase', 'active_count')}
    terminal.update(state='succeeded', files=[record], errors=[])
    case = {'stage': stage, 'source': source, 'item': item, 'settings': settings,
            'manifest': manifest, 'status': status, 'cache': cache, 'terminal': terminal}

    def seal():
        cache['cache_key'] = protocol.worker_digest(cache['identity'])
        cache['result_sha256'] = protocol.worker_digest(cache['result'])
        record['cache_key'] = cache['cache_key']
        payloads = asr._outputs(asr._normalized_result(result, settings))
        payloads['json'] = (json.dumps(result, ensure_ascii=False) + '\n').encode()
        artifacts = []
        for format, raw in payloads.items():
            path = stage / 'results' / ('record.part.1.' + format)
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(raw)
            artifacts.append({'path': path.name, 'format': format, 'bytes': len(raw),
                              'sha256': hashlib.sha256(raw).hexdigest()})
        record['artifacts'] = artifacts
        terminal['artifacts'] = copy.deepcopy(artifacts)
        cache_path = stage / 'logs' / 'cache-v1' / cache['cache_key'][:2] / (cache['cache_key'] + '.json')
        atomic_json(cache_path, cache, replace=True)
        atomic_json(stage / 'job_result.json', terminal, replace=True)
    case['seal'] = seal
    seal()
    return case


def accept(case):
    return protocol.verify_hardened_result(case['stage'], case['source'], case['item'],
        case['settings'], case['manifest'], case['status'], job_id=case['status']['job_id'],
        worker_sha256=asr.HARDENED_SHA256)


def test_fresh_consistent_receipts_keep_full_dotted_output_names(receipt_case):
    result, outputs = accept(receipt_case)
    assert result['text'] == 'Тестовий запис'
    assert set(outputs) == set(asr.FORMATS)
    assert outputs['txt'].name == 'record.part.1.txt'


def test_actual_hardened_bootstrap_spawns_isolated_child_and_commits_verified_formats(
        receipt_case, tmp_path, monkeypatch):
    from carr_library.audio import supplied_whisper
    from carr_library.audio.models import AudioBudget
    from carr_library.durable_state import digest_file, read_json
    helpers = runpy.run_path(str(Path(__file__).with_name('test_supplied_transcription.py')))
    manifest_path = helpers['installation'].__wrapped__(tmp_path, monkeypatch)
    manifest = read_json(manifest_path)
    worker = Path(manifest['worker'])
    worker.write_bytes(SPAWN_WORKER.encode('utf-8'))
    monkeypatch.setattr(supplied_whisper, 'HARDENED_SHA256', digest_file(worker))
    monkeypatch.setattr(asr, 'HARDENED_SHA256', digest_file(worker))
    runtime = Path(manifest['runtime_directory'])
    fixture = runtime / 'hardened-fixture.json'
    template = {name: receipt_case[name] for name in ('cache', 'status', 'terminal')}
    template['exports'] = {format: (receipt_case['stage'] / 'results' /
        ('record.part.1.' + format)).read_text(encoding='utf-8') for format in asr.FORMATS}
    atomic_json(fixture, template)
    manifest['runtime_files'][fixture.name] = digest_file(fixture)
    atomic_json(manifest_path, manifest, replace=True)
    service = asr.TranscriptionService(tmp_path / 'state', asr.SuppliedWhisperRuntime(manifest_path),
        budget=AudioBudget(timeout=30, reserve_disk_bytes=0, max_output_bytes=4 * 1024**2))
    source = tmp_path / 'authored.wav'
    source.write_text('Тестовий запис', encoding='utf-8')
    identity = service.create([source], settings=asr.TranscriptionSettings(language='uk'))
    complete = service.run(identity)
    assert complete['status'] == 'complete'
    directory = service.root / identity / 'outputs' / complete['completed'][0]
    result = read_json(directory / 'transcript.json')
    assert result['worker_protocol'] == asr.HARDENED_PROTOCOL
    assert (directory / 'transcript.txt').read_text(encoding='utf-8') == 'Тестовий запис\n'
    assert {path.suffix[1:] for path in directory.glob('transcript.*')} == set(asr.FORMATS)
    proof = json.loads(result['warnings'][0].removeprefix('Authored spawn proof: '))
    assert proof['child_pid'] != proof['parent_pid'] and proof['marker'] == 'reviewed dependency'
    assert proof['isolated'] == proof['no_site'] == proof['utf8'] == 1
    assert proof['no_bytecode'] is True and proof['network_denied'] is True
    assert not list(runtime.rglob('*.pyc')) and not service.broker.snapshot()['leases']
    assert not list((service.root / identity).glob('.pending-asr-*'))


@pytest.mark.parametrize('field,value', [('run_id', 'e' * 32), ('job_id', 'f' * 64),
    ('state', 'partial'), ('done', 0), ('failed', 1), ('effective_parallel_files', 2)])
def test_terminal_mismatch_cannot_accept_export_bytes(receipt_case, field, value):
    receipt_case['terminal'][field] = value
    receipt_case['seal']()
    with pytest.raises(ValueError):
        accept(receipt_case)


@pytest.mark.parametrize('change', ['runtime_worker', 'model', 'settings', 'device', 'attempt'])
def test_rehashed_receipt_still_cannot_change_selected_inference(receipt_case, change):
    identity = receipt_case['cache']['identity']
    if change == 'runtime_worker':
        runtime = identity['runtime']
        runtime['worker_sha256'] = '0' * 64
        runtime['sha256'] = protocol.worker_digest({k: v for k, v in runtime.items() if k != 'sha256'})
    elif change == 'model':
        identity['model']['files'][0]['sha256'] = '0' * 64
        identity['model']['sha256'] = protocol.worker_digest(identity['model']['files'])
    elif change == 'settings':
        identity['recognition']['beam_size'] = 9
    elif change == 'device':
        identity['actual_runtime']['device'] = 'cuda'
    else:
        receipt_case['cache']['result']['attempt'] = 'fallback-language'
    receipt_case['seal']()
    with pytest.raises(ValueError):
        accept(receipt_case)


def test_artifact_cannot_escape_its_own_stage_even_with_matching_checksum(receipt_case):
    terminal = receipt_case['terminal']
    terminal['files'][0]['artifacts'][0]['path'] = '../outside.txt'
    terminal['artifacts'][0]['path'] = '../outside.txt'
    atomic_json(receipt_case['stage'] / 'job_result.json', terminal, replace=True)
    with pytest.raises(ValueError, match='outside'):
        accept(receipt_case)


def test_changed_export_is_not_accepted_from_stale_hash(receipt_case):
    (receipt_case['stage'] / 'results' / 'record.part.1.txt').write_text('changed')
    with pytest.raises(ValueError, match='identity'):
        accept(receipt_case)


def test_missing_output_and_json_receipt_cannot_claim_complete(receipt_case):
    (receipt_case['stage'] / 'results' / 'record.part.1.json').unlink()
    with pytest.raises((ValueError, OSError)):
        accept(receipt_case)
