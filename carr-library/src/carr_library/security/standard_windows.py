"""Current standard-layout Windows activation, over the existing owned launcher.

Only explicitly configured trusted update transactions call this adapter.
It does not find repositories, roots, policies, accounts or update services.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time

from ..audio.engines import _start_process
from ..audio.jobs import verified_reader
from ..audio.model_manifest import local_path
from ..audio.recording_process import _Accounting, _stop_owned
from ..audio.cleanup import pending_cleanup
from ..installation_lease import installation_lease
from ._files import TrustError, atomic_write, directory, file_info, json_bytes, read_bytes, sync_directory
from .installation import WindowsBundleAdapter
from .standard_state import MAX_STATE, PENDING, PROTOCOL, generation_name, read_pending

REGISTRY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\Resonance.StandardInstall_is1'
SETUP_MUTEXES = ('Resonance.StandardInstall', r'Global\Resonance.StandardInstall')
MAX_COMPONENT = 16 * 1024**2
MAX_COMPONENTS = 32
_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_SOURCE = re.compile(r'[0-9a-f]{40}\Z')
_UNINSTALL = re.compile(r'unins[0-9]{3}\.(exe|dat|msg)\Z', re.I)


def _file_digest(path, maximum=16 * 1024**3, *, cancelled=lambda: False):
    from .updates import _cancel
    _cancel(cancelled)
    info = file_info(path)
    if info.st_size > maximum:
        raise TrustError('Standard installation file exceeds its byte budget.')
    digest, total = sha256(), 0
    with verified_reader(path) as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            _cancel(cancelled)
            total += len(block)
            if total > maximum:
                raise TrustError('Standard installation file grew beyond its budget.')
            digest.update(block)
    _cancel(cancelled)
    return digest.hexdigest()


def _binary(raw):
    if type(raw) is not bytes or len(raw) > MAX_COMPONENT:
        raise TrustError('Standard activation component exceeds its size budget.')
    return {'bytes': len(raw), 'sha256': sha256(raw).hexdigest(),
            'base64': base64.b64encode(raw).decode('ascii')}


def _decode(value):
    if (type(value) is not dict or set(value) != {'bytes', 'sha256', 'base64'}
            or type(value['bytes']) is not int or not 0 <= value['bytes'] <= MAX_COMPONENT
            or type(value['sha256']) is not str or not _DIGEST.fullmatch(value['sha256'])
            or type(value['base64']) is not str or len(value['base64']) > 4 * ((MAX_COMPONENT + 2) // 3)):
        raise TrustError('Invalid saved standard activation component.')
    try:
        raw = base64.b64decode(value['base64'], validate=True)
    except (ValueError, TypeError) as error:
        raise TrustError('Invalid saved standard activation bytes.') from error
    if (_binary(raw) != value):
        raise TrustError('Saved standard activation component checksum mismatch.')
    return raw


def validate_snapshot(value, root):
    root = directory(root)
    fields = {'schema', 'program', 'generation', 'source_commit', 'manifest_sha256',
              'python_sha256', 'setup_sha256', 'registration', 'shortcut_path',
              'shortcut', 'uninstallers'}
    if (type(value) is not dict or set(value) != fields or value['schema'] != PROTOCOL
            or value['program'] != str(root) or type(value['source_commit']) is not str
            or not _SOURCE.fullmatch(value['source_commit'])):
        raise TrustError('Invalid standard activation snapshot.')
    generation_name(value['generation'], value['source_commit'])
    for name in ('manifest_sha256', 'python_sha256', 'setup_sha256'):
        if type(value[name]) is not str or not _DIGEST.fullmatch(value[name]):
            raise TrustError('Invalid standard generation identity.')
    registration = value['registration']
    if (type(registration) is not dict or not 1 <= len(registration) <= 128
            or not {'InstallLocation', 'UninstallString', 'ModifyPath', 'DisplayIcon'}.issubset(registration)):
        raise TrustError('Invalid registered standard installation.')
    for name, row in registration.items():
        if (type(name) is not str or not 1 <= len(name) <= 256 or any(ord(c) < 32 for c in name)
                or type(row) is not dict or set(row) != {'type', 'value'} or type(row['type']) is not int):
            raise TrustError('Invalid standard registry value.')
        kind, item = row['type'], row['value']
        if kind in (1, 2):
            valid = type(item) is str and len(item) <= 32768 and '\0' not in item
        elif kind in (4, 11):
            valid = type(item) is int and 0 <= item < 2 ** (32 if kind == 4 else 64)
        elif kind == 7:
            valid = (type(item) is list and len(item) <= 64
                     and all(type(text) is str and len(text) <= 4096 and '\0' not in text for text in item))
        else:
            valid = False
        if not valid:
            raise TrustError('Unsupported standard registry value type.')
    generation = root.joinpath(*value['generation'].split('/'))
    def registered_path(name):
        row = registration[name]
        if row['type'] not in (1, 2) or type(row['value']) is not str:
            raise TrustError('Invalid registered standard executable path.')
        return row['value']
    if directory(registered_path('InstallLocation').rstrip('\\/')) != root:
        raise TrustError('Registered installation belongs to another directory.')
    if local_path(registered_path('ModifyPath').strip('"')) != generation / 'maintenance-setup.exe':
        raise TrustError('Registered repair program is not the recorded generation.')
    if local_path(registered_path('DisplayIcon').strip('"')) != generation / 'ResearchLibrary.exe':
        raise TrustError('Registered native entrypoint is not the recorded generation.')
    uninstallers = value['uninstallers']
    if (type(uninstallers) is not dict or not 2 <= len(uninstallers) <= MAX_COMPONENTS
            or any(type(name) is not str or not _UNINSTALL.fullmatch(name) for name in uninstallers)):
        raise TrustError('Invalid registered uninstaller inventory.')
    command = registered_path('UninstallString')
    valid_commands = {root / name for name in uninstallers if name.lower().endswith('.exe')}
    if local_path(command.strip('"')) not in valid_commands:
        raise TrustError('The registered uninstaller is outside this recorded installation.')
    for name, item in uninstallers.items():
        _decode(item)
    if type(value['shortcut_path']) is not str or not Path(value['shortcut_path']).is_absolute():
        raise TrustError('Invalid standard shortcut location.')
    _decode(value['shortcut'])
    if len(json_bytes(value)) > MAX_STATE:
        raise TrustError('Standard activation snapshot exceeds its byte budget.')
    return value


def _drain(process, deadline, cancelled, diagnostics=None):
    job = getattr(process, '_carr_job', None)
    if os.name != 'nt':
        return
    if job is None:
        raise TrustError('The native installer lacks its existing owned Windows job.')
    query = job.api.kernel.QueryInformationJobObject
    query.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                      ctypes.c_uint32, ctypes.c_void_p]
    query.restype = ctypes.c_int
    while True:
        if diagnostics is not None and os.fstat(diagnostics.fileno()).st_size > 2 * 1024**2:
            raise TrustError('The standard installer exceeded its diagnostic budget.')
        info = _Accounting()
        if not query(job.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise job.api.error()
        if info.active_processes == 0:
            return
        if cancelled() or time.monotonic() >= deadline:
            raise TrustError('The native installer has not completed its owned child processes.')
        time.sleep(.02)


def run_owned(arguments, *, cwd, environment, timeout, cancelled=lambda: False):
    if type(timeout) not in (int, float) or not 1 <= timeout <= 3600:
        raise TrustError('Invalid standard installer deadline.')
    process, output = None, tempfile.TemporaryFile()
    try:
        process = _start_process([str(value) for value in arguments], cwd=cwd,
            env=environment, stdin=subprocess.DEVNULL, stdout=output,
            stderr=subprocess.STDOUT, shell=False)
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if cancelled() or time.monotonic() >= deadline:
                raise TrustError('The standard installation was stopped before completion.')
            if os.fstat(output.fileno()).st_size > 2 * 1024**2:
                raise TrustError('The standard installer exceeded its diagnostic budget.')
            time.sleep(.02)
        _drain(process, deadline, cancelled, output)
        if os.fstat(output.fileno()).st_size > 2 * 1024**2:
            raise TrustError('The standard installer exceeded its diagnostic budget.')
        output.seek(0)
        return process.returncode, output.read(2 * 1024**2 + 1)
    finally:
        try:
            if process is not None:
                try:
                    _stop_owned(process)
                except Exception:
                    # Retain the actual output handle and existing cleanup owner.
                    # The caller additionally transfers its lease stack to it.
                    retained, output = output, None
                    raise pending_cleanup(lambda: _stop_owned(process),
                        message='Standard installation cleanup is pending; retained program state must not be replaced.',
                        callbacks=(retained.close,))
        finally:
            if output is not None:
                output.close()


class WindowsStandardHost:
    """Bind the existing Inno layout, verifier and isolated health commands."""
    def __init__(self, verify_bundle, validate_manifest, *, timeout=1800):
        if os.name != 'nt':
            raise TrustError('The standard installation adapter requires native Windows.')
        if not callable(verify_bundle) or not callable(validate_manifest):
            raise TrustError('Use the current trusted host bundle and transfer validators.')
        if type(timeout) not in (int, float) or not 1 <= timeout <= 3600:
            raise TrustError('Invalid standard installation deadline.')
        self.verify_bundle, self.validate_manifest, self.timeout = verify_bundle, validate_manifest, timeout
        self.health_adapter = WindowsBundleAdapter(verify_bundle, timeout=min(timeout, 1800))

    @staticmethod
    def environment():
        names = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'USERPROFILE',
                 'APPDATA', 'LOCALAPPDATA', 'HOMEDRIVE', 'HOMEPATH', 'PROGRAMDATA',
                 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE'}
        env = {key: value for key, value in os.environ.items() if key.upper() in names}
        env.update(PIP_NO_INDEX='1', PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
        return env

    @contextmanager
    def setup_guard(self):
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        api.CreateMutexW.restype = ctypes.c_void_p
        api.CloseHandle.argtypes = [ctypes.c_void_p]
        api.CloseHandle.restype = ctypes.c_int
        handles = []
        try:
            for name in SETUP_MUTEXES:
                ctypes.set_last_error(0)
                handle = api.CreateMutexW(None, False, name)
                error = ctypes.get_last_error()
                if not handle:
                    raise ctypes.WinError(error)
                handles.append(handle)
                if error == 183:
                    raise TrustError('Another standard installer or recovery is active.')
            yield
        finally:
            for handle in reversed(handles):
                api.CloseHandle(handle)

    def lifetime(self, root):
        return installation_lease(root)

    @staticmethod
    def _registration():
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            result = {}
            count, _, _ = winreg.QueryInfoKey(key)
            if count:
                raise TrustError('The standard uninstaller registry has unexpected child keys.')
            for index in range(129):
                try:
                    name, value, kind = winreg.EnumValue(key, index)
                except OSError as error:
                    if getattr(error, 'winerror', None) == 259:
                        return result
                    raise
                result[name] = {'type': kind, 'value': value}
            raise TrustError('The standard uninstaller registry exceeds its value budget.')

    @staticmethod
    def shortcut():
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r'Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders') as key:
            value, kind = winreg.QueryValueEx(key, 'Programs')
        if kind != winreg.REG_SZ or type(value) is not str:
            raise TrustError('The user Start-menu folder is unavailable.')
        return directory(value) / 'Resonance.lnk'

    @staticmethod
    def _powershell():
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        api.GetSystemDirectoryW.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        size = api.GetSystemDirectoryW(buffer, len(buffer))
        if not 0 < size < len(buffer):
            raise TrustError('The trusted Windows system directory is unavailable.')
        # This fixed, OS-owned helper is not an update payload or private state.
        # Windows servicing may retain legitimate hardlinks to system binaries.
        # All ancestor and leaf redirection checks remain mandatory; file_info
        # keeps its stricter single-link contract for every application file.
        parent = directory(directory(buffer.value) / 'WindowsPowerShell' / 'v1.0')
        path = parent / 'powershell.exe'
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400 or info.st_nlink < 1):
            raise TrustError('The fixed Windows system helper is linked or redirected.')
        return path

    def _shortcut_target(self, shortcut, root):
        environment = self.environment()
        environment['CARR_STANDARD_SHORTCUT'] = str(shortcut)
        # The bounded native diagnosis reproduced New-Object module bootstrap
        # stalls under this deliberately filtered environment. Activate only the
        # same fixed local COM class, with no PowerShell module autoload.
        script = ("$ErrorActionPreference='Stop';$PSModuleAutoLoadingPreference='None';"
                  "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();"
                  "$shell=$null;$link=$null;"
                  "try{try{$shell=[Activator]::CreateInstance("
                  "[Type]::GetTypeFromProgID('WScript.Shell'));"
                  "$link=$shell.CreateShortcut($env:CARR_STANDARD_SHORTCUT);"
                  "[Console]::Write([string]$link.TargetPath)}finally{"
                  "try{if($null -ne $link){[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($link)}}"
                  "finally{if($null -ne $shell){[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)}}"
                  "}}catch{exit 7}")
        code, raw = run_owned([self._powershell(), '-NoProfile', '-NonInteractive', '-Command', script],
                             cwd=root, environment=environment, timeout=30)
        if code or len(raw) > 32768:
            raise TrustError('The current Start-menu target could not be verified.')
        return local_path(raw.decode('utf-8-sig').strip())

    def snapshot(self, root):
        from ._files import json_value
        root = directory(root)
        if read_bytes(root / 'install-layout-v1.txt', 128).strip() != b'Resonance.StandardInstall/1':
            raise TrustError('This updater requires the current standard versions layout.')
        registration = self._registration()
        modify = registration.get('ModifyPath', {}).get('value')
        if type(modify) is not str:
            raise TrustError('The current standard installation is not registered.')
        generation = directory(Path(modify.strip('"')).parent)
        relative = generation.relative_to(root).as_posix()
        raw = read_bytes(generation / 'bundle-manifest.json', 16 * 1024**2)
        manifest = json_value(raw, maximum=16 * 1024**2)
        source = manifest.get('source_commit') if type(manifest) is dict else None
        if type(source) is not str or not _SOURCE.fullmatch(source):
            raise TrustError('The installed generation has no exact source identity.')
        generation_name(relative, source)
        self.validate_manifest(raw, source)
        self.verify_bundle(generation, manifest_bytes=raw)
        file_info(generation / '.venv' / '.installation-complete')
        python = generation / '.venv' / 'Scripts' / 'python.exe'
        shortcut = self.shortcut()
        if self._shortcut_target(shortcut, root) != generation / 'ResearchLibrary.exe':
            raise TrustError('The Start-menu target differs from the registered generation.')
        uninstallers, saved_bytes = {}, 0
        for count, path in enumerate(root.iterdir(), 1):
            if count > 4096:
                raise TrustError('The program root exceeds its activation inventory budget.')
            if _UNINSTALL.fullmatch(path.name):
                item = _binary(read_bytes(path, MAX_COMPONENT))
                saved_bytes += len(item['base64']) + 256
                if saved_bytes > MAX_STATE - 65536:
                    raise TrustError('Standard activation components exceed their aggregate byte budget.')
                uninstallers[path.name] = item
                if len(uninstallers) > MAX_COMPONENTS:
                    raise TrustError('Too many registered uninstaller components.')
        value = {'schema': PROTOCOL, 'program': str(root), 'generation': relative,
            'source_commit': source, 'manifest_sha256': sha256(raw).hexdigest(),
            'python_sha256': _file_digest(python),
            'setup_sha256': _file_digest(generation / 'maintenance-setup.exe'),
            'registration': registration, 'shortcut_path': str(shortcut),
            'shortcut': _binary(read_bytes(shortcut, 256 * 1024)), 'uninstallers': uninstallers}
        return validate_snapshot(value, root)

    def launch(self, root, staged, installer, transaction, *, cancelled):
        # The candidate's own fixed installer handshake rechecks the retained A
        # snapshot under Inno's mutex before it creates or activates B.
        command = [str(staged / installer), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                   '/DIR=' + str(root), '/CARRUPDATE=' + transaction]
        with verified_reader(staged / installer):
            code, _ = run_owned(command, cwd=staged, environment=self.environment(),
                                timeout=self.timeout, cancelled=cancelled)
        return code

    def health(self, root, snapshot, release, *, cancelled=lambda: False):
        generation = directory(root.joinpath(*snapshot['generation'].split('/')))
        if os.path.lexists(generation / 'standard-update-rejected.json'):
            return False
        self.health_adapter._verify_release(generation, release)
        file_info(generation / '.venv' / '.installation-complete')
        python = generation / '.venv' / 'Scripts' / 'python.exe'
        file_info(python)
        for arguments in (
                ['-m', 'carr_library.native_checks', '--bundle-root', str(generation)],
                ['-m', 'carr_library', '--self-test'],
                ['-c', "from importlib.metadata import version; import sys; sys.exit(version('universal-research-library') != sys.argv[1])", release.version]):
            code, _ = run_owned([python, '-I', '-B', '-X', 'utf8', *arguments],
                cwd=generation, environment=self.environment(), timeout=min(180, self.timeout), cancelled=cancelled)
            if code:
                return False
        return True

    def verify_previous(self, root, snapshot):
        generation = directory(root.joinpath(*snapshot['generation'].split('/')))
        raw = read_bytes(generation / 'bundle-manifest.json', 16 * 1024**2)
        if sha256(raw).hexdigest() != snapshot['manifest_sha256']:
            raise TrustError('The retained previous generation changed.')
        self.validate_manifest(raw, snapshot['source_commit'])
        self.verify_bundle(generation, manifest_bytes=raw)

    def _write_registration(self, previous, observed, current):
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            for name in sorted(set(previous) | set(observed)):
                value = previous.get(name)
                if value is None:
                    if name in current:
                        winreg.DeleteValue(key, name)
                else:
                    winreg.SetValueEx(key, name, 0, value['type'], value['value'])
            winreg.FlushKey(key)

    def restore(self, root, previous, observed):
        """Restore only A/B bytes; a concurrent newer C is never overwritten."""
        previous, observed = validate_snapshot(previous, root), validate_snapshot(observed, root)
        self.verify_previous(root, previous)
        shortcut = self.shortcut()
        if str(shortcut) != previous['shortcut_path'] or str(shortcut) != observed['shortcut_path']:
            raise TrustError('The Start-menu location changed during update recovery.')
        current = self._registration()
        names = set(previous['registration']) | set(observed['registration'])
        if set(current) - names:
            raise TrustError('A newer uninstaller registration prevents automatic rollback.')
        for name in names:
            if current.get(name) not in (previous['registration'].get(name), observed['registration'].get(name)):
                raise TrustError('A newer registry value prevents automatic rollback.')
        current_shortcut = _binary(read_bytes(shortcut, 256 * 1024))
        if current_shortcut not in (previous['shortcut'], observed['shortcut']):
            raise TrustError('A newer Start-menu shortcut prevents automatic rollback.')
        names = set(previous['uninstallers']) | set(observed['uninstallers'])
        actual = set()
        for count, path in enumerate(root.iterdir(), 1):
            if count > 4096:
                raise TrustError('The program root exceeds its recovery inventory budget.')
            if _UNINSTALL.fullmatch(path.name):
                actual.add(path.name)
        if actual - names:
            raise TrustError('A newer uninstaller component prevents automatic rollback.')
        for name in names:
            current_bytes = _binary(read_bytes(root / name, MAX_COMPONENT)) if (root / name).exists() else None
            if current_bytes not in (previous['uninstallers'].get(name), observed['uninstallers'].get(name)):
                raise TrustError('A changed uninstaller prevents automatic rollback.')
        # The caller retains the same native SetupMutex and shared application
        # lease. An interrupted restore can repeat only these exact A/B states.
        for name in sorted(names):
            target = root / name
            old = previous['uninstallers'].get(name)
            if old is None:
                if target.exists():
                    file_info(target)
                    target.unlink()
            else:
                atomic_write(target, _decode(old))
        atomic_write(shortcut, _decode(previous['shortcut']))
        self._write_registration(previous['registration'], observed['registration'], current)
        sync_directory(root)
        if self.snapshot(root) != previous:
            raise TrustError('The previous registered installation did not restore exactly.')
