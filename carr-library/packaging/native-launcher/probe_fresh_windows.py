"""CI-only real EXE first installation and repeated native window lifecycle.

Run using the staged installed application's Python, with the workflow's outbound
firewall rules already active. Never run on an owner's workstation. This probe
briefly selects an owned synthetic library through the actual preferences API;
it restores the exact prior preference bytes only while its own write is intact.
"""
import argparse
from dataclasses import dataclass
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from uuid import uuid4
from zipfile import ZipFile


def safe_path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('The acceptance paths must not contain links or reparse points.')
    return path


def digest(path):
    with safe_path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


@dataclass(frozen=True)
class Settings:
    content: bytes | None
    stamp: tuple | None


def settings_snapshot(path):
    path = safe_path(path)
    if not path.exists():
        return Settings(None, None)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 64000:
        raise ValueError('The existing desktop settings are not a bounded private regular file.')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(64001)
    after = path.stat()
    stamp = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if len(raw) > 64000 or stamp(info) != stamp(opened) or stamp(info) != stamp(after):
        raise ValueError('Desktop settings changed while their exact bytes were captured.')
    return Settings(raw, stamp(after))


def restore_settings(path, before, owned):
    """Refuse to replace any intervening write, including an equal-byte rewrite."""
    if settings_snapshot(path) != owned:
        return False
    if before.content is None:
        safe_path(path).unlink()
        return True
    temporary = path.with_name('settings-probe-' + uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(before.content); stream.flush(); os.fsync(stream.fileno())
        if settings_snapshot(path) != owned:
            return False
        os.replace(temporary, path)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def descendant(pid, ancestor, parents):
    seen = set()
    while pid and pid not in seen:
        if pid == ancestor:
            return True
        seen.add(pid)
        pid = parents.get(pid, 0)
    return False


class NativeWindows:
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w = c, w
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        self.user = c.WinDLL('user32', use_last_error=True)
        self.callback = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        self.user.EnumWindows.argtypes = [self.callback, w.LPARAM]
        self.user.EnumWindows.restype = w.BOOL
        self.user.IsWindowVisible.argtypes = [w.HWND]; self.user.IsWindowVisible.restype = w.BOOL
        self.user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, c.c_int]; self.user.GetWindowTextW.restype = c.c_int
        self.user.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
        self.user.GetWindowThreadProcessId.restype = w.DWORD
        self.user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]; self.user.PostMessageW.restype = w.BOOL
        self.kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]; self.kernel.OpenProcess.restype = w.HANDLE
        self.kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
        self.kernel.QueryFullProcessImageNameW.restype = w.BOOL
        self.kernel.CloseHandle.argtypes = [w.HANDLE]; self.kernel.CloseHandle.restype = w.BOOL
        class ProcessEntry(c.Structure):
            _fields_ = [('dwSize', w.DWORD), ('cntUsage', w.DWORD), ('th32ProcessID', w.DWORD),
                ('th32DefaultHeapID', c.c_size_t), ('th32ModuleID', w.DWORD), ('cntThreads', w.DWORD),
                ('th32ParentProcessID', w.DWORD), ('pcPriClassBase', w.LONG), ('dwFlags', w.DWORD),
                ('szExeFile', w.WCHAR * 260)]
        self.entry = ProcessEntry
        self.kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        self.kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
        for name in ('Process32FirstW', 'Process32NextW'):
            method = getattr(self.kernel, name)
            method.argtypes = [w.HANDLE, c.POINTER(ProcessEntry)]; method.restype = w.BOOL

    def parents(self):
        snapshot = self.kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
        if snapshot == self.c.c_void_p(-1).value:
            raise OSError('Cannot enumerate the owned launcher process tree.')
        try:
            entry = self.entry(); entry.dwSize = self.c.sizeof(entry)
            result = {}
            found = self.kernel.Process32FirstW(snapshot, self.c.byref(entry))
            while found:
                result[entry.th32ProcessID] = entry.th32ParentProcessID
                found = self.kernel.Process32NextW(snapshot, self.c.byref(entry))
            return result
        finally:
            self.kernel.CloseHandle(snapshot)

    def image(self, pid):
        handle = self.kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            length = self.w.DWORD(32768); value = self.c.create_unicode_buffer(length.value)
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, value, self.c.byref(length)):
                return None
            try:
                return os.path.normcase(str(Path(value.value).resolve(strict=True)))
            except OSError:
                return None
        finally:
            self.kernel.CloseHandle(handle)

    def owned_windows(self, launcher_pid, allowed_images):
        parents = self.parents()
        matches = []
        @self.callback
        def visit(hwnd, unused):
            if not self.user.IsWindowVisible(hwnd):
                return True
            title = self.c.create_unicode_buffer(512)
            self.user.GetWindowTextW(hwnd, title, len(title))
            if title.value != 'Resonance':
                return True
            pid = self.w.DWORD()
            if not self.user.GetWindowThreadProcessId(hwnd, self.c.byref(pid)):
                return True
            if descendant(pid.value, launcher_pid, parents):
                image = self.image(pid.value)
                if image in allowed_images:
                    matches.append((int(hwnd), pid.value, image))
            return True
        if not self.user.EnumWindows(visit, 0):
            raise OSError('Native window enumeration failed.')
        return matches

    def close(self, selected, launcher_pid, allowed_images):
        # Recheck both HWND ownership and actual executable immediately before
        # sending WM_CLOSE; never broadcast or close by title alone.
        if selected not in self.owned_windows(launcher_pid, allowed_images):
            raise RuntimeError('The selected native window changed before closure.')
        if not self.user.PostMessageW(selected[0], 0x10, 0, 0):
            raise OSError('Could not request normal closure of the owned application window.')


def launcher_state(launcher, expected, exit_code):
    result = subprocess.run([str(launcher), '--launcher-check'], stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=20)
    if result.returncode != exit_code or json.loads(result.stdout).get('state') != expected:
        raise RuntimeError('Unexpected native launcher state: ' + expected)


def run_gui(bundle, native, log, timeout):
    allowed = {os.path.normcase(str((bundle / relative).resolve(strict=False)))
               for relative in ('.venv/Scripts/python.exe', 'runtime/python.exe')}
    marker = bundle / '.venv' / '.installation-complete'
    started = monotonic()
    with log.open('xb') as output:
        process = subprocess.Popen([str(bundle / 'ResearchLibrary.exe')], cwd=bundle,
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            selected = None
            while monotonic() - started < timeout:
                if process.poll() is not None:
                    raise RuntimeError('The native launcher exited before a visible owned Resonance window appeared.')
                if log.stat().st_size > 16 * 1024 * 1024:
                    raise RuntimeError('Native installation exceeded its diagnostic log budget.')
                matches = native.owned_windows(process.pid, allowed)
                if len(matches) > 1:
                    raise RuntimeError('More than one owned Resonance window appeared.')
                if matches:
                    selected = matches[0]
                    break
                sleep(.2)
            if selected is None:
                raise TimeoutError('No actual owned Resonance window appeared before the installation/startup deadline.')
            ready = settings_snapshot(marker)
            if ready.content is None:
                raise RuntimeError('The native application opened without the completed installation marker.')
            launcher_state(bundle / 'ResearchLibrary.exe', 'ready', 0)
            native.close(selected, process.pid, allowed)
            if process.wait(timeout=60) != 0:
                raise RuntimeError('The actual application did not close through the launcher with exit code zero.')
            return {'visible_title': 'Resonance', 'owned_process_image': selected[2],
                    'launcher_exit': 0, 'elapsed_seconds': round(monotonic() - started, 3)}, ready
        except BaseException:
            output.flush()
            with log.open('rb') as details:
                details.seek(max(0, log.stat().st_size - 65536))
                print(details.read().decode('utf-8', 'replace'), file=sys.stderr, flush=True)
            raise
        finally:
            if process.poll() is None:
                # The Popen handle still owns this live root PID. /T is confined
                # to its descendants; unrelated Python/Resonance processes stay.
                taskkill = Path(os.environ['SystemRoot']) / 'System32' / 'taskkill.exe'
                subprocess.run([str(taskkill), '/F', '/T', '/PID', str(process.pid)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                process.wait(timeout=15)


def inspect_fresh(bundle, *, timeout=900):
    if os.name != 'nt' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('This installation probe is restricted to a native Windows GitHub Actions runner.')
    if type(timeout) is not int or not 60 <= timeout <= 1800:
        raise ValueError('Choose an installation deadline from 60 to 1800 seconds.')
    bundle = safe_path(bundle).resolve(strict=True)
    if os.path.lexists(bundle / '.venv'):
        raise RuntimeError('Fresh acceptance requires an absent .venv; nothing was installed or removed.')
    launcher = safe_path(bundle / 'ResearchLibrary.exe')
    manifest_path = safe_path(bundle / 'bundle-manifest.json')
    before_manifest, before_launcher = digest(manifest_path), digest(launcher)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not manifest.get('bundled_launcher') or manifest['files'].get('ResearchLibrary.exe') != before_launcher:
        raise ValueError('The actual native EXE is not in the selected sealed bundle.')
    evidence_path = bundle / 'application-source.json'
    if digest(evidence_path) != manifest['files'].get('application-source.json'):
        raise ValueError('The application source identity is not sealed.')
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    if version('universal-research-library') != evidence['project_version']:
        raise ValueError('Run this probe with the staged installed application of the same version.')
    import carr_library
    from carr_library.application import LibraryApp
    from carr_library.preferences import preference_file, remember_directory, selected_directory
    if 'site-packages' not in Path(carr_library.__file__).resolve().parts:
        raise ValueError('Seed through the staged installed package, not source imports.')
    wheels = [bundle / name for name, value in manifest['files'].items()
              if name.startswith('wheelhouse/universal_research_library-') and name.endswith('.whl')]
    if len(wheels) != 1 or digest(wheels[0]) != evidence['wheel_sha256']:
        raise ValueError('The sealed application wheel does not match its source evidence.')
    with ZipFile(wheels[0]) as wheel:
        package = Path(carr_library.__file__).resolve().parent
        for name in ('application.py', 'preferences.py', '__init__.py'):
            if (package / name).read_bytes() != wheel.read('carr_library/' + name):
                raise ValueError('The seed API differs from the exact application wheel.')
    launcher_state(launcher, 'needs-install', 10)
    if os.path.lexists(bundle / '.venv'):
        raise RuntimeError('The read-only launcher check unexpectedly installed an environment.')
    settings = safe_path(preference_file())
    previous = settings_snapshot(settings)
    owned, restored = None, False
    native = NativeWindows()
    with TemporaryDirectory(prefix='resonance-fresh-native-') as temporary:
        temporary = Path(temporary)
        try:
            data = temporary / 'Synthetic library Україна'
            app = LibraryApp(data)
            workspace = app.create_workspace('Synthetic native first launch')
            phrase = 'Synthetic launcher evidence'
            source = phrase + ' remains unchanged.\r\nУкраїнське джерело для перевірки.'
            imported = app.add_text(workspace, 'Native launcher source', source)
            if imported['state'] != 'complete':
                raise RuntimeError('The synthetic acceptance source did not finish importing.')
            originals = {str(path.relative_to(app.assets_dir)): digest(path)
                         for path in app.assets_dir.rglob('*') if path.is_file()}
            # Recheck immediately before the only intentional global selection.
            if settings_snapshot(settings) != previous:
                raise RuntimeError('Desktop settings changed before the CI selection; no preference was replaced.')
            remember_directory(data)
            written = settings_snapshot(settings)
            # remember_directory uses the platform's text newline translation.
            # Its exact Windows bytes end in CRLF, despite writing '\n'.
            expected = (json.dumps({'version': 1, 'active_data_dir': str(data.resolve())}, ensure_ascii=False) + os.linesep).encode('utf-8')
            if written.content != expected:
                raise RuntimeError('Desktop settings changed after selection; the newer settings were preserved.')
            owned = written
            if selected_directory() != data.resolve():
                raise RuntimeError('The actual preference API did not select the synthetic library.')
            first, marker = run_gui(bundle, native, temporary / 'first-install.log', timeout)
            second, repeat_marker = run_gui(bundle, native, temporary / 'repeat-launch.log', min(timeout, 180))
            if marker != repeat_marker:
                raise RuntimeError('Repeated native launch changed the installation marker; no reinstall was expected.')
            if digest(manifest_path) != before_manifest or digest(launcher) != before_launcher:
                raise RuntimeError('The sealed manifest or native EXE changed during installation/launch.')
            reopened = LibraryApp(data)
            hit, = reopened.search(workspace, phrase)
            if reopened.copy(workspace, hit.ref, hit.revision, 'quote') != phrase:
                raise RuntimeError('The synthetic source quotation did not survive actual application launches.')
            after_originals = {str(path.relative_to(reopened.assets_dir)): digest(path)
                               for path in reopened.assets_dir.rglob('*') if path.is_file()}
            if not originals or originals != after_originals:
                raise RuntimeError('Original source bytes changed during native application acceptance.')
        finally:
            if owned is not None:
                restored = restore_settings(settings, previous, owned)
                if not restored:
                    raise RuntimeError('Desktop settings changed outside this probe; the newer settings were preserved.')
    return {'schema': 1, 'status': 'passed', 'first_launch': first, 'repeat_launch': second,
        'manifest_sha256': before_manifest, 'launcher_sha256': before_launcher,
        'actual_fresh_installation_verified': True, 'actual_native_window_lifecycle_verified': True,
        'installation_marker_unchanged': True, 'synthetic_source_and_search_verified': True,
        'previous_settings_restored': restored, 'gui_feature_workflows_verified': False,
        'nvda_verified': False, 'authenticode_verified': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle-root', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--timeout', type=int, default=900)
    args = parser.parse_args()
    result = inspect_fresh(args.bundle_root, timeout=args.timeout)
    with args.report.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
