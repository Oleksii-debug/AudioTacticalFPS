"""Synthetic file-protocol peer. This fixture performs no speech recognition."""
import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import sys
import time


def raw(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def identity(path):
    data = Path(path).read_bytes()
    return {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_bytes(raw(value))
    # Match durable_state's bounded Windows reader/replace collision policy.
    # Keep the one complete stage; never unlink or truncate the old status.
    delays = iter((0.01, 0.02, 0.04, 0.08, 0.16, 0.32))
    while True:
        try:
            os.replace(temporary, path)
            return
        except PermissionError as exc:
            delay = next(delays, None)
            if getattr(exc, 'winerror', None) not in (5, 32, 33) or delay is None:
                raise
            time.sleep(delay)


def runtime():
    packages = {}
    for name in ('faster-whisper', 'ctranslate2', 'av', 'onnxruntime', 'numpy', 'huggingface-hub', 'tokenizers'):
        distribution = metadata.distribution(name)
        files = {str(entry): identity(Path(distribution.locate_file(entry)).resolve())
                 for entry in sorted(distribution.files, key=str)
                 if '__pycache__' not in entry.parts and not str(entry).endswith(('.pyc', '.pyo'))}
        packages[name] = {'version': distribution.version, 'file_count': len(files),
                          'installed_files_sha256': hashlib.sha256(raw(files)).hexdigest()}
    return {'executable': identity(Path(sys.executable).resolve()),
            'worker': identity(Path(__file__).resolve()), 'packages': packages, 'fixture': True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job-dir', required=True)
    directory = Path(parser.parse_args().job_dir)
    config = json.loads((directory / 'job_config.json').read_text(encoding='utf-8'))
    options_file = Path(__file__).with_name('fixture-options.json')
    options = json.loads(options_file.read_text(encoding='utf-8')) if options_file.exists() else {}
    if options.get('require_utf8') and not sys.flags.utf8_mode:
        raise RuntimeError('The external protocol process requires explicit UTF-8 mode.')
    if options.get('attempt_network'):
        import socket
        socket.socket()  # The real bootstrap must reject construction before any connection.
    if options.get('outside_write'):
        Path(options['outside_write']).write_bytes(b'Forbidden worker output')
    if options.get('import_probe'):
        from importlib.util import module_from_spec, spec_from_file_location
        probe = Path(options['import_probe'])
        spec = spec_from_file_location('synthetic_bytecode_probe', probe)
        loaded = module_from_spec(spec)
        spec.loader.exec_module(loaded)
        write(directory / 'bytecode-probe.json',
              {'dont_write_bytecode': sys.dont_write_bytecode, 'value': loaded.VALUE})
    if options.get('diagnostic'):
        print(options['diagnostic'], flush=True)
        print(options['diagnostic'], file=sys.stderr, flush=True)
    observation = directory / 'logs' / 'progress.log'
    observation.parent.mkdir(parents=True, exist_ok=True)
    with observation.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'fixture_event': 'started'}) + '\n')
    sources = [path for folder in config['folders'] for path in sorted(Path(folder).glob('*')) if path.is_file()]
    settings = config['settings']
    model_root = Path(config['models_dir']) / settings['model']
    model_files = {path.relative_to(model_root).as_posix(): identity(path)
                   for path in sorted(model_root.rglob('*')) if path.is_file()}
    installed = runtime()
    status_path = directory / 'job_status.json'

    def status(state, done=0, failed=0):
        write(status_path, {'state': state, 'done': done, 'failed': failed,
                            'total': len(sources), 'current': 'Synthetic protocol fixture'})

    status('обробка')
    if options.get('flood'):
        sys.stdout.write('x' * 200000)
        sys.stdout.flush()
    if options.get('malformed_status'):
        write(status_path, {'state': 'обробка', 'done': -1, 'failed': 0, 'total': len(sources)})
        time.sleep(2)
        return
    for number, source in enumerate(sources):
        deadline = time.monotonic() + options.get('delay', 0)
        while time.monotonic() < deadline:
            if (directory / 'stop.flag').exists() and not options.get('ignore_stop'):
                status('зупинено', number)
                return
            time.sleep(.02)
        if (directory / 'stop.flag').exists() and not options.get('ignore_stop'):
            status('зупинено', number)
            return
        text = 'Синтетичний протокольний результат.'
        segments = [{'start': 59.9996, 'end': 61.0, 'text': text, 'words': []}]
        meta = {'text': text, 'detected_language': 'uk', 'duration': 61.0,
                'segments': segments, 'settings': settings, 'effective_settings': settings}
        source_identity = {'path': str(source), **identity(source)}
        if options.get('wrong_source'):
            source_identity['sha256'] = '0' * 64
        if options.get('wrong_language'):
            meta['effective_settings'] = {**settings, 'language': 'ru'}
        if options.get('invalid_timing'):
            meta['segments'][0]['end'] = -1
        bound = {'schema': 'synthetic-file-job/1', 'source': source_identity,
                 'group': source.parent.name, 'relative': source.name,
                 'model': {'name': model_root.name, 'root': str(model_root), 'files': model_files},
                 'runtime': installed, 'settings': settings}
        key = hashlib.sha256(raw(bound)).hexdigest()
        folder = directory / 'logs' / 'cache' / key[:2]
        folder.mkdir(parents=True, exist_ok=True)
        txt, structured = folder / (key + '.txt'), folder / (key + '.json')
        txt.write_text(text + '\n', encoding='utf-8')
        structured.write_bytes(raw(meta))
        write(folder / (key + '.cache.json'), {'identity': bound, 'text': identity(txt), 'meta': identity(structured)})
        with observation.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'fixture_event': 'source', 'source_id': source.parent.name}) + '\n')
        status('обробка', number + 1)
        deadline = time.monotonic() + options.get('hold_after_checkpoint', 0)
        while time.monotonic() < deadline:
            if (directory / 'stop.flag').exists() and not options.get('ignore_stop'):
                status('зупинено', number + 1)
                return
            time.sleep(.02)
        if options.get('fail_after') == number + 1:
            raise SystemExit(3)
    status('завершено', len(sources))
    if options.get('exit_failure'):
        raise SystemExit(4)


if __name__ == '__main__':
    main()
