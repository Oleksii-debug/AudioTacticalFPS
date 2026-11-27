"""Cooperating application/maintenance leases for the standard Windows layout.

Unmarked portable distributions are unchanged. A managed installation has an ``app``
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
import os
from pathlib import Path
import stat
import sys
import threading


MARKER = 'managed-install.ini'
LEASE = 'installation.lease'
BOOTSTRAP_OWNER = 'bootstrap-owner.json'
_application_holds = {}
_application_hold_lock = threading.RLock()


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
            buffer = ctypes.create_unicode_buffer(32768)
            size = api.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
            expected = os.path.normcase(str(path.resolve()))
            actual = os.path.normcase(buffer.value.removeprefix('\\\\?\\'))
            if not 0 < size < len(buffer) or actual != expected:
                api.CloseHandle(handle)
                raise ValueError('The installer handle does not own this installation lease.')
        else:
            handle = api.CreateFileW(str(path), 0xC0000000 if exclusive else 0x80000000,
                                    0 if exclusive else 1, None, 4 if exclusive else 3,
                                    0x00200080, None)  # OPEN_REPARSE_POINT | NORMAL
            if handle == ctypes.c_void_p(-1).value:
                raise RuntimeError('Close Resonance and its running jobs before repairing or uninstalling.')
        try:
            plain(path)
            yield handle
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
            yield descriptor
        finally:
            os.close(descriptor)


def require_clean_bootstrap(root):
    """An unacknowledged managed installer can never be inferred idle.

    This receipt is a conservative veto, not authority based on a PID or age.
    Check it only while holding the shared application or exclusive setup lease.
    """
    root = Path(root).absolute()
    plain(root, directory=True)
    if any(path.name.casefold() == BOOTSTRAP_OWNER for path in root.iterdir()):
        raise RuntimeError(
            'The previous installation bootstrap did not confirm complete cleanup. '
            'Preserve this program folder and its retained recovery files; use the '
            'matching verified setup in a new program folder.')


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
def application_lease(arguments=(), *, prefix=None, allow_bootstrap_self_test=False):
    """Guard managed entries and explicitly admitted portable installations."""
    environment = Path(sys.prefix if prefix is None else prefix).absolute()
    from .portable_entry import active_program, portable_contract, control_root, owner_authorized
    portable = active_program(prefix)
    if portable is not None:
        portable_contract(portable)
        if not (environment / '.installation-complete').is_file():
            if allow_bootstrap_self_test and list(arguments) == ['--self-test']:
                yield
                return
            raise RuntimeError('Portable installation is incomplete. Use its verified offline installer.')
        if not owner_authorized(portable):
            raise RuntimeError('A verified portable lifetime owner is required before application work.')
        with _process_application_hold(control_root(portable, create=True)):
            yield
        return
    program, root = environment.parent, environment.parent.parent
    if environment.name.casefold() != '.venv' or program.name.casefold() != 'app' or not os.path.lexists(root / MARKER):
        yield
        return
    plain(root / MARKER)
    if not (environment / '.installation-complete').is_file():
        # The existing offline installer runs this exact synthetic check before
        # publishing readiness. It cannot open the desktop or a user library.
        if allow_bootstrap_self_test and list(arguments) == ['--self-test']:
            yield
            return
        raise RuntimeError('Installation is incomplete. Run Repair from the Resonance setup.')
    with _process_application_hold(root):
        require_clean_bootstrap(root)
        yield


def installed_entrypoint(function=None, *, allow_bootstrap_self_test=False):
    """Guard a CLI lifetime, preserving its existing operation/error handling.

    Only the primary entrypoint opts into the exact offline-bootstrap self-test.
    Module imports have no lease side effects; callbacks start only after entry.
    """
    def decorate(callback):
        @wraps(callback)
        def guarded(argv=None, *args, **kwargs):
            arguments = list(sys.argv[1:] if argv is None else argv)
            context = application_lease(arguments,
                allow_bootstrap_self_test=allow_bootstrap_self_test)
            try:
                from .portable_entry import active_program, owner_authorized, run_entry
                portable = active_program()
                bootstrap = (allow_bootstrap_self_test and arguments == ['--self-test']
                             and not (Path(sys.prefix) / '.installation-complete').is_file())
                if portable is not None and not bootstrap and not owner_authorized(portable):
                    if args or kwargs:
                        raise ValueError('Portable owned entrypoints accept CLI arguments only.')
                    return run_entry(callback, arguments, portable)
                context.__enter__()
            except (ValueError, RuntimeError, OSError) as error:
                print('Resonance: ' + str(error), file=sys.stderr)
                return 1
            try:
                return callback(arguments, *args, **kwargs)
            finally:
                context.__exit__(*sys.exc_info())
        return guarded
    return decorate if function is None else decorate(function)
