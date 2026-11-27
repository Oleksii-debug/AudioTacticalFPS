"""Bounded file and native inspection adapters for local audio publication.

The serializers are reused from the reviewed publisher. These helpers bind
them to the current Audio Studio's local files and FFmpeg selection; they do
not add a renderer, codec, provider, job registry or network transport.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import stat
import struct
from types import SimpleNamespace
from zipfile import ZipFile

from ..durable_state import local_path, regular
from ..operations.process import ProcessBudget, finite, run_process
from .jobs import RenderCancelled, checked_directory
from .runtime import trusted_executable


@dataclass(frozen=True)
class PublicationBudget:
    max_output_bytes: int = 32 * 1024**3
    timeout: float = 900
    reserve_disk_bytes: int = 128 * 1024**2

    def __post_init__(self):
        if type(self.max_output_bytes) is not int or not 1 <= self.max_output_bytes <= 32 * 1024**3:
            raise ValueError('Choose a publication byte budget up to 32 GiB.')
        if type(self.reserve_disk_bytes) is not int or not 0 <= self.reserve_disk_bytes <= 32 * 1024**3:
            raise ValueError('Invalid publication free-space reserve.')
        finite(self.timeout, 'Publication time budget', 1, 86400)


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _binding(info):
    # Windows path and descriptor APIs can expose different timestamp fields.
    # Compare full stamps within each API and stable identity across APIs.
    return info.st_dev, info.st_ino, info.st_nlink, info.st_size


def _file(path, allow_hardlinks):
    checked_directory(Path(path).parent)
    info = Path(path).lstat()
    if (not stat.S_ISREG(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or info.st_nlink < 1 or (not allow_hardlinks and info.st_nlink != 1)):
        raise ValueError('Publication requires checked regular local files.')
    return info


@contextmanager
def stable_reader(path, *, allow_hardlinks=False):
    """Retain path/handle identity without mistaking Windows API clocks.

    Only declared current Studio source artifacts use allow_hardlinks: its
    renderer intentionally retains chapter PCM and music companions as sealed
    cache hardlinks. All such reads still require exact recorded size/hash and
    full source reattestation. Staging, receipts and published files are unlinked.
    """
    path = Path(path)
    before = _file(path, allow_hardlinks)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if _binding(opened) != _binding(before) or _stamp(_file(path, allow_hardlinks)) != _stamp(before):
            raise ValueError('A publication input changed while opening.')
        yield stream
        if (_stamp(os.fstat(stream.fileno())) != _stamp(opened)
                or _stamp(_file(path, allow_hardlinks)) != _stamp(before)):
            raise ValueError('A publication input changed while reading.')


def read_identity(path, check, maximum, *, expected=None, collect=False,
                  copy_to=None, allow_hardlinks=False):
    if type(maximum) is not int or maximum < 0:
        raise ValueError('Invalid publication read budget.')
    check()
    chunks, count, digest = [], 0, sha256()
    out = None
    created = False
    try:
        with stable_reader(path, allow_hardlinks=allow_hardlinks) as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > maximum or expected is not None and size != expected['size']:
                raise ValueError('A publication input exceeds or differs from its recorded size.')
            if copy_to is not None:
                out = Path(copy_to).open('xb')
                created = True
            while block := stream.read(1024 * 1024):
                check()
                count += len(block)
                if count > size or count > maximum:
                    raise ValueError('A publication input grew beyond its read budget.')
                digest.update(block)
                if collect:
                    chunks.append(block)
                if out is not None:
                    out.write(block)
            if count != size:
                raise ValueError('A publication input changed length.')
        result = {'size': count, 'sha256': digest.hexdigest()}
        if expected is not None and result != expected:
            raise ValueError('A publication input differs from its recorded checksum.')
        if out is not None:
            out.flush()
            os.fsync(out.fileno())
            out.close()
            out = None
        check()
        return (b''.join(chunks), result) if collect else result
    except BaseException:
        if out is not None:
            out.close()
        if created:
            Path(copy_to).unlink(missing_ok=True)
        raise


def _zip_directory(stream, size, expected_count):
    """Bound ZIP central-directory allocation before constructing ZipFile."""
    if size < 22:
        raise ValueError('The publication archive is incomplete.')
    length = min(size, 65557)
    stream.seek(size - length)
    tail = stream.read(length)
    index = tail.rfind(b'PK\x05\x06')
    if index < 0 or len(tail) - index < 22:
        raise ValueError('The publication archive has no bounded directory.')
    fields = struct.unpack('<4s4H2LH', tail[index:index + 22])
    _, disk, cd_disk, count_disk, count, cd_size, cd_offset, comment = fields
    end_offset = size - length + index
    if disk or cd_disk or comment or index + 22 != len(tail):
        raise ValueError('Unsupported publication ZIP disk, comment or trailing data.')
    sentinel = count == 65535 or count_disk == 65535 or cd_size == 0xffffffff or cd_offset == 0xffffffff
    if sentinel:
        if end_offset < 20:
            raise ValueError('Missing publication ZIP64 locator.')
        stream.seek(end_offset - 20)
        signature, locator_disk, record_offset, disks = struct.unpack('<4sLQL', stream.read(20))
        if signature != b'PK\x06\x07' or locator_disk or disks != 1:
            raise ValueError('Unsupported publication ZIP64 locator.')
        if not 0 <= record_offset <= end_offset - 76:
            raise ValueError('Invalid publication ZIP64 directory offset.')
        stream.seek(record_offset)
        record = stream.read(56)
        if len(record) != 56:
            raise ValueError('Incomplete publication ZIP64 directory.')
        (signature, record_size, _made, _needed, disk, cd_disk,
         count_disk64, count64, cd_size64, cd_offset64) = struct.unpack('<4sQ2H2L4Q', record)
        if (signature != b'PK\x06\x06' or record_size != 44 or disk or cd_disk
                or record_offset + 56 != end_offset - 20
                or count not in (65535, count64) or count_disk not in (65535, count_disk64)
                or cd_size not in (0xffffffff, cd_size64) or cd_offset not in (0xffffffff, cd_offset64)):
            raise ValueError('Unsupported publication ZIP64 directory.')
        count, count_disk, cd_size, cd_offset = count64, count_disk64, cd_size64, cd_offset64
        directory_end = record_offset
    else:
        directory_end = end_offset
    if (count != expected_count or count_disk != count or not 0 < cd_size <= 65536
            or not 0 <= cd_offset <= directory_end or cd_offset + cd_size != directory_end):
        raise ValueError('Unexpected publication ZIP directory count or size.')
    stream.seek(0)


@contextmanager
def bounded_zip(path, expected_count, max_bytes, check):
    check()
    with stable_reader(path) as stream:
        size = os.fstat(stream.fileno()).st_size
        if size > max_bytes:
            raise ValueError('Publication archive exceeds its byte budget.')
        _zip_directory(stream, size, expected_count)
        check()
        with ZipFile(stream) as archive:
            yield archive
        check()


def inspect_audio(path, ffmpeg, *, check, timeout=30):
    """Reuse the canonical ffprobe query and the existing owned worker runner."""
    path = local_path(path, exists=True)
    check()
    selected = trusted_executable(ffmpeg)
    probe = trusted_executable(selected.with_name('ffprobe.exe' if os.name == 'nt' else 'ffprobe'))
    args = [str(probe), '-v', 'error', '-protocol_whitelist', 'file',
            '-format_whitelist', 'wav,mp3', '-enable_drefs', '0', '-use_absolute_path', '0',
            '-select_streams', 'a:0', '-show_entries',
            'stream=codec_name,sample_rate,channels,duration:format=format_name,duration',
            '-of', 'json', str(path)]
    environment = dict(os.environ)
    environment.pop('FFREPORT', None)
    before = _stamp(regular(path))
    def stopped():
        check()
        return False
    result = run_process(args, budget=ProcessBudget(timeout=timeout, max_log_bytes=65536),
                         cancelled=stopped, env=environment)
    check()
    try:
        from .publishing import _json
        data = _json(result.stdout)
        stream, = data['streams']
        rate, channels = int(stream['sample_rate']), int(stream['channels'])
        if not 8000 <= rate <= 192000 or channels not in (1, 2):
            raise ValueError('Publication needs supported mono or stereo audio.')
        duration = finite(float(stream.get('duration', data['format'].get('duration'))),
                          'Actual audio duration', 0, 365 * 86400)
        if _stamp(regular(path)) != before:
            raise ValueError('A staged recording changed during native inspection.')
        return SimpleNamespace(duration=duration, sample_rate=rate, channels=channels,
                               codec=str(stream['codec_name']), container=str(data['format']['format_name']))
    except (KeyError, TypeError, OverflowError, ZeroDivisionError) as exc:
        raise ValueError('The selected recording has no measurable supported audio stream.') from exc
