"""Atomic directory publication shared by local artifact producers.

Callers validate their own paths and finish writing the private staging tree.
This primitive only performs the no-replace rename. Returning marks the commit
point; callers record that fact before attempting directory fsync or callbacks.
"""
import ctypes
import errno
import os
import sys


def publish_directory_no_replace(stage, destination):
    """Publish a complete tree without replacing even an empty destination.

    Fail closed where the operating system has no supported exclusive rename.
    No durability operation follows the rename here, so errors cannot hide a
    successful commit. The caller separately syncs the destination's parent.
    """
    if os.name == 'nt':
        # Windows rename is exclusive for both files and directories.
        os.rename(stage, destination)
        return
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith('linux'):
        operation = getattr(library, 'renameat2', None)
        if operation is None:
            raise RuntimeError('Atomic no-replace directory publication is unavailable.')
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                              ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        failed = operation(-100, os.fsencode(stage), -100, os.fsencode(destination), 1)
    elif sys.platform == 'darwin':
        operation = getattr(library, 'renamex_np', None)
        if operation is None:
            raise RuntimeError('Atomic no-replace directory publication is unavailable.')
        operation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        failed = operation(os.fsencode(stage), os.fsencode(destination), 4)
    else:
        raise RuntimeError('Atomic no-replace directory publication is unavailable on this platform.')
    if failed:
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise FileExistsError('The destination already exists.')
        raise OSError(code, 'Atomic directory publication failed.')

