"""Explicit local audio setup using the existing settings and runtime verifier.

Preparing a binding performs expensive work without changing the active library.
Activation is a short, guarded completion step; callers retain and close it.
No executable, model or transfer permission is supplied by this module.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import os
from pathlib import Path
import shutil
import sys
import tempfile
from uuid import uuid4

from . import runtime
from . import windows_setup as setup
from .render import check_cancelled, _require_local_volume, RenderCancelled
from ..durable_state import local_path
from ..native_runtime import _plain, MAX_MANIFEST

SEAL = 'audio_runtime_manifest_sha256'


def _stamp(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns, value.st_nlink)


def _snapshot(root, *, limit=setup.MAX_FILES, cancelled=None):
    """Bounded metadata freshness check; never hash large binaries on the GUI."""
    root = setup._plain(root, directory=True)
    result, pending = {}, [root]
    while pending:
        check_cancelled(cancelled)
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                check_cancelled(cancelled)
                if len(result) >= limit:
                    raise ValueError('Audio runtime inventory changed or exceeds its entry budget.')
                path = Path(entry.path)
                # Fresh lstat, not Windows DirEntry's incomplete cached fields.
                info = path.lstat()
                import stat
                is_directory = stat.S_ISDIR(info.st_mode)
                _plain(path, directory=is_directory)
                name = path.relative_to(root).as_posix()
                result[name] = _stamp(info)
                if is_directory:
                    pending.append(path)
    return result



def _manifest_digest(path, expected_size):
    """Never hash a concurrently enlarged manifest without a byte ceiling."""
    detail = _plain(path)
    if not 0 < expected_size <= MAX_MANIFEST or detail.st_size != expected_size:
        raise ValueError('The reviewed audio manifest size changed before activation.')
    with path.open('rb') as stream:
        raw = stream.read(expected_size + 1)
    if len(raw) != expected_size:
        raise ValueError('The reviewed audio manifest changed while reading.')
    return sha256(raw).hexdigest()


@dataclass
class AudioRuntimeBinding:
    library_root: Path
    settings: dict | None = None
    directory: Path | None = None
    report: dict | None = None
    snapshot: dict | None = None
    manifest_path: Path | None = None
    manifest_size: int = 0
    notice: str = ''
    handle: object = None
    _owner: object = field(default_factory=object, repr=False)
    _activated: bool = False
    _closed: bool = False

    def activate(self, *, previous=None, strict=False):
        """Use only after the caller's current-library/stale-result guard.

        Metadata and the small manifest are rechecked here. Executable bytes
        are checked again by resolve_active_audio_tool before every invocation.
        """
        from ..runtime_settings import load_settings
        if self._closed:
            raise RuntimeError('The prepared audio binding is closed.')
        if self._activated:
            if strict and self.notice:
                raise RuntimeError(self.notice)
            return self
        if previous is not None and not isinstance(previous, AudioRuntimeBinding):
            raise ValueError('Expected the previous local audio binding.')
        runtime._claim_audio_binding(self._owner, previous._owner if previous is not None else None)
        try:
            if self.notice:
                raise RuntimeError(self.notice)
            if load_settings(self.library_root / 'state') != self.settings:
                raise ValueError('Local engine settings changed after runtime verification.')
            if self.directory is not None:
                if _snapshot(self.directory, limit=len(self.snapshot) + 1) != self.snapshot:
                    raise ValueError('The reviewed audio runtime changed before activation.')
                if _manifest_digest(self.manifest_path, self.manifest_size) != self.report['manifest_sha256']:
                    raise ValueError('The reviewed audio manifest changed before activation.')
                self.handle = runtime._activate_verified_audio(self.directory, self.report['executables'],
                    previous=previous.handle if previous is not None else None)
            if previous is not None:
                previous.close()
            runtime._set_audio_block(self._owner, None)
        except Exception as error:
            if previous is not None:
                previous.close()
            self.notice = 'Selected local audio is unavailable. ' + str(error)[:2048]
            runtime._set_audio_block(self._owner, self.notice)
        self._activated = True
        if strict and self.notice:
            raise RuntimeError(self.notice)
        return self

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.handle is not None:
            self.handle.close()
        runtime._clear_audio_block(self._owner)
        runtime._release_audio_binding(self._owner)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _prepare_bundled(binding, *, cancelled=None, progress=None):
    """Retain the existing bundled fallback under the same single owner."""
    check_cancelled(cancelled)
    bundle = runtime.discover_bundled_audio()
    if bundle is None:
        return binding
    full, compact = bundle / runtime.DIRECTORY, bundle / 'audio'
    directory = full if os.path.lexists(full) else compact
    before = _snapshot(directory, cancelled=cancelled) if os.path.lexists(directory) else None
    manifest_path = bundle / 'bundle-manifest.json'
    manifest_size = _plain(manifest_path).st_size if os.path.lexists(manifest_path) else 0
    manifest_digest = _manifest_digest(manifest_path, manifest_size) if manifest_size else None
    if progress is not None:
        progress('Verifying the installed bundled audio runtime', None, None)
    verified = runtime._bundle(bundle, with_executables=True)
    check_cancelled(cancelled)
    if verified is None or sys.platform != 'win32':
        return binding
    directory, executables = verified
    after = _snapshot(directory, cancelled=cancelled)
    if before != after or _manifest_digest(manifest_path, manifest_size) != manifest_digest:
        raise ValueError('The bundled audio installation changed during verification.')
    binding.directory, binding.snapshot = directory, after
    binding.manifest_path, binding.manifest_size = manifest_path, manifest_size
    binding.report = {'executables': executables, 'manifest_sha256': manifest_digest,
                      'selection': 'verified-existing-application-bundle'}
    return binding


def _prepared(library_root, settings, *, cancelled=None, progress=None):
    from ..runtime_settings import configured_audio_runtime_root
    binding = AudioRuntimeBinding(Path(library_root).absolute(), settings=settings)
    root = configured_audio_runtime_root(settings)  # Restore barrier before external paths.
    if root is None:
        return _prepare_bundled(binding, cancelled=cancelled, progress=progress)
    if sys.platform != 'win32':
        raise RuntimeError('The selected complete audio runtime requires Windows.')
    check_cancelled(cancelled)
    before = _snapshot(root, cancelled=cancelled)
    report = setup.verify_runtime(root, settings[SEAL], cancelled=cancelled, progress=progress)
    setup.verify_native_versions(root, cancelled=cancelled, progress=progress)
    after = _snapshot(root, cancelled=cancelled)
    if before != after:
        raise ValueError('The audio runtime changed during verification.')
    binding.directory, binding.report, binding.snapshot = root, report, after
    binding.manifest_path, binding.manifest_size = root / runtime.MANIFEST, after[runtime.MANIFEST][3]
    return binding


def prepare_local_audio_runtime(library_root, *, cancelled=None, progress=None):
    """Prepare off the GUI thread (or before wx); do not mutate PATH or players."""
    from ..runtime_settings import load_settings
    root = Path(library_root).absolute()
    try:
        check_cancelled(cancelled)
        settings = load_settings(root / 'state')
        return _prepared(root, settings, cancelled=cancelled, progress=progress)
    except (InterruptedError, RenderCancelled):
        raise
    except Exception as error:
        return AudioRuntimeBinding(root, notice=str(error)[:2048])


def open_local_audio_runtime(library_root, *, strict=True, cancelled=None, progress=None):
    """Activated lifetime context for startup and existing headless dispatchers."""
    binding = prepare_local_audio_runtime(library_root, cancelled=cancelled, progress=progress)
    try:
        return binding.activate(strict=strict)
    except BaseException:
        binding.close()
        raise


def source_summary():
    lock = setup.load_lock()
    return {'release': lock['release'], 'publisher': lock['publisher'],
            'license': lock['license'], 'archive': dict(lock['archive']),
            'expanded_bytes': sum(row['size_bytes'] for row in lock['original_files'].values()),
            'use_scope': 'private-local-installation',
            'transfer_clearance': 'NOT_ESTABLISHED'}


def default_install_parent():
    from platformdirs import user_data_path
    return user_data_path('Universal Research Library', appauthor=False, roaming=False) / 'local-runtimes'


def install_or_select(library_root, *, archive=None, existing=None, download=False,
                      expected_settings=None, cancelled=None, progress=None,
                      install_parent=None):
    """Explicit one-operation setup; no half-activation or hidden network access.

    The returned binding is not activated. A caller checks its live scope, then
    activates it in completion. A completed settings save remains saved even if
    cancellation is requested immediately afterward.
    """
    from ..runtime_settings import load_settings, save_settings, _settings_document
    check_cancelled(cancelled)
    if type(download) is not bool or sum((archive is not None, existing is not None, download)) != 1:
        raise ValueError('Choose one local archive, existing complete runtime, or explicit download.')
    root = Path(library_root).absolute()
    previous = load_settings(root / 'state')
    if previous.get('restore_requires_revalidation', False):
        raise ValueError('Review restored local engine registrations before installing or selecting audio.')
    if expected_settings is not None and previous != expected_settings:
        raise ValueError('Local engine settings changed. Reopen audio setup before continuing.')
    if sys.platform != 'win32':
        raise RuntimeError('The local runtime setup controls require Windows.')
    if existing is not None:
        directory = setup._plain(Path(existing).absolute(), directory=True)
        report = setup.verify_runtime(directory, cancelled=cancelled, progress=progress)
    else:
        parent = local_path(install_parent or default_install_parent())
        _require_local_volume(parent)
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        setup._plain(parent, directory=True)
        information = source_summary()
        needed = information['expanded_bytes'] + (information['archive']['size_bytes'] if download else 0)
        if shutil.disk_usage(parent).free < needed + 64 * 1024**2:
            raise OSError('There is not enough free disk space for the reviewed local runtime and setup reserve.')
        directory = parent / ('ffmpeg-' + information['release'] + '-full-' + uuid4().hex)
        if download:
            with tempfile.TemporaryDirectory(prefix='.pending-audio-download-', dir=parent) as temporary:
                archive_path = setup.download_archive(Path(temporary) / 'ffmpeg.zip',
                                                       cancelled=cancelled, progress=progress)
                report = setup.prepare(archive_path, directory, cancelled=cancelled, progress=progress)
        else:
            report = setup.prepare(archive, directory, cancelled=cancelled, progress=progress)
    candidate = _settings_document({**previous,
        'ffmpeg': str(directory / 'bin' / 'ffmpeg.exe'),
        'ffprobe': str(directory / 'bin' / 'ffprobe.exe'),
        SEAL: report['manifest_sha256']})
    binding = _prepared(root, candidate, cancelled=cancelled, progress=progress)
    check_cancelled(cancelled)
    saved = save_settings(root / 'state', candidate, expected_settings=previous, cancelled=cancelled)
    binding.settings = saved
    # Publication has completed. Do not reclassify a later cancellation as an
    # uncommitted operation or activate a closed/stale view.
    return {'status': 'REGISTERED', 'settings': saved, 'binding': binding,
            'manifest_sha256': report['manifest_sha256'],
            'source_lock_sha256': report['source_lock_sha256'],
            'runtime_directory': str(directory), 'download_requested': download,
            'transfer_clearance': 'NOT_ESTABLISHED'}
