"""Verify the exact private worker with synthetic SDK/audio, never real inference.

Pass the locally held original worker. Nothing is downloaded or redistributed.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import wave

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.durable_state import digest_file, encode
from carr_library.audio.transcription import _RUNTIME_PACKAGES
from carr_library.integrations.supplied_transcription import (
    SuppliedTranscriptionAdapter, SuppliedWorkerInstallation, prepare_worker)


SDK = '''from pathlib import Path
from types import SimpleNamespace
class WhisperModel:
 def __init__(self, model, **kwargs):
  assert Path(model).is_absolute() and Path(model).is_dir()
  assert kwargs['local_files_only'] is True and kwargs['num_workers'] == 1
 def transcribe(self, source, **kwargs):
  assert kwargs['task'] == 'transcribe'
  if kwargs['language'] is None:
   raise TypeError('synthetic unsupported detection; preserve failure')
  if kwargs['language'] == 'en':
   original = Path.write_text
   def denied(path, *args, **kw):
    if path.suffix == '.srt':
     raise PermissionError('synthetic required-export failure')
    return original(path, *args, **kw)
   Path.write_text = denied
  segment = SimpleNamespace(id=0, seek=0, start=0.0, end=1.0,
                            text='Український протокол', words=None)
  return iter([segment]), SimpleNamespace(language=kwargs['language'], duration=1.0)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', required=True, type=Path)
    args = parser.parse_args()
    worker = args.worker.absolute()
    prepared = prepare_worker(worker.read_bytes())
    evidence = {'source_sha256': digest_file(worker),
        'prepared_sha256': sha256(prepared).hexdigest(), 'scenarios': [],
        'real_model_inference': 'NOT_RUN', 'audio_quality': 'NOT_RUN'}
    with tempfile.TemporaryDirectory(prefix='resonance-asr-protocol-') as directory:
        root = Path(directory)
        packages = root / 'packages'
        (packages / 'faster_whisper').mkdir(parents=True)
        (packages / 'faster_whisper/__init__.py').write_text(SDK, encoding='utf-8')
        for name in _RUNTIME_PACKAGES:
            if name != 'faster_whisper':
                (packages / name).mkdir()
                (packages / name / '__init__.py').write_text('# Authored empty protocol fixture.\n', encoding='utf-8')
        model = root / 'model'
        model.mkdir()
        for name in ('config.json', 'model.bin', 'tokenizer.json'):
            (model / name).write_bytes(b'{}')
        def inventory(path):
            return {f.relative_to(path).as_posix(): {'size': f.stat().st_size,
                'sha256': digest_file(f)} for f in path.rglob('*') if f.is_file()}
        python = Path(sys.executable).resolve()
        manifest = root / 'installation.json'
        manifest.write_bytes(encode({'schema': 1, 'engine': 'supplied-whisper-v43',
            'python_sha256': digest_file(python),
            'python_version': '.'.join(map(str, sys.version_info[:3])),
            'runtime_files': inventory(packages), 'model_files': inventory(model),
            'runtime_notice': 'Synthetic protocol fixture, no recognition engine.',
            'model_notice': 'Synthetic empty files, no model or weights rights claim.'}))
        installation = SuppliedWorkerInstallation(python, worker, packages, model,
                                                 manifest, digest_file(manifest),
                                                 runtime_reviewed=True, rights_reviewed=True)
        app = LibraryApp(root / 'data')
        output, inputs = root / 'outputs', root / 'inputs'
        output.mkdir()
        inputs.mkdir()
        source = inputs / 'synthetic.wav'
        with wave.open(str(source), 'wb') as stream:
            stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            stream.writeframes(b'\0\0' * 16000)
        service = AssistantService(app, transcription=SuppliedTranscriptionAdapter(installation))
        scope = Scope(input_roots=(inputs,), output_root=output,
                      max_seconds=30, max_bytes=1024 * 1024)
        for language, name, expected in [('uk', 'selected', 'complete'),
                ('auto', 'auto-error', 'failed'), ('en', 'required-export-error', 'failed')]:
            plan = Plan(scope, (Step('audio.transcribe', {'path': str(source),
                'name': name, 'language': language}),))
            report = service.run(service.submit(plan))
            passed = report.status == expected
            if expected == 'complete':
                passed = passed and len(list((output / name).iterdir())) == 4
                passed = passed and (output / name / 'transcript.txt').read_text(
                    encoding='utf-8') == 'Український протокол\n'
            else:
                passed = passed and not (output / name).exists()
            evidence['scenarios'].append({'name': name, 'status': report.status,
                                          'result': 'PASS' if passed else 'FAIL'})
    evidence['result'] = ('PASS' if all(row['result'] == 'PASS'
                                      for row in evidence['scenarios']) else 'FAIL')
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if evidence['result'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
