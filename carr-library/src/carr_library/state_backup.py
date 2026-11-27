"""Manifest-bound private application sidecars for complete library backups.

Snapshot committed JSON/project files and optionally generated audio. Ordinary
atomic writers may continue: a changing file causes a clear retryable failure.
Transcription admission/run locks remain held throughout a snapshot so backup
readers cannot disrupt an active Windows worker's output replacement. Rebuildable
audio cache is optional; restored renders verify and regenerate their chunks.
"""
from contextlib import ExitStack
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata

from .durable_state import local_path, regular, state_lock, sync_directory

MAX_STATE_FILES = 100_000
MAX_STATE_BYTES = 32 * 1024**3
MAX_STATE_PATH_LENGTH = 4096
MAX_STATE_PATH_DEPTH = 64


def relative_name(value):
    if not isinstance(value, str) or not value:
        raise ValueError('Invalid state manifest path.')
    # Bound parsing and ancestor expansion before constructing any Path objects.
    # A small manifest can otherwise contain a deeply nested, quadratic path.
    if len(value) > MAX_STATE_PATH_LENGTH:
        raise ValueError('State manifest path exceeds the 4096-character limit.')
    if value.count('/') + 1 > MAX_STATE_PATH_DEPTH:
        raise ValueError('State manifest path exceeds the 64-component depth limit.')
    if any(c in '<>:"\\|?*' or ord(c) < 32 for c in value):
        raise ValueError('Invalid state manifest path.')
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or str(path) != value
            or any(x in ('.', '..') or x.endswith((' ', '.')) for x in path.parts)):
        raise ValueError('Invalid state manifest path.')
    # Win32 also reserves superscript 1, 2 and 3 for COM/LPT devices.
    # A name valid on a POSIX source must not create an unrestorable backup.
    digits = '123456789\u00b9\u00b2\u00b3'
    reserved = {'con', 'prn', 'aux', 'nul', 'conin$', 'conout$',
                *(f'{prefix}{i}' for prefix in ('com', 'lpt') for i in digits)}
    if any(x.split('.')[0].rstrip(' ').casefold() in reserved for x in path.parts):
        raise ValueError('Invalid portable state filename.')
    return path


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _include(path, generated):
    return (not any(part.startswith('.pending-') or part.endswith(('.lock', '.tmp')) for part in path.parts)
            and (generated or 'cache' not in path.parts))


def _transcription_snapshot_guard(root, locks, cancelled):
    """Hold existing admission/run locks before inventory opens worker outputs."""
    admitted = {}
    transcription = root / 'transcription'

    def check_cancelled():
        if cancelled():
            raise InterruptedError('Backup cancelled.')

    def identity(path):
        value = regular(path, directory=True)
        return value.st_dev, value.st_ino

    def acquire(path, name):
        check_cancelled()
        try:
            locks.enter_context(state_lock(path, name))
        except RuntimeError:
            raise RuntimeError('Wait for transcription operations to finish or stop the active job '
                               'before creating a backup; pausing is not sufficient.') from None

    if os.path.lexists(transcription):
        local_path(transcription, root=root, exists=True)
        root_identity = identity(transcription)
        acquire(transcription, 'jobs')
        if identity(transcription) != root_identity:
            raise ValueError('Transcription state changed during backup; retry after it settles.')
        admitted[('transcription',)] = (transcription, root_identity)
        jobs = transcription / 'jobs'
        if os.path.lexists(jobs):
            local_path(jobs, root=transcription, exists=True)
            jobs_identity = identity(jobs)
            admitted[('transcription', 'jobs')] = (jobs, jobs_identity)
            # Match the public service's finite job budget without importing
            # the service at module load (it imports relative_name from here).
            from .transcription import MAX_JOBS
            selected = []
            for job in jobs.iterdir():
                check_cancelled()
                if not _include(job.relative_to(root), True):
                    continue
                # Classify the directory entry without following it. Even a
                # preflight is_dir() can follow an external link/reparse point.
                is_directory = stat.S_ISDIR(job.lstat().st_mode)
                regular(job, directory=is_directory)
                local_path(job, root=jobs, exists=True)
                if is_directory:
                    selected.append(job)
                    if len(selected) > MAX_JOBS:
                        raise ValueError('Transcription jobs exceed the backup admission budget.')
            for job in sorted(selected):
                check_cancelled()
                if not _include(job.relative_to(root), True):
                    continue
                local_path(job, root=jobs, exists=True)
                job_identity = identity(job)
                acquire(job, 'transcription')
                if identity(job) != job_identity:
                    raise ValueError('Transcription state changed during backup; retry after it settles.')
                key = ('transcription', 'jobs', job.name.casefold())
                if key in admitted:
                    raise ValueError('Transcription job names are ambiguous during backup.')
                admitted[key] = (job, job_identity)

    def guard(directory):
        parts = directory.relative_to(root).parts
        folded = tuple(part.casefold() for part in parts)
        if not folded or folded[0] != 'transcription':
            return
        keys = [folded[:1]]
        if len(folded) >= 2 and folded[1] == 'jobs':
            keys.append(folded[:2])
            if len(folded) >= 3:
                keys.append(folded[:3])
        for key in keys:
            actual = root.joinpath(*parts[:len(key)])
            expected = admitted.get(key)
            if (expected is None or actual != expected[0]
                    or identity(actual) != expected[1]):
                # This includes a newly appearing namespace/job that was absent
                # at admission. Fail before reading any of its status/results.
                raise RuntimeError('Transcription state appeared or changed during backup. '
                                   'Wait for completion or stop the job, then retry.')

    return guard


def _walk_error(error):
    # os.walk silently suppresses directory enumeration errors by default.
    # An unreadable subtree is not evidence that it contains no user state.
    raise error


def snapshot_state(root, target, *, include_generated=False, max_bytes=MAX_STATE_BYTES,
                   cancelled=lambda: False):
    root = Path(root)
    if cancelled():
        raise InterruptedError('Backup cancelled.')
    if not os.path.lexists(root):
        return []
    root = local_path(root, exists=True)
    regular(root, directory=True)
    with ExitStack() as locks:
        guard = _transcription_snapshot_guard(root, locks, cancelled)
        return _snapshot_locked_state(root, target, include_generated=include_generated,
                                      max_bytes=max_bytes, cancelled=cancelled, guard=guard)


def _snapshot_locked_state(root, target, *, include_generated, max_bytes, cancelled, guard):
    def inventory():
        found = {}
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=_walk_error):
            if cancelled():
                raise InterruptedError('Backup cancelled.')
            guard(Path(directory))
            for name in dirs:
                regular(Path(directory) / name, directory=True)
            dirs[:] = [name for name in dirs if _include((Path(directory) / name).relative_to(root), include_generated)]
            for name in sorted(files):
                source = Path(directory) / name
                relative = source.relative_to(root)
                if _include(relative, include_generated):
                    found[relative] = _stamp(regular(source))
                    if len(found) > MAX_STATE_FILES:
                        raise ValueError('Application state exceeds the file budget.')
        return found
    before_inventory = inventory()
    entries, total = [], 0
    for relative in sorted(before_inventory):
            source = root / relative
            relative_name(relative.as_posix())
            if source.suffix.casefold() in ('.sqlite', '.db') or source.name.endswith(('-wal', '-shm', '-journal')):
                raise ValueError('An active state database needs its own consistent snapshot adapter.')
            before = regular(source)
            if _stamp(before) != before_inventory[relative]:
                raise ValueError('Application state changed during backup; retry after saving.')
            total += before.st_size
            if len(entries) >= MAX_STATE_FILES or total > max_bytes:
                raise ValueError('Application state exceeds the backup budget.')
            destination = Path(target) / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest, count = sha256(), 0
            with source.open('rb') as read, destination.open('xb') as write:
                if _stamp(os.fstat(read.fileno())) != _stamp(before):
                    raise ValueError('Application state changed during backup; retry after saving.')
                while block := read.read(1024 * 1024):
                    if cancelled():
                        raise InterruptedError('Backup cancelled.')
                    count += len(block)
                    if count > before.st_size:
                        raise ValueError('Application state grew during backup; retry after saving.')
                    digest.update(block)
                    write.write(block)
                write.flush()
                os.fsync(write.fileno())
                if _stamp(os.fstat(read.fileno())) != _stamp(before):
                    raise ValueError('Application state changed during backup; retry after saving.')
            if count != before.st_size or _stamp(regular(source)) != _stamp(before):
                raise ValueError('Application state changed during backup; retry after saving.')
            entries.append({'filename': relative.as_posix(), 'size_bytes': count, 'sha256': digest.hexdigest()})
    # Studio augments the staged snapshot with retained external inputs and
    # rewrites only its staged project records. Verify the original source
    # inventory against the digests captured before that augmentation.
    source_entries = tuple(entries)
    from .studio.backup_inputs import snapshot_inputs
    entries = snapshot_inputs(target, entries, max_bytes=max_bytes,
                              max_files=MAX_STATE_FILES, cancelled=cancelled)
    if inventory() != before_inventory:
        raise ValueError('Application state changed during backup; retry after saving.')
    # Windows creation-time/mtime metadata can stay unchanged across a same-size
    # overwrite while another file is being copied. Re-read earlier source bytes
    # against their copied digest; stat equality alone is not snapshot evidence.
    for entry in source_entries:
        source = root / entry['filename']
        before = regular(source)
        digest, count = sha256(), 0
        if _stamp(before) != before_inventory[Path(entry['filename'])]:
            raise ValueError('Application state changed during backup; retry after saving.')
        with source.open('rb') as stream:
            if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                raise ValueError('Application state changed during backup; retry after saving.')
            while True:
                if cancelled():
                    raise InterruptedError('Backup cancelled.')
                block = stream.read(min(1024 * 1024, entry['size_bytes'] + 1 - count))
                if not block:
                    break
                count += len(block)
                if count > entry['size_bytes']:
                    raise ValueError('Application state grew during backup; retry after saving.')
                digest.update(block)
            if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                raise ValueError('Application state changed during backup; retry after saving.')
        if (count != entry['size_bytes'] or digest.hexdigest() != entry['sha256']
                or _stamp(regular(source)) != _stamp(before)):
            raise ValueError('Application state changed during backup; retry after saving.')
    if inventory() != before_inventory:
        raise ValueError('Application state changed during backup; retry after saving.')
    # A snapshot must pass the same portable manifest checks as a restore.
    # Keep this after content revalidation and before publication.
    validate_state(entries)
    return sorted(entries, key=lambda entry: entry['filename'])


def validate_state(entries):
    if not isinstance(entries, list) or len(entries) > MAX_STATE_FILES:
        raise ValueError('Invalid application state manifest.')
    names, directories, portable_paths, total = set(), set(), {}, 0
    for item in entries:
        if (not isinstance(item, dict) or set(item) != {'filename', 'size_bytes', 'sha256'}
                or type(item['size_bytes']) is not int or item['size_bytes'] < 0
                or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
            raise ValueError('Invalid application state manifest.')
        name = relative_name(item['filename']).as_posix()
        key = unicodedata.normalize('NFC', name).casefold()
        if key in names:
            raise ValueError('Duplicate application state filename.')
        names.add(key)
        # Reuse the typed sidecar portable-path policy, including ancestors.
        # A/one and a/two may coexist on POSIX but alias on Windows.
        path = PurePosixPath(name)
        for component in (path, *path.parents):
            current = str(component)
            if current == '.':
                continue
            portable = unicodedata.normalize('NFC', current).casefold()
            previous = portable_paths.setdefault(portable, current)
            if previous != current:
                raise ValueError('Application state paths collide on a portable filesystem.')
            if component != path:
                directories.add(portable)
        total += item['size_bytes']
    if names & directories:
        raise ValueError('Application state file and directory paths collide.')
    if total > MAX_STATE_BYTES:
        raise ValueError('Application state manifest exceeds its byte budget.')


def restore_state(source, target, entries, *, destination_root=None):
    validate_state(entries)
    source = local_path(source, exists=True)
    regular(source, directory=True)
    missing = {item['filename'] for item in entries}
    for directory, dirs, files in os.walk(source, followlinks=False, onerror=_walk_error):
        for name in dirs:
            regular(Path(directory) / name, directory=True)
        for name in files:
            path = Path(directory) / name
            regular(path)
            # Reject the first unexpected file rather than accumulating an
            # attacker-controlled inventory beyond the manifest's file budget.
            relative = path.relative_to(source).as_posix()
            if relative not in missing:
                raise ValueError('Backup has missing or unlisted application state.')
            missing.remove(relative)
    if missing:
        raise ValueError('Backup has missing or unlisted application state.')
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    for item in entries:
        incoming = local_path(source / item['filename'], root=source, exists=True)
        if regular(incoming).st_size != item['size_bytes']:
            raise ValueError('Application state size mismatch.')
        destination = target / item['filename']
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest, size = sha256(), 0
        with incoming.open('rb') as read, destination.open('xb') as write:
            while block := read.read(1024 * 1024):
                size += len(block)
                if size > item['size_bytes']:
                    raise ValueError('Application state changed during restore.')
                digest.update(block)
                write.write(block)
            write.flush()
            os.fsync(write.fileno())
        if size != item['size_bytes'] or digest.hexdigest() != item['sha256']:
            raise ValueError('Application state checksum mismatch.')
    from .studio.backup_inputs import restore_inputs
    restore_inputs(target, target if destination_root is None else destination_root, entries)
    sync_directory(target)


def publish_state(source, target):
    target = Path(target)
    target.mkdir(mode=0o700)
    for path in sorted(Path(source).rglob('*')):
        relative = path.relative_to(source)
        if path.is_dir():
            (target / relative).mkdir(exist_ok=True)
        else:
            (target / relative).parent.mkdir(parents=True, exist_ok=True)
            os.link(path, target / relative)
    sync_directory(target)
