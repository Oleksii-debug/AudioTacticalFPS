"""Cooperating application/maintenance leases for the standard Windows layout.

The portable distribution is unchanged. A managed installation has an ``app``
directory beside ``managed-install.ini`` and a persistent, empty lease file.
Windows sharing rules (not process IDs or stale lock contents) own the lease.
POSIX flock supports the same lifecycle in source tests. This is a same-user
coordination boundary, not protection against an administrator or malicious
code running with the user's credentials.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from functools import wraps
import inspect
import os
from pathlib import Path
import stat
import sys
import threading


MARKER = 'managed-install.ini'
LEASE = 'installation.lease'
_application_holds = {}
_application_hold_lock = threading.RLock()
_issued_leases = set()


class _HeldLease:
    """A live same-process borrow; never serialized as update authority."""
    def __init__(self, root, exclusive, native_handle=None):
        self.root, self.exclusive = root, exclusive
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.native_handle, self.active = native_handle, False

    def check(self, root, exclusive):
        with _application_hold_lock:
            if (self not in _issued_leases or not self.active or self.pid != os.getpid()
                    or self.thread != threading.get_ident() or self.root != root
                    or exclusive and not self.exclusive):
                raise ValueError('The actual maintenance lease is no longer held for this installation.')

    def windows_owner(self):
        self.check(self.root, True)
        if self.native_handle is None:
            raise RuntimeError('A native Windows lease is required for the setup handoff.')
        return self.pid, self.native_handle


@contextmanager
def _issued_lease(root, exclusive, native_handle=None):
    held = _HeldLease(root, exclusive, native_handle)
    with _application_hold_lock:
        held.active = True
        _issued_leases.add(held)
    try:
        yield held
    finally:
        with _application_hold_lock:
            held.active = False
            _issued_leases.discard(held)


def plain(path: Path, *, directory=False, missing=False):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('Installation paths cannot contain parent traversal.')
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            if item == path and missing:
                return None
            raise
        expected = stat.S_ISDIR if item != path or directory else stat.S_ISREG
        if (not expected(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or (not directory and item == path and info.st_nlink != 1)):
            raise ValueError('Installation paths must be plain, unlinked local files and directories.')
    return info


def _windows():
    from ctypes import wintypes as w
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = (w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                               w.DWORD, w.DWORD, w.HANDLE)
    api.CreateFileW.restype = w.HANDLE
    api.CloseHandle.argtypes, api.CloseHandle.restype = (w.HANDLE,), w.BOOL
    api.GetCurrentProcess.restype = w.HANDLE
    api.OpenProcess.argtypes, api.OpenProcess.restype = (w.DWORD, w.BOOL, w.DWORD), w.HANDLE
    api.DuplicateHandle.argtypes = (w.HANDLE, w.HANDLE, w.HANDLE, ctypes.POINTER(w.HANDLE),
                                    w.DWORD, w.BOOL, w.DWORD)
    api.DuplicateHandle.restype = w.BOOL
    api.GetFinalPathNameByHandleW.argtypes = (w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD)
    api.GetFinalPathNameByHandleW.restype = w.DWORD
    return api, w


@contextmanager
def installation_lease(root: Path, *, exclusive=False, owner=None):
    """Hold a shared app lease or an exclusive installer lease, without waiting.

    ``owner`` is only used by the compiled Inno script: duplicate its live,
    exact file handle while it synchronously waits for this fixed helper. The
    installer holds that handle for its entire transaction, including shortcuts
    and uninstall registration. No PID-based termination occurs.
    """
    root = Path(root).absolute()
    plain(root, directory=True)
    if isinstance(owner, _HeldLease):
        owner.check(root, exclusive)
        yield owner
        return
    path = root / LEASE
    plain(path, missing=exclusive and owner is None)
    if os.name == 'nt':
        api, w = _windows()
        handle = None
        if owner is not None:
            if not exclusive or len(owner) != 2 or any(type(n) is not int or n <= 0 for n in owner):
                raise ValueError('Invalid installer lease owner.')
            parent = api.OpenProcess(0x0040, False, owner[0])  # PROCESS_DUP_HANDLE
            if not parent:
                raise OSError(ctypes.get_last_error(), 'The installer lease owner is unavailable.')
            try:
                target = w.HANDLE()
                if not api.DuplicateHandle(parent, owner[1], api.GetCurrentProcess(),
                                           ctypes.byref(target), 0, False, 2):
                    raise OSError(ctypes.get_last_error(), 'Cannot retain the exact installer lease.')
                handle = target.value
            finally:
                api.CloseHandle(parent)
            try:
                buffer = ctypes.create_unicode_buffer(32768)
                size = api.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
                expected = os.path.normcase(str(path.resolve()))
                actual = os.path.normcase(buffer.value.removeprefix('\\\\?\\'))
                if not 0 < size < len(buffer) or actual != expected:
                    raise ValueError('The installer handle does not own this installation lease.')
                # An application's shared read handle points at the same file
                # too. It must not be relabelled as exclusive by passing its
                # PID/handle through the setup command line. A genuine native
                # exclusive handle denies even a fully shared read-only open.
                probe = api.CreateFileW(str(path), 0x80000000, 0x7, None, 3,
                                        0x00200080, None)
                if probe != ctypes.c_void_p(-1).value:
                    api.CloseHandle(probe)
                    raise ValueError('The borrowed installer lease is not exclusive.')
                error = ctypes.get_last_error()
                if error != 32:  # ERROR_SHARING_VIOLATION, not access denial
                    raise OSError(error, 'Cannot confirm the exclusive installer lease.')
            except BaseException:
                api.CloseHandle(handle)
                raise
        else:
            handle = api.CreateFileW(str(path), 0xC0000000 if exclusive else 0x80000000,
                                    0 if exclusive else 1, None, 4 if exclusive else 3,
                                    0x00200080, None)  # OPEN_REPARSE_POINT | NORMAL
            if handle == ctypes.c_void_p(-1).value:
                raise RuntimeError('Close Resonance and its running jobs before repairing or uninstalling.')
        try:
            plain(path)
            with _issued_lease(root, exclusive, handle) as held:
                yield held
        finally:
            api.CloseHandle(handle)
    else:
        if owner is not None:
            raise ValueError('A Windows installer handle is only valid on Windows.')
        import fcntl
        flags = (os.O_RDWR | os.O_CREAT if exclusive else os.O_RDONLY) | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            current = plain(path)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError('The installation lease changed while opening.')
            try:
                fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError('Close Resonance and its running jobs before repairing or uninstalling.') from error
            with _issued_lease(root, exclusive) as held:
                yield held
        finally:
            os.close(descriptor)


@contextmanager
def _process_application_hold(root):
    # Nested CLI dispatch and concurrent jobs share one actual OS lease. The
    # short registry lock is never retained while an application job executes.
    key = (os.getpid(), os.path.normcase(str(root)))
    with _application_hold_lock:
        held = _application_holds.get(key)
        if held is None:
            context = installation_lease(root)
            context.__enter__()
            held = [context, 0]
            _application_holds[key] = held
        held[1] += 1
    try:
        yield
    finally:
        with _application_hold_lock:
            held[1] -= 1
            if held[1] == 0:
                del _application_holds[key]
                held[0].__exit__(*sys.exc_info())


@contextmanager
def application_lease(arguments=(), *, prefix=None, allow_bootstrap_self_test=False,
                      allow_bootstrap_native_checks=False):
    """Guard all ordinary installed entrypoints, retaining portable behavior."""
    environment = Path(sys.prefix if prefix is None else prefix).absolute()
    program, root = environment.parent, environment.parent.parent
    if environment.name.casefold() != '.venv' or program.name.casefold() != 'app' or not os.path.lexists(root / MARKER):
        yield
        return
    def state():
        import configparser
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        marker_info = plain(root / MARKER)
        if marker_info.st_size > 8192:
            raise ValueError('Managed installation state exceeds its byte budget.')
        with (root / MARKER).open('rb') as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            raise ValueError('Managed installation state exceeds its byte budget.')
        parser.read_string(raw.decode('utf-8'))
        return parser.get('installation', 'state', fallback='')

    # The unchanged offline installer runs these two fixed checks before it
    # writes readiness. Each public entrypoint opts into only its own exact
    # argument shape; neither check opens the desktop or a user library.
    requested = list(arguments)
    bootstrap = allow_bootstrap_self_test and requested == ['--self-test']
    if (allow_bootstrap_native_checks and len(requested) == 2
            and requested[0] == '--bundle-root' and type(requested[1]) is str):
        selected = Path(requested[1])
        if (selected.is_absolute() and '..' not in selected.parts
                and os.path.normcase(str(selected)) == os.path.normcase(str(program))):
            plain(program, directory=True)
            bootstrap = True
    if bootstrap:
        current = state()
        if current in ('preparing', 'updating') or (
                current == 'complete' and not (environment / '.installation-complete').is_file()):
            yield
            return
    with _process_application_hold(root):
        # Read admission state after acquiring the actual shared lease. A
        # maintenance transaction may have started and crashed between an
        # earlier observation and lease acquisition; its ready B environment
        # alone must never admit an ordinary launch.
        if state() != 'complete':
            raise RuntimeError('The managed installation has pending maintenance. Finish its reviewed recovery before opening the application.')
        if not (environment / '.installation-complete').is_file():
            raise RuntimeError('Installation is incomplete. Run Repair from the Resonance setup.')
        yield root


def _release_after_cleanup(context, exception_info, managed_root):
    """Keep a real managed process alive until its existing owners are quiet."""
    if managed_root is None:
        # Portable use and the exact externally-owned bootstrap self-test keep
        # their previous ordinary return/exception behavior.
        context.__exit__(*exception_info)
        return

    # Imported only by an ordinary managed application entrypoint, never by the
    # standalone fixed helper or low-level lease acquisition.
    from .audio import cleanup

    owners = {id(owner): owner
              for owner in cleanup._pending_owners(exception_info[1])}
    interrupted = None
    while True:
        try:
            # A command may catch AudioCleanupPending itself. Its actual
            # registered owner still has to settle before SystemExit can close
            # this process's shared installation handle. Refresh after each
            # barrier in case closing one owner registered another one.
            with cleanup._RETAINED_LOCK:
                owners.update((id(owner), owner) for owner in cleanup._RETAINED)
            waiting = tuple(owner for owner in owners.values()
                            if not owner.quiescent_event.is_set())
            if not waiting:
                break
            for owner in waiting:
                owner.close()
            waiting[0].quiescent_event.wait(.25)
        except (KeyboardInterrupt, SystemExit) as signal:
            # A signal changes neither native quiescence nor the ownership of
            # an executing program tree. Deliver it after every owner settles.
            if interrupted is None:
                interrupted = signal

    context.__exit__(*exception_info)
    if interrupted is not None:
        raise interrupted from exception_info[1]


def installed_entrypoint(function=None, *, allow_bootstrap_self_test=False,
                         allow_bootstrap_native_checks=False):
    """Guard a CLI lifetime without replacing its signature or operation errors.

    Captured native cleanup retains the same shared lease even when the
    existing command catches AudioCleanupPending and returns its normal result.
    The primary self-test and native-check entrypoints opt into their separate,
    exact offline-bootstrap argument shapes.
    """
    def decorate(callback):
        signature = inspect.signature(callback)
        parameters = tuple(signature.parameters.values())
        if not parameters or parameters[0].kind not in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD):
            raise TypeError('An installed entrypoint needs its existing argument parameter.')
        argument_name = parameters[0].name

        @wraps(callback)
        def guarded(*args, **kwargs):
            # Bind once through the real callback signature. This preserves
            # argv=, arguments= and keyword-only injected service/delivery.
            bound = signature.bind(*args, **kwargs)
            supplied = bound.arguments.get(argument_name, parameters[0].default)
            if supplied is inspect.Parameter.empty:
                raise TypeError('The application argument parameter is required.')
            arguments = list(sys.argv[1:] if supplied is None else supplied)
            bound.arguments[argument_name] = arguments
            bootstrap_options = {'allow_bootstrap_self_test': allow_bootstrap_self_test}
            if allow_bootstrap_native_checks:
                bootstrap_options['allow_bootstrap_native_checks'] = True
            context = application_lease(arguments, **bootstrap_options)
            try:
                managed_root = context.__enter__()
            except (ValueError, RuntimeError, OSError) as error:
                print('Resonance: ' + str(error), file=sys.stderr)
                return 1
            try:
                return callback(*bound.args, **bound.kwargs)
            finally:
                _release_after_cleanup(context, sys.exc_info(), managed_root)
        return guarded
    return decorate if function is None else decorate(function)
