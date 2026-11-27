"""Managed Audio Studio documents inside the existing private library.

Only one reserved project namespace is writable. Source assets, databases,
arbitrary project-selected paths and external output folders are not admitted.
The library writer guard and draft lock are shared with complete backups.
"""
from contextlib import contextmanager, ExitStack
import hashlib
import os
from pathlib import Path
import re
import tempfile
import warnings

from filelock import FileLock, Timeout

from ..ingest.writer_guard import exclusive_writer
from ..reader.narration_text import read_document, decode_json
# The shared primitive performs an exclusive native rename for files as well
# as directories, without post-publication cleanup or durability callbacks.
from ..file_publication import publish_directory_no_replace as _publish_new
from .jobs import checked_directory, regular_file, project_lock, _sync_directory


MAX_DOCUMENT_BYTES = 64 * 1024 ** 2
MAX_PROJECTS = 1000
MAX_INPUT_FILES = 10000
MAX_INPUT_BYTES = 128 * 1024 ** 3
MAX_INPUT_FILE_BYTES = 32 * 1024 ** 3


class MissingInputAssets(ValueError):
    """Only explicit folder selection may authorize missing external inputs."""
    def __init__(self, names):
        self.names = tuple(sorted(names))
        shown = ', '.join(self.names[:5]) + (' …' if len(self.names) > 5 else '')
        super().__init__('Choose a local folder containing these referenced PCM sound inputs: ' + shown)


def _check(cancelled):
    if not callable(cancelled):
        raise ValueError('A cancellation callback is required.')
    if cancelled():
        raise TimeoutError('Audio Studio save cancelled; the committed project is unchanged.')
    return False


class ManagedProjects:
    def __init__(self, library_root):
        self.library = Path(library_root).absolute()
        self._root_identity = None
        self.last_warning = None
        if os.path.lexists(self.library):
            self.check_root()

    def check_root(self, library_root=None):
        if library_root is not None and Path(library_root).absolute() != self.library:
            raise ValueError('The Audio Studio library changed. Open the project again.')
        checked_directory(self.library)
        info = self.library.lstat()
        identity = info.st_dev, info.st_ino
        if self._root_identity is not None and identity != self._root_identity:
            raise ValueError('The Audio Studio library folder changed. Open the project again.')
        self._root_identity = identity
        return self.library

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
        self.check_root()
        with exclusive_writer(self.library, timeout=0):
            with self.lock(project_id, create=True) as paths:
                yield paths

    @contextmanager
    def lock(self, project_id, *, create=False):
        """Share native draft/render locks; callers hold the library writer first."""
        self.check_root()
        root, document, audio = self.paths(project_id)
        checked_directory(root, create=create)
        checked_directory(document.parent, create=create)
        checked_directory(audio, create=create)
        lock_path = root / '.draft.lock'
        if os.path.lexists(lock_path):
            info = regular_file(lock_path)
            if info.st_nlink != 1 or info.st_size != 0:
                raise ValueError('The managed project draft lock is not a regular lock file.')
        try:
            with FileLock(lock_path, mode=0o600).acquire(timeout=0):
                with project_lock(audio):
                    self.check_root()
                    yield root, document, audio
        except Timeout as exc:
            raise RuntimeError('The audio project is being saved or backed up. Try again after that operation.') from exc

    @staticmethod
    def _identity(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    @staticmethod
    def _state(path):
        if not os.path.lexists(path):
            return None
        info = regular_file(path)
        if info.st_nlink != 1:
            raise ValueError('A managed Audio Studio document cannot be a hard link.')
        if not 0 < info.st_size <= MAX_DOCUMENT_BYTES:
            raise ValueError('The Audio Studio document exceeds its save budget.')
        identity = ManagedProjects._identity(info)
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(path, flags), 'rb') as stream:
            if ManagedProjects._identity(os.fstat(stream.fileno())) != identity:
                raise ValueError('The Audio Studio document changed while opening.')
            data = stream.read(MAX_DOCUMENT_BYTES + 1)
            if ManagedProjects._identity(os.fstat(stream.fileno())) != identity:
                raise ValueError('The Audio Studio document changed while reading.')
        if len(data) != info.st_size or ManagedProjects._identity(regular_file(path)) != identity:
            raise ValueError('The Audio Studio document changed while reading.')
        return identity, hashlib.sha256(data).hexdigest()

    def input_plan(self, project, input_directory=None, *, cancelled=lambda: False):
        """Resolve exact typed names; never consult a saved output_dir pointer."""
        from .backup import _asset_references, _local, _safe
        from .ffmpeg import wav_info
        from ..research.export_file import _filename
        self.check_root()
        _check(cancelled)
        required = _asset_references(project, file_budget=MAX_INPUT_FILES,
                                     cancelled=lambda: _check(cancelled))
        if len({name.casefold() for name in required}) != len(required):
            raise ValueError('Referenced sound filenames collide on a case-insensitive filesystem.')
        folder = None
        folder_identity = None
        if input_directory is not None:
            folder = _local(input_directory)
            folder_identity = self._identity(_safe(folder, directory=True))[:2]
        assets = self.paths(project['project_id'])[2] / 'assets'
        if os.path.lexists(assets):
            _safe(assets, directory=True)
        existing, sources, missing, total = {}, [], [], 0
        for name in sorted(required):
            _check(cancelled)
            _filename(name)
            target = assets / name
            source = target if os.path.lexists(target) else folder / name if folder is not None else None
            if source is None or not os.path.lexists(source):
                missing.append(name)
                continue
            info = _safe(source)
            if info.st_nlink != 1 or not 1 <= info.st_size <= MAX_INPUT_FILE_BYTES:
                raise ValueError('Sound inputs must be bounded ordinary files without hard links.')
            total += info.st_size
            if total > MAX_INPUT_BYTES:
                raise ValueError('The referenced sound inputs exceed the import byte budget.')
            identity = self._identity(info)
            wav_info(source)
            if self._identity(_safe(source)) != identity:
                raise ValueError('A sound input changed during validation.')
            if source == target:
                existing[target] = identity
                candidate = folder / name if folder is not None else None
                if candidate is not None and candidate != target and os.path.lexists(candidate):
                    selected_info = _safe(candidate)
                    selected_identity = self._identity(selected_info)
                    if selected_info.st_nlink != 1 or selected_info.st_size != info.st_size:
                        raise ValueError('A selected sound input conflicts with the existing library filename.')
                    from .library_sidecars import _copy
                    guard = lambda: _check(cancelled)
                    retained = _copy(target, None, maximum=MAX_INPUT_FILE_BYTES, cancelled=guard)
                    selected = _copy(candidate, None, maximum=MAX_INPUT_FILE_BYTES, cancelled=guard)
                    if (retained != selected or self._identity(_safe(target)) != identity
                            or self._identity(_safe(candidate)) != selected_identity):
                        raise ValueError('A selected sound input conflicts with the existing library filename.')
                    existing[candidate] = selected_identity
            else:
                sources.append((source, target, identity))
        if missing:
            raise MissingInputAssets(missing)
        return existing, tuple(sources), (folder, folder_identity) if folder is not None else None

    def write(self, project_id, path, value, *, expected_sha256=None,
              expected_project_sha256=None, cancelled=lambda: False,
              input_directory=None, require_inputs=False):
        """Commit exact UTF-8 bytes with conflict detection and crash-safe publication."""
        selected = Path(path).absolute()
        if self.locate(selected, project_id) is None:
            raise ValueError('Managed saves require a file inside this library project.')
        if not isinstance(value, str) or not callable(cancelled):
            raise ValueError('A text project and cancellation callback are required.')
        payload = value.encode('utf-8')
        if not 0 < len(payload) <= MAX_DOCUMENT_BYTES:
            raise ValueError('The Audio Studio document exceeds its save budget.')
        from .backup import _studio
        document = decode_json(value)
        recovery = selected.name == 'project.json.recovery.json'
        if recovery:
            if (type(document) is not dict
                    or set(document) != {'kind', 'version', 'project_id', 'base_sha256', 'project'}
                    or document['kind'] != 'audio-studio-recovery'
                    or type(document['version']) is not int or document['version'] != 1
                    or document['project_id'] != project_id
                    or not isinstance(document['base_sha256'], str)
                    or re.fullmatch('[0-9a-f]{64}', document['base_sha256']) is None):
                raise ValueError('The Audio Studio recovery document is invalid.')
            project = _studio(document['project'])
        else:
            project = _studio(document)
        if (project['project_id'] != project_id
                or project['output_dir'] != str(self.paths(project_id)[2])):
            raise ValueError('Managed project identity or audio folder differs from its reserved location.')
        if type(require_inputs) is not bool or (recovery and (require_inputs or input_directory is not None)):
            raise ValueError('Input adoption requires a saved project document.')
        self.last_warning = None
        temporary = None
        _check(cancelled)
        with self.writer(project_id) as (owned, project_path, audio), ExitStack() as staging:
            _check(cancelled)
            parent_identity = self._identity(selected.parent.lstat())[:2]
            previous = self._state(selected)
            if ((previous is None and expected_sha256 is not None)
                    or (previous is not None and previous[1] != expected_sha256)):
                raise ValueError('The saved project changed outside Audio Studio. Use Save As to preserve both versions.')
            base_state = self._state(project_path) if recovery else None
            if recovery and (base_state is None or base_state[1] != document['base_sha256']
                    or (expected_project_sha256 is not None and base_state[1] != expected_project_sha256)):
                raise ValueError('The recovery draft belongs to a different saved project revision.')
            existing, sources, source_root = {}, (), None
            prepared, published, committed = [], [], False
            if require_inputs or input_directory is not None:
                existing, sources, source_root = self.input_plan(project, input_directory, cancelled=cancelled)
            if sources:
                from .library_sidecars import _copy
                from .ffmpeg import wav_info
                stage = Path(staging.enter_context(tempfile.TemporaryDirectory(prefix='.studio-inputs-', dir=owned)))
                for source, target, identity in sources:
                    _check(cancelled)
                    if self._identity(regular_file(source)) != identity:
                        raise ValueError('A selected sound input changed before copying.')
                    copied = stage / target.name
                    receipt = _copy(source, copied, maximum=MAX_INPUT_FILE_BYTES,
                                    cancelled=lambda: _check(cancelled))
                    copy_identity = self._identity(regular_file(copied))
                    wav_info(copied)
                    _copy(copied, None, maximum=MAX_INPUT_FILE_BYTES, expected=receipt,
                          cancelled=lambda: _check(cancelled))
                    if self._identity(regular_file(copied)) != copy_identity:
                        raise ValueError('A staged sound input changed during verification.')
                    if self._identity(regular_file(source)) != identity:
                        raise ValueError('A selected sound input changed while copying.')
                    prepared.append((copied, target, copy_identity, receipt))
            try:
                descriptor, name = tempfile.mkstemp(prefix='.studio-save-', dir=selected.parent)
                temporary = Path(name)
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                _check(cancelled)
                self.check_root()
                checked_directory(selected.parent)
                if (self._identity(selected.parent.lstat())[:2] != parent_identity
                        or self._state(selected) != previous
                        or (recovery and self._state(project_path) != base_state)):
                    raise ValueError('The saved project changed during Audio Studio save.')
                if source_root is not None:
                    source_folder, source_identity = source_root
                    checked_directory(source_folder)
                    if self._identity(source_folder.lstat())[:2] != source_identity:
                        raise ValueError('The selected sound input folder changed.')
                if any(self._identity(regular_file(path)) != identity for path, identity in existing.items()):
                    raise ValueError('An existing library sound input changed.')
                if prepared:
                    # Verify every private staged file before creating the
                    # destination namespace. Windows metadata alone may hide
                    # an equal-size write after the initial readback.
                    for copied, _target, _copied_identity, receipt in prepared:
                        _copy(copied, None, maximum=MAX_INPUT_FILE_BYTES, expected=receipt,
                              cancelled=lambda: _check(cancelled))
                    assets = checked_directory(audio / 'assets', create=True)
                    asset_identity = self._identity(assets.lstat())[:2]
                    for copied, target, copy_identity, receipt in prepared:
                        _check(cancelled)
                        checked_directory(assets)
                        self.check_root()
                        if self._identity(assets.lstat())[:2] != asset_identity:
                            raise ValueError('The owned sound input folder changed.')
                        if self._identity(regular_file(copied)) != copy_identity:
                            raise ValueError('A staged sound input changed before publication.')
                        _publish_new(copied, target)
                        actual = self._identity(regular_file(target))
                        if actual[:3] != copy_identity[:3] or actual[4:6] != copy_identity[4:6]:
                            raise ValueError('A sound input changed during publication.')
                        published.append((target, actual, receipt))
                    _sync_directory(assets)
                    for _copied, target, _copied_identity, receipt in prepared:
                        _copy(target, None, maximum=MAX_INPUT_FILE_BYTES, expected=receipt,
                              cancelled=lambda: _check(cancelled))
                    receipts = {target: receipt for _, target, _, receipt in prepared}
                    for source, target, _source_identity in sources:
                        _copy(source, None, maximum=MAX_INPUT_FILE_BYTES, expected=receipts[target],
                              cancelled=lambda: _check(cancelled))
                # Dispose private input staging before the metadata commit. A
                # cleanup fault must not turn a committed adoption into failure.
                staging.close()
                _check(cancelled)
                self.check_root()
                if source_root is not None:
                    source_folder, source_identity = source_root
                    checked_directory(source_folder)
                    if self._identity(source_folder.lstat())[:2] != source_identity:
                        raise ValueError('The selected sound input folder changed.')
                if (any(self._identity(regular_file(path)) != identity for path, identity in existing.items())
                        or any(self._identity(regular_file(source)) != identity for source, _, identity in sources)
                        or any(self._identity(regular_file(path)) != identity for path, identity, _ in published)):
                    raise ValueError('A sound input changed before project publication.')
                if (self._identity(selected.parent.lstat())[:2] != parent_identity
                        or self._state(selected) != previous):
                    raise ValueError('The saved project changed before publication.')
                if previous is None:
                    _publish_new(temporary, selected)
                else:
                    os.replace(temporary, selected)
                committed = True
                temporary = None  # The committed document has no temporary alias.
                # Publication is the commit point. A later directory-sync fault
                # cannot truthfully be reported as an uncommitted project.
                try:
                    _sync_directory(selected.parent)
                except OSError:
                    self.last_warning = 'Audio Studio project saved; directory durability could not be confirmed.'
                    try:
                        warnings.warn(self.last_warning, RuntimeWarning, stacklevel=2)
                    except RuntimeWarning:
                        pass  # Warning filters cannot turn a committed save into a failure.
            finally:
                if not committed:
                    for target, identity, receipt in reversed(published):
                        try:
                            current = self._identity(regular_file(target))
                            if current == identity:
                                _copy(target, None, maximum=MAX_INPUT_FILE_BYTES,
                                      expected=receipt, cancelled=lambda: False)
                        except (OSError, ValueError):
                            continue
                        if current == identity and self._identity(regular_file(target)) == identity:
                            target.unlink()
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return selected

    def projects(self, *, metadata_budget=32 * 1024 ** 2):
        """List owned documents for the keyboard chooser with a bounded metadata read."""
        self.check_root()
        if type(metadata_budget) is not int or not 0 < metadata_budget <= MAX_DOCUMENT_BYTES * MAX_PROJECTS:
            raise ValueError('A bounded project-list budget is required.')
        base = self.library / 'audio-projects'
        if not base.exists():
            return ()
        checked_directory(base)
        result, used = [], 0
        directories = []
        with os.scandir(base) as children:
            for child in children:
                if len(directories) >= MAX_PROJECTS:
                    raise ValueError('The library has too many Audio Studio project entries.')
                directories.append(Path(child.path))
        for directory in sorted(directories):
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
            from .backup import _studio
            _studio(payload)
            if payload['project_id'] != directory.name:
                raise ValueError('A saved managed Audio Studio project is invalid.')
            result.append((payload['title'], path))
        return tuple(result)
