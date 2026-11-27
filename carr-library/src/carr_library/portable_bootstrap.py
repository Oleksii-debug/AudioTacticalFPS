"""Fixed stdlib-only portable admission; safe to run with Python -I -S -B.

No site initialization, application import or worker can run before the exact
parent lease and Job have been proved. Only the parent owns the pipe's write
end, so a parent that dies before assignment leaves EOF instead of an orphan.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import stat
import sys


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate portable admission field.')
        value[key] = item
    return value


def _plain(path, *, directory=False):
    path = Path(path).absolute()
    if '..' in path.parts or str(path).startswith(('//', '\\\\')):
        raise ValueError('Portable admission requires local paths without traversal.')
    for item in reversed((path, *path.parents)):
        info = item.lstat()
        expected = stat.S_ISDIR if item != path or directory else stat.S_ISREG
        if (not expected(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or (item == path and not directory and info.st_nlink != 1)):
            raise ValueError('Portable admission requires plain, unlinked paths.')


def _api():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    functions = {
        'GetCurrentProcess': ((), w.HANDLE),
        'OpenProcess': ((w.DWORD, w.BOOL, w.DWORD), w.HANDLE),
        'CloseHandle': ((w.HANDLE,), w.BOOL),
        'DuplicateHandle': ((w.HANDLE, w.HANDLE, w.HANDLE, ctypes.POINTER(w.HANDLE),
                             w.DWORD, w.BOOL, w.DWORD), w.BOOL),
        'IsProcessInJob': ((w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)), w.BOOL),
        'QueryInformationJobObject': ((w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                      w.DWORD, ctypes.c_void_p), w.BOOL),
        'GetFinalPathNameByHandleW': ((w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD), w.DWORD),
        'GetFileType': ((w.HANDLE,), w.DWORD),
    }
    for name, (arguments, result) in functions.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


class _BasicLimits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                ('flags', ctypes.c_uint32), ('minimum_working_set', ctypes.c_size_t),
                ('maximum_working_set', ctypes.c_size_t), ('active_processes', ctypes.c_uint32),
                ('affinity', ctypes.c_size_t), ('priority', ctypes.c_uint32),
                ('scheduling', ctypes.c_uint32)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ('read_operations', 'write_operations', 'other_operations',
                 'read_bytes', 'write_bytes', 'other_bytes')]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', _BasicLimits), ('io', _IoCounters),
                ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                ('peak_process_memory', ctypes.c_size_t), ('peak_job_memory', ctypes.c_size_t)]


@contextmanager
def owner_lease(program, admission):
    """Verify exact live ownership and keep only its duplicated shared lease."""
    if os.name != 'nt':
        raise RuntimeError('Portable admission requires native Windows.')
    program = Path(program).absolute()
    control = program.with_name(program.name + '.update-control')
    _plain(program, directory=True)
    _plain(control, directory=True)
    _plain(control / 'installation.lease')
    if (type(admission) is not dict
            or set(admission) != {'protocol', 'pid', 'job', 'lease', 'program'}
            or type(admission['protocol']) is not int or admission['protocol'] != 1
            or any(type(admission[key]) is not int or admission[key] <= 0
                   for key in ('pid', 'job', 'lease'))
            or admission['pid'] == os.getpid() or admission['pid'] >= 2**32
            or any(admission[key] >= 2**(ctypes.sizeof(ctypes.c_void_p) * 8) for key in ('job', 'lease'))
            or admission['program'] != str(program)):
        raise ValueError('Invalid portable owner admission.')
    api = _api()
    parent = api.OpenProcess(0x0040, False, admission['pid'])  # PROCESS_DUP_HANDLE
    if not parent:
        raise OSError(ctypes.get_last_error(), 'The portable entry guardian is unavailable.')
    handles = []
    try:
        for key in ('job', 'lease'):
            target = w.HANDLE()
            if not api.DuplicateHandle(parent, admission[key], api.GetCurrentProcess(),
                                       ctypes.byref(target), 0, False, 2):
                raise OSError(ctypes.get_last_error(), 'Cannot verify the exact portable owner.')
            handles.append(target.value)
    except BaseException:
        for handle in handles:
            api.CloseHandle(handle)
        raise
    finally:
        api.CloseHandle(parent)
    job, lease = handles
    try:
        member, limits = w.BOOL(), _ExtendedLimits()
        if (not api.IsProcessInJob(api.GetCurrentProcess(), job, ctypes.byref(member))
                or not member.value
                or not api.QueryInformationJobObject(job, 9, ctypes.byref(limits),
                                                     ctypes.sizeof(limits), None)
                or not limits.basic.flags & 0x2000  # KILL_ON_JOB_CLOSE
                or limits.basic.flags & (0x00000800 | 0x00001000)):  # no breakaway
            raise ValueError('The portable application is not contained by its exact lifetime owner.')
        buffer = ctypes.create_unicode_buffer(32768)
        size = api.GetFinalPathNameByHandleW(lease, buffer, len(buffer), 0)
        actual = os.path.normcase(buffer.value.removeprefix('\\\\?\\'))
        expected = os.path.normcase(str((control / 'installation.lease').resolve(strict=True)))
        if not 0 < size < len(buffer) or actual != expected:
            raise ValueError('The guardian does not own this portable installation lease.')
        # Retaining this Job duplicate would defeat kill-on-owner-close.
        if not api.CloseHandle(job):
            raise OSError(ctypes.get_last_error(), 'Cannot release the temporary Job proof handle.')
        job = None
        yield
    finally:
        if job is not None:
            api.CloseHandle(job)
        api.CloseHandle(lease)


def _read_admission(text):
    import msvcrt
    if not text.isdecimal() or not 0 < int(text) < 2**(ctypes.sizeof(ctypes.c_void_p) * 8):
        raise ValueError('Invalid inherited admission pipe.')
    api = _api()
    handle = int(text)
    if api.GetFileType(handle) != 3:  # FILE_TYPE_PIPE
        api.CloseHandle(handle)
        raise ValueError('Portable admission requires its private inherited pipe.')
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        api.CloseHandle(handle)
        raise
    try:
        pipe = os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise
    with pipe:
        raw = pipe.read(256 * 1024 + 1)
    if not raw or len(raw) > 256 * 1024:
        raise ValueError('Portable admission was absent or exceeded its bound.')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)


def main(arguments):
    if os.name != 'nt' or len(arguments) != 1 or not sys.flags.isolated or not sys.flags.no_site:
        raise ValueError('An isolated, site-disabled native admission bootstrap is required.')
    # Python 3.11 -S has not initialized the venv prefix yet. Its selected
    # lexical executable path still identifies that exact environment.
    executable = Path(sys.executable).absolute()
    environment = executable.parent.parent
    if executable.parent.name.casefold() != 'scripts' or environment.name.casefold() != '.venv':
        raise ValueError('Portable admission requires the selected installed interpreter.')
    _plain(executable)
    program = environment.parent
    packet = _read_admission(arguments[0])
    if (type(packet) is not dict or set(packet) != {'owner', 'entry'}
            or type(packet['entry']) is not list or not 2 <= len(packet['entry']) <= 32768
            or not all(isinstance(value, str) and '\x00' not in value for value in packet['entry'])):
        raise ValueError('Invalid portable entry packet.')
    admission = packet['owner']
    with owner_lease(program, admission):
        # This is the first point where .pth/site/application code may execute.
        import site
        site.main()
        if Path(sys.prefix).absolute() != environment:
            raise ValueError('The admitted interpreter changed its installed environment.')
        from carr_library import portable_entry
        if not Path(portable_entry.__file__).resolve(strict=True).is_relative_to(environment):
            raise ValueError('Portable dispatch was imported outside its installed environment.')
        return portable_entry._dispatch(packet['entry'], admission)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
