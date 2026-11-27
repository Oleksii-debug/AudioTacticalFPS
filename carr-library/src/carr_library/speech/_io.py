"""Small local worker boundary used by installed speech engines.

These are process/resource boundaries, not a sandbox for an untrusted voice DLL.
Only explicitly installed local engines are executed; no model is downloaded.
"""
from contextlib import contextmanager
from hashlib import sha256
import math
import os
from pathlib import Path, PureWindowsPath
import signal
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
import time


def text_value(value, label='text', *, maximum=2_000_000, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f'{label} must be nonempty text of at most {maximum} characters.')
    if '\0' in value:
        raise ValueError(f'{label} contains a NUL character.')
    try:
        value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ValueError(f'{label} contains invalid Unicode.') from exc
    return value


def check_cancelled(cancelled):
    if not callable(cancelled):
        raise ValueError('A cancellation callback is required.')
    if cancelled():
        raise InterruptedError('Speech operation cancelled; no output was published.')


def linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0)
        & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def local_file(path, *, must_exist=True, _allow_hardlinks=False):
    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw or '\0' in raw or raw.startswith(('\\\\', '//')):
        raise ValueError('Choose a local file; network paths are not permitted.')
    # A colon in the basename is an NTFS stream, even when validated on POSIX.
    result = Path(raw).absolute()
    name = result.name
    if (not name or name.rstrip(' .') != name or PureWindowsPath(name).is_reserved()
            or any(c in '<>:"/\\|?*' or ord(c) < 32 for c in name)):
        raise ValueError('Choose a portable local filename without reserved characters.')
    for parent in (result.parent, *result.parent.parents):
        info = parent.lstat()
        if linked(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError('Choose a local folder without symbolic links or reparse points.')
    try:
        info = result.lstat()
    except FileNotFoundError:
        if must_exist:
            raise
    else:
        if linked(info) or not stat.S_ISREG(info.st_mode) or (info.st_nlink != 1 and not _allow_hardlinks):
            raise ValueError('The file must be regular and must not have links or reparse points.')
    return result


def windows_directory():
    """Ask Windows itself, rather than trusting an overridable environment path."""
    if os.name != 'nt':
        raise RuntimeError('The Windows system directory is only available on Windows.')
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if not 0 < length < len(buffer):
        raise OSError('Windows did not return its system directory.')
    return Path(buffer.value)


def system_powershell(path, *, architecture=None):
    """Accept Windows' serviced PowerShell executable, including WinSxS hard links.

    This exception is confined to the two fixed OS executables. Source files,
    voice models, helpers and rendered outputs keep the single-link requirement.
    Reparse points and linked ancestors remain forbidden in every case.
    """
    candidate = Path(path).absolute()
    root = windows_directory()
    if architecture not in (None, '32', '64'):
        raise ValueError('SAPI host architecture must be 32 or 64.')
    folders = (('System32', 'SysWOW64') if architecture is None else
               (('System32',) if architecture == '64' else ('SysWOW64',)))
    allowed = {root / folder / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
               for folder in folders}
    if candidate not in allowed:
        raise ValueError('SAPI must use the installed Windows system PowerShell host.')
    return local_file(candidate, _allow_hardlinks=True)


def digest(path):
    h = sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _sync_directory(parent):
    if os.name != 'nt':
        fd = os.open(parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextmanager
def new_output(destination, *, cancelled=lambda: False):
    """Stage beside the target and publish with an atomic no-clobber link.

    No placeholder is ever published. Filesystems lacking hard-link support
    fail explicitly instead of substituting a race-prone overwrite.
    """
    check_cancelled(cancelled)
    target = local_file(destination, must_exist=False)
    if os.path.lexists(target):
        raise FileExistsError('The output already exists. Choose a new filename.')
    parent_identity = (target.parent.stat().st_dev, target.parent.stat().st_ino)
    with TemporaryDirectory(prefix='.speech-', dir=target.parent) as temporary:
        staged = Path(temporary) / 'output.wav'
        yield staged
        check_cancelled(cancelled)
        local_file(staged)
        checked = local_file(target, must_exist=False)
        if checked != target or (target.parent.stat().st_dev, target.parent.stat().st_ino) != parent_identity:
            raise ValueError('The output folder changed during rendering.')
        # Windows requires a writable descriptor to flush the completed staging file.
        with staged.open('r+b') as stream:
            os.fsync(stream.fileno())
        check_cancelled(cancelled)
        os.link(staged, target)
        # Remove the staging name before readers validate the final inode.
        staged.unlink()
        _sync_directory(target.parent)


def poll_process(process):
    """Observe a Linux worker exit without releasing its owned group identity.

    The caller must stop the group before wait() reaps its leader. Retaining
    that leader prevents its PID/PGID from being reused during cleanup.
    """
    if sys.platform == 'linux' and process.returncode is None:
        status = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if status is None:
            return None
        return status.si_status if status.si_code == os.CLD_EXITED else -status.si_status
    return process.poll()


def _wait_linux_group(group, *, timeout=5):
    """Wait for execution to end, without waiting for orphan zombies to be reaped."""
    deadline = time.monotonic() + timeout
    expected = str(group).encode('ascii')
    while True:
        active = False
        with os.scandir('/proc') as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    raise TimeoutError('The owned worker group did not terminate in time.')
                if not entry.name.isdigit():
                    continue
                try:
                    fields = (Path(entry.path) / 'stat').read_bytes().rsplit(b') ', 1)[1].split()
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except PermissionError:
                    # Unrelated users may hide their process details. An owned
                    # group member must remain observable before completion.
                    try:
                        member = os.getpgid(int(entry.name)) == group
                    except ProcessLookupError:
                        continue
                    if member:
                        raise RuntimeError('Cannot verify termination of an owned worker.')
                    continue
                if fields[2] == expected and fields[0] not in {b'Z', b'X', b'x'}:
                    active = True
                    break
        if not active:
            return
        time.sleep(min(.01, max(0, deadline - time.monotonic())))


def stop_process(process, *, wait_for_group=False):
    """Request bounded shutdown; opt-in success requires observed group exit.

    An error remains unconfirmed shutdown. The direct child is still reaped;
    callers must propagate that error instead of admitting completion.
    """
    linux = wait_for_group and sys.platform == 'linux'
    if process.returncode is not None or (not linux and process.poll() is not None):
        return
    if linux:
        try:
            os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            raise RuntimeError('The owned worker leader was reaped outside its lifecycle.') from None
    if os.name == 'nt':
        try:
            system = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32' / 'taskkill.exe'
            subprocess.run([str(system), '/PID', str(process.pid), '/T', '/F'],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.SubprocessError):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        if linux:
            # Keep the leader unreaped until its group has stopped executing.
            # Waiting only for Popen's direct child does not wait for descendants.
            _wait_linux_group(process.pid)
    finally:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

def run_process(args, *, cwd=None, input_bytes=None, cancelled=lambda: False,
                timeout=120, output_file=None, max_output=256 * 1024 * 1024,
                allow_failure=False):
    """Execute a fixed argv and bounded private input; never invoke a shell."""
    check_cancelled(cancelled)
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('A positive finite worker timeout is required.')
    if type(max_output) is not int or max_output < 1:
        raise ValueError('A positive output-byte budget is required.')
    if input_bytes is not None and (not isinstance(input_bytes, bytes) or len(input_bytes) > 8_000_000):
        raise ValueError('The speech worker input is too large.')
    env_names = ('PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL',
                 'LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'HOME', 'PATHEXT')
    environment = {key: os.environ[key] for key in env_names if key in os.environ}
    environment.update(ORT_DISABLE_TELEMETRY='1', HF_HUB_OFFLINE='1',
                       HF_HUB_DISABLE_TELEMETRY='1', TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1')
    options = ({'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)} if os.name == 'nt'
               else {'start_new_session': True})
    with TemporaryFile() as incoming, TemporaryFile() as outgoing, TemporaryFile() as errors:
        if input_bytes:
            incoming.write(input_bytes)
            incoming.seek(0)
        process = subprocess.Popen([os.fspath(arg) for arg in args], cwd=cwd, env=environment,
                                   stdin=incoming, stdout=outgoing, stderr=errors, shell=False, **options)
        started = time.monotonic()
        try:
            while process.poll() is None:
                check_cancelled(cancelled)
                if time.monotonic() - started > timeout:
                    raise TimeoutError('The local speech engine exceeded its time budget.')
                _check_sizes(outgoing, errors, output_file, max_output)
                time.sleep(.04)
            check_cancelled(cancelled)
            _check_sizes(outgoing, errors, output_file, max_output)
            outgoing.seek(0)
            errors.seek(0)
            reply, diagnostic = outgoing.read(2_000_000), errors.read(2_000_000)
            if process.returncode and not allow_failure:
                # Native diagnostics can contain source passages or credentials.
                raise RuntimeError(f'The local speech engine failed (exit {process.returncode}).')
            return reply, diagnostic, process.returncode
        finally:
            stop_process(process)


def _check_sizes(outgoing, errors, output_file, max_output):
    if os.fstat(outgoing.fileno()).st_size > 2_000_000 or os.fstat(errors.fileno()).st_size > 2_000_000:
        raise ValueError('The local speech engine exceeded its diagnostic-output budget.')
    if output_file is not None and Path(output_file).exists() and Path(output_file).stat().st_size > max_output:
        raise ValueError('The local speech engine exceeded its audio-output budget.')
