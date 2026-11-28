"""Build a real isolated Python process with synthetic ASR metadata only.

No dependency or model is downloaded. The protocol peer never imports an ASR
engine. Tests therefore prove file/process orchestration, not acoustic quality.
"""
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import venv


def make_runtime(directory, *, options=None):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    environment = root / 'python-environment'
    venv.EnvBuilder(with_pip=False, symlinks=os.name != 'nt').create(environment)
    python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    site = environment / 'Lib/site-packages' if os.name == 'nt' else next((environment / 'lib').glob('python*/site-packages'))
    dependencies = {}
    for name in ('faster-whisper', 'ctranslate2', 'av', 'onnxruntime', 'numpy', 'huggingface-hub', 'tokenizers'):
        module = 'synthetic_' + name.replace('-', '_') + '.py'
        payload = site / module
        payload.write_text('# Synthetic installed file; never an inference implementation.\n', encoding='utf-8')
        info = site / (name.replace('-', '_') + '-0.0.synthetic.dist-info')
        info.mkdir()
        (info / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: {name}\nVersion: 0.0.synthetic\n', encoding='utf-8')
        (info / 'RECORD').write_text('\n'.join([module + ',,', info.name + '/METADATA,,', info.name + '/RECORD,,']) + '\n', encoding='utf-8')
        dependencies[name] = payload
    worker = root / 'protocol_worker.py'
    shutil.copyfile(Path(__file__).with_name('fixtures') / 'protocol_worker.py', worker)
    options_path = worker.with_name('fixture-options.json')
    options_path.write_text(json.dumps(options or {}), encoding='utf-8')
    model = root / 'models' / 'synthetic-model'
    model.mkdir(parents=True)
    for name in ('model.bin', 'config.json', 'tokenizer.json'):
        (model / name).write_bytes(b'Synthetic model identity only; never loaded for inference. ' + name.encode())
    return SimpleNamespace(root=root, python=python, worker=worker, model=model,
                           options=options_path, dependencies=dependencies, site=site)


def register(service, runtime, *, trusted=True):
    inspected = service.inspect_runtime(runtime.python, runtime.worker, runtime.model)
    return service.register_runtime('Synthetic local protocol fixture', runtime.python,
        runtime.worker, runtime.model, owner_trusted=trusted,
        expected_fingerprint=inspected['fingerprint'] if trusted else None)


def make_service(root, *, broker=None, budget=None, stop_grace=2.0):
    """Keep canonical protocol assertions while constructing the real scoped host.

    This is test-only explicit fixture selection, not a production auto-binding
    path. The returned object is the host's single internal local-file adapter.
    """
    import sqlite3
    from carr_library.application import LibraryApp
    from carr_library.transcription import TranscriptionService
    from carr_library.operations import ResourceBroker, ResourceLimits
    root = Path(root)
    if root.name != 'transcription' or root.parent.name != 'state':
        raise ValueError('The canonical test fixture requires an exact library state root.')
    app = LibraryApp(root.parent.parent)
    with sqlite3.connect(Path(app.database).as_uri() + '?mode=ro', uri=True) as database:
        rows = database.execute('SELECT workspace FROM workspace_meta ORDER BY workspace').fetchall()
    workspace = rows[0][0] if rows else app.create_workspace('Canonical ASR protocol fixture')
    if broker is None:
        broker = ResourceBroker.for_app(app.root)
        broker.configure(ResourceLimits(min_free_disk_bytes=0))
    host = TranscriptionService(app, workspace, broker=broker, budget=budget, stop_grace=stop_grace)
    return host._local_protocol()
