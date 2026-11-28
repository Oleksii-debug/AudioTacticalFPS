"""Native install/repair/uninstall acceptance for the exact compiled setup.

Execute only in an isolated Windows CI account. Actual compiled Inno and the
existing sealed offline runtime are required; no native operation is mocked.
The JSON result contains identity and outcomes, never a public binary upload.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from carr_library.audio.windows_process import WindowsJobProcess

REGISTRY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\{7421E0E1-9394-4A8D-8AF8-DAD3CF947D18}_is1'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def registry():
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
    except FileNotFoundError:
        return None
    # Missing values in an existing key are not an absent installation. Let
    # that error refuse this isolated acceptance before any program changes.
    with key:
        return {name: winreg.QueryValueEx(key, name)[0] for name in
                ('InstallLocation', 'UninstallString', 'ModifyPath', 'NoModify')}


class _BasicAccounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in
                ('user_time', 'kernel_time', 'period_user_time', 'period_kernel_time')] + [
                (name, ctypes.c_uint32) for name in
                ('page_faults', 'total_processes', 'active_processes', 'terminated_processes')]


def active_job_processes(child):
    """Query the owned handle, never enumerate or signal a process by its PID."""
    job = getattr(child, '_carr_job', None)
    if job is None or not job.handle:
        raise RuntimeError('The native acceptance process has no live owned job.')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    query = kernel.QueryInformationJobObject
    query.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                      ctypes.c_uint32, ctypes.c_void_p)
    query.restype = ctypes.c_int
    value = _BasicAccounting()
    if not query(job.handle, 1, ctypes.byref(value), ctypes.sizeof(value), None):
        raise ctypes.WinError(ctypes.get_last_error())
    return value.active_processes


def wait_owned_tree(child, *, deadline):
    # Inno 6.7.4 asks its first-phase uninstaller to exit before the second
    # phase removes its executable. Retain our existing private Job until that
    # descendant finishes; closing it immediately would abort real cleanup.
    while active_job_processes(child):
        if time.monotonic() >= deadline:
            raise TimeoutError('The owned native installation tree did not finish.')
        time.sleep(0.05)


def stop_owned_process(child):
    try:
        child._carr_job.close()
    finally:
        try:
            child.wait(timeout=5)
        finally:
            child._handle.Close()


def owned_process(command, *, cwd, log, timeout=1200, success=True):
    environment = {k: v for k, v in os.environ.items()
                   if not k.upper().startswith(('PYTHON', 'PIP_'))}
    environment.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                       PIP_NO_INDEX='1', DO_NOT_TRACK='1')
    with log.open('xb') as stream:
        child = WindowsJobProcess([str(item) for item in command], cwd=cwd,
            env=environment, stdin=subprocess.DEVNULL, stdout=stream,
            stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + timeout
            result = child.wait(timeout=timeout)
            wait_owned_tree(child, deadline=deadline)
        finally:
            stop_owned_process(child)
    if (result == 0) != success:
        raise RuntimeError(f'Native acceptance failed at {log.name}: exit {result}')
    return result


def wait_ready(path, child, seconds=30):
    deadline = time.monotonic() + seconds
    while not path.exists():
        if child.poll() is not None or time.monotonic() >= deadline:
            raise RuntimeError('The real installed application lease worker did not become ready.')
        time.sleep(0.05)


SHORTCUT_INSPECTOR = r"""param([string]$Folder,[string]$Output,[string]$Evidence)
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

[ComImport, Guid("00021401-0000-0000-C000-000000000046")]
internal class CarrShortcutObject {}

[ComImport, Guid("000214F9-0000-0000-C000-000000000046"),
 InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface CarrShortcutReadW {
    [PreserveSig]
    int GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path,
                int characters, IntPtr findData, uint flags);
}

public static class CarrShortcutInspector {
    public static string ReadTarget(string filename) {
        object link = new CarrShortcutObject();
        try {
            ((IPersistFile)link).Load(filename, 0); // STGM_READ; never Resolve or Save.
            var target = new StringBuilder(32768);
            int result = ((CarrShortcutReadW)link).GetPath(target, target.Capacity,
                                                        IntPtr.Zero, 4); // SLGP_RAWPATH
            if (result != 0 || target.Length == 0)
                throw new InvalidOperationException("IShellLinkW.GetPath did not return an exact target.");
            return target.ToString();
        } finally {
            Marshal.FinalReleaseComObject(link);
        }
    }
}
'@
$values = @{}
$observations = @{}
$shell = New-Object -ComObject WScript.Shell
try {
    foreach ($name in @('Resonance','Repair Resonance','Uninstall Resonance')) {
        $path = Join-Path $Folder ($name + '.lnk')
        $target = [CarrShortcutInspector]::ReadTarget($path)
        $legacy = $null
        $legacyError = $null
        $shortcut = $null
        try {
            $shortcut = $shell.CreateShortcut($path)
            $legacy = $shortcut.TargetPath
        } catch {
            $legacyError = $_.Exception.GetType().FullName
        } finally {
            if ($null -ne $shortcut) {
                [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
            }
        }
        $values[$name] = $target
        $observations[$name] = [ordered]@{
            unicode_target = $target
            legacy_target = $legacy
            legacy_error = $legacyError
            link_bytes = (Get-Item -LiteralPath $path).Length
            link_sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
} finally {
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
}
$values | ConvertTo-Json | Set-Content -Encoding utf8 $Output
$observations | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 $Evidence
"""


def inspect_shortcuts(root, report):
    folder = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Resonance'
    names = ('Resonance', 'Repair Resonance', 'Uninstall Resonance')
    if not all((folder / (name + '.lnk')).is_file() for name in names):
        raise RuntimeError('The per-user Start-menu shortcuts were not installed.')
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = report / 'inspect-shortcuts.ps1'
    script.write_text(SHORTCUT_INSPECTOR, encoding='utf-8')
    output = report / 'shortcuts.json'
    evidence = report / 'shortcut-inspection.json'
    log = report / 'shortcuts-process.log'
    try:
        owned_process([powershell, '-NoProfile', '-NonInteractive', '-File', script,
                       '-Folder', folder, '-Output', output, '-Evidence', evidence], cwd=report,
                      log=log, timeout=30)
    except RuntimeError as error:
        # Preserve the failed child and its owned cleanup; expose only a bounded
        # copy of the existing log so a temporary CI directory cannot hide why.
        detail = {'log': log.name, 'status': 'UNAVAILABLE'}
        try:
            with log.open('rb') as stream:
                saved = stream.read(16385)
            captured = saved[:16384]
            encoding = ('utf-16' if captured.startswith((b'\xff\xfe', b'\xfe\xff'))
                        else 'utf-8-sig')
            detail.update(status='READ', captured_bytes=len(captured),
                          truncated=len(saved) > len(captured),
                          captured_sha256=hashlib.sha256(captured).hexdigest(),
                          text=captured.decode(encoding, errors='backslashreplace'))
        except Exception as read_error:
            detail['read_error'] = type(read_error).__name__
        raise RuntimeError(f'{error}; child log: {json.dumps(detail, ensure_ascii=True)}') from error
    result = json.loads(output.read_text(encoding='utf-8-sig'))
    expected = {'Resonance': root / 'app/ResearchLibrary.exe',
                'Repair Resonance': root / 'maintenance/ResonanceSetup.exe',
                'Uninstall Resonance': root / 'unins000.exe'}
    if any(not path.is_file() or Path(result[name]).resolve() != path.resolve()
           for name, path in expected.items()):
        raise RuntimeError('A Start-menu shortcut targets another installation.')
    return folder


HOLD_APPLICATION = """
from pathlib import Path
import sys
import time
from carr_library.installation_lease import application_lease
ready, release = map(Path, sys.argv[1:])
with application_lease():
    ready.write_text('ready')
    while not release.exists():
        time.sleep(.05)
"""

CREATE_LIBRARY = """
from pathlib import Path
import sys
from carr_library.application import LibraryApp
app = LibraryApp(Path(sys.argv[1]))
workspace = app.create_workspace('Installer fixture')
result = app.add_text(workspace, 'Synthetic retained source',
                      'Original Unicode Ω Україна\\f[pause 2]')
assert result['state'] == 'complete'
"""


def probe(setup, build_evidence, work, report):
    if os.name != 'nt':
        raise RuntimeError('This gate requires native Windows; no skip substitutes for execution.')
    from carr_library.installation_lease import installation_lease
    record = json.loads(build_evidence.read_text(encoding='utf-8'))
    if (record.get('status') != 'BUILT_NOT_INSTALLED'
            or record.get('setup_sha256') != sha(setup)
            or record.get('release_status') != 'unsigned-private-candidate'):
        raise RuntimeError('Setup does not match its exact build evidence.')
    if registry() is not None:
        raise RuntimeError('This account already has a Resonance installation; it will not be modified.')
    shortcuts = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Resonance'
    if os.path.lexists(shortcuts):
        raise RuntimeError('This account has unowned Resonance shortcuts; they will not be modified.')
    work.mkdir(parents=False, exist_ok=False)
    report.mkdir(parents=False, exist_ok=False)
    root = work / 'Resonance installed Ω'
    common = ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-']
    # An actual fresh setup must not overwrite another program's same-name
    # Start-menu folder. This is our own isolated CI sentinel, not user data.
    shortcuts.mkdir(parents=True, exist_ok=False)
    sentinel = shortcuts / 'foreign-shortcut-sentinel.txt'
    sentinel.write_bytes(b'original synthetic Start-menu content')
    owned_process([setup, *common, '/DIR=' + str(root), '/LOG=' + str(report / 'foreign-shortcut.log')],
                  cwd=work, log=report / 'foreign-shortcut-process.log', timeout=90, success=False)
    if (sentinel.read_bytes() != b'original synthetic Start-menu content'
            or set(shortcuts.iterdir()) != {sentinel}
            or (root / 'app').exists() or (root / 'managed-install.ini').exists()):
        raise RuntimeError('Refused installation changed preexisting shortcut data or started bootstrap.')
    sentinel.unlink()
    shortcuts.rmdir()
    owned_process([setup, *common, '/DIR=' + str(root), '/LOG=' + str(report / 'install.log')],
                  cwd=work, log=report / 'install-process.log')
    python = root / 'app/.venv/Scripts/python.exe'
    if not python.is_file() or not (root / 'app/.venv/.installation-complete').is_file():
        raise RuntimeError('The actual offline installation did not complete.')
    registered = registry()
    if (not registered or Path(registered['InstallLocation']).resolve() != root.resolve()
            or registered['NoModify'] != 0
            or str(root / 'maintenance/ResonanceSetup.exe') not in registered['ModifyPath']):
        raise RuntimeError('Per-user uninstall/repair registration is incomplete.')
    shortcuts = inspect_shortcuts(root, report)
    launcher = root / 'app/ResearchLibrary.exe'
    owned_process([launcher, '--self-test'], cwd=work,
                  log=report / 'installed-native-self-test.log', timeout=300)
    owned_process([launcher, '--check-installation'], cwd=work,
                  log=report / 'installed-native-check.log', timeout=300)

    # Populate an actual SQLite library through the installed wheel, outside all
    # program files. This fixture contains only original synthetic text.
    library = work / 'external synthetic user library'
    owned_process([python, '-I', '-B', '-X', 'utf8', '-c', CREATE_LIBRARY, library], cwd=work,
                  log=report / 'create-library.log', timeout=60)
    before = {p.relative_to(library).as_posix(): sha(p)
              for p in library.rglob('*') if p.is_file()}
    if not before:
        raise RuntimeError('The installed application did not populate its synthetic library.')

    # The actual native launcher must reject maintenance before starting Python.
    with installation_lease(root, exclusive=True):
        owned_process([launcher, '--self-test'], cwd=work,
                      log=report / 'native-prelaunch-refused.log', timeout=15, success=False)
    ready, release = work / 'app-ready', work / 'app-release'
    child = WindowsJobProcess([str(python).upper(), '-I', '-B', '-X', 'utf8', '-c', HOLD_APPLICATION,
                            str(ready), str(release)], cwd=work,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    try:
        wait_ready(ready, child)
        owned_process([root / 'maintenance/ResonanceSetup.exe', *common,
                       '/LOG=' + str(report / 'busy-repair.log')], cwd=work,
                      log=report / 'busy-repair-process.log', timeout=90, success=False)
        owned_process([root / 'unins000.exe', *common,
                       '/LOG=' + str(report / 'busy-uninstall.log')], cwd=work,
                      log=report / 'busy-uninstall-process.log', timeout=30, success=False)
        if child.poll() is not None:
            raise RuntimeError('Maintenance stopped the running application.')
    finally:
        release.write_text('release')
        try:
            child.wait(timeout=10)
        finally:
            stop_owned_process(child)

    original_readme = (root / 'app/README.txt').read_bytes()
    changed = b'original user-modified recovery fixture\r\n'
    (root / 'app/README.txt').write_bytes(changed)
    (root / 'app/foreign-note.txt').write_bytes(b'user note must survive repair')
    owned_process([root / 'unins000.exe', *common,
                   '/LOG=' + str(report / 'modified-uninstall.log')], cwd=work,
                  log=report / 'modified-uninstall-process.log', timeout=180, success=False)
    if (root / 'app/README.txt').read_bytes() != changed:
        raise RuntimeError('Refused uninstall changed a modified file.')
    owned_process([root / 'maintenance/ResonanceSetup.exe', *common,
                   '/LOG=' + str(report / 'repair.log')], cwd=work,
                  log=report / 'repair-process.log')
    recovery = list(work.glob(root.name + '-recovery-*'))
    if (len(recovery) != 1 or (recovery[0] / 'app/README.txt').read_bytes() != changed
            or (recovery[0] / 'app/foreign-note.txt').read_bytes() != b'user note must survive repair'
            or (root / 'app/README.txt').read_bytes() != original_readme):
        raise RuntimeError('Repair did not retain changed/foreign files and rebuild the exact bundle.')
    owned_process([launcher, '--check-installation'], cwd=work,
                  log=report / 'repaired-native-check.log', timeout=300)
    owned_process([root / 'unins000.exe', *common,
                   '/LOG=' + str(report / 'uninstall.log')], cwd=work,
                  log=report / 'uninstall-process.log', timeout=300)
    if registry() is not None or (root / 'app').exists() or (root / 'maintenance').exists():
        raise RuntimeError('Normal uninstall left program files or registration.')
    if set(path.name for path in root.iterdir()) != {'managed-install.ini', 'installation.lease'}:
        raise RuntimeError('The uninstaller did not finish removing its own wrapper files.')
    if shortcuts.exists() and any(shortcuts.iterdir()):
        raise RuntimeError('Normal uninstall left its Start-menu entries.')
    after = {p.relative_to(library).as_posix(): sha(p)
             for p in library.rglob('*') if p.is_file()}
    if after != before:
        raise RuntimeError('Maintenance changed external user-library bytes.')
    if (recovery[0] / 'app/foreign-note.txt').read_bytes() != b'user note must survive repair':
        raise RuntimeError('Uninstall removed retained recovery data.')
    result = dict(schema=1, status='PASS', source_commit=record['source_commit'],
        setup_sha256=record['setup_sha256'],
        bundle_manifest_sha256=record['bundle_manifest_sha256'],
        actual_inno_install=True, actual_offline_runtime_bootstrap=True,
        per_user_start_menu=True, per_user_repair_uninstall_registration=True,
        fresh_shortcut_collision_refused=True, uninstaller_wrapper_removed=True,
        native_launcher_prelaunch_lease=True, live_job_preserved=True,
        repair_retains_modified_and_foreign_files=True, actual_repair=True,
        actual_uninstall=True, populated_library_bytes_preserved=True,
        authenticode_verified=False, nvda_verified=False,
        unprivileged_account_qualification='NOT_RUN',
        native_network_trace='NOT_RUN', release_status='unsigned-private-candidate')
    (report / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup', type=Path, required=True)
    parser.add_argument('--build-evidence', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(probe(args.setup.absolute(), args.build_evidence.absolute(),
        args.work.absolute(), args.report.absolute()), indent=2))


if __name__ == '__main__':
    main()
