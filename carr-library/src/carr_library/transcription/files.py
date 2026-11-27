"""Bounded local snapshots, using the shared exclusive publication primitive."""
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from ..audio.jobs import checked_directory, _sync_directory
from ..audio.model_manifest import local_path
from ..file_publication import publish_directory_no_replace


def canonical(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def regular(path):
    path = local_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('Transcription requires private regular local files, without links.')
    return info


def stamp(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def read_bytes(path, maximum, check=lambda: None):
    before = regular(path)
    if before.st_size > maximum:
        raise ValueError('Transcription metadata exceeds its byte budget.')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    with os.fdopen(os.open(path, flags), 'rb', buffering=0) as stream:
        if stamp(os.fstat(stream.fileno())) != stamp(before):
            raise ValueError('Transcription file changed while opening.')
        raw = stream.read(maximum + 1)
        check()
        stream.seek(0)
        if stream.read(maximum + 1) != raw or stamp(os.fstat(stream.fileno())) != stamp(before):
            raise ValueError('Transcription file changed while reading.')
    check()
    if len(raw) != before.st_size or stamp(regular(path)) != stamp(before):
        raise ValueError('Transcription file changed while reading.')
    return raw


def parse_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate transcription metadata field.')
            result[key] = value
        return result
    def nonfinite(_):
        raise ValueError('Nonfinite transcription metadata is not accepted.')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=nonfinite)


def read_json(path, maximum, check=lambda: None):
    raw = read_bytes(path, maximum, check)
    return parse_json(raw), raw


def file_digest(path, check=lambda: None):
    # Reuse the independently tested coarse-timestamp content verifier.
    from ..assistant.service import _hash_file
    def cancelled():
        check()
        return False
    return _hash_file(path, cancelled)


def write_new(path, raw):
    path = Path(path)
    checked_directory(path.parent)
    temporary = path.with_name('.transcription-' + uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        publish_directory_no_replace(temporary, path)
        temporary = None  # Commit happened; cleanup must never erase it.
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def copy_snapshot(source, target, maximum, check):
    source = local_path(source)
    before = regular(source)
    if not 0 < before.st_size <= maximum:
        raise ValueError('The selected source exceeds the remaining byte budget or is empty.')
    count = 0
    digest = sha256()
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    with os.fdopen(os.open(source, flags), 'rb', buffering=0) as incoming, target.open('xb') as outgoing:
        if stamp(os.fstat(incoming.fileno())) != stamp(before):
            raise ValueError('The selected source changed while opening.')
        while block := incoming.read(min(1024**2, maximum - count + 1)):
            check()
            count += len(block)
            if count > maximum or count > before.st_size:
                raise ValueError('The selected source grew while copying.')
            outgoing.write(block)
            digest.update(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
        if count != before.st_size or stamp(os.fstat(incoming.fileno())) != stamp(before):
            raise ValueError('The selected source changed while copying.')
    if stamp(regular(source)) != stamp(before) or file_digest(source, check) != digest.hexdigest():
        raise ValueError('The selected source content changed while copying.')
    if file_digest(target, check) != digest.hexdigest():
        raise ValueError('The retained source snapshot failed verification.')
    return {'bytes': count, 'sha256': digest.hexdigest()}


def sync_parent(path):
    try:
        _sync_directory(Path(path).parent)
    except OSError:
        return ('The result was published; directory durability could not be confirmed.',)
    return ()
