"""Read-only admission of explicit local worker/interpreter/model installations."""
import csv
from email.parser import Parser
from hashlib import sha256
from importlib import metadata
import io
import json
import os
import re
from pathlib import Path
import stat

from ..durable_state import _open_json_file, local_path, regular
from .models import fingerprint

MAX_MODEL_FILES = 4096
MAX_MODEL_ENTRIES = 16384
MAX_MODEL_BYTES = 64 * 1024**3
MAX_PYTHON_SUPPORT_FILES = 256
MAX_PYTHON_SUPPORT_BYTES = 512 * 1024**2
MAX_RUNTIME_ENTRIES = 65536
MAX_RUNTIME_SITES = 16
MAX_RUNTIME_DISTRIBUTIONS = 8192
MAX_RUNTIME_AUXILIARY_FILES = 256
MAX_RUNTIME_METADATA_BYTES = 128 * 1024**2
MAX_RUNTIME_RECORDS = 65536
MAX_RUNTIME_FILES = 262144
MAX_RUNTIME_BYTES = 64 * 1024**3
RUNTIME_DISTRIBUTIONS = ('faster-whisper', 'ctranslate2', 'av', 'onnxruntime',
                         'numpy', 'huggingface-hub', 'tokenizers')


def stopped(cancel=None):
    if cancel is not None and cancel():
        raise InterruptedError('Transcription operation cancelled; existing data was preserved.')


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _directory_identity(path):
    value = regular(path, directory=True)
    return value.st_dev, value.st_ino


def _open_selected_file(path):
    if os.name == 'nt':
        # The shared native reader opens the reparse point itself, with read /
        # write / delete sharing and a non-inheritable handle. Validate its
        # actual descriptor below before consuming any selected bytes.
        return _open_json_file(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0))
    try:
        return os.fdopen(descriptor, 'rb', buffering=0)
    except BaseException:
        os.close(descriptor)
        raise


def _selected_file(path, *, cancel, maximum, capture):
    budget = 32 * 1024**2 if capture else MAX_MODEL_BYTES
    if type(maximum) is not int or not 0 < maximum <= budget:
        raise ValueError('Choose a positive selected-file byte budget within the supported limit.')
    stopped(cancel)
    path = local_path(path, exists=True)
    before = regular(path)
    parent = _directory_identity(path.parent)
    if before.st_size > maximum:
        raise ValueError('A selected file exceeds the configured byte budget.')
    digest, size = sha256(), 0
    blocks = [] if capture else None
    with _open_selected_file(path) as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode)
                or getattr(opened, 'st_file_attributes', 0) & 0x400
                or opened.st_nlink != 1):
            raise ValueError('A selected file must remain a regular local file, without links.')
        if _stamp(opened) != _stamp(before):
            raise ValueError('A selected file changed while opening.')
        local_path(path, exists=True)
        if _directory_identity(path.parent) != parent:
            raise ValueError('A selected file parent changed while opening.')
        # Bound growth against both the caller's cap and the captured length.
        # A FIFO substituted after lstat is opened non-blocking and rejected
        # above; it can never block this read before descriptor validation.
        limit = min(maximum, before.st_size)
        while True:
            stopped(cancel)
            block = stream.read(min(1024 * 1024, limit - size + 1))
            stopped(cancel)
            if not block:
                break
            size += len(block)
            if size > limit:
                raise ValueError('A selected file grew beyond its captured byte budget.')
            digest.update(block)
            if blocks is not None:
                blocks.append(block)
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError('A selected file changed while hashing.')
        local_path(path, exists=True)
        if (size != before.st_size or _stamp(regular(path)) != _stamp(before)
                or _directory_identity(path.parent) != parent):
            raise ValueError('A selected file changed during verification.')
    stopped(cancel)
    return {'sha256': digest.hexdigest(), 'size': size}, (
        b''.join(blocks) if blocks is not None else None)


def file_identity(path, *, cancel=None, maximum=64 * 1024**3):
    return _selected_file(path, cancel=cancel, maximum=maximum, capture=False)[0]


def read_file_bytes(path, *, cancel=None, maximum=16 * 1024**2):
    """Capture bounded reviewed bytes through the same verified descriptor."""
    return _selected_file(path, cancel=cancel, maximum=maximum, capture=True)[1]


def _python_layout(python, resolved_python, *, cancel=None):
    """Pin a venv's operational base without executing its copied launcher."""
    environment = python.parent.parent if python.parent.name.lower() in ('bin', 'scripts') else python.parent
    base = resolved_python.parent.parent if resolved_python.parent.name.lower() in ('bin', 'scripts') else resolved_python.parent
    base_home = resolved_python.parent
    roots, configurations, support = [environment], {}, {}
    configuration = environment / 'pyvenv.cfg'
    system_site = not configuration.exists()
    if configuration.exists():
        raw = read_file_bytes(configuration, cancel=cancel, maximum=65536)
        identity = {'sha256': sha256(raw).hexdigest(), 'size': len(raw)}
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


def _optional_directory(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    regular(path, directory=True)
    return local_path(path, exists=True)


def _site_packages(roots, *, cancel=None):
    search, visited = [], 0
    for root in roots:
        stopped(cancel)
        regular(local_path(root, exists=True), directory=True)
        candidates = [root / 'Lib' / 'site-packages']
        library = _optional_directory(root / 'lib')
        if library is not None:
            with os.scandir(library) as entries:
                for entry in entries:
                    stopped(cancel)
                    visited += 1
                    if visited > MAX_MODEL_ENTRIES:
                        raise ValueError('The selected Python exceeds its layout inventory budget.')
                    if not entry.name.startswith('python'):
                        continue
                    child = Path(entry.path)
                    value = child.lstat()
                    if stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & 0x400:
                        raise ValueError('Python layout paths must use regular local directories.')
                    if not stat.S_ISDIR(value.st_mode):
                        continue
                    if len(candidates) >= MAX_RUNTIME_SITES:
                        raise ValueError('The selected Python has an ambiguous site-packages layout.')
                    candidates.append(child / 'site-packages')
        for candidate in [candidates[0], *sorted(candidates[1:])]:
            stopped(cancel)
            directory = _optional_directory(candidate)
            if directory is not None and directory not in search:
                if len(search) >= MAX_RUNTIME_SITES:
                    raise ValueError('The selected Python has an ambiguous site-packages layout.')
                search.append(directory)
    return search


def _captured_record_entries(raw, *, cancel=None):
    entries = []
    try:
        for row in csv.reader(io.StringIO(raw.decode('utf-8'), newline='')):
            stopped(cancel)
            if not 1 <= len(row) <= 3 or not row[0] or '\x00' in row[0]:
                raise ValueError('An installed-file record is malformed.')
            if len(entries) >= MAX_RUNTIME_RECORDS:
                raise ValueError('A selected ASR distribution exceeds its installed-file budget.')
            # Match importlib.metadata's PackagePath spelling and sorting for
            # existing valid profiles; no selected-environment code is loaded.
            entries.append(metadata.PackagePath(row[0]))
    except (csv.Error, UnicodeError) as exc:
        raise ValueError('An installed-file record is malformed.') from exc
    if not entries:
        raise ValueError('A selected ASR distribution has no bounded installed file inventory.')
    return entries


def _inspect_inventory(roots, *, cancel=None, progress=None):
    """Capture inert metadata and RECORD bytes before parsing bounded values."""
    auxiliary, selected = {}, {}
    search = _site_packages(roots, cancel=cancel)
    visited, distributions, metadata_bytes = 0, 0, 0
    directory_stamps = {}
    for directory in search:
        directory_stamps[directory] = _stamp(regular(directory, directory=True))
        auxiliary_paths, distribution_paths = [], []
        with os.scandir(directory) as entries:
            for entry in entries:
                stopped(cancel)
                visited += 1
                if visited > MAX_RUNTIME_ENTRIES:
                    raise ValueError('The selected runtime exceeds its directory inventory budget.')
                name = os.path.normcase(entry.name)
                if name.endswith('.pth') or name in ('sitecustomize.py', 'usercustomize.py'):
                    if len(auxiliary) + len(auxiliary_paths) >= MAX_RUNTIME_AUXILIARY_FILES:
                        raise ValueError('The selected runtime exceeds its auxiliary-file budget.')
                    auxiliary_paths.append(Path(entry.path))
                if name.endswith('.dist-info'):
                    distributions += 1
                    if distributions > MAX_RUNTIME_DISTRIBUTIONS:
                        raise ValueError('The selected runtime exceeds its installed-distribution budget.')
                    distribution_paths.append(Path(entry.path))
        for path in sorted(auxiliary_paths):
            identity = file_identity(path, cancel=cancel, maximum=1024**2)
            metadata_bytes += identity['size']
            if metadata_bytes > MAX_RUNTIME_METADATA_BYTES:
                raise ValueError('The selected runtime exceeds its metadata byte budget.')
            auxiliary[str(path)] = identity
        for path in distribution_paths:
            stopped(cancel)
            regular(local_path(path, exists=True), directory=True)
            meta_path = path / 'METADATA'
            try:
                meta_size = regular(meta_path).st_size
            except FileNotFoundError as error:
                raise ValueError('A selected distribution has no bounded METADATA.') from error
            if meta_size > 1024**2:
                raise ValueError('A selected distribution has no bounded METADATA.')
            raw = read_file_bytes(meta_path, cancel=cancel, maximum=1024**2)
            metadata_bytes += len(raw)
            if metadata_bytes > MAX_RUNTIME_METADATA_BYTES:
                raise ValueError('The selected runtime exceeds its metadata byte budget.')
            message = Parser().parsestr(raw.decode('utf-8'))
            name = re.sub(r'[-_.]+', '-', (message.get('Name') or '').lower())
            version = message.get('Version')
            if (not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or len(name) > 512
                    or not version or len(version) > 1024):
                raise ValueError('A selected distribution has no valid name or version.')
            if name in selected:
                raise ValueError('The selected runtime has ambiguous installed distribution: ' + name)
            selected[name] = (path, version, {'sha256': sha256(raw).hexdigest(), 'size': len(raw)})
    missing = [name for name in RUNTIME_DISTRIBUTIONS if name not in selected]
    if missing:
        raise ValueError('The selected Python lacks a readable installed ASR file inventory: '
                         + ', '.join(missing) + '. No installation or download was attempted.')
    result = {}
    total_files, total_bytes = 0, 0
    # The seven protocol packages are a minimum. Every recorded distribution
    # in the reviewed import roots contributes to the outer profile identity.
    for name in sorted(selected):
        path, version, meta_identity = selected[name]
        record_path = path / 'RECORD'
        try:
            raw = read_file_bytes(record_path, cancel=cancel, maximum=16 * 1024**2)
        except FileNotFoundError as exc:
            raise ValueError('A selected ASR distribution has no bounded installed file inventory.') from exc
        metadata_bytes += len(raw)
        if metadata_bytes > MAX_RUNTIME_METADATA_BYTES:
            raise ValueError('The selected runtime exceeds its metadata byte budget.')
        record_identity = {'sha256': sha256(raw).hexdigest(), 'size': len(raw)}
        files, recorded_paths = {}, set()
        for relative in sorted(_captured_record_entries(raw, cancel=cancel), key=str):
            stopped(cancel)
            label = str(relative)
            if '__pycache__' in Path(label).parts or label.endswith(('.pyc', '.pyo')):
                continue
            if label in files:
                raise ValueError('An installed-file record repeats a selected path.')
            total_files += 1
            if total_files > MAX_RUNTIME_FILES:
                raise ValueError('The selected runtime exceeds its total installed-file budget.')
            if progress:
                progress({'phase': 'Verifying installed ASR runtime files', 'status': 'running',
                          'current': name + ': ' + label, 'done': None, 'total': None, 'indeterminate': True})
            # PackagePath uses POSIX parts even on Windows. Parse its exact
            # spelling through the host Path so an absolute drive keeps its root.
            selected_path = local_path(path.parent / label, exists=True)
            if not any(selected_path.is_relative_to(root.resolve()) for root in roots):
                raise ValueError('An installed-file record escapes the explicitly selected Python environment.')
            if selected_path in recorded_paths:
                raise ValueError('A selected distribution has duplicate resolved installed-file records.')
            recorded_paths.add(selected_path)
            remaining = MAX_RUNTIME_BYTES - total_bytes
            if remaining <= 0:
                raise ValueError('The selected runtime exceeds its total installed-byte budget.')
            identity = file_identity(selected_path, cancel=cancel, maximum=remaining)
            total_bytes += identity['size']
            if total_bytes > MAX_RUNTIME_BYTES:
                raise ValueError('The selected runtime exceeds its total installed-byte budget.')
            files[label] = identity
        if not files:
            raise ValueError('A selected distribution has an empty installed-file inventory.')
        required = {local_path(path / name, exists=True) for name in ('METADATA', 'RECORD')}
        if not required <= recorded_paths:
            raise ValueError('A selected distribution inventory omits METADATA or RECORD.')
        # Recheck the captured declarations after every installed-file read.
        if (file_identity(path / 'METADATA', cancel=cancel, maximum=1024**2) != meta_identity
                or file_identity(record_path, cancel=cancel, maximum=16 * 1024**2) != record_identity):
            raise ValueError('Selected runtime metadata changed during inspection.')
        result[name] = {'version': version, 'file_count': len(files),
            'installed_files_sha256': sha256(json.dumps(files, ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()}
    for directory, before in directory_stamps.items():
        stopped(cancel)
        local_path(directory, exists=True)
        if _stamp(regular(directory, directory=True)) != before:
            raise ValueError('The selected runtime directory changed during inspection.')
    return result, auxiliary, search


def _runtime_inventory(roots, *, cancel=None, progress=None):
    result, auxiliary, _ = _inspect_inventory(roots, cancel=cancel, progress=progress)
    return result, auxiliary


def verified_runtime_paths(profile, *, cancel=None):
    """Return reviewed import roots without executing site, .pth or packages.

    The caller supplies the original inspect_runtime manifest, without its
    separate registration/trust fields. This repeats file inspection; it does
    not grant trust or replace the caller's worker/model admission checks.
    """
    stopped(cancel)
    if (type(profile) is not dict or profile.get('schema') != 'resonance-transcription-runtime/1'
            or profile.get('protocol') != 'local-file-job/1'
            or profile.get('fingerprint') != fingerprint(
                {key: value for key, value in profile.items() if key != 'fingerprint'})):
        raise ValueError('The selected runtime inspection fingerprint is invalid.')
    value = profile.get('python_executable')
    if type(value) is not str or not Path(value).is_absolute():
        raise ValueError('The selected runtime requires an absolute Python executable.')
    python = Path(value)
    local_path(python.parent, exists=True)
    resolved = local_path(python.resolve(strict=True), exists=True)
    python_identity = file_identity(resolved, cancel=cancel)
    roots, configurations, support = _python_layout(python, resolved, cancel=cancel)
    distributions, auxiliary, search = _inspect_inventory(roots, cancel=cancel)
    actual = {'python_resolved': str(resolved), 'python_identity': python_identity,
              'python_configuration': configurations, 'python_support': support,
              'runtime_distributions': {name: distributions[name] for name in RUNTIME_DISTRIBUTIONS},
              'runtime_auxiliary': auxiliary}
    additional = {name: identity for name, identity in distributions.items()
                  if name not in RUNTIME_DISTRIBUTIONS}
    if additional or 'runtime_additional_distributions' in profile:
        actual['runtime_additional_distributions'] = additional
    if any(profile.get(key) != value for key, value in actual.items()):
        raise ValueError('The selected runtime changed since its explicit review.')
    local_path(python.parent, exists=True)
    if (python.resolve(strict=True) != resolved
            or file_identity(resolved, cancel=cancel) != python_identity):
        raise ValueError('The selected Python executable changed during verification.')
    stopped(cancel)
    return tuple(search)


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
        # Preserve the supplied worker's exact seven-package checkpoint dialect.
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
