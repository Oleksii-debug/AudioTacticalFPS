"""Read-only admission of the canonical pinned audio and citation runtimes.

Both the initial bundle and a refreshed bundle use these exact file identities.
No downloaded code, native program, package manager or supplied script executes.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('carr_optional_audio_policy', HERE / 'prepare_windows_audio.py')
_audio = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_audio)


def admit(directory, component, *, locks=None):
    """Return admitted paths, byte identities and the exact provenance snapshot."""
    if component not in ('audio', 'citation-runtime'):
        raise ValueError('Unknown optional runtime component.')
    if directory is None:
        return None
    directory = _audio.local(directory, exists=True)
    _audio.regular(directory, directory=True)
    locks = HERE if locks is None else Path(locks)
    filename = 'windows-audio.lock.json' if component == 'audio' else 'windows-citations.lock.json'
    path = _audio.local(locks / filename, exists=True)
    if _audio.regular(path).st_size > 1024 * 1024:
        raise ValueError('Optional runtime lock exceeds its size limit.')
    raw = path.read_bytes()
    lock = json.loads(raw, object_pairs_hook=_audio._unique)
    if component == 'audio':
        _reviewed, identity = _audio.load_lock(path)
        if hashlib.sha256(raw).hexdigest() != identity:
            raise ValueError('Optional runtime lock changed while being read.')
        records = {name: (record['sha256'], record['size']) for name, record in lock['output_files'].items()}
    else:
        files = lock.get('files')
        if (type(files) is not dict or set(files) != {'node.exe', 'LICENSE', 'README.md'}
                or any(not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value)
                       for value in files.values())):
            raise ValueError('Unsupported pinned citation runtime inventory.')
        records = {name: (identity, None) for name, identity in files.items()}
    selected = {}
    for name, (identity, size) in records.items():
        path = _audio.local(directory / name, exists=True)
        info = _audio.regular(path)
        if ((size is not None and info.st_size != size) or not 0 < info.st_size <= 512 * 1024**2
                or _audio.digest(path) != identity):
            raise ValueError('Optional runtime differs from its pinned bytes: ' + component + '/' + name)
        if path.suffix == '.exe':
            _audio._pe64(path)
        selected[name] = (path, identity)
    return {'component': component, 'files': selected, 'provenance': raw}


def copy_admitted(record, destination):
    """Copy only admitted files and verify again before publication."""
    if record is None:
        return
    destination = Path(destination) / record['component']
    destination.mkdir(exist_ok=False)
    for name, (source, identity) in record['files'].items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        _audio.local(source, exists=True)
        shutil.copyfile(source, target)
        if _audio.digest(target) != identity:
            raise ValueError('Optional runtime changed during bundle assembly.')
    (destination / 'provenance.json').write_bytes(record['provenance'])
