"""Synthetic protocol fixtures for the current assistant-to-ASR boundary.

The actual supervised child emits reviewed protocol receipts. It does not decode
audio, contain model weights, infer speech, or qualify a language. Only this test
helper substitutes the current test interpreter for an inert manifest fixture.
"""
from contextlib import contextmanager
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import sys
from unittest.mock import patch

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.assistant.transcription_tools import SelectedTranscription
from carr_library.runtime_settings import EngineProfileStore
from carr_library import transcription_service as domain_service
from carr_library.transcription_receipt import DEFAULT_SETTINGS, PROTOCOL

CHILD = r"""
import hashlib, json, os, sys, time
from pathlib import Path
def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
def digest(value):
    return hashlib.sha256(value).hexdigest()
def write(path, value):
    stage = path.with_name(path.name + '.synthetic-part')
    stage.write_text(canonical(value), encoding='utf-8')
    os.replace(stage, path)
job = Path(sys.argv[sys.argv.index('--job-dir') + 1])
config_path = job / 'job_config.json'
config = json.loads(config_path.read_text(encoding='utf-8'))
settings = config['settings']
runtime = Path(config['runtime_manifest'])
manifest = json.loads(runtime.read_text(encoding='utf-8'))
prefix = manifest['settings']['models_dir'] + '/' + settings['model'] + '/'
model = {name[len(prefix):]: checksum for name, checksum in manifest['files'].items() if name.startswith(prefix)}
model_sha = digest(canonical(model).encode('utf-8'))
mode = (Path(__file__).parent / 'behavior.txt').read_text(encoding='ascii')
protocol = 'supplied-whisper-v43-hardened-1'
status = dict(protocol=protocol, run_id=config['run_id'], state='processing', done=0,
              failed=0, total=len(config['inputs']), active_files=[])
write(job / 'job_status.json', status)
if mode == 'wait':
    while not (job / 'stop.flag').exists():
        time.sleep(.01)
    status['state'] = 'зупинено'
    write(job / 'job_status.json', status)
    raise SystemExit(130)
if mode == 'status_race':
    time.sleep(.25)
if mode == 'held_status':
    deadline = time.monotonic() + 10
    while not (job / 'reader-held.flag').is_file():
        if time.monotonic() >= deadline:
            raise RuntimeError('Synthetic live-status reader was not opened.')
        time.sleep(.01)
    (job / 'reader-held.flag').unlink()
if mode == 'zero_only':
    status.update(state='завершено', done=len(config['inputs']))
    write(job / 'job_status.json', status)
    raise SystemExit(0)
owner = dict(job_dir=str(job), protocol=protocol)
root = Path(config['results_root']) / 'verified-v43' / digest(canonical(owner).encode('utf-8'))[:24]
root.mkdir(parents=True, exist_ok=True)
write(root / 'owner.json', dict(schema='supplied-whisper-v43-integrity/1/owner',
    payload=owner, sha256=digest(canonical(owner).encode('utf-8'))))
outputs = []
for format in ('txt', 'srt', 'vtt', 'json'):
    if not settings['output_' + format]:
        continue
    groups = [[index] for index in range(len(config['inputs']))]
    if format == 'txt' and settings['output_mode'] == 'one_whole_job':
        groups = [list(range(len(config['inputs'])))]
    elif format == 'txt' and settings['output_mode'] == 'one_per_folder':
        grouped = {}
        for index, row in enumerate(config['inputs']):
            grouped.setdefault(row['root'], []).append(index)
        groups = list(grouped.values())
    for number, indices in enumerate(groups):
        relative = 'output-' + str(number) + '.' + format
        data = ('Synthetic protocol text; не реальна транскрипція. ' + str(indices) + '\n').encode('utf-8')
        if mode == 'silence':
            data = b''
        (root / relative).write_bytes(data)
        outputs.append(dict(path=relative, format=format, source_indexes=indices,
                            bytes=len(data), sha256=digest(data)))
if mode == 'nonzero':
    raise SystemExit(3)
payload = dict(run_id=config['run_id'], job_root=str(job),
    job_config_sha256=digest(config_path.read_bytes()), results_root=str(root),
    output_settings={key: settings[key] for key in
        ('output_mode','include_headers','output_txt','output_srt','output_vtt','output_json')},
    inputs=[dict(row, effective_settings=settings, model_sha256=model_sha,
                 runtime_sha256=config['runtime_manifest_sha256']) for row in config['inputs']],
    outputs=outputs)
receipt = dict(schema=protocol, payload=payload, sha256=digest(canonical(payload).encode('utf-8')))
receipt_path = job / 'logs' / 'completion-v1.json'
write(receipt_path, receipt)
status.update(state='завершено', done=len(config['inputs']),
              completion_sha256=digest(receipt_path.read_bytes()))
write(job / 'job_status.json', status)
if mode == 'tamper':
    (root / outputs[0]['path']).write_bytes(b'changed after receipt')
"""


@contextmanager
def protocol_interpreter():
    """Keep manifest/worker validation and the real process owner in production."""
    original = domain_service.review_runtime
    def selected(*args, **kwargs):
        reviewed = original(*args, **kwargs)
        return replace(reviewed, interpreter=Path(sys.executable))
    with patch.object(domain_service, 'review_runtime', selected):
        yield


@dataclass
class Environment:
    root: Path
    app: object
    source: Path
    manifest: Path
    outputs: Path
    settings: dict
    profile: object
    scope: object
    adapter: object
    service: object

    def plan(self, *, name='transcript', settings=None):
        args = {'path': str(self.source), 'source_sha256': sha256(self.source.read_bytes()).hexdigest(),
                'name': name, 'settings': dict(self.settings if settings is None else settings)}
        return Plan(self.scope, (Step('audio.transcribe', args),))

    def submit(self, *, name='transcript', settings=None):
        return self.service.submit(self.plan(name=name, settings=settings))


def open_environment(root, *, current=lambda: True):
    root = Path(root)
    app = LibraryApp(root / 'library')
    profiles = EngineProfileStore(app.root).snapshot().profiles
    if len(profiles) != 1:
        raise ValueError('The fixture needs its one explicitly registered profile.')
    profile = profiles[0]
    source, outputs = root / 'input' / 'Джерело ü.mp3', root / 'output'
    settings = json.loads((root / 'fixture-settings.json').read_text(encoding='utf-8'))
    scope = Scope(input_roots=(source.parent,), output_root=outputs,
                  max_files=16, max_bytes=16 * 1024**2, max_seconds=45)
    adapter = SelectedTranscription(app, profile.id, current=current)
    return Environment(root, app, source, root / 'runtime' / 'manifest.json',
                       outputs, settings, profile, scope, adapter,
                       AssistantService(app, transcription=adapter))


def make_environment(root, *, mode='success', settings=None, current=lambda: True):
    root = Path(root)
    root.mkdir()
    runtime = root / 'runtime'
    runtime.mkdir()
    assets = {
        'python-fixture': b'Inert interpreter fixture; only the test context selects sys.executable.',
        'worker.py': CHILD.encode('utf-8'),
        'behavior.txt': mode.encode('ascii'),
        'models/small/model.bin': b'SYNTHETIC MODEL BYTES, NOT WEIGHTS',
        'models/small/config.json': b'{}',
        'models/small/tokenizer.json': b'{}',
    }
    for name, raw in assets.items():
        path = runtime / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    manifest = {
        'schema_version': 1, 'engine': 'supplied-whisper',
        'model_id': 'assistant-synthetic-protocol-peer', 'revision': 'a' * 40,
        'license': 'TEST FIXTURE', 'license_accepted': True, 'trusted_weights': True,
        'files': {name: sha256(raw).hexdigest() for name, raw in assets.items()},
        'settings': {'protocol': PROTOCOL, 'interpreter': 'python-fixture',
            'worker': 'worker.py', 'models_dir': 'models', 'model': 'small',
            'languages': ['uk', 'en'], 'devices': ['cpu'], 'compute_types': ['int8'],
            'tasks': ['transcribe', 'translate']},
    }
    manifest_path = runtime / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    (root / 'input').mkdir()
    (root / 'input' / 'Джерело ü.mp3').write_bytes(
        b'Synthetic retained input only; no audio decoding or inference is performed.')
    (root / 'output').mkdir()
    selected = {**DEFAULT_SETTINGS, 'output_txt': True, 'output_srt': True,
                'output_vtt': True, 'output_json': True, **(settings or {})}
    (root / 'fixture-settings.json').write_text(json.dumps(selected), encoding='utf-8')
    app = LibraryApp(root / 'library')
    EngineProfileStore(app.root).register(manifest_path,
        name='Synthetic protocol fixture; no model inference',
        expected_sha256=sha256(manifest_path.read_bytes()).hexdigest(), enabled=True)
    return open_environment(root, current=current)
