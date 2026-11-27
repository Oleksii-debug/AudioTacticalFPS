"""Read-only admission of explicit local worker/interpreter/model installations."""
from hashlib import sha256
from importlib import metadata
import json
import os
from pathlib import Path

from ..durable_state import local_path, regular
from .models import fingerprint

MAX_MODEL_FILES = 4096
MAX_MODEL_ENTRIES = 16384
MAX_MODEL_BYTES = 64 * 1024**3
RUNTIME_DISTRIBUTIONS = ('faster-whisper', 'ctranslate2', 'av', 'onnxruntime',
                         'numpy', 'huggingface-hub', 'tokenizers')


def stopped(cancel=None):
    if cancel is not None and cancel():
        raise InterruptedError('Transcription operation cancelled; existing data was preserved.')


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def file_identity(path, *, cancel=None, maximum=64 * 1024**3):
    path = local_path(path, exists=True)
    before = regular(path)
    if before.st_size > maximum:
        raise ValueError('A selected file exceeds the configured byte budget.')
    digest, size = sha256(), 0
    with path.open('rb') as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('A selected file changed while opening.')
        while block := stream.read(1024 * 1024):
            stopped(cancel)
            size += len(block)
            if size > maximum:
                raise ValueError('A selected file grew beyond its byte budget.')
            digest.update(block)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('A selected file changed while hashing.')
    if size != before.st_size or _stamp(regular(path)) != _stamp(before):
        raise ValueError('A selected file changed during verification.')
    return {'sha256': digest.hexdigest(), 'size': size}


def _runtime_inventory(python, resolved_python, *, cancel=None, progress=None):
    """Read selected environment metadata; never import or execute its code."""
    environment = python.parent.parent if python.parent.name.lower() in ('bin', 'scripts') else python.parent
    base = resolved_python.parent.parent if resolved_python.parent.name.lower() in ('bin', 'scripts') else resolved_python.parent
    roots, auxiliary = [environment], {}
    configuration = environment / 'pyvenv.cfg'
    if configuration.exists():
        auxiliary[str(configuration)] = file_identity(configuration, cancel=cancel, maximum=65536)
        text = configuration.read_text(encoding='utf-8').casefold()
        if re_search_system_site(text) and base != environment:
            roots.append(base)
    elif base != environment:
        roots.append(base)
    search = []
    for root in roots:
        candidates = [root / 'Lib' / 'site-packages', *sorted((root / 'lib').glob('python*/site-packages'))]
        if len(candidates) > 16:
            raise ValueError('The selected Python has an ambiguous site-packages layout.')
        for candidate in candidates:
            if candidate.is_dir():
                directory = local_path(candidate, exists=True)
                if directory not in search:
                    search.append(directory)
    selected = {}
    for directory in search:
        for path in sorted(directory.glob('*.pth')):
            auxiliary[str(path)] = file_identity(path, cancel=cancel, maximum=1024**2)
        for name in ('sitecustomize.py', 'usercustomize.py'):
            if (directory / name).exists():
                auxiliary[str(directory / name)] = file_identity(directory / name, cancel=cancel, maximum=1024**2)
        entries = list(directory.glob('*.dist-info'))
        if len(entries) > 8192:
            raise ValueError('The selected runtime exceeds its installed-distribution budget.')
        for path in entries:
            stopped(cancel)
            # The metadata parser runs in the application interpreter. It reads
            # inert installation records from the selected external environment.
            regular(local_path(path, exists=True), directory=True)
            meta_path = path / 'METADATA'
            if not meta_path.exists() or regular(meta_path).st_size > 1024**2:
                continue
            distribution = metadata.PathDistribution(path)
            name = (distribution.metadata.get('Name') or '').lower().replace('_', '-')
            if name in RUNTIME_DISTRIBUTIONS and name not in selected:
                selected[name] = distribution
    missing = [name for name in RUNTIME_DISTRIBUTIONS if name not in selected]
    if missing:
        raise ValueError('The selected Python lacks a readable installed ASR file inventory: '
                         + ', '.join(missing) + '. No installation or download was attempted.')
    result = {}
    for name in RUNTIME_DISTRIBUTIONS:
        distribution = selected[name]
        record_path = distribution._path / 'RECORD'
        if not record_path.exists() or regular(local_path(record_path, exists=True)).st_size > 16 * 1024**2:
            raise ValueError('A selected ASR distribution has no bounded installed file inventory.')
        entries = distribution.files
        if not entries or len(entries) > 65536:
            raise ValueError('A selected ASR distribution has no bounded installed file inventory.')
        files = {}
        for relative in sorted(entries, key=str):
            label = str(relative)
            if '__pycache__' in Path(label).parts or label.endswith(('.pyc', '.pyo')):
                continue
            if progress:
                progress({'phase': 'Verifying installed ASR runtime files', 'status': 'running',
                          'current': name + ': ' + label, 'done': None, 'total': None, 'indeterminate': True})
            path = local_path(Path(distribution.locate_file(relative)).resolve(strict=True), exists=True)
            if not any(path.is_relative_to(root.resolve()) for root in roots):
                raise ValueError('An installed-file record escapes the explicitly selected Python environment.')
            files[label] = file_identity(path, cancel=cancel)
        result[name] = {'version': distribution.version, 'file_count': len(files),
            'installed_files_sha256': sha256(json.dumps(files, ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()}
    return result, auxiliary


def re_search_system_site(text):
    return any(line.strip().replace(' ', '') == 'include-system-site-packages=true'
               for line in text.splitlines())


def inspect_runtime(python_executable, worker_path, model_directory, *, cancel=None, progress=None):
    """Inspect bytes only. The selected Python and worker are never executed.

    A final interpreter symlink is permitted for normal virtual environments;
    its launch spelling and resolved target are both pinned. Model/worker paths
    use the existing strict local-file contract. Model files are never copied.
    """
    stopped(cancel)
    python = Path(python_executable).absolute()
    local_path(python.parent, exists=True)
    resolved_python = local_path(python.resolve(strict=True), exists=True)
    worker = local_path(worker_path, exists=True)
    if worker.suffix.lower() != '.py':
        raise ValueError('Choose the reviewed Python worker source file.')
    runtime_files, runtime_auxiliary = _runtime_inventory(python, resolved_python,
                                                         cancel=cancel, progress=progress)
    model = local_path(model_directory, exists=True)
    regular(model, directory=True)
    if model.name in ('', '.', '..'):
        raise ValueError('Choose the existing named local model directory.')
    for name in ('model.bin', 'config.json', 'tokenizer.json'):
        if regular(local_path(model / name, exists=True)).st_size <= 0:
            raise ValueError(f'The existing model requires nonempty {name}; no download is attempted.')
    model_files, total, visited = {}, 0, 0
    pending = [model]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                stopped(cancel)
                visited += 1
                if visited > MAX_MODEL_ENTRIES:
                    raise ValueError('The model exceeds its directory inventory budget.')
                path = local_path(entry.path, root=model, exists=True)
                if path.is_dir():
                    regular(path, directory=True)
                    pending.append(path)
                    continue
                if len(model_files) >= MAX_MODEL_FILES:
                    raise ValueError('The model exceeds its file inventory budget.')
                label = path.relative_to(model).as_posix()
                if progress:
                    progress({'phase': 'Verifying existing model files', 'status': 'running',
                              'current': label, 'done': len(model_files), 'total': None, 'indeterminate': True})
                identity = file_identity(path, cancel=cancel, maximum=MAX_MODEL_BYTES)
                total += identity['size']
                if total > MAX_MODEL_BYTES:
                    raise ValueError('The model exceeds its total byte budget.')
                model_files[label] = identity
    auxiliary = {}
    for candidate in (python.parent / 'pyvenv.cfg', python.parent.parent / 'pyvenv.cfg',
                      python.with_suffix('._pth')):
        if candidate.exists():
            auxiliary[str(local_path(candidate, exists=True))] = file_identity(candidate, cancel=cancel)
    manifest = {
        'schema': 'resonance-transcription-runtime/1', 'protocol': 'local-file-job/1',
        'python_executable': str(python), 'python_resolved': str(resolved_python),
        'python_identity': file_identity(resolved_python, cancel=cancel),
        'python_configuration': auxiliary,
        'runtime_distributions': runtime_files, 'runtime_auxiliary': runtime_auxiliary,
        'runtime_capability': 'installed-files-reviewed; execution-and-quality-unqualified',
        'worker_path': str(worker), 'worker_identity': file_identity(worker, cancel=cancel, maximum=16 * 1024**2),
        'model_directory': str(model), 'model_name': model.name,
        'model_files': dict(sorted(model_files.items())), 'model_bytes': total,
    }
    manifest['model_content_fingerprint'] = fingerprint(manifest['model_files'])
    manifest['fingerprint'] = fingerprint(manifest)
    return manifest
