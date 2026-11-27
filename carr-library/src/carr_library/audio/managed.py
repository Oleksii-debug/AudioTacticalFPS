"""Managed Audio Studio documents inside the existing private library.

Only one reserved project namespace is writable. Source assets, databases,
arbitrary project-selected paths and external output folders are not admitted.
The library writer guard and draft lock are shared with complete backups.
"""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import tempfile

from filelock import FileLock, Timeout

from ..ingest.writer_guard import exclusive_writer
from ..reader.narration_text import read_document, decode_json
from .jobs import checked_directory, regular_file, _sync_directory


MAX_DOCUMENT_BYTES = 64 * 1024 ** 2


class ManagedProjects:
    def __init__(self, library_root):
        self.library = Path(library_root).absolute()

    def paths(self, project_id):
        if not isinstance(project_id, str) or not re.fullmatch('[0-9a-f]{32}', project_id):
            raise ValueError('Invalid managed Audio Studio project identity.')
        root = self.library / 'audio-projects' / project_id
        return root, root / 'studio' / 'project.json', root / 'audio'

    def locate(self, path, project_id=None):
        """Return the managed root, None for external files; reject other internals."""
        selected = Path(path).absolute()
        try:
            parts = selected.relative_to(self.library).parts
        except ValueError:
            return None
        if (len(parts) != 4 or parts[0] != 'audio-projects'
                or not re.fullmatch('[0-9a-f]{32}', parts[1]) or parts[2] != 'studio'
                or parts[3] not in ('project.json', 'project.json.recovery.json')):
            raise ValueError('Use the reserved Audio Studio project folder; library sources cannot be replaced.')
        if project_id is not None and parts[1] != project_id:
            raise ValueError('The Audio Studio document belongs to a different managed project.')
        if selected.parent.exists():
            checked_directory(selected.parent)
        return self.paths(parts[1])[0]

    @contextmanager
    def writer(self, project_id):
        with exclusive_writer(self.library, timeout=0):
            root, document, audio = self.paths(project_id)
            checked_directory(root, create=True)
            checked_directory(document.parent, create=True)
            checked_directory(audio, create=True)
            lock_path = root / '.draft.lock'
            if os.path.lexists(lock_path):
                info = regular_file(lock_path)
                if info.st_nlink != 1 or info.st_size != 0:
                    raise ValueError('The managed project draft lock is not a regular lock file.')
            try:
                with FileLock(lock_path, mode=0o600).acquire(timeout=0):
                    yield root, document, audio
            except Timeout as exc:
                raise RuntimeError('The audio project is being saved or backed up. Try again after that operation.') from exc

    @staticmethod
    def _state(path):
        if not os.path.lexists(path):
            return None
        info = regular_file(path)
        if info.st_nlink != 1:
            raise ValueError('A managed Audio Studio document cannot be a hard link.')
        data = read_document(path, byte_budget=MAX_DOCUMENT_BYTES)
        return ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns),
                hashlib.sha256(data).hexdigest())

    def write(self, project_id, path, value, *, expected_sha256=None, cancelled=lambda: False):
        """Commit exact UTF-8 bytes with conflict detection and crash-safe publication."""
        selected = Path(path).absolute()
        if self.locate(selected, project_id) is None:
            raise ValueError('Managed saves require a file inside this library project.')
        payload = value.encode('utf-8')
        if len(payload) > MAX_DOCUMENT_BYTES:
            raise ValueError('The Audio Studio document exceeds its save budget.')
        temporary = None
        with self.writer(project_id):
            if cancelled():
                raise TimeoutError('Audio Studio save cancelled; the committed project is unchanged.')
            previous = self._state(selected)
            if ((previous is None and expected_sha256 is not None)
                    or (previous is not None and previous[1] != expected_sha256)):
                raise ValueError('The saved project changed outside Audio Studio. Use Save As to preserve both versions.')
            try:
                descriptor, name = tempfile.mkstemp(prefix='.studio-save-', dir=selected.parent)
                temporary = Path(name)
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                if cancelled():
                    raise TimeoutError('Audio Studio save cancelled; the committed project is unchanged.')
                if self._state(selected) != previous:
                    raise ValueError('The saved project changed during Audio Studio save.')
                if previous is None:
                    os.link(temporary, selected)
                    # Retire our private twin before the directory commit. A
                    # process exit after that commit must leave an ordinary
                    # one-link document that readers and backups can reopen.
                    temporary.unlink()
                    temporary = None
                else:
                    os.replace(temporary, selected)
                _sync_directory(selected.parent)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return selected

    def projects(self, *, metadata_budget=32 * 1024 ** 2):
        """List owned documents for the keyboard chooser with a bounded metadata read."""
        base = self.library / 'audio-projects'
        if not base.exists():
            return ()
        checked_directory(base)
        result, used = [], 0
        for directory in sorted(base.iterdir()):
            if not re.fullmatch('[0-9a-f]{32}', directory.name):
                continue
            checked_directory(directory)
            path = directory / 'studio' / 'project.json'
            if not os.path.lexists(path):
                continue
            info = regular_file(path)
            used += info.st_size
            if used > metadata_budget:
                raise ValueError('The project list exceeds its metadata budget. Open a specific saved project file.')
            payload = decode_json(read_document(path, byte_budget=metadata_budget))
            if (not isinstance(payload, dict) or payload.get('kind') != 'research-audio-studio'
                    or payload.get('project_id') != directory.name or not isinstance(payload.get('title'), str)):
                raise ValueError('A saved managed Audio Studio project is invalid.')
            result.append((payload['title'], path))
        return tuple(result)
