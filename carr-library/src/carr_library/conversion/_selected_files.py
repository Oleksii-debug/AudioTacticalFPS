"""Small adapters over the current local-path and immutable-source readers.

No separate filesystem authority is introduced here. Native selections reuse
the catalog reader's held Windows handle, which excludes writes and renames;
the portable admission tests retain named/descriptor/change-time checks.
"""
from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path

from ..catalog.intake import _open_file_source, _stream_change_time
from ..durable_state import local_path, regular
from ..speech._io import local_file
from .models import ConversionError


def checked_directory(path):
    path = local_path(path, exists=True)
    regular(path, directory=True)
    return path


def regular_file(path):
    return regular(local_file(path))


def file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


@contextmanager
def verified_reader(path, *, expected_identity=None):
    path = local_file(path)
    before = regular(path)
    identity = file_identity(before)
    if expected_identity is not None and tuple(expected_identity) != identity:
        raise ConversionError('The selected conversion file changed before reading.')
    parent = regular(path.parent, directory=True)
    with _open_file_source(path) as stream:
        opened = os.fstat(stream.fileno())
        if file_identity(opened) != identity or opened.st_nlink != 1:
            raise ConversionError('The selected conversion file changed while opening.')
        change = _stream_change_time(stream)
        try:
            yield stream
        finally:
            current_parent = regular(local_path(path.parent, exists=True), directory=True)
            named = regular(local_file(path))
            if ((current_parent.st_dev, current_parent.st_ino) != (parent.st_dev, parent.st_ino)
                    or file_identity(os.fstat(stream.fileno())) != identity
                    or file_identity(named) != identity
                    or _stream_change_time(stream) != change):
                raise ConversionError('The selected conversion file changed while reading.')


def digest_stream(stream, expected_size, cancelled=lambda: False):
    digest, count = sha256(), 0
    if type(expected_size) is not int or expected_size < 0:
        raise ConversionError('A bounded conversion file size is required.')
    while block := stream.read(min(1024 * 1024, expected_size - count + 1)):
        if cancelled():
            raise InterruptedError('Document conversion cancelled during file verification.')
        count += len(block)
        if count > expected_size:
            raise ConversionError('A selected conversion file grew during verification.')
        digest.update(block)
    if count != expected_size:
        raise ConversionError('A selected conversion file was truncated.')
    if cancelled():
        raise InterruptedError('Document conversion cancelled during file verification.')
    return digest.hexdigest()


def digest_file(path, *, cancelled=lambda: False, expected_identity=None):
    info = regular_file(path)
    identity = file_identity(info)
    if expected_identity is not None and tuple(expected_identity) != identity:
        raise ConversionError('The selected conversion file changed before verification.')
    with verified_reader(path, expected_identity=identity) as stream:
        return digest_stream(stream, info.st_size, cancelled)
