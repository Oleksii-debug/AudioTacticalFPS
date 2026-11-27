"""Explicitly admitted portable entries, owned before application work starts.

The private Job owner already used by the current Windows process runner supplies kill-on-close
containment. A fixed child bootstrap waits on a separate inherited admission
pipe before application work. Parent death before assignment gives EOF; after
assignment it closes the owned Job. The pipe never replaces ordinary stdin.
Unmarked portable bundles and standard installations do not use this module.
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import zipfile
from uuid import uuid4

from .installation_lease import LEASE, MARKER, installation_lease, plain

PORTABLE_MARKER = 'portable-update.ini'
PORTABLE_CONTRACT = b'[Resonance portable updates]\nProtocol=2\nOwner=windows-job-receipt-v1\n'
CONTROL_SUFFIX = '.update-control'
_CALLBACKS = {
    ('carr_library.__main__', 'main'),
    ('carr_library.native_checks', 'main'),
    ('carr_library.bibliography_cli', 'main'),
    ('carr_library.local_audio_setup', 'main'),
    ('carr_library.operations_cli', 'main'),
    ('carr_library.transcription_cli', 'main'),
    ('carr_library.security.__main__', 'main'),
    ('carr_library.conversion.cli', 'main'),
    ('carr_library.sync.cli', 'main'),
    ('carr_library.studio.cli', 'main'),
    ('carr_library.studio.cli', 'assistant_main'),
    ('carr_library.extract.ocr_model_catalog', 'main'),
    ('carr_library.assistant.translation_cli', 'main'),
    ('carr_library.assistant.templates_cli', 'main'),
    ('carr_library.assistant.translation_batch_cli', 'main'),
}
OWNED_SOURCES = (
    'installation_lease.py', 'portable_entry.py', 'portable_bootstrap.py',
    'durable_state.py',
    'audio/windows_job.py', '__main__.py', 'native_checks.py',
    'bibliography_cli.py',
    'transcription_cli.py', 'security/__main__.py', 'studio/cli.py', 'local_audio_setup.py',
    'extract/ocr_model_catalog.py', 'assistant/translation_cli.py', 'operations_cli.py',
    'assistant/templates_cli.py', 'assistant/translation_batch_cli.py',
    'conversion/cli.py', 'sync/cli.py',
)
_owner_root = None


def _read(path, maximum):
    before = plain(path)
    if before.st_size > maximum:
        raise ValueError('Portable update metadata exceeds its configured bound.')
    from .durable_state import _open_json_file
    with _open_json_file(path) as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or getattr(opened, 'st_file_attributes', 0) & 0x400
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError('Portable metadata changed before bounded reading.')
        raw = stream.read(maximum + 1)
        finished = os.fstat(stream.fileno())
    after = plain(path)
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')
    if len(raw) > maximum or any(
            len({getattr(item, key) for item in (before, opened, finished, after)}) != 1
            for key in fields):
        raise ValueError('Portable update metadata changed while reading.')
    return raw


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate portable update metadata key.')
        result[key] = value
    return result


def managed_layout(program):
    """Managed ownership metadata requires the standard setup transaction."""
    program = Path(program).absolute()
    return (any(os.path.lexists(program.parent / name)
                for name in (MARKER, 'owned-files.json'))
            or (program.parent.name.casefold() == 'versions'
                and os.path.lexists(program.parent.parent / 'install-layout-v1.txt')))


def portable_contract(program):
    """Read only the bounded, inventory-bound coordination capability.

    The external handoff additionally authenticates the current full manifest
    against its configured digest and the candidate against VerifiedPackage.
    This function does not turn a local manifest into a signing authority.
    """
    program = Path(program).absolute()
    if str(program).startswith(('//', '\\\\')) or '..' in program.parts:
        raise ValueError('Portable updates require a plain local program path.')
    plain(program, directory=True)
    if managed_layout(program):
        raise ValueError('Managed updates require an authenticated setup handoff that preserves Repair and Uninstall ownership.')
    marker = _read(program / PORTABLE_MARKER, 256)
    if marker != PORTABLE_CONTRACT:
        raise ValueError('This portable bundle has no supported child-ownership contract.')
    raw = _read(program / 'bundle-manifest.json', 16 * 1024**2)
    manifest = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
    source = manifest.get('source_commit') if type(manifest) is dict else None
    if (type(manifest) is not dict or type(manifest.get('schema')) is not int or manifest['schema'] != 1
            or manifest.get('bundled_python') is not True
            or manifest.get('bundled_launcher') is not True
            or type(manifest.get('files')) is not dict
            or manifest['files'].get(PORTABLE_MARKER) != sha256(marker).hexdigest()
            or not isinstance(source, str) or len(source) != 40
            or any(character not in '0123456789abcdef' for character in source)):
        raise ValueError('The portable child-ownership contract is outside its sealed source inventory.')
    return raw


def installed_ownership(program, manifest_bytes):
    """Bind installed entry/guardian bytes to the retained exact wheel."""
    program = Path(program).absolute()
    manifest = json.loads(manifest_bytes.decode('utf-8'), object_pairs_hook=_unique)
    wheels = [(name, digest) for name, digest in manifest['files'].items()
              if name.startswith('wheelhouse/universal_research_library-') and name.endswith('.whl')]
    if len(wheels) != 1:
        raise ValueError('Portable ownership requires one exact application wheel.')
    name, digest = wheels[0]
    if '/' in name[len('wheelhouse/'):] or '\\' in name or ':' in name or '..' in Path(name).parts:
        raise ValueError('Invalid portable application wheel path.')
    raw = _read(program / name, 64 * 1024**2)
    if sha256(raw).hexdigest() != digest:
        raise ValueError('The portable ownership wheel differs from the sealed inventory.')
    environment = program / '.venv'
    plain(environment / '.installation-complete')
    package = environment / 'Lib/site-packages/carr_library'
    from .security.installation import _central_budget
    stream = io.BytesIO(raw)
    _central_budget(stream, 50000)
    with zipfile.ZipFile(stream) as archive:
        for relative in OWNED_SOURCES:
            name = 'carr_library/' + relative
            matches = [info for info in archive.infolist() if info.filename.casefold() == name.casefold()]
            if (len(matches) != 1 or matches[0].filename != name or matches[0].is_dir()
                    or matches[0].file_size > 4 * 1024**2):
                raise ValueError('The portable ownership wheel has incomplete or ambiguous source entries.')
            expected = archive.read(matches[0])
            if _read(package / relative, 4 * 1024**2) != expected:
                raise ValueError('Installed portable ownership code differs from its sealed wheel.')


def control_root(program, *, create=False):
    program = Path(program).absolute()
    if str(program).startswith(('//', '\\\\')) or '..' in program.parts:
        raise ValueError('Portable updates require a plain local program path.')
    plain(program.parent, directory=True)
    root = program.with_name(program.name + CONTROL_SUFFIX)
    if create:
        root.mkdir(mode=0o700, exist_ok=True)
    plain(root, directory=True)
    if create and not os.path.lexists(root / LEASE):
        # Competing first launches and maintenance contend on the same OS file.
        # The short creation hold does not authorize an application operation.
        with installation_lease(root, exclusive=True):
            pass
    plain(root / LEASE)
    return root


def require_clean_owners(program, root=None):
    """An unclean receipt vetoes activation; it can never prove a process idle.

    Called only while the external helper holds the exclusive installation
    lease. Never clear a receipt from a PID, an empty Job count, a missing Job
    name or a clock heuristic. An abrupt owner exit requires explicit recovery
    or a separately verified new bundle at a different program location.
    """
    root = control_root(program) if root is None else Path(root)
    plain(root, directory=True)
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.name.casefold().startswith('owner-'):
                raise RuntimeError(
                    'A previous application owner did not confirm complete cleanup. '
                    'Automatic activation is blocked; preserve its owner receipt and use '
                    'owner recovery or a separately verified new bundle.')


def _record_owner(program, root):
    from .durable_state import atomic_json, encode
    identity = uuid4().hex
    value = {'schema': 'carr-portable-owner/1', 'id': identity,
             'program': str(program), 'owner_pid': os.getpid()}
    receipt = root / ('owner-' + identity + '.json')
    raw = encode(value)
    atomic_json(receipt, value)
    return receipt, raw


def _clear_owner(receipt):
    path, expected = receipt
    if _read(path, 4096) != expected:
        raise RuntimeError('The owned cleanup receipt changed; automatic activation remains blocked.')
    path.unlink()


def active_program(prefix=None):
    environment = Path(sys.prefix if prefix is None else prefix).absolute()
    if environment.name.casefold() != '.venv':
        return None
    program = environment.parent
    return program if os.path.lexists(program / PORTABLE_MARKER) else None


def owner_authorized(program):
    return _owner_root == os.path.normcase(str(Path(program).absolute()))


@contextmanager
def verified_owner(program, admission):
    """Recheck the same proof after admission before exposing CLI callbacks."""
    global _owner_root
    from .portable_bootstrap import owner_lease

    program = Path(program).absolute()
    portable_contract(program)
    with owner_lease(program, admission):
        if _owner_root is not None:
            raise RuntimeError('A portable lifetime owner is already installed.')
        _owner_root = os.path.normcase(str(program))
        try:
            yield
        finally:
            _owner_root = None


def _callback_identity(callback):
    module, name = callback.__module__, callback.__name__
    if module == '__main__':
        spec = getattr(sys.modules.get('__main__'), '__spec__', None)
        module = spec.name if spec is not None else ''
    if (module, name) not in _CALLBACKS:
        raise ValueError('This portable entrypoint has no reviewed ownership dispatch.')
    return module, name


def _dispatch(arguments, admission):
    """Called only by the fixed bootstrap after site-free owner admission."""
    if len(arguments) < 2:
        raise ValueError('A reviewed portable entrypoint is required.')
    module, name, *forwarded = arguments
    if (module, name) not in _CALLBACKS:
        raise ValueError('Unsupported portable entry dispatch.')
    program = active_program()
    if program is None:
        raise ValueError('The selected interpreter has no portable ownership contract.')
    with verified_owner(program, admission):
        imported = importlib.import_module(module)
        location = Path(imported.__file__).resolve(strict=True)
        if not location.is_relative_to(Path(sys.prefix).resolve(strict=True)):
            raise ValueError('The portable entrypoint is outside its installed environment.')
        return getattr(imported, name)(forwarded)


def run_entry(callback, arguments, program):
    """Keep the shared lease until the existing owned-process tree is stopped."""
    if os.name != 'nt':
        raise RuntimeError('Portable owned entries require native Windows.')
    import msvcrt
    from .audio.windows_job import LifetimeJob

    program = Path(program).absolute()
    portable_contract(program)
    module, name = _callback_identity(callback)
    if not all(isinstance(argument, str) and '\x00' not in argument for argument in arguments):
        raise ValueError('Portable CLI arguments must be ordinary strings.')
    environment = Path(sys.prefix).resolve(strict=True)
    if (environment.name.casefold() != '.venv' or environment.parent != program.resolve(strict=True)
            or not Path(sys.executable).absolute().is_relative_to(environment)
            or not Path(callback.__code__.co_filename).resolve(strict=True).is_relative_to(environment)):
        raise ValueError('Portable ownership requires the selected installed interpreter and entrypoint.')
    root = control_root(program, create=True)
    with installation_lease(root) as lease:
        read_fd = write_fd = process = job = None
        spawn_attempted = False
        receipt = _record_owner(program, root)
        cleanup_confirmed = False
        try:
            job = LifetimeJob()
            read_fd, write_fd = os.pipe()
            read_handle = msvcrt.get_osfhandle(read_fd)
            os.set_handle_inheritable(read_handle, True)
            os.set_handle_inheritable(msvcrt.get_osfhandle(write_fd), False)
            startup = subprocess.STARTUPINFO()
            startup.lpAttributeList = {'handle_list': [read_handle]}
            bootstrap = _read(Path(__file__).with_name('portable_bootstrap.py'), 65536).decode('utf-8')
            executable = str(Path(getattr(sys, '_base_executable', sys.executable)).resolve(strict=True))
            plain(Path(executable))
            environment_variables = dict(os.environ, __PYVENV_LAUNCHER__=sys.executable)
            command = [executable, '-I', '-S', '-B', '-X', 'utf8', '-c', bootstrap,
                       str(read_handle)]
            try:
                spawn_attempted = True
                # With an explicit inherited handle list, omitted stdio is
                # not inherited on Windows. Let Popen duplicate exactly the
                # current streams alongside the private admission read handle.
                process = subprocess.Popen(
                    command, cwd=program, env=environment_variables, close_fds=True, startupinfo=startup,
                    stdin=sys.stdin if sys.stdin is not None else subprocess.DEVNULL,
                    stdout=sys.stdout if sys.stdout is not None else subprocess.DEVNULL,
                    stderr=sys.stderr if sys.stderr is not None else subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                job.assign(int(process._handle))
            finally:
                os.set_handle_inheritable(read_handle, False)
                os.close(read_fd)
                read_fd = None
            admission = json.dumps({'owner': {'protocol': 1, 'pid': os.getpid(),
                                    'job': int(job.handle), 'lease': int(lease),
                                    'program': str(program), 'receipt': receipt[0].stem[6:]},
                                    'entry': [module, name, *arguments]},
                                   ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            if len(admission) > 256 * 1024:
                raise ValueError('Portable owner admission exceeds its bound.')
            with os.fdopen(write_fd, 'wb', buffering=0) as pipe:
                write_fd = None
                if pipe.write(admission) != len(admission):
                    raise OSError('The portable owner admission was incomplete.')
            return process.wait()
        finally:
            try:
                try:
                    if read_fd is not None:
                        os.close(read_fd)
                    if write_fd is not None:
                        os.close(write_fd)
                finally:
                    if job is not None:
                        job.stop_and_wait(int(process._handle) if process is not None else None)
                    if process is not None:
                        process.wait(timeout=5)
                    # Popen can raise after native creation but before returning
                    # its exact handle. EOF blocks application admission, but
                    # cannot certify that an unobserved runtime process exited.
                    cleanup_confirmed = process is not None or not spawn_attempted
            finally:
                try:
                    if job is not None:
                        job.close()
                finally:
                    if process is not None:
                        try:
                            if process.poll() is None:
                                process.kill()
                            process.wait(timeout=5)
                        finally:
                            process._handle.Close()
                if cleanup_confirmed:
                    _clear_owner(receipt)
                # A failure or abrupt owner death leaves its durable veto.
                # Dropping the shared lease therefore cannot certify idle.


def activation_status(prefix=None):
    """Cached UI text uses only layout presence, not package hashing or I/O jobs."""
    environment = Path(sys.prefix if prefix is None else prefix).absolute()
    program = environment.parent
    if environment.name.casefold() == '.venv' and managed_layout(program):
        return ('Managed updates require an authenticated setup handoff that updates '
                'Repair and Uninstall ownership together. Generic folder activation is unavailable.')
    if active_program(prefix) is not None:
        return ('This portable bundle declares owned application entries. Activation requires '
                'a separately configured trusted helper outside the program and confirmed cleanup. '
                'Any unclean owner receipt blocks automatic activation until owner recovery.')
    return ('Automatic activation is not enabled for this portable layout. '
            'Use a separately verified new bundle; existing research data remains available.')
