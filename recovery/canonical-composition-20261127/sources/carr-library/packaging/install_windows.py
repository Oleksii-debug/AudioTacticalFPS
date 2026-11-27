"""Install the reviewed wheel bundle offline in a new local environment."""
from __future__ import annotations

import sys

# Direct script launches must also leave the hash-verified runtime unchanged.
# Supplied launchers use -B from interpreter startup, before even this file runs.
if __name__ == "__main__":
    sys.dont_write_bytecode = True

import hashlib
import codecs
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import signal
import stat
import subprocess
import threading
import time
from uuid import uuid4
import venv


MAX_LOG_BYTES = 256 * 1024
_LOG_HEAD = 64 * 1024
_LOG_TAIL = MAX_LOG_BYTES - _LOG_HEAD - 4096
_LOG_NAME = re.compile(r'installation-\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{32}\.log')


def _plain_directory(root):
    root = Path(root).absolute()
    for component in (root, *root.parents):
        info = component.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('Installer diagnostic folders cannot be linked or redirected.')
    return root


def _identity(info):
    return info.st_dev, info.st_ino


def _safe_text(value):
    # A log is data. Do not replay terminal control sequences from child output.
    return ''.join(character if character in '\n\r\t' or ord(character) >= 32 and not 127 <= ord(character) <= 159
                   else f'\\x{ord(character):02x}' for character in str(value))


def _console(text, *, error=False):
    stream = getattr(sys, 'stderr' if error else 'stdout', None)
    if stream is not None:
        try:
            stream.write(_safe_text(text))
            stream.flush()
        except (OSError, UnicodeError, ValueError):
            # A disappearing first-install console must not lose the file log.
            pass


def _stop_child(process):
    """Stop a timed-out trusted installer child and its still-owned descendants."""
    if os.name == 'nt':
        import ctypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        query = api.GetSystemDirectoryW
        query.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
        query.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(32768)
        size = query(buffer, len(buffer))
        if not 0 < size < len(buffer):
            raise OSError('Windows did not provide its system directory for child cleanup.')
        tool = Path(buffer.value) / 'taskkill.exe'
        if not tool.is_absolute():
            raise OSError('Windows returned a relative system directory.')
        # The executable comes from Windows, never PATH or a child-provided value.
        subprocess.run([str(tool), '/PID', str(process.pid), '/T', '/F'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


class InstallDiagnostics:
    """Exclusive per-attempt UTF-8 log with bounded first and latest output."""
    def __init__(self, root, *, name=None):
        self.root = _plain_directory(root)
        self._root_identity = _identity(self.root.lstat())
        name = name or ('installation-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ-')
                        + uuid4().hex + '.log')
        if type(name) is not str or not _LOG_NAME.fullmatch(name):
            raise ValueError('Invalid installer diagnostic filename.')
        self.path = self.root / name
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
        descriptor = os.open(self.path, flags, 0o600)
        self._stream = None
        try:
            info, visible = os.fstat(descriptor), self.path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or _identity(info) != _identity(visible)
                    or _identity(self.root.lstat()) != self._root_identity):
                raise ValueError('The exclusive diagnostic destination changed during creation.')
            self._identity = _identity(info)
            self._stream = os.fdopen(descriptor, 'wb')
            descriptor = None
            self._head, self._tail, self._total = b'', b'', 0
            self._last_flush = 0.0
            self._closed = False
            self._pending = False
            self._lock = threading.RLock()
            self.current_phase = 'starting installer'
            self.append('Resonance installation diagnostics\nAttempt: ' + name + '\n'
                        'Child stdout and stderr are merged below. Environment secrets are not logged.\n')
        except BaseException:
            if self._stream is not None:
                self._stream.close()
            raise
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def append(self, text, *, flush=True):
        encoded = _safe_text(text).encode('utf-8', errors='replace')
        with self._lock:
            if self._closed:
                raise ValueError('The installation diagnostic attempt has already closed.')
            self._total += len(encoded)
            count = min(len(encoded), _LOG_HEAD - len(self._head))
            self._head += encoded[:count]
            self._tail = (self._tail + encoded[count:])[-_LOG_TAIL:]
            self._pending = True
            if flush or time.monotonic() - self._last_flush >= .2:
                self._write()

    def _write(self):
        info, visible = os.fstat(self._stream.fileno()), self.path.lstat()
        if (not stat.S_ISREG(visible.st_mode) or info.st_nlink != 1
                or getattr(visible, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or _identity(info) != self._identity or _identity(visible) != self._identity
                or _identity(self.root.lstat()) != self._root_identity):
            raise ValueError('The diagnostic file changed; no existing file was overwritten.')
        omitted = self._total - len(self._head) - len(self._tail)
        marker = f'\n\n[... {omitted} bytes omitted; latest diagnostic output follows ...]\n\n' if omitted else ''
        text = self._head.decode('utf-8', errors='replace') + marker + self._tail.decode('utf-8', errors='replace')
        payload = text.encode('utf-8')
        if len(payload) > MAX_LOG_BYTES:
            raise ValueError('The installer diagnostic size bound was exceeded.')
        self._stream.seek(0)
        self._stream.write(payload)
        self._stream.truncate()
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._last_flush = time.monotonic()
        self._pending = False

    def _flush_pending(self):
        # A sparse or stalled child may produce no further read to trigger a
        # flush. The supervising thread persists those bytes while it waits.
        with self._lock:
            if self._pending and time.monotonic() - self._last_flush >= .2:
                self._write()

    def phase(self, label):
        self.current_phase = str(label)
        self.append('\n[PHASE] ' + self.current_phase + '\n')

    def run(self, command, *, check=True, env=None, cwd=None, timeout=1200):
        if type(timeout) not in (int, float) or not 0 < timeout <= 7200:
            raise ValueError('Installer subprocess timeouts must be bounded.')
        self.append('[CHILD] ' + subprocess.list2cmdline([str(item) for item in command]) + '\n')
        options = ({'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)}
                   if os.name == 'nt' else {'start_new_session': True})
        failure, finished = [], threading.Event()
        process = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False, **options)

        def consume():
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            try:
                read = getattr(process.stdout, 'read1', process.stdout.read)
                while raw := read(8192):
                    text = decoder.decode(raw)
                    self.append(text, flush=False)
                    _console(text)
                text = decoder.decode(b'', final=True)
                if text:
                    self.append(text, flush=False)
                    _console(text)
            except BaseException as exc:
                failure.append(exc)
            finally:
                finished.set()

        worker = None
        try:
            worker = threading.Thread(target=consume, name='installer-diagnostics', daemon=True)
            worker.start()
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if failure:
                    raise failure[0]
                self._flush_pending()
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                finished.wait(.05)
                if finished.is_set() and process.poll() is None:
                    time.sleep(.01)
            if not finished.wait(5):
                raise RuntimeError('Installer child output did not close after the process ended.')
            if failure:
                raise failure[0]
            self.append(f'\n[CHILD EXIT] {process.returncode}\n')
            if check and process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command)
            return subprocess.CompletedProcess(command, process.returncode)
        except BaseException as error:
            try:
                self.append('\n[CHILD ERROR] ' + type(error).__name__ + ': ' + str(error) + '\n')
            except Exception:
                pass
            raise
        finally:
            if process.poll() is None or (worker is not None and worker.is_alive()):
                try:
                    _stop_child(process)
                except Exception as error:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    try:
                        self.append('\n[CLEANUP] ' + type(error).__name__ + ': ' + str(error) + '\n')
                    except Exception:
                        pass
            if worker is not None and worker.ident is not None:
                worker.join(timeout=5)
            if worker is None or not worker.is_alive():
                process.stdout.close()
            else:
                try:
                    self.append('\n[CLEANUP] A descendant kept its output pipe open; '
                                'this attempt is incomplete.\n')
                except Exception:
                    pass

    def close(self):
        # Every status/child completion forces a flush before ready publication.
        # Closing this descriptor performs no additional status mutation.
        with self._lock:
            self._closed = True
            self._stream.close()


def read_install_log(path):
    path = Path(path).absolute()
    _plain_directory(path.parent)
    if not _LOG_NAME.fullmatch(path.name):
        raise ValueError('Only per-attempt installer text logs can be opened.')
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or getattr(before, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not 0 < before.st_size <= MAX_LOG_BYTES):
        raise ValueError('The installation log is linked, empty, or outside its size bound.')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(MAX_LOG_BYTES + 1)
    after = path.lstat()
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
    if (len(raw) != before.st_size or any(getattr(before, field) != getattr(opened, field)
            or getattr(before, field) != getattr(after, field) for field in fields)):
        raise ValueError('The installation log changed while being read. Try again after the attempt finishes.')
    return _safe_text(raw.decode('utf-8'))


def show_latest_install_log(root):
    root = _plain_directory(root)
    logs = []
    for count, path in enumerate(root.iterdir()):
        if count >= 10000:
            raise ValueError('Too many files in the program folder to select a diagnostic log safely.')
        if _LOG_NAME.fullmatch(path.name):
            logs.append(path)
    if not logs:
        _console('No persistent installation log was found. Look for installation-*.log in this program folder.\n')
        return 1
    latest = max(logs, key=lambda path: path.name)
    text = read_install_log(latest)
    _console('Persistent installation log: ' + str(latest) + '\n\n' + text + '\n')
    return 0



MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_OPTIONAL_ENTRIES = 50000


def read_manifest(root):
    """Capture one bounded manifest for verification and later copy decisions."""
    root = _plain_directory(root)
    path = root / "bundle-manifest.json"
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or path.is_symlink() or before.st_nlink != 1
            or getattr(before, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not 0 < before.st_size <= MAX_MANIFEST_BYTES):
        raise ValueError("The installation manifest must be a bounded ordinary file.")
    stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if stamp(os.fstat(stream.fileno())) != stamp(before):
            raise ValueError("The installation manifest changed before reading.")
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if (len(raw) != before.st_size or stamp(os.fstat(stream.fileno())) != stamp(before)
                or stamp(path.lstat()) != stamp(before)):
            raise ValueError("The installation manifest changed while reading.")
    return raw


def _unique_manifest(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate installation manifest key.")
        result[key] = value
    return result


def _optional_runtime_inventory(root, component):
    """Bound the actual local tree without following links or ignoring I/O errors."""
    directory = root / component
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError("Optional runtime folders cannot be linked or redirected.")
    pending, actual, count = [directory], set(), 0
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                count += 1
                if count > MAX_OPTIONAL_ENTRIES:
                    raise ValueError("Optional runtime inventory exceeds its file limit.")
                info = entry.stat(follow_symlinks=False)
                if (stat.S_ISLNK(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    raise ValueError("Optional runtime paths cannot be linked or redirected.")
                path = Path(entry.path)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    actual.add(path.relative_to(root).as_posix())
                else:
                    raise ValueError("Optional runtime payload must contain ordinary files.")
    return actual


def verify(root, *, manifest_bytes=None):
    # Packaging callers can bind verification to the exact snapshot they seal,
    # without reopening a manifest that another operation might replace.
    root = Path(root)
    raw = read_manifest(root) if manifest_bytes is None else manifest_bytes
    if not isinstance(raw, (bytes, str)) or len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("The installation manifest exceeds its size limit.")
    manifest = json.loads(raw, object_pairs_hook=_unique_manifest)
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if (not isinstance(manifest, dict) or type(manifest.get("schema")) is not int
            or manifest["schema"] != 1 or not isinstance(files, dict) or not files):
        raise ValueError("invalid bundle manifest")
    expected = {"install.lock", "install_windows.py", "Install.cmd", "ResearchLibrary.cmd",
                "CheckInstallation.cmd", "README.txt"}
    if not expected.issubset(files) or not any(p.startswith("wheelhouse/") for p in files):
        raise ValueError("incomplete installation bundle")
    for name, digest in files.items():
        relative = PurePosixPath(name)
        if (relative.is_absolute() or ".." in relative.parts or "\\" in name
                or ":" in name or not relative.parts or "." in relative.parts):
            raise ValueError("invalid manifest path")
        path = root.joinpath(*relative.parts)
        for component in (path, *path.parents):
            info = component.lstat()
            if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                    & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError("linked bundle paths are not supported")
            if component == root:
                break
        if not path.is_file():
            raise ValueError("missing bundle file")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            raise ValueError(f"bundle checksum failed: {name}")
    actual_wheels = {"wheelhouse/" + p.name for p in (root / "wheelhouse").iterdir()}
    if actual_wheels != {p for p in files if p.startswith("wheelhouse/")}:
        raise ValueError("unexpected or missing wheelhouse file")
    if manifest.get("bundled_python"):
        runtime = root / "runtime"
        actual = {p.relative_to(root).as_posix() for p in runtime.rglob("*") if p.is_file()}
        if ("runtime/python.exe" not in actual
                or actual != {p for p in files if p.startswith("runtime/")}):
            raise ValueError("unexpected or missing Python runtime file")
    if manifest.get("bundled_ocr"):
        ocr = root / "ocr"
        for directory, dirs, _names in os.walk(ocr, followlinks=False):
            for name in dirs:
                info = (Path(directory) / name).lstat()
                if (not stat.S_ISDIR(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    raise ValueError("linked OCR runtime directories are not supported")
        actual = {p.relative_to(root).as_posix() for p in ocr.rglob("*") if p.is_file()}
        if ("ocr/ocr-manifest.json" not in actual or "ocr/tesseract.exe" not in actual
                or "ocr/tessdata/eng.traineddata" not in actual
                or actual != {p for p in files if p.startswith("ocr/")}):
            raise ValueError("unexpected or missing OCR runtime file")
    native = root / "native-runtime"
    included = manifest.get("bundled_native_runtime", False)
    expected_native = {p for p in files if p.startswith("native-runtime/")}
    if type(included) is not bool:
        raise ValueError("invalid native runtime installation flag")
    if included:
        if not manifest.get("bundled_python"):
            raise ValueError("native runtime requires bundled Python")
        for directory, dirs, _names in os.walk(native, followlinks=False):
            for name in dirs:
                info = (Path(directory) / name).lstat()
                if (not stat.S_ISDIR(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    raise ValueError("linked native runtime directories are not supported")
        actual = {p.relative_to(root).as_posix() for p in native.rglob("*") if p.is_file()}
        required_native = {"native-runtime/native-manifest.json",
                           "native-runtime/bin/msvcp140.dll", "native-runtime/bin/msvcp140_1.dll"}
        if not required_native.issubset(actual) or actual != expected_native:
            raise ValueError("unexpected or missing native runtime file")
    elif os.path.lexists(native) or expected_native:
        raise ValueError("unannounced native runtime files")
    audio_layouts = []
    optional_components = (
        ('audio', 'bundled_audio', {'audio/bin/ffmpeg.exe', 'audio/bin/ffprobe.exe', 'audio/LICENSE'}),
        ('audio-runtime', 'bundled_audio_runtime', {
            'audio-runtime/bin/ffmpeg.exe', 'audio-runtime/bin/ffprobe.exe', 'audio-runtime/bin/ffplay.exe',
            'audio-runtime/audio-manifest.json', 'audio-runtime/provenance/source-lock.json'}),
        ('citation-runtime', 'bundled_citation_runtime', {'citation-runtime/node.exe', 'citation-runtime/LICENSE'}))
    # Windows is case insensitive; a differently spelled directory is not a
    # second way to declare one of the optional executable payloads.
    names = {row.name for row in root.iterdir()}
    for component, flag, required in optional_components:
        included = manifest.get(flag, False)
        if type(included) is not bool:
            raise ValueError('invalid optional runtime flag')
        if (any(name.casefold() == component and name != component for name in names)
                or any(name.casefold().startswith(component + '/') and not name.startswith(component + '/')
                       for name in files)):
            raise ValueError('ambiguous optional runtime directory')
        expected_component = {p for p in files if p.startswith(component + '/')}
        actual = _optional_runtime_inventory(root, component)
        present = actual is not None
        if not present:
            if component in names:
                raise ValueError('optional runtime directory changed during verification')
            actual = set()
        if component in ('audio', 'audio-runtime') and (included or present or expected_component):
            audio_layouts.append(component)
        if len(audio_layouts) > 1:
            raise ValueError('both canonical and legacy audio layouts are present')
        if included:
            if not required <= actual or actual != expected_component:
                raise ValueError('unexpected or missing ' + component + ' runtime file')
        elif present or actual or expected_component:
            raise ValueError('unannounced ' + component + ' runtime')


def main(diagnostics=None):
    phase = diagnostics.phase if diagnostics is not None else lambda label: None
    run = diagnostics.run if diagnostics is not None else subprocess.run
    phase('Checking Windows and CPython requirements')
    if os.name != "nt" or sys.version_info[:2] != (3, 11) or platform.architecture()[0] != "64bit":
        raise RuntimeError("Use Windows x64 with CPython 3.11 (py -3.11 install_windows.py).")
    root = Path(__file__).absolute().parent
    phase('Verifying the complete offline bundle')
    verify(root)
    phase('Checking program and research-data separation')
    local = os.environ.get("LOCALAPPDATA")
    if not local or not Path(local).is_absolute():
        raise RuntimeError("Windows did not provide an absolute LOCALAPPDATA directory.")
    data = (Path(local) / "Universal Research Library").resolve()
    program = root.resolve()
    if data.is_relative_to(program) or program.is_relative_to(data):
        raise RuntimeError("Extract the program outside its research data directory.")
    destination = root / ".venv"
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("This folder already has an environment. Extract a fresh package for a new installation.")
    _console("Verifying complete. Creating the application environment.\n")
    phase('Creating a fresh application environment')
    destination.mkdir(exist_ok=False)
    # Keep the hash-sealed base runtime unchanged. EnvBuilder's implicit pip
    # bootstrap does not carry the caller's -B flag into its child interpreter.
    # Launch the stdlib ensurepip entrypoint with a sanitized environment below.
    venv.EnvBuilder(with_pip=False, clear=False, symlinks=False).create(destination)
    executable = destination / "Scripts" / "python.exe"
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("PYTHON", "PIP_"))}
    environment["PIP_NO_INDEX"] = "1"
    if (root / "ocr" / "ocr-manifest.json").is_file():
        environment["CARR_OCR_DIR"] = str(root / "ocr")
    # Python 3.11 ensurepip forwards -I, but not -B, to its own pip subprocess.
    # Explicit -s/-P and these three controlled variables preserve no-user-site,
    # safe import paths and no-bytecode behavior across that nested process.
    bootstrap_environment = dict(environment, PYTHONNOUSERSITE="1", PYTHONSAFEPATH="1",
                                 PYTHONDONTWRITEBYTECODE="1")
    phase('Bootstrapping the local pip runtime without bytecode writes')
    run([str(executable), "-B", "-X", "utf8", "-s", "-P", "-m", "ensurepip", "--upgrade", "--default-pip"],
                   check=True, env=bootstrap_environment, cwd=destination)
    phase('Installing hash-pinned packages from the offline wheelhouse')
    run([str(executable), "-I", "-B", "-X", "utf8", "-m", "pip", "--isolated", "--disable-pip-version-check",
                    "install", "--no-index", "--only-binary=:all:", "--require-hashes",
                    "--no-cache-dir", "--find-links", str(root / "wheelhouse"),
                    "-r", str(root / "install.lock")], check=True, env=environment)
    phase('Checking the installed native desktop and document dependencies')
    run([str(executable), "-I", "-B", "-X", "utf8", "-m",
                    "carr_library.native_checks", "--bundle-root", str(root)],
                   check=True, env=environment, cwd=root)
    phase('Running the installed application self-test')
    run([str(executable), "-I", "-B", "-X", "utf8", "-m", "carr_library", "--self-test"],
                   check=True, env=environment, cwd=root)
    # An incomplete or interrupted install must never be treated as ready.
    # Exclusive creation is the final lifecycle step; never delete an old environment.
    phase('Every installation check passed; creating the final completion marker')
    (destination / ".installation-complete").touch(exist_ok=False)
    _console("Installation complete. Open ResearchLibrary.cmd. Research data has not been changed.\n")
    return 0


def entrypoint(arguments=None):
    arguments = sys.argv[1:] if arguments is None else list(arguments)
    root = Path(__file__).absolute().parent
    if arguments == ['--show-install-log']:
        try:
            return show_latest_install_log(root)
        except Exception as error:
            _console('The persistent installation log could not be read safely: ' + str(error) + '\n', error=True)
            return 1
    if arguments:
        _console('Usage: install_windows.py [--show-install-log]\n', error=True)
        return 2
    diagnostics = None
    try:
        diagnostics = InstallDiagnostics(root)
        _console('Persistent installation log: ' + str(diagnostics.path) + '\n')
        return main(diagnostics=diagnostics)
    except (Exception, KeyboardInterrupt) as error:
        if diagnostics is not None:
            try:
                diagnostics.append('\n[FAILED] ' + diagnostics.current_phase + '\n'
                                   + type(error).__name__ + ': ' + str(error) + '\n')
            except Exception as logging_error:
                _console('The installation log could not be updated: ' + str(logging_error) + '\n', error=True)
        _console('Installation could not finish: ' + str(error) + '\n', error=True)
        if diagnostics is not None:
            _console('Diagnostic text remains in ' + str(diagnostics.path) + '\n', error=True)
        else:
            _console('No environment was created: a new persistent log could not be opened in the program folder.\n', error=True)
        return 1
    finally:
        if diagnostics is not None:
            try:
                diagnostics.close()
            except OSError as error:
                _console('The diagnostic file could not be closed: ' + str(error) + '\n', error=True)


if __name__ == "__main__":
    raise SystemExit(entrypoint())
