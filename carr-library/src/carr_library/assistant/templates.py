"""Named instruction text, never saved execution authority or resumable jobs."""
from contextlib import contextmanager
import os
import unicodedata

from filelock import FileLock, Timeout

from ..durable_state import encode, local_path, read_json, regular
from .state import atomic_json

SCHEMA = 'carr-assistant-templates/1'
MAX_TEMPLATES = 100
MAX_INSTRUCTION_BYTES = 256 * 1024
MAX_BYTES = 4 * 1024 * 1024


def _name(value):
    if (type(value) is not str or not 1 <= len(value) <= 120
            or value != value.strip() or any(ord(c) < 32 or 127 <= ord(c) < 160
                                             or c in '/\\' for c in value)):
        raise ValueError('Use a template name of 1–120 characters without outer whitespace, slashes or control characters.')
    value.encode('utf-8', 'strict')
    return unicodedata.normalize('NFC', value).casefold()


def _instruction(value):
    if (type(value) is not str or not value.strip() or '\x00' in value
            or len(value.encode('utf-8', 'strict')) > MAX_INSTRUCTION_BYTES):
        raise ValueError('Template instruction text must be nonempty and at most 256 KiB.')


def validate_templates(value):
    """Validate inert text for both the store and passive backup restoration."""
    if (type(value) is not dict or set(value) != {'schema', 'templates'}
            or value['schema'] != SCHEMA or type(value['templates']) is not list
            or len(value['templates']) > MAX_TEMPLATES):
        raise ValueError('Unsupported assistant template schema or template count.')
    seen = set()
    for item in value['templates']:
        if type(item) is not dict or set(item) != {'name', 'instruction'}:
            raise ValueError('A template contains only its name and instruction text.')
        key = _name(item['name'])
        _instruction(item['instruction'])
        if key in seen:
            raise ValueError('Duplicate template name.')
        seen.add(key)
    if len(encode(value)) > MAX_BYTES:
        raise ValueError('Assistant templates exceed the 4 MiB storage allowance.')
    return value


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class TemplateStore:
    """Optional templates.json beside JobStore, excluded from its job IDs."""
    def __init__(self, directory):
        self.directory = local_path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        local_path(self.directory, exists=True)
        self.path = self.directory / 'templates.json'

    @contextmanager
    def _lock(self):
        local_path(self.directory, exists=True)
        path = self.directory / 'templates.lock'
        if os.path.lexists(path):
            regular(path)
        try:
            with FileLock(path, timeout=0, mode=0o600):
                yield
        except Timeout:
            raise RuntimeError('Templates are being changed by another operation; try again.') from None

    def _read(self):
        local_path(self.directory, exists=True)
        if not os.path.lexists(self.path):
            return {'schema': SCHEMA, 'templates': []}
        before = regular(self.path)
        if not 0 < before.st_size <= MAX_BYTES:
            raise ValueError('Assistant templates exceed their storage allowance.')
        value = read_json(self.path)
        if _identity(before) != _identity(regular(self.path)):
            raise ValueError('Templates changed while reading; try again.')
        return validate_templates(value)

    def list(self):
        return tuple(sorted(self._read()['templates'], key=lambda item: _name(item['name'])))

    def load(self, name):
        key = _name(name)
        for item in self._read()['templates']:
            if _name(item['name']) == key:
                return item['instruction']
        raise ValueError('The selected template no longer exists.')

    def save(self, name, instruction):
        key = _name(name)
        _instruction(instruction)
        with self._lock():
            value = self._read()
            if any(_name(item['name']) == key for item in value['templates']):
                raise FileExistsError('That template name already exists. Choose a new name or delete it explicitly.')
            value['templates'].append({'name': name, 'instruction': instruction})
            # Keep the assistant writer's established lock/temp namespace so
            # common backups need no new ignored files or schema migration.
            atomic_json(self.path, validate_templates(value))

    def delete(self, name, *, expected_instruction):
        """Delete only the exact template shown before the caller confirmed."""
        key = _name(name)
        with self._lock():
            value = self._read()
            for index, item in enumerate(value['templates']):
                if _name(item['name']) == key:
                    if item['instruction'] != expected_instruction:
                        raise ValueError('The selected template changed; choose it again.')
                    del value['templates'][index]
                    atomic_json(self.path, validate_templates(value))
                    return
            raise ValueError('The selected template no longer exists.')
