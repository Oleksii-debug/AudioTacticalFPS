"""Read-only admission of explicit local worker/interpreter/model installations."""
from hashlib import sha256
from importlib import metadata
import csv
import io
import json
import os
import re
from pathlib import Path

from ..durable_state import local_path, regular
from .models import fingerprint

MAX_MODEL_FILES = 4096
MAX_MODEL_ENTRIES = 16384
MAX_MODEL_BYTES = 64 * 1024**3
MAX_RUNTIME_FILES = 262144
MAX_RUNTIME_BYTES = 64 * 1024**3
MAX_PYTHON_SUPPORT_FILES = 256
MAX_PYTHON_SUPPORT_BYTES = 512 * 1024**2
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


def _python_layout(python, resolved_python, *, cancel=None):
    """Pin a venv's operational base without executing its copied launcher."""
    environment = python.parent.parent if python.parent.name.lower() in ('bin', 'scripts') else python.parent
    base = resolved_python.parent.parent if resolved_python.parent.name.lower() in ('bin', 'scripts') else resolved_python.parent
    base_home = resolved_python.parent
    roots, configurations, support = [environment], {}, {}
    configuration = environment / 'pyvenv.cfg'
    system_site = not configuration.exists()
    if configuration.exists():
        identity = file_identity(configuration, cancel=cancel, maximum=65536)
        with configuration.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) != identity['size'] or sha256(raw).hexdigest() != identity['sha256']:
            raise ValueError('The selected Python configuration changed during inspection.')
        configurations[str(configuration)] = identity
        values = {}
        for line in raw.decode('utf-8').splitlines():
            if '=' not in line:
                continue
            key, value = (item.strip() for item in line.split('=', 1))
            key = key.casefold()
            if key in values:
                raise ValueError('The selected Python has ambiguous virtual-environment configuration.')
            values[key] = value
        home = Path(values.get('home', ''))
        if not home.is_absolute():
            raise ValueError('The selected virtual environment requires an explicit local base Python home.')
        base_home = local_path(home, exists=True)
        regular(base_home, directory=True)
        base = base_home.parent if base_home.name.lower() in ('bin', 'scripts') else base_home
        system_site = values.get('include-system-site-packages', '').casefold() == 'true'
        # On Windows Scripts/python.exe is a copied redirector. Its bytes do
        # not identify the base interpreter selected by pyvenv.cfg's home.
        names = dict.fromkeys((resolved_python.name, python.name,
                              'python.exe' if python.suffix.lower() == '.exe' else 'python'))
        executable = next((base_home / name for name in names if (base_home / name).is_file()), None)
        if executable is None:
            raise ValueError('The selected virtual environment has no readable base Python executable.')
        executable = local_path(executable.resolve(strict=True), exists=True)
        support[str(executable)] = file_identity(executable, cancel=cancel, maximum=MAX_PYTHON_SUPPORT_BYTES)
    if system_site and base != environment:
        roots.append(base)
    locations = {python.parent, resolved_python.parent, base_home}
    native = base / 'DLLs'
    if native.exists():
        locations.add(local_path(native, exists=True))
    total = sum(item['size'] for item in support.values())
    for directory in sorted(locations):
        regular(local_path(directory, exists=True), directory=True)
        with os.scandir(directory) as entries:
            for number, entry in enumerate(entries, 1):
                stopped(cancel)
                if number > MAX_MODEL_ENTRIES:
                    raise ValueError('The selected Python exceeds its support-directory inventory budget.')
                name = entry.name.casefold()
                # Only the selected launch target and operational base EXE
                # execute. Unused sibling aliases such as hosted python3.exe
                # are not dependencies; native/path support remains strict.
                selected = ((name.startswith('python') and name.endswith(('.dll', '._pth', '.zip')))
                            or (name.startswith('vcruntime') and name.endswith('.dll'))
                            or (directory == native and name.endswith(('.pyd', '.dll'))))
                if not selected:
                    continue
                path = local_path(entry.path, exists=True)
                if str(path) in support:
                    continue
                if len(support) >= MAX_PYTHON_SUPPORT_FILES:
                    raise ValueError('The selected Python exceeds its support-file inventory budget.')
                identity = file_identity(path, cancel=cancel, maximum=MAX_PYTHON_SUPPORT_BYTES)
                total += identity['size']
                if total > MAX_PYTHON_SUPPORT_BYTES:
                    raise ValueError('The selected Python exceeds its support-file byte budget.')
                support[str(path)] = identity
    return roots, configurations, support


def _record_labels(record_path, *, cancel=None):
    """Parse the actual RECORD, including missing files hidden by metadata.files."""
    record_path = local_path(record_path, exists=True)
    maximum = 16 * 1024**2
    identity = file_identity(record_path, cancel=cancel, maximum=maximum)
    stopped(cancel)
    with record_path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    stopped(cancel)
    if len(raw) != identity['size'] or sha256(raw).hexdigest() != identity['sha256']:
        raise ValueError('The installed RECORD changed during verification.')
    labels, seen = [], set()
    try:
        rows = csv.reader(io.StringIO(raw.decode('utf-8'), newline=''), strict=True)
        for row in rows:
            stopped(cancel)
            if len(labels) >= 65536:
                raise ValueError('A selected ASR distribution exceeds its installed-record budget.')
            if len(row) != 3 or not row[0] or '\x00' in row[0]:
                raise ValueError('A selected ASR distribution has a malformed RECORD row.')
            # Preserve the existing importlib.metadata PackagePath label dialect
            # used by the supplied worker's seven-package checkpoint hashes.
            label = str(metadata.PackagePath(row[0]))
            if label in seen:
                raise ValueError('A selected distribution has a duplicate installed-file record.')
            seen.add(label)
            labels.append(label)
    except (csv.Error, UnicodeDecodeError) as exc:
        raise ValueError('A selected ASR distribution has a malformed RECORD.') from exc
    if not labels:
        raise ValueError('A selected ASR distribution has no bounded installed file inventory.')
    return labels, identity


def _runtime_inventory(roots, *, cancel=None, progress=None):
    """Read all recorded distributions in the selected environment without importing them."""
    auxiliary = {}
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
                raise ValueError('A selected distribution has no bounded METADATA.')
            distribution = metadata.PathDistribution(path)
            name = re.sub(r'[-_.]+', '-', (distribution.metadata.get('Name') or '').lower())
            if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or not distribution.version:
                raise ValueError('A selected distribution has no valid name or version.')
            if name in selected:
                raise ValueError('The selected runtime has ambiguous installed distribution: ' + name)
            selected[name] = distribution
    missing = [name for name in RUNTIME_DISTRIBUTIONS if name not in selected]
    if missing:
        raise ValueError('The selected Python lacks a readable installed ASR file inventory: '
                         + ', '.join(missing) + '. No installation or download was attempted.')
    result = {}
    total_files = total_bytes = 0
    # The required seven SDK distributions are a minimum, not an exhaustive
    # dependency closure. Include every recorded distribution in the selected
    # environment; do not ignore installed transitive dependencies like tqdm.
    for name in sorted(selected):
        distribution = selected[name]
        record_path = distribution._path / 'RECORD'
        if not record_path.exists() or regular(local_path(record_path, exists=True)).st_size > 16 * 1024**2:
            raise ValueError('A selected ASR distribution has no bounded installed file inventory.')
        record_path = local_path(record_path, exists=True)
        entries, record_identity = _record_labels(record_path, cancel=cancel)
        files = {}
        recorded_paths = set()
        for relative in sorted(entries):
            stopped(cancel)
            label = str(relative)
            if '__pycache__' in Path(label).parts or label.endswith(('.pyc', '.pyo')):
                continue
            if label in files:
                raise ValueError('A selected distribution has a duplicate installed-file record.')
            total_files += 1
            if total_files > MAX_RUNTIME_FILES:
                raise ValueError('The selected runtime exceeds its total installed-file budget.')
            if progress:
                progress({'phase': 'Verifying installed ASR runtime files', 'status': 'running',
                          'current': name + ': ' + label, 'done': None, 'total': None, 'indeterminate': True})
            path = local_path(Path(distribution.locate_file(relative)), exists=True)
            if not any(path.is_relative_to(root.resolve()) for root in roots):
                raise ValueError('An installed-file record escapes the explicitly selected Python environment.')
            if path in recorded_paths:
                raise ValueError('A selected distribution has duplicate resolved installed-file records.')
            recorded_paths.add(path)
            identity = file_identity(path, cancel=cancel, maximum=MAX_RUNTIME_BYTES - total_bytes)
            if path == record_path and identity != record_identity:
                raise ValueError('The installed RECORD changed during verification.')
            total_bytes += identity['size']
            if total_bytes > MAX_RUNTIME_BYTES:
                raise ValueError('The selected runtime exceeds its total installed-byte budget.')
            files[label] = identity
        if not files:
            raise ValueError('A selected distribution has an empty installed-file inventory.')
        required = {local_path(distribution._path / filename, exists=True)
                    for filename in ('METADATA', 'RECORD')}
        if not required.issubset(recorded_paths):
            raise ValueError('A selected distribution inventory omits METADATA or RECORD.')
        result[name] = {'version': distribution.version, 'file_count': len(files),
            'installed_files_sha256': sha256(json.dumps(files, ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()}
    return result, auxiliary


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
    roots, configurations, python_support = _python_layout(python, resolved_python, cancel=cancel)
    runtime_files, runtime_auxiliary = _runtime_inventory(roots, cancel=cancel, progress=progress)
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
    manifest = {
        'schema': 'resonance-transcription-runtime/1', 'protocol': 'local-file-job/1',
        'python_executable': str(python), 'python_resolved': str(resolved_python),
        'python_identity': file_identity(resolved_python, cancel=cancel),
        'python_configuration': configurations, 'python_support': python_support,
        # Keep the supplied worker checkpoint's seven-package dialect. Extra
        # installed distributions are bound by the outer profile fingerprint,
        # which the service revalidates before publication and on rebind.
        'runtime_distributions': {name: runtime_files[name] for name in RUNTIME_DISTRIBUTIONS},
        'runtime_auxiliary': runtime_auxiliary,
        'runtime_capability': 'installed-files-reviewed; execution-and-quality-unqualified',
        'worker_path': str(worker), 'worker_identity': file_identity(worker, cancel=cancel, maximum=16 * 1024**2),
        'model_directory': str(model), 'model_name': model.name,
        'model_files': dict(sorted(model_files.items())), 'model_bytes': total,
    }
    local_path(python.parent, exists=True)
    if python.resolve(strict=True) != resolved_python:
        raise ValueError('The selected Python executable link changed during inspection.')
    additional = {name: identity for name, identity in runtime_files.items()
                  if name not in RUNTIME_DISTRIBUTIONS}
    if additional:
        manifest['runtime_additional_distributions'] = additional
    manifest['model_content_fingerprint'] = fingerprint(manifest['model_files'])
    manifest['fingerprint'] = fingerprint(manifest)
    return manifest
