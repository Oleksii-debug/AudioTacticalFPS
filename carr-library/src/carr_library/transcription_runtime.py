"""Passive installed-file seals for the existing supplied-worker service.

The bounded inventory reuses canonical PR147 admission without importing the
selected environment. Missing declared packages are recorded explicitly: this
seal does not assert that an environment can perform inference.
"""
from hashlib import sha256
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

from .durable_state import encode, local_path, regular


def fingerprint(value):
    return sha256(encode(value)).hexdigest()

MAX_MODEL_FILES = 4096
MAX_MODEL_ENTRIES = 16384
MAX_MODEL_BYTES = 64 * 1024**3
MAX_PYTHON_SUPPORT_FILES = 256
MAX_PYTHON_SUPPORT_BYTES = 512 * 1024**2
MAX_RUNTIME_FILES = 100000
MAX_RUNTIME_BYTES = 16 * 1024**3
MAX_AUXILIARY_FILES = 256
MAX_AUXILIARY_BYTES = 16 * 1024**2
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


def _runtime_inventory(roots, *, cancel=None, progress=None):
    """Read selected environment metadata; never import or execute its code."""
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
                continue
            distribution = metadata.PathDistribution(path)
            name = (distribution.metadata.get('Name') or '').lower().replace('_', '-')
            if name in RUNTIME_DISTRIBUTIONS and name not in selected:
                selected[name] = distribution
    if (len(auxiliary) > MAX_AUXILIARY_FILES
            or sum(row['size'] for row in auxiliary.values()) > MAX_AUXILIARY_BYTES):
        raise ValueError('The selected runtime exceeds its path-configuration budget.')
    result, file_count, byte_count = {}, 0, 0
    for name in RUNTIME_DISTRIBUTIONS:
        if name not in selected:
            result[name] = {'present': False}
            continue
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
            identity = file_identity(path, cancel=cancel, maximum=MAX_RUNTIME_BYTES)
            file_count += 1
            byte_count += identity['size']
            if file_count > MAX_RUNTIME_FILES or byte_count > MAX_RUNTIME_BYTES:
                raise ValueError('The installed ASR file inventory exceeds its aggregate budget.')
            files[label] = identity
        result[name] = {'present': True, 'version': distribution.version, 'file_count': len(files),
            'installed_files_sha256': sha256(json.dumps(files, ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()}
    return result, auxiliary



def inspect_python(python_executable, *, cancelled=lambda: False, progress=None):
    """Pin the selected launch spelling, operational base and installed files."""
    stopped(cancelled)
    python = Path(python_executable).absolute()
    local_path(python.parent, exists=True)
    selected = _stamp(python.lstat())
    resolved = local_path(python.resolve(strict=True), exists=True)
    roots, configurations, support = _python_layout(python, resolved, cancel=cancelled)
    distributions, auxiliary = _runtime_inventory(roots, cancel=cancelled, progress=progress)
    value = {
        'schema': 'carr-transcription-runtime-files/1',
        'python_executable': str(python), 'python_resolved': str(resolved),
        'python_identity': file_identity(resolved, cancel=cancelled),
        'python_configuration': configurations, 'python_support': support,
        'runtime_distributions': distributions, 'runtime_auxiliary': auxiliary,
        'qualification': 'installed-files-recorded; inference-unqualified',
    }
    if _stamp(python.lstat()) != selected or python.resolve(strict=True) != resolved:
        raise ValueError('The selected Python entry changed during inspection.')
    stopped(cancelled)
    value['fingerprint'] = fingerprint(value)
    validate_runtime(value)
    return value


def inspect_models(directory, *, cancelled=lambda: False, progress=None):
    """Seal existing local model files without copying or decoding them."""
    root = local_path(directory, exists=True)
    regular(root, directory=True)
    pending, files, total, entries = [root], {}, 0, 0
    while pending:
        stopped(cancelled)
        folder = pending.pop()
        before = _stamp(regular(folder, directory=True))
        with os.scandir(folder) as iterator:
            children = []
            for entry in iterator:
                stopped(cancelled)
                entries += 1
                if entries > MAX_MODEL_ENTRIES:
                    raise ValueError('The selected model directory exceeds its inventory budget.')
                children.append(entry.name)
        for name in sorted(children):
            stopped(cancelled)
            path = local_path(folder / name, root=root, exists=True)
            if path.is_dir():
                regular(path, directory=True)
                pending.append(path)
                continue
            if len(files) >= MAX_MODEL_FILES:
                raise ValueError('The selected models exceed their file budget.')
            relative = path.relative_to(root).as_posix()
            if progress:
                progress({'phase': 'Verifying existing local model files', 'status': 'running',
                          'current': relative, 'done': len(files), 'total': None,
                          'indeterminate': True})
            identity = file_identity(path, cancel=cancelled, maximum=MAX_MODEL_BYTES)
            total += identity['size']
            if total > MAX_MODEL_BYTES:
                raise ValueError('The selected models exceed their aggregate byte budget.')
            files[relative] = identity
        if _stamp(regular(folder, directory=True)) != before:
            raise ValueError('The selected model directory changed during inspection.')
    value = {'schema': 'carr-transcription-model-files/1', 'root': str(root),
             'files': dict(sorted(files.items())), 'size': total}
    value['content_fingerprint'] = fingerprint(value['files'])
    value['fingerprint'] = fingerprint(value)
    validate_models(value)
    stopped(cancelled)
    return value


def _fields(value, required):
    if type(value) is not dict or set(value) != set(required):
        raise ValueError('Invalid passive runtime inventory fields.')


def _text(value, maximum=4096):
    if type(value) is not str or not value or len(value) > maximum or '\0' in value:
        raise ValueError('Invalid passive runtime inventory text.')
    value.encode('utf-8', 'strict')


def _path(value):
    _text(value, 32768)
    if not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
        raise ValueError('A recorded runtime path must be absolute.')


def _digest(value):
    if type(value) is not str or re.fullmatch('[a-f0-9]{64}', value) is None:
        raise ValueError('Invalid passive runtime inventory digest.')


def _identity(value, maximum):
    _fields(value, ('sha256', 'size'))
    _digest(value['sha256'])
    if type(value['size']) is not int or not 0 <= value['size'] <= maximum:
        raise ValueError('Invalid passive runtime inventory byte count.')


def _inventory(value, *, maximum_files, maximum_bytes):
    if type(value) is not dict or len(value) > maximum_files:
        raise ValueError('Invalid passive runtime file inventory.')
    total = 0
    for path, identity in value.items():
        _path(path)
        _identity(identity, maximum_bytes)
        total += identity['size']
    if total > maximum_bytes:
        raise ValueError('The passive runtime inventory exceeds its byte budget.')


def validate_runtime(value):
    """Validate recorded data only; never follow a stored external path."""
    _fields(value, ('schema', 'python_executable', 'python_resolved', 'python_identity',
                    'python_configuration', 'python_support', 'runtime_distributions',
                    'runtime_auxiliary', 'qualification', 'fingerprint'))
    if (value['schema'] != 'carr-transcription-runtime-files/1'
            or value['qualification'] != 'installed-files-recorded; inference-unqualified'):
        raise ValueError('Invalid recorded runtime inventory schema.')
    _path(value['python_executable'])
    _path(value['python_resolved'])
    _identity(value['python_identity'], MAX_PYTHON_SUPPORT_BYTES)
    _inventory(value['python_configuration'], maximum_files=1, maximum_bytes=65536)
    _inventory(value['python_support'], maximum_files=MAX_PYTHON_SUPPORT_FILES,
               maximum_bytes=MAX_PYTHON_SUPPORT_BYTES)
    _inventory(value['runtime_auxiliary'], maximum_files=MAX_AUXILIARY_FILES,
               maximum_bytes=MAX_AUXILIARY_BYTES)
    distributions = value['runtime_distributions']
    _fields(distributions, RUNTIME_DISTRIBUTIONS)
    for entry in distributions.values():
        if type(entry) is dict and entry == {'present': False}:
            continue
        _fields(entry, ('present', 'version', 'file_count', 'installed_files_sha256'))
        if entry['present'] is not True:
            raise ValueError('Invalid installed runtime package presence.')
        _text(entry['version'], 256)
        if type(entry['file_count']) is not int or not 1 <= entry['file_count'] <= 65536:
            raise ValueError('Invalid installed runtime package file count.')
        _digest(entry['installed_files_sha256'])
    _digest(value['fingerprint'])
    if value['fingerprint'] != fingerprint({key: item for key, item in value.items() if key != 'fingerprint'}):
        raise ValueError('The recorded runtime inventory fingerprint changed.')
    return value


def validate_models(value):
    """Validate a model declaration without reading or opening its files."""
    _fields(value, ('schema', 'root', 'files', 'size', 'content_fingerprint', 'fingerprint'))
    if value['schema'] != 'carr-transcription-model-files/1':
        raise ValueError('Invalid recorded model inventory schema.')
    _path(value['root'])
    files = value['files']
    if type(files) is not dict or len(files) > MAX_MODEL_FILES:
        raise ValueError('Invalid recorded model file inventory.')
    total = 0
    for name, identity in files.items():
        _text(name, 4096)
        path = PurePosixPath(name)
        if (path.is_absolute() or str(path) != name or '..' in path.parts
                or '\\' in name or ':' in name or len(path.parts) > 64):
            raise ValueError('Invalid relative recorded model path.')
        _identity(identity, MAX_MODEL_BYTES)
        total += identity['size']
    if type(value['size']) is not int or value['size'] != total or total > MAX_MODEL_BYTES:
        raise ValueError('Invalid recorded model aggregate size.')
    _digest(value['content_fingerprint'])
    _digest(value['fingerprint'])
    if (value['content_fingerprint'] != fingerprint(files)
            or value['fingerprint'] != fingerprint({key: item for key, item in value.items() if key != 'fingerprint'})):
        raise ValueError('The recorded model inventory fingerprint changed.')
    return value
