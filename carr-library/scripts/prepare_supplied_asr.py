"""Prepare a private V4.3 worker candidate; never redistribute its source.

This reuses strict recognition preparation and repairs timestamp carry/newlines.
It is not an approved engine installer or a redistribution grant.
"""
import argparse
import ast
import hashlib
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace

# Reuse the packaged exact-source preparation in both CLI and runtime adapter.
# Running this source helper directly also works before installing the wheel.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from carr_library.audio.supplied_whisper import SOURCE_SHA256, prepare


def checks(path: Path):
    spec = importlib.util.spec_from_file_location('checked_worker', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    settings = {'language': 'auto', 'multilingual': True, 'beam_size': 5,
                'vad_filter': True, 'condition_on_previous_text': True}
    attempts = module._v43_settings_attempts(settings)
    verdicts = {'no_implicit_language_or_settings_retry':
                attempts == [('original', settings)]}
    worker = module.Worker.__new__(module.Worker)
    worker.load_model_if_needed = lambda settings: None
    item = {'path': 'synthetic.wav', 'group': 'test', 'relative': 'synthetic.wav'}
    calls = []

    def unsupported(path, **kwargs):
        calls.append(kwargs)
        if kwargs.get('multilingual'):
            raise TypeError('synthetic unsupported option')
        return iter([]), SimpleNamespace(language='uk', duration=0)

    worker.model = SimpleNamespace(transcribe=unsupported)
    try:
        worker.transcribe_one(item, settings)
    except TypeError:
        rejected = True
    else:
        rejected = False
    verdicts['unsupported_multilingual_fails_without_retry'] = rejected and len(calls) == 1
    calls.clear()

    def supported(path, **kwargs):
        calls.append(kwargs)
        return iter([]), SimpleNamespace(language='uk', duration=0)

    worker.model = SimpleNamespace(transcribe=supported)
    text, detected, _, meta = worker.transcribe_one(item, settings)
    verdicts['auto_detection_keeps_language_and_empty_result'] = (
        text == '' and detected == 'uk' and meta['selected_language'] == 'auto'
        and len(calls) == 1 and calls[0]['language'] is None)
    return verdicts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    raw = args.source.read_bytes()
    candidate = prepare(raw)
    print('source_sha256=' + hashlib.sha256(raw).hexdigest())
    print('candidate_sha256=' + hashlib.sha256(candidate).hexdigest())
    if args.verify:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, data in [('baseline', raw), ('candidate', candidate)]:
                path = root / (name + '.py')
                path.write_bytes(data)
                verdicts = checks(path)
                for test, passed in verdicts.items():
                    print(name, test, 'PASS' if passed else 'FAIL')
                if name == 'candidate' and not all(verdicts.values()):
                    raise SystemExit(1)
    if args.output:
        # Exclusive creation: do not overwrite the owner source or any other file.
        with args.output.open('xb') as stream:
            stream.write(candidate)
    if not args.verify and not args.output:
        parser.error('select --verify and/or --output')


if __name__ == '__main__':
    main()
