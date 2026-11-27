"""Named, inert typed workflows; scope and execution approval are never saved."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import tempfile
from hashlib import sha256
import json
import os
import re
from uuid import uuid4

from ..audio.jobs import (read_json as _read_json, publish_noreplace, replace_json_file)
from ..backup_files import local_path, optional_present, regular, sync_directory
from ..ingest.writer_guard import exclusive_writer
from .state import state_write_guard, _native_guard
from .models import Plan, SCHEMA, Step, canonical, text

SCHEMA_TEMPLATE = 'carr-assistant-template/1'
MAX_TEMPLATES = 256
MAX_TEMPLATE_STEPS = 128
MAX_TEMPLATE_BYTES = 8 * 1024 * 1024
MAX_COLLECTION_BYTES = 64 * 1024 * 1024
PREFIX = 'state/assistant/templates/'
_ID = re.compile(r'[0-9a-f]{32}\Z')


def checked_draft(value):
    """Validate without constructing Scope or inspecting paths in arguments."""
    if (type(value) is not dict or set(value) != {'schema', 'steps', 'summary'}
            or value['schema'] != SCHEMA or type(value['steps']) is not list
            or not 1 <= len(value['steps']) <= MAX_TEMPLATE_STEPS):
        raise ValueError('A workflow draft needs its schema, summary and 1 to 128 typed steps.')
    text(value['summary'], 'workflow summary', maximum=16384, empty=True)
    from .validation import validate_arguments as _check_arguments
    steps, encoded_bytes = [], len(value['summary'].encode('utf-8'))
    for row in value['steps']:
        if type(row) is not dict or set(row) != {'tool', 'arguments'}:
            raise ValueError('A workflow step needs only its allowed tool and arguments.')
        step = Step(row['tool'], row['arguments'])
        _check_arguments(step)
        encoded_bytes += len(encode(step.to_dict()))
        if encoded_bytes > MAX_TEMPLATE_BYTES:
            raise ValueError('The workflow draft exceeds its 8 MiB record budget.')
        steps.append(step)
    result = {'schema': SCHEMA, 'steps': [step.to_dict() for step in steps],
              'summary': value['summary']}
    if len(encode(result)) > MAX_TEMPLATE_BYTES:
        raise ValueError('The workflow draft exceeds its 8 MiB record budget.')
    return result


def draft_from_plan(value):
    """Reuse reviewed or restored steps as an unapproved current-scope draft."""
    if isinstance(value, Plan):
        value = value.to_dict()
    if (type(value) is not dict or set(value) - {'schema', 'scope', 'steps', 'summary'}
            or not {'schema', 'steps'} <= set(value)):
        raise ValueError('Choose a supported saved assistant plan.')
    return checked_draft({'schema': value['schema'], 'steps': value['steps'],
                          'summary': value.get('summary', '')})


@dataclass(frozen=True)
class WorkflowTemplate:
    id: str
    name: str
    steps: tuple
    summary: str = ''
    revision: int = 1

    def __post_init__(self):
        if not isinstance(self.id, str) or not _ID.fullmatch(self.id):
            raise ValueError('Invalid workflow template identity.')
        text(self.name, 'workflow name', maximum=120)
        if any(ord(character) < 32 or ord(character) == 127 for character in self.name):
            raise ValueError('A workflow name cannot contain control characters.')
        if type(self.revision) is not int or not 1 <= self.revision < 2**31:
            raise ValueError('Invalid workflow template revision.')
        if (not isinstance(self.steps, (list, tuple))
                or not all(isinstance(step, Step) for step in self.steps)):
            raise ValueError('A workflow template needs typed steps.')
        checked = checked_draft({'schema': SCHEMA,
            'steps': [step.to_dict() for step in self.steps], 'summary': self.summary})
        object.__setattr__(self, 'steps', tuple(
            Step(row['tool'], row['arguments']) for row in checked['steps']))
        if len(encode(self.to_dict())) > MAX_TEMPLATE_BYTES:
            raise ValueError('The workflow template exceeds its 8 MiB record budget.')

    @property
    def draft(self):
        return checked_draft({'schema': SCHEMA,
            'steps': [step.to_dict() for step in self.steps], 'summary': self.summary})

    def instruction(self):
        return json.dumps(self.draft, ensure_ascii=False, indent=2, allow_nan=False)

    def to_dict(self):
        return {'schema': SCHEMA_TEMPLATE, 'id': self.id, 'name': self.name,
                'revision': self.revision,
                'draft': {'schema': SCHEMA, 'steps': [
                    json.loads(canonical(step.to_dict())) for step in self.steps],
                    'summary': self.summary}}

    @property
    def fingerprint(self):
        return sha256(encode(self.to_dict())).hexdigest()

    @classmethod
    def from_dict(cls, value):
        if (type(value) is not dict
                or set(value) != {'schema', 'id', 'name', 'revision', 'draft'}
                or value['schema'] != SCHEMA_TEMPLATE):
            raise ValueError('Unsupported workflow template fields or schema.')
        draft = checked_draft(value['draft'])
        return cls(value['id'], value['name'], tuple(
            Step(row['tool'], row['arguments']) for row in draft['steps']),
            draft['summary'], value['revision'])


def combine_instructions(values):
    """Expand a finite selected batch into ordinary steps, never a loop or macro."""
    if (not isinstance(values, (tuple, list)) or not values
            or len(values) > MAX_TEMPLATES
            or not all(isinstance(value, WorkflowTemplate) for value in values)):
        raise ValueError('Choose one or more saved workflow templates.')
    steps = []
    for value in values:
        steps.extend(value.draft['steps'])
        if len(steps) > MAX_TEMPLATE_STEPS:
            raise ValueError('The selected workflow batch exceeds 128 steps.')
    summary = '\n'.join(value.name for value in values)
    draft = checked_draft({'schema': SCHEMA, 'steps': steps, 'summary': summary[:16384]})
    return json.dumps(draft, ensure_ascii=False, indent=2, allow_nan=False)


def validate_saved_template(name, value):
    identity = name.removeprefix(PREFIX).removesuffix('.json')
    if name != PREFIX + identity + '.json' or not _ID.fullmatch(identity):
        raise ValueError('Invalid saved workflow template path.')
    result = WorkflowTemplate.from_dict(value)
    if result.id != identity:
        raise ValueError('Workflow template identity differs from its filename.')
    return result


def validate_template_collection(entries):
    """Apply the same collection budget to live state and passive backup."""
    count, size = 0, 0
    for entry in entries:
        if entry['filename'].startswith(PREFIX):
            count += 1
            size += entry['size_bytes']
            if count > MAX_TEMPLATES or size > MAX_COLLECTION_BYTES:
                raise ValueError('Workflow templates exceed 256 records or 64 MiB.')


class TemplateChangeCommittedError(OSError):
    """A change is present, but its final durability could not be confirmed."""
    def __init__(self, operation, template):
        self.operation, self.template = operation, template
        self.durability_confirmed = False
        super().__init__(f'Workflow {operation} is present, but durability is unconfirmed. '
                         'Reload the saved templates before another change.')


def _check_cancelled(cancelled):
    if cancelled():
        raise InterruptedError('Workflow settings operation cancelled.')


class WorkflowTemplateStore:
    def __init__(self, library_root):
        self.library_root = local_path(library_root, exists=True)
        info = regular(self.library_root, directory=True)
        self.root_identity = info.st_dev, info.st_ino
        self.directory = self.library_root / 'state' / 'assistant'
        self.root = local_path(self.directory / 'templates', root=self.library_root)

    def _current(self):
        root = local_path(self.library_root, exists=True)
        info = regular(root, directory=True)
        if (info.st_dev, info.st_ino) != self.root_identity:
            raise ValueError('The workflow library changed. Open its current templates again.')

    @contextmanager
    def _writer(self):
        # The same application writer and assistant state guard own these
        # settings. Retain the donor's exact native template lock as well.
        self._current()
        with exclusive_writer(self.library_root):
            for path in (self.library_root / 'state', self.directory):
                if not optional_present(path):
                    path.mkdir(mode=0o700)
                    sync_directory(path.parent)
                regular(path, directory=True)
            with state_write_guard(self.directory):
                if not optional_present(self.root):
                    self.root.mkdir(mode=0o700)
                    sync_directory(self.root.parent)
                regular(self.root, directory=True)
                with template_snapshot_guard(self.library_root, writing=True):
                    self._current()
                    yield
                    self._current()

    def _path(self, identity):
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise ValueError('Choose an existing workflow template identity.')
        # Reads do not implicitly create the template directory.
        self._current()
        return local_path(self.root / (identity + '.json'), root=self.library_root)

    def get(self, identity):
        value = WorkflowTemplate.from_dict(read_json(
            self._path(identity), maximum=MAX_TEMPLATE_BYTES))
        if value.id != identity:
            raise ValueError('Workflow template identity differs from its filename.')
        return value

    def list(self, *, cancelled=lambda: False):
        self._current()
        values, size = [], 0
        for path in template_paths(self.library_root, cancelled=cancelled):
            _check_cancelled(cancelled)
            value = self.get(path.stem)
            size += max(len(encode(value.to_dict())), regular(path).st_size)
            if size > MAX_COLLECTION_BYTES:
                raise ValueError('Workflow templates exceed 256 records or 64 MiB.')
            values.append(value)
        self._current()
        _check_cancelled(cancelled)
        return tuple(sorted(values, key=lambda value: (value.name.casefold(), value.id)))

    def save(self, name, draft, *, previous=None, cancelled=lambda: False):
        _check_cancelled(cancelled)
        if previous is not None and not isinstance(previous, WorkflowTemplate):
            raise ValueError('Review the exact saved workflow before updating it.')
        checked = draft_from_plan(draft)
        value = WorkflowTemplate(previous.id if previous else uuid4().hex, name,
            tuple(Step(row['tool'], row['arguments']) for row in checked['steps']),
            checked['summary'], previous.revision + 1 if previous else 1)
        committed = False
        try:
            with self._writer():
                existing = self.list(cancelled=cancelled)
                if previous is not None:
                    current = self.get(previous.id)
                    if current.fingerprint != previous.fingerprint:
                        raise ValueError('This workflow changed after review. Reload it before saving.')
                elif len(existing) >= MAX_TEMPLATES:
                    raise ValueError('The workflow collection has reached 256 records.')
                retained = [item for item in existing if item.id != value.id]
                if any(item.name.casefold() == value.name.casefold() for item in retained):
                    raise ValueError('Choose a distinct name for this workflow template.')
                retained_bytes = sum(max(len(encode(item.to_dict())),
                    regular(self._path(item.id)).st_size) for item in retained)
                if retained_bytes + len(encode(value.to_dict())) > MAX_COLLECTION_BYTES:
                    raise ValueError('The workflow collection exceeds 64 MiB.')
                path = self._path(value.id)
                _check_cancelled(cancelled)
                try:
                    atomic_json(path, value.to_dict(), replace=previous is not None, cancelled=cancelled)
                except FileExistsError:
                    # A create-only competitor is never this attempt's receipt,
                    # even when its payload happens to be byte-for-byte equal.
                    raise
                except OSError as error:
                    try:
                        committed = self.get(value.id).fingerprint == value.fingerprint
                    except (OSError, ValueError):
                        committed = False
                    if committed:
                        raise TemplateChangeCommittedError('save', value) from error
                    raise
                committed = True
        except TemplateChangeCommittedError:
            raise
        except Exception as error:
            if committed:
                raise TemplateChangeCommittedError('save', value) from error
            raise
        return value

    def remove(self, previous, *, cancelled=lambda: False):
        _check_cancelled(cancelled)
        if not isinstance(previous, WorkflowTemplate):
            raise ValueError('Review the exact workflow before removing it.')
        committed = False
        try:
            with self._writer():
                if self.get(previous.id).fingerprint != previous.fingerprint:
                    raise ValueError('This workflow changed after review. Reload it before removal.')
                path = self._path(previous.id)
                _check_cancelled(cancelled)
                path.unlink()
                committed = True
                sync_directory(self.root)
        except Exception as error:
            if committed:
                raise TemplateChangeCommittedError('removal', previous) from error
            raise

    def read_import(self, path):
        """Read one inert candidate before choosing its local name; never save it."""
        self._current()
        return WorkflowTemplate.from_dict(read_json(local_path(path, exists=True), maximum=MAX_TEMPLATE_BYTES))

    def import_file(self, path, *, name=None, cancelled=lambda: False):
        _check_cancelled(cancelled)
        value = self.read_import(path)
        # Import is a new identity/revision and can never replace an existing record.
        return self.save(value.name if name is None else name, value.draft, cancelled=cancelled)

    def export_json(self, previous):
        if not isinstance(previous, WorkflowTemplate):
            raise ValueError('Review the exact workflow before exporting it.')
        current = self.get(previous.id)
        if current.fingerprint != previous.fingerprint:
            raise ValueError('This workflow changed after review. Reload it before export.')
        return json.dumps(current.to_dict(), ensure_ascii=False, indent=2, allow_nan=False) + '\n'


def encode(value):
    raw = (canonical(value) + '\n').encode('utf-8')
    if len(raw) > MAX_TEMPLATE_BYTES:
        raise ValueError('The workflow template exceeds its 8 MiB record budget.')
    return raw


def read_json(path, *, maximum=MAX_TEMPLATE_BYTES):
    regular(path)
    return _read_json(path, limit=maximum)


def atomic_json(path, value, *, replace=False, cancelled=lambda: False):
    """Use the current atomic JSON publisher; never link or unlink a target."""
    path = local_path(path)
    regular(path.parent, directory=True)
    if optional_present(path):
        regular(path)
        if not replace:
            raise FileExistsError(path)
    payload = encode(value)
    _check_cancelled(cancelled)
    descriptor, name = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    stage = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _check_cancelled(cancelled)
        if replace:
            regular(path)
            replace_json_file(stage, path)
            sync_directory(path.parent)
        else:
            publish_noreplace(stage, path, cancelled=cancelled)
    finally:
        stage.unlink(missing_ok=True)


def is_template_path(relative):
    return (isinstance(relative, str) and relative.startswith(PREFIX)
            and re.fullmatch(r'[0-9a-f]{32}\.json', relative[len(PREFIX):]) is not None)


def template_paths(library_root, *, cancelled=lambda: False):
    """Complete bounded inventory; only the donor lock and private stages are ignored."""
    root = local_path(library_root, exists=True)
    folder = local_path(root / PREFIX.rstrip('/'), root=root)
    if not optional_present(folder):
        return ()
    regular(folder, directory=True)
    paths, total = [], 0
    transient = re.compile(r'\.pending-[A-Za-z0-9_-]{8,32}\Z')
    with os.scandir(folder) as entries:
        for count, entry in enumerate(entries, 1):
            _check_cancelled(cancelled)
            if count > MAX_TEMPLATES * 2 + 16:
                raise ValueError('Workflow template entries exceed the finite inventory budget.')
            path = Path(entry.path)
            info = regular(path)
            if path.name == '.workflow-templates.lock':
                if info.st_size > 1:
                    raise ValueError('Invalid workflow template lock file.')
                continue
            if transient.fullmatch(path.name):
                if info.st_size > MAX_TEMPLATE_BYTES:
                    raise ValueError('A pending workflow template exceeds its byte budget.')
                continue
            relative = path.relative_to(root).as_posix()
            if not is_template_path(relative):
                raise ValueError('A workflow template has an unexpected filename.')
            if not 0 < info.st_size <= MAX_TEMPLATE_BYTES:
                raise ValueError('The workflow template exceeds its 8 MiB record budget.')
            total += info.st_size
            if len(paths) >= MAX_TEMPLATES or total > MAX_COLLECTION_BYTES:
                raise ValueError('Workflow templates exceed 256 records or 64 MiB.')
            paths.append(path)
    _check_cancelled(cancelled)
    return tuple(sorted(paths))


@contextmanager
def template_snapshot_guard(library_root, *, writing=False):
    """Call under the current assistant state guard; legacy same-name writers conflict."""
    root = local_path(library_root, exists=True)
    folder = local_path(root / PREFIX.rstrip('/'), root=root)
    if not optional_present(folder):
        if writing:
            raise ValueError('The selected workflow directory is unavailable.')
        yield
        return
    regular(folder, directory=True)
    lock = folder / '.workflow-templates.lock'
    if optional_present(lock) and regular(lock).st_size > 1:
        raise ValueError('Invalid workflow template lock file.')
    with _native_guard(lock, snapshot=not writing):
        yield
