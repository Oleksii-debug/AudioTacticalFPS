"""Optional authenticated envelope for an already prepared local archive.

Version 1 uses PyCA cryptography, not an application cipher implementation:
AES-256-GCM with full 128-bit tags, scrypt (N=2**17, r=8, p=1), and
HKDF-Expand/SHA-256 for separate control and 1-GiB data-group keys. Each file
has a fresh 16-byte salt and 4-byte nonce prefix; each record nonce appends a
distinct 64-bit counter. The fixed header, chunk position/length, and final
record are authenticated. There are no variable-size unauthenticated fields.

Wire order: 52-byte big-endian header, 16-byte header tag, ceil(length/1MiB)
encrypted chunks (each followed by its 16-byte tag), 16-byte final tag, EOF.
Header fields: magic[8], version/cipher/KDF/log2(N)[1 each], r/p/chunk[4 each],
plaintext length[8], salt[16], nonce prefix[4]. All algorithm/cost fields must
equal this version's constants, and the complete input size must agree before
derivation. The header exposes archive length, never archive names or content.

Reviewed API: cryptography 50.0.2 (Apache-2.0 OR BSD-3-Clause).
https://cryptography.io/en/50.0.2/hazmat/primitives/aead/
https://cryptography.io/en/50.0.2/hazmat/primitives/key-derivation-functions/

Callers own archive validation, trusted local staging directories and secret
entry. No archive members are inspected, credentials retained, or logs emitted.
Python/native allocations cannot promise secure erasure or crash cleanup.
Cancellation is checked around the bounded KDF and between bounded chunks;
the final exclusive publication is the commit point. Directory replacement by
a hostile process with the same account is not a supported sandbox boundary.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path, PureWindowsPath
import stat
import struct

from ..research.export_file import _filename, _linked
from ..file_publication import publish_directory_no_replace


FORMAT_VERSION = 1
CHUNK_SIZE = 1024 * 1024
MAX_PLAINTEXT_SIZE = 1024 ** 4
MAX_PASSPHRASE_BYTES = 1024
MIN_PASSPHRASE_CHARACTERS = 12
_MAGIC = b'CARRSYNC'
_HEADER = struct.Struct('>8sBBBBIIIQ16s4s')
_LOG_N, _R, _P = 17, 8, 1
_TAG_SIZE = 16
_GROUP_CHUNKS = 1024
_AUTH_ERROR = 'The encrypted snapshot is invalid or the passphrase is incorrect.'
_CAPABILITY = ('Snapshot encryption requires the optional cryptography==50.0.2 '
               'dependency. Install the application sync extra and try again.')
_MAX_CIPHERTEXT_SIZE = (_HEADER.size + 2 * _TAG_SIZE + MAX_PLAINTEXT_SIZE
                        + (MAX_PLAINTEXT_SIZE // CHUNK_SIZE) * _TAG_SIZE)


class CryptoError(ValueError):
    """Invalid envelope, secret, source or destination; no fallback is allowed."""


class CryptoUnavailable(RuntimeError):
    """The optional cryptographic capability is unavailable."""


@dataclass(frozen=True)
class CryptoReceipt:
    path: Path
    sha256: str
    size: int
    plaintext_size: int
    encrypted: bool
    format_version: int = FORMAT_VERSION


def _load_crypto():
    try:
        from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except (ImportError, OSError):
        raise CryptoUnavailable(_CAPABILITY) from None
    return AESGCM, Scrypt, HKDFExpand, hashes.SHA256, InvalidTag, UnsupportedAlgorithm


def _password(passphrase):
    if not isinstance(passphrase, str) or not MIN_PASSPHRASE_CHARACTERS <= len(passphrase) <= MAX_PASSPHRASE_BYTES:
        raise CryptoError('Use a passphrase of at least 12 characters and at most 1024 UTF-8 bytes.')
    try:
        encoded = passphrase.encode('utf-8', errors='strict')
    except UnicodeError:
        raise CryptoError('The passphrase must be valid Unicode.') from None
    if len(encoded) > MAX_PASSPHRASE_BYTES:
        raise CryptoError('Use a passphrase of at most 1024 UTF-8 bytes.')
    return encoded  # Deliberately do not strip or normalize the secret.


class _Control:
    def __init__(self, cancelled, on_progress):
        if not callable(cancelled) or (on_progress is not None and not callable(on_progress)):
            raise CryptoError('Invalid snapshot progress or cancellation callback.')
        self.cancelled, self.on_progress = cancelled, on_progress
        self.stopped = False

    def check(self):
        self.stopped = self.stopped or bool(self.cancelled())
        if self.stopped:
            raise InterruptedError('Snapshot operation cancelled; no destination was published.')

    def progress(self, done, total):
        self.check()
        if self.on_progress is not None:
            self.on_progress(done, total)
        self.check()


def _path(value):
    try:
        raw = os.fspath(value)
    except TypeError:
        raise CryptoError('Choose a regular file on a local filesystem.') from None
    if (not isinstance(raw, str) or not raw or raw.endswith(('/', '\\'))
            or raw.startswith(('//', '\\\\')) or '\x00' in raw):
        raise CryptoError('Choose a regular file on a local filesystem.')
    requested = Path(raw)
    if '..' in requested.parts:
        raise CryptoError('Parent traversal is not allowed in a snapshot path.')
    path = requested.absolute()
    for part in path.parts[1:]:
        try:
            _filename(part)
        except ValueError:
            raise CryptoError('Choose a portable local filename without reserved characters.') from None
    if PureWindowsPath(raw).drive.startswith('\\\\'):
        raise CryptoError('Network paths are not accepted for snapshot encryption.')
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        drive_type = ctypes.WinDLL('kernel32', use_last_error=True).GetDriveTypeW
        drive_type.argtypes, drive_type.restype = (wintypes.LPCWSTR,), wintypes.UINT
        if drive_type(path.anchor) in (0, 1, 4):
            raise CryptoError('Choose an available local drive for snapshot encryption.')
    return path


def _identity(info):
    return info.st_dev, info.st_ino


def _state(info):
    return (_identity(info), info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, getattr(info, 'st_file_attributes', 0))


def _parents(path):
    result = []
    for parent in (path.parent, *path.parent.parents):
        info = parent.lstat()
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            raise CryptoError('Snapshot folders must not contain links or reparse points.')
        result.append((parent, _identity(info)))
    return tuple(result)


def _check_parents(bindings):
    for path, identity in bindings:
        info = path.lstat()
        if _linked(info) or not stat.S_ISDIR(info.st_mode) or _identity(info) != identity:
            raise CryptoError('A snapshot folder changed during the operation.')


def _regular(info, maximum):
    if (_linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or not 0 <= info.st_size <= maximum):
        raise CryptoError('The snapshot source must be a bounded regular file without links.')


@dataclass
class _Input:
    path: Path
    stream: object
    before: object
    parents: tuple

    def check(self):
        _check_parents(self.parents)
        if (_state(self.before) != _state(os.fstat(self.stream.fileno()))
                or _state(self.before) != _state(self.path.lstat())):
            raise CryptoError('The snapshot source changed during the operation.')


@contextmanager
def _input(value, maximum):
    path = _path(value)
    parents = _parents(path)
    before = path.lstat()
    _regular(before, maximum)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    # A raced replacement by a FIFO must not turn file validation into a wait.
    flags |= getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        _regular(opened, maximum)
        if _state(before) != _state(opened):
            raise CryptoError('The snapshot source changed before opening.')
        source = _Input(path, stream, opened, parents)
        source.check()
        yield source


class _Output:
    """Same-directory private staging and exclusive complete-file publication."""

    def __init__(self, value):
        self.path = _path(value)
        self.parents = _parents(self.path)
        self.dir_fd = None
        self.name = None
        self.stream = None
        self.digest = sha256()
        self.size = 0
        self.check()

    def check(self):
        _check_parents(self.parents)
        if os.path.lexists(self.path):
            raise CryptoError('Choose a new destination; existing files are never replaced.')

    def _relative(self, name):
        return name if self.dir_fd is not None else self.path.parent / name

    def _kwargs(self):
        return {'dir_fd': self.dir_fd} if self.dir_fd is not None else {}

    def __enter__(self):
        self.check()
        try:
            if os.name == 'posix':
                self.dir_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY
                                      | getattr(os, 'O_NOFOLLOW', 0))
                if _identity(os.fstat(self.dir_fd)) != self.parents[0][1]:
                    raise CryptoError('The destination folder changed before staging.')
            name = '.carr-crypto-' + os.urandom(16).hex() + '.tmp'
            fd = os.open(self._relative(name), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0),
                         0o600, **self._kwargs())
            self.name = name  # Only our successfully created entry may be removed.
            try:
                self.stream = os.fdopen(fd, 'wb')
            except BaseException:
                os.close(fd)
                raise
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def write(self, data):
        self.stream.write(data)
        self.digest.update(data)
        self.size += len(data)

    def publish(self, source, control):
        self.stream.flush()
        os.fsync(self.stream.fileno())
        staged = os.fstat(self.stream.fileno())
        self.stream.close()
        control.check()
        source.check()
        self.check()
        current = os.stat(self._relative(self.name), follow_symlinks=False, **self._kwargs())
        if _state(staged) != _state(current) or current.st_nlink != 1:
            raise CryptoError('The staged snapshot changed before publication.')
        # The shared native no-replace rename preserves single-link files even
        # if the process dies immediately after publication. The existing open
        # directory descriptor still owns cleanup before that commit.
        try:
            publish_directory_no_replace(self.path.parent / self.name, self.path)
            self.name = None
        except FileExistsError:
            raise CryptoError('The destination appeared during publication; it was preserved.') from None
        except (OSError, RuntimeError):
            raise CryptoError('This destination does not support exclusive atomic snapshot publication.') from None

    def __exit__(self, *_):
        try:
            if self.stream is not None and not self.stream.closed:
                self.stream.close()
            if self.name is not None:
                try:
                    os.unlink(self._relative(self.name), **self._kwargs())
                except FileNotFoundError:
                    pass
        finally:
            if self.dir_fd is not None:
                os.close(self.dir_fd)
                self.dir_fd = None


def _count(length):
    return (length + CHUNK_SIZE - 1) // CHUNK_SIZE


def _serialized_size(length):
    return _HEADER.size + 2 * _TAG_SIZE + length + _count(length) * _TAG_SIZE


def _parse_header(header, file_size):
    if len(header) != _HEADER.size:
        raise CryptoError(_AUTH_ERROR)
    magic, version, cipher, kdf, log_n, r, p, chunk, length, salt, prefix = _HEADER.unpack(header)
    if ((magic, version, cipher, kdf, log_n, r, p, chunk)
            != (_MAGIC, FORMAT_VERSION, 1, 1, _LOG_N, _R, _P, CHUNK_SIZE)
            or length > MAX_PLAINTEXT_SIZE or file_size != _serialized_size(length)):
        raise CryptoError(_AUTH_ERROR)
    return length, salt, prefix


def _master(password, salt, backend, control):
    control.check()
    try:
        result = backend[1](salt=salt, length=32, n=2 ** _LOG_N, r=_R, p=_P).derive(password)
    except backend[5]:
        raise CryptoUnavailable('The installed cryptographic backend does not support snapshot encryption.') from None
    except MemoryError:
        raise CryptoUnavailable('Snapshot encryption needs memory for the bounded 128-MiB scrypt operation.') from None
    control.check()
    return result


def _cipher(master, group, backend):
    # The master is scrypt output, never a raw passphrase. Distinct fixed-domain
    # HKDF keys keep each AES-GCM data key below 1 GiB even for large archives.
    try:
        key = backend[2](algorithm=backend[3](), length=32,
                         info=b'CARRSYNC-v1-key\x00' + struct.pack('>Q', group)).derive(master)
        return backend[0](key)
    except backend[5]:
        raise CryptoUnavailable('The installed cryptographic backend does not support snapshot encryption.') from None


def _nonce(prefix, index):
    return prefix + struct.pack('>Q', index)


def _chunk_aad(header, tag, index, size):
    return b'CARRSYNC-v1-chunk\x00' + header + tag + struct.pack('>QI', index, size)


def _final_aad(header, tag, count, length):
    return b'CARRSYNC-v1-final\x00' + header + tag + struct.pack('>QQ', count, length)


def encrypt_file(source, new_destination, passphrase, *, cancelled=lambda: False, on_progress=None):
    """Encrypt opaque bytes to a new file; progress is (plaintext done, total).

    Passphrases are exact Unicode strings (12+ characters, at most 1024 UTF-8
    bytes). Progress/cancellation callbacks never receive secrets or content.
    A successful receipt identifies the ciphertext bytes, not the source hash.
    """
    control = _Control(cancelled, on_progress)
    control.check()
    password = _password(passphrase)
    backend = _load_crypto()
    with _input(source, MAX_PLAINTEXT_SIZE) as original:
        destination = _Output(new_destination)
        length = original.before.st_size
        salt, prefix = os.urandom(16), os.urandom(4)
        header = _HEADER.pack(_MAGIC, FORMAT_VERSION, 1, 1, _LOG_N, _R, _P,
                              CHUNK_SIZE, length, salt, prefix)
        master = _master(password, salt, backend, control)
        control_cipher = _cipher(master, 0, backend)
        tag = control_cipher.encrypt(_nonce(prefix, 0), b'', b'CARRSYNC-v1-header\x00' + header)
        control.progress(0, length)
        with destination as output:
            output.write(header + tag)
            completed, group, cipher = 0, None, None
            for index in range(1, _count(length) + 1):
                control.check()
                size = min(CHUNK_SIZE, length - completed)
                data = original.stream.read(size)
                if len(data) != size:
                    raise CryptoError('The snapshot source changed during encryption.')
                next_group = 1 + (index - 1) // _GROUP_CHUNKS
                if group != next_group:
                    group, cipher = next_group, _cipher(master, next_group, backend)
                output.write(cipher.encrypt(_nonce(prefix, index), data,
                                            _chunk_aad(header, tag, index, size)))
                completed += size
                control.progress(completed, length)
            if original.stream.read(1):
                raise CryptoError('The snapshot source changed during encryption.')
            output.write(control_cipher.encrypt(_nonce(prefix, _count(length) + 1), b'',
                         _final_aad(header, tag, _count(length), length)))
            output.publish(original, control)
            receipt = CryptoReceipt(output.path, output.digest.hexdigest(), output.size, length, True)
        return receipt


def decrypt_file(source, new_destination, passphrase, *, cancelled=lambda: False, on_progress=None):
    """Authenticate a complete envelope before publishing its opaque plaintext.

    The password, header, each position/length-bound chunk, final marker, exact
    EOF and unchanged source must all pass. Failure removes private staging;
    there is no plaintext fallback and an existing destination is preserved.
    """
    control = _Control(cancelled, on_progress)
    control.check()
    password = _password(passphrase)
    with _input(source, _MAX_CIPHERTEXT_SIZE) as original:
        destination = _Output(new_destination)
        header = original.stream.read(_HEADER.size)
        length, salt, prefix = _parse_header(header, original.before.st_size)
        backend = _load_crypto()
        master = _master(password, salt, backend, control)
        control_cipher = _cipher(master, 0, backend)
        tag = original.stream.read(_TAG_SIZE)
        try:
            control_cipher.decrypt(_nonce(prefix, 0), tag, b'CARRSYNC-v1-header\x00' + header)
            control.progress(0, length)
            with destination as output:
                completed, group, cipher = 0, None, None
                for index in range(1, _count(length) + 1):
                    control.check()
                    size = min(CHUNK_SIZE, length - completed)
                    data = original.stream.read(size + _TAG_SIZE)
                    if len(data) != size + _TAG_SIZE:
                        raise CryptoError(_AUTH_ERROR)
                    next_group = 1 + (index - 1) // _GROUP_CHUNKS
                    if group != next_group:
                        group, cipher = next_group, _cipher(master, next_group, backend)
                    output.write(cipher.decrypt(_nonce(prefix, index), data,
                                                _chunk_aad(header, tag, index, size)))
                    completed += size
                    control.progress(completed, length)
                final = original.stream.read(_TAG_SIZE)
                control_cipher.decrypt(_nonce(prefix, _count(length) + 1), final,
                                       _final_aad(header, tag, _count(length), length))
                if original.stream.read(1):
                    raise CryptoError(_AUTH_ERROR)
                output.publish(original, control)
                receipt = CryptoReceipt(output.path, output.digest.hexdigest(), output.size, length, False)
            return receipt
        except backend[4]:
            raise CryptoError(_AUTH_ERROR) from None
