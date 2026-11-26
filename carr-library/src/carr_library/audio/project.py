"""Atomic local project saves and checksum-verified portable audio projects."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile

from filelock import FileLock, Timeout

from .contracts import AudioProject

MAX_PROJECT_BYTES = 32 * 1024 * 1024  # parser/resource budget, not an audio-duration ceiling


def file_sha256(path, *, cancel=None):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            _check_cancel(cancel)
            h.update(block)
    return h.hexdigest()


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field.")
        result[key] = value
    return result


def _read_document(path, *, maximum=MAX_PROJECT_BYTES):
    path = _local_path(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Expected a regular local JSON file.")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Expected a regular local JSON file.")
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("JSON exceeds the configured input resource budget.")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON number.")))
        return document, raw
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Invalid UTF-8 JSON document.") from exc


def read_json(path, *, maximum=MAX_PROJECT_BYTES):
    return _read_document(path, maximum=maximum)[0]


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                       allow_nan=False) + "\n").encode("utf-8")


def _fsync_directory(path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_bytes(path, data, *, overwrite=False):
    """Publish complete bytes. A new destination is never silently replaced."""
    path = _local_path(path)
    local_directory(path.parent)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Unsafe output destination.")
    fd, name = tempfile.mkstemp(prefix=".audio-save-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _local_path(path):
    """Check every existing ancestor before creating, reading or publishing.

    Resolving first would hide an ancestor link or a Windows junction. Reparse
    points and mapped network drives are refused in the local-only profile.
    """
    path = Path(path).expanduser()
    raw = str(path)
    if any(ord(char) < 32 for char in raw) or raw.startswith(("\\\\", "//")) or re.match(r"^[A-Za-z][A-Za-z0-9+.-]+:[/\\]", raw):
        raise ValueError("Audio projects must use a local directory.")
    path = path.absolute()
    if ".." in path.parts:
        raise ValueError("Choose a direct local path without parent-directory traversal.")
    if os.name == "nt":
        import ctypes
        drive_type = ctypes.windll.kernel32.GetDriveTypeW
        drive_type.argtypes = (ctypes.c_wchar_p,)
        drive_type.restype = ctypes.c_uint
        if drive_type(path.anchor) == 4:
            raise ValueError("Mapped network drives are unavailable in LOCAL ONLY mode.")
    for current in (*reversed(path.parents), path):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Choose a real local project path without symbolic links or reparse points.")
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError("A project path ancestor is not a directory.")
    return path


def local_directory(path, *, create=False):
    path = _local_path(path)
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        _local_path(path)
    if not path.is_dir():
        raise ValueError("Project directory does not exist.")
    return path.resolve()


def publish_directory(source, destination):
    """Atomically publish a complete sibling directory without replacing one.

    POSIX rename alone can remove a concurrently created empty directory.
    Linux RENAME_NOREPLACE and Windows rename both fail if a destination exists.
    Other platforms fail safely instead of silently weakening this contract.
    """
    source, destination = local_directory(source), _local_path(destination)
    parent = local_directory(destination.parent)
    if source.parent != parent:
        raise ValueError("Directory publication requires staging on the destination volume.")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    _fsync_directory(source)
    if os.name == "nt":
        os.rename(source, destination)  # Windows refuses any existing destination.
    elif sys.platform.startswith("linux"):
        import ctypes
        import errno
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise OSError(errno.ENOTSUP, "Atomic no-replace directory publication is unavailable.")
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            if rename(descriptor, os.fsencode(source.name), descriptor, os.fsencode(destination.name), 1) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error), str(destination))
        finally:
            os.close(descriptor)
    else:
        raise OSError("Atomic no-replace directory publication is unsupported on this platform.")
    _fsync_directory(parent)


def contained_file(root, relative):
    """Resolve only regular, non-linked files contained in a selected project."""
    root = local_directory(root)
    if not isinstance(relative, str) or "\\" in relative or any(ord(char) < 32 for char in relative):
        raise ValueError("Invalid portable file name.")
    parts = PurePosixPath(relative)
    if parts.is_absolute() or not parts.parts or any(p in (".", "..") or ":" in p for p in parts.parts):
        raise ValueError("File name is outside the project.")
    candidate = _local_path(root.joinpath(*parts.parts))
    current = root
    for part in parts.parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("Linked files are not allowed in an audio project.")
    if not stat.S_ISREG(candidate.stat().st_mode) or not candidate.resolve().is_relative_to(root):
        raise ValueError("Expected a regular project file.")
    return candidate


@contextmanager
def project_lock(root, name="project", *, timeout=0):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Invalid project lock identity.")
    lock_path = local_directory(root) / f".{name}.lock"
    if lock_path.is_symlink():
        raise ValueError("Unsafe project lock.")
    if lock_path.exists():
        info = lock_path.lstat()
        # filelock opens a lock path with O_TRUNC on POSIX. A pre-existing
        # hardlink must never let lock acquisition truncate a source or draft.
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Project lock must be an independent regular file.")
    try:
        with FileLock(lock_path, timeout=timeout, mode=0o600):
            yield
    except Timeout as exc:
        raise RuntimeError("This audio project is busy in another operation. Retry when it finishes.") from exc


@dataclass(frozen=True)
class LoadedProject:
    project: AudioProject
    file_sha256: str


class ProjectStore:
    def __init__(self, root):
        self.root = local_directory(root)
        self.path = self.root / "project.json"

    @classmethod
    def create(cls, root, project):
        project.validate()
        root = _local_path(root)
        local_directory(root.parent, create=True)
        # A selected non-empty folder is never appropriated as a project.
        root.mkdir(exist_ok=False, mode=0o700)
        store = cls(root)
        try:
            store.save(project)
        except BaseException:
            # Preserve anything created concurrently; only remove our empty dir.
            try:
                if not tuple(root.iterdir()):
                    root.rmdir()
            except OSError:
                pass
            raise
        return store

    def load(self):
        with project_lock(self.root):
            document, raw = _read_document(self.path)
            result = AudioProject.from_dict(document)
            checksum = hashlib.sha256(raw).hexdigest()
            return LoadedProject(result, checksum)

    def save(self, project, *, expected_sha256=None):
        data = json_bytes(project.to_dict())
        if len(data) > MAX_PROJECT_BYTES:
            raise ValueError("Project exceeds the configured input resource budget.")
        with project_lock(self.root):
            if self.path.exists() or self.path.is_symlink():
                contained_file(self.root, "project.json")
                if expected_sha256 is None or file_sha256(self.path) != expected_sha256:
                    raise ValueError("The project has changed on disk. Reload it or use Save As; unsaved text is retained.")
                overwrite = True
            else:
                if expected_sha256 is not None:
                    raise ValueError("The original project file is missing. Use Save As.")
                overwrite = False
            atomic_bytes(self.path, data, overwrite=overwrite)
        return hashlib.sha256(data).hexdigest()

    def backup(self, destination, *, include_audio=True, cancel=None):
        """Copy a coherent snapshot of completed checkpoints, even during render.

        The journal is atomically replaced; only its completed immutable cache
        objects are included. In-flight private staging files are never copied.
        """
        if type(include_audio) is not bool:
            raise ValueError("Choose explicitly whether completed audio belongs in the backup.")
        from .drafts import DraftStore
        _check_cancel(cancel)
        destination = _local_path(destination)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        if destination.resolve().is_relative_to(self.root):
            raise ValueError("Choose a backup destination outside the project.")
        local_directory(destination.parent, create=True)
        stage = Path(tempfile.mkdtemp(prefix=".audio-backup-", dir=destination.parent))
        try:
            with project_lock(self.root):
                original = contained_file(self.root, "project.json")
                document, payload = _read_document(original)
                AudioProject.from_dict(document)
                drafts = DraftStore(self.root).snapshots(cancel=cancel)
            atomic_bytes(stage / "project.json", payload)
            files = {"project.json": hashlib.sha256(payload).hexdigest()}
            for relative, _draft, raw in drafts:
                _check_cancel(cancel)
                target = stage / relative
                local_directory(target.parent, create=True)
                atomic_bytes(target, raw)
                files[relative] = hashlib.sha256(raw).hexdigest()
            journal_path = self.root / "render-state.json"
            if include_audio and journal_path.exists():
                state = read_json(journal_path)
                completed = completed_checkpoints(state)
                state = dict(state)
                entries = {}
                for key, entry in completed.items():
                    _check_cancel(cancel)
                    relative = entry.get("path")
                    source = contained_file(self.root, relative)
                    checksum = entry.get("sha256")
                    if source.stat().st_size != entry["bytes"] or file_sha256(source, cancel=cancel) != checksum:
                        raise ValueError("A completed audio checkpoint is corrupt; backup was not published.")
                    target = stage / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    if file_sha256(target, cancel=cancel) != checksum:
                        raise ValueError("Audio checkpoint changed during backup.")
                    with target.open("r+b") as output:
                        os.fsync(output.fileno())
                    files[relative] = checksum
                    entries[key] = entry
                state.update(entries=entries, status="interrupted", current=None,
                             message="Restored checkpoint snapshot; render to resume verified completed work.")
                state.pop("manifest", None)
                atomic_bytes(stage / "render-state.json", json_bytes(state))
                files["render-state.json"] = file_sha256(stage / "render-state.json")
            manifest = {"schema_version": 1, "kind": "carr-audio-backup", "files": files,
                        "includes_completed_audio": bool(include_audio)}
            atomic_bytes(stage / "backup-manifest.json", json_bytes(manifest))
            _check_cancel(cancel)
            publish_directory(stage, destination)
            return file_sha256(destination / "backup-manifest.json")
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def _check_cancel(cancel):
    if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
        raise InterruptedError("Audio operation cancelled; completed work is retained.")


def restore_project(backup, destination, *, manifest_sha256, cancel=None):
    from .drafts import DraftStore, draft_path
    backup = local_directory(backup)
    manifest_path = contained_file(backup, "backup-manifest.json")
    manifest, raw = _read_document(manifest_path)
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise ValueError("Audio backup manifest checksum does not match.")
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1 or manifest.get("kind") != "carr-audio-backup":
        raise ValueError("Unsupported audio backup.")
    files = manifest.get("files")
    if not isinstance(files, dict) or "project.json" not in files:
        raise ValueError("Incomplete audio backup manifest.")
    if type(manifest.get("includes_completed_audio")) is not bool or (not manifest["includes_completed_audio"] and any(name != "project.json" and not draft_path(name) for name in files)):
        raise ValueError("Audio backup contents differ from its declared scope.")
    _check_cancel(cancel)
    destination = _local_path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    local_directory(destination.parent, create=True)
    stage = Path(tempfile.mkdtemp(prefix=".audio-restore-", dir=destination.parent))
    try:
        for relative, checksum in files.items():
            _check_cancel(cancel)
            source = contained_file(backup, relative)
            if not (relative in ("project.json", "render-state.json") or re_cache_path(relative) or draft_path(relative)):
                raise ValueError("Unsupported file in audio backup.")
            if not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise ValueError("Invalid backup artifact checksum.")
            if file_sha256(source, cancel=cancel) != checksum:
                raise ValueError("Corrupted or incomplete audio backup.")
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if file_sha256(target, cancel=cancel) != checksum:
                raise ValueError("Backup changed while restoring.")
            with target.open("r+b") as output:
                os.fsync(output.fileno())
        AudioProject.from_dict(read_json(stage / "project.json"))
        DraftStore(stage).snapshots(cancel=cancel)
        cache_files = {name for name in files if re_cache_path(name)}
        if "render-state.json" in files:
            state = read_json(stage / "render-state.json")
            completed = completed_checkpoints(state)
            if len(completed) != len(state["entries"]) or cache_files != {entry["path"] for entry in completed.values()}:
                raise ValueError("Backup checkpoint journal and cache inventory disagree.")
            for entry in completed.values():
                if files.get(entry["path"]) != entry["sha256"] or (stage / entry["path"]).stat().st_size != entry["bytes"]:
                    raise ValueError("Backup checkpoint checksum or size differs from its manifest.")
        elif cache_files:
            raise ValueError("Audio backup is missing its checkpoint journal.")
        _check_cancel(cancel)
        publish_directory(stage, destination)
        return ProjectStore(destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def re_cache_path(value):
    return isinstance(value, str) and bool(re.fullmatch(r"cache/[0-9a-f]{64}\.(wav|flac|mid)", value))


def completed_checkpoints(state):
    """Validate journal identities before copying any checkpoint payload."""
    if not isinstance(state, dict) or type(state.get("schema_version")) is not int or state.get("schema_version") != 1 or not isinstance(state.get("entries"), dict):
        raise ValueError("Invalid audio checkpoint journal.")
    complete = {}
    for key, entry in state["entries"].items():
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(entry, dict):
            raise ValueError("Invalid checkpoint entry identity.")
        relative = entry.get("path")
        if not re_cache_path(relative) or Path(relative).stem != key:
            raise ValueError("Checkpoint path must match its exact cache identity.")
        if entry.get("status") not in ("running", "complete", "failed", "interrupted"):
            raise ValueError("Invalid checkpoint state.")
        if entry["status"] == "complete":
            if not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) or type(entry.get("bytes")) is not int or entry["bytes"] < 1:
                raise ValueError("Completed checkpoint requires a valid checksum and size.")
            complete[key] = entry
    return complete
