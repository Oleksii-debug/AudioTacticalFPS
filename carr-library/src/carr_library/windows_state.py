"""Atomic replacement of local state, including open Windows read snapshots.

The standard-library-only function is also included verbatim in the trusted
transcription bootstrap; selected external worker files are never modified.
"""


def replace_file(source, destination):
    """Replace a sibling regular file; never delete the destination first."""
    import os
    import pathlib
    import stat
    import sys

    source = pathlib.Path(os.fsdecode(source)).absolute()
    destination = pathlib.Path(os.fsdecode(destination)).absolute()
    if source.parent != destination.parent or source == destination:
        raise ValueError('Atomic state replacement requires distinct sibling files.')
    for path in (source, destination):
        if str(path).startswith(('//', '\\\\')):
            raise ValueError('State replacement requires local files.')
        for parent in reversed(path.parents):
            value = parent.lstat()
            if (not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode)
                    or getattr(value, 'st_file_attributes', 0) & 0x400):
                raise ValueError('State replacement cannot traverse links.')
        if path == source or os.path.lexists(path):
            value = path.lstat()
            if (not stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode)
                    or getattr(value, 'st_file_attributes', 0) & 0x400
                    or value.st_nlink != 1):
                raise ValueError('State replacement requires regular unlinked files.')
    if os.name != 'nt':
        return os.replace(source, destination)

    import ctypes
    from ctypes import wintypes

    # Preserve Python audit/write guards when using the native API directly.
    sys.audit('os.rename', str(source), str(destination), -1, -1)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes, close.restype = (wintypes.HANDLE,), wintypes.BOOL
    rename = kernel.SetFileInformationByHandle
    rename.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    rename.restype = wintypes.BOOL

    class FileInformation(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('creation', wintypes.FILETIME),
                    ('access', wintypes.FILETIME), ('write', wintypes.FILETIME),
                    ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD),
                    ('size_low', wintypes.DWORD), ('links', wintypes.DWORD),
                    ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]

    query = kernel.GetFileInformationByHandle
    query.argtypes = (wintypes.HANDLE, ctypes.POINTER(FileInformation))
    query.restype = wintypes.BOOL

    class RenameInformation(ctypes.Structure):
        _fields_ = [('flags', wintypes.DWORD), ('root', wintypes.HANDLE),
                    ('length', wintypes.DWORD), ('name', ctypes.c_uint16 * 1)]

    # DELETE access, all three sharing modes, OPEN_EXISTING and open the final
    # reparse point itself. Do not request bypasses for ACLs or read-only files.
    handle = create('\\\\?\\' + str(source), 0x00010000, 0x7, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = FileInformation()
        if not query(handle, ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & (0x10 | 0x400) or info.links != 1:
            raise ValueError('State replacement source changed to a linked or non-regular file.')
        name = ('\\\\?\\' + str(destination)).encode('utf-16-le')
        size = max(ctypes.sizeof(RenameInformation), RenameInformation.name.offset + len(name))
        buffer = ctypes.create_string_buffer(size)
        value = RenameInformation.from_buffer(buffer)
        value.flags, value.root, value.length = 0x3, None, len(name)
        ctypes.memmove(ctypes.addressof(buffer) + RenameInformation.name.offset, name, len(name))
        # FileRenameInfoEx, REPLACE_IF_EXISTS | POSIX_SEMANTICS: existing read
        # handles keep their old bytes; new opens resolve to the new file.
        # https://learn.microsoft.com/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information
        if not rename(handle, 22, buffer, size):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close(handle)
