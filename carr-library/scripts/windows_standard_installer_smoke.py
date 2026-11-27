"""Native install/repair/uninstall acceptance for the exact compiled setup.

Execute only in an isolated Windows CI account. Actual compiled Inno and the
existing sealed offline runtime are required; no native operation is mocked.
The JSON result contains identity and outcomes, never a public binary upload.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time

from carr_library.audio.windows_job import JobProcess
from carr_library.audio.cleanup import pending_cleanup
from carr_library.audio.models import AudioCleanupPending

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


def active_job_processes(child):
    """Use the exact shared owner's live handle, never PID enumeration."""
    if not getattr(child, '_job', None):
        raise RuntimeError('The native acceptance process has no live owned job.')
    return child._active_processes()


def wait_owned_tree(child, *, deadline):
    # Inno 6.7.4 asks its first-phase uninstaller to exit before the second
    # phase removes its executable. Retain our existing private Job until that
    # descendant finishes; closing it immediately would abort real cleanup.
    while active_job_processes(child):
        if time.monotonic() >= deadline:
            raise TimeoutError('The owned native installation tree did not finish.')
        time.sleep(0.05)


def wait_cleanup_owner(owner):
    """Retain stream and native ownership until a failed shutdown is confirmed."""
    try:
        print('Waiting for owned native acceptance processes to stop.', flush=True)
    except (OSError, UnicodeError):
        # A closed console must not release ownership of a live process tree.
        pass
    interrupted = None
    while not owner.quiescent_event.is_set():
        try:
            owner.close()
            owner.quiescent_event.wait(.25)
        except (KeyboardInterrupt, SystemExit) as error:
            if interrupted is None:
                interrupted = error
    if interrupted is not None:
        raise interrupted


def stop_owned_process(child):
    try:
        if child.close() is False:
            raise RuntimeError('Native process cleanup is not confirmed.')
    except (Exception, KeyboardInterrupt, SystemExit) as error:
        pending = pending_cleanup(child.close,
            message='Native installation acceptance cleanup is still pending.')
        wait_cleanup_owner(pending.cleanup_owner)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise


def _environment():
    environment = {k: v for k, v in os.environ.items()
                   if not k.upper().startswith(('PYTHON', 'PIP_'))
                   and k.upper() != '__PYVENV_LAUNCHER__'}
    environment.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                       PIP_NO_INDEX='1', DO_NOT_TRACK='1')
    return environment


def owned_process(command, *, cwd, log, timeout=1200, success=True):
    environment = _environment()
    with log.open('xb') as stream:
        try:
            child = JobProcess([str(item) for item in command], cwd=cwd,
                env=environment, stdin_pipe=False)
        except AudioCleanupPending as error:
            wait_cleanup_owner(error.cleanup_owner)
            raise
        # The canonical JobProcess exposes owned PIPE streams. These probe-only
        # readers drain them while preserving the existing whole-job completion
        # barrier, including Inno's second-phase uninstaller.
        lock, errors, threads = threading.Lock(), [], []
        logged = [0]
        maximum = 16 * 1024**2

        def drain(pipe):
            try:
                while True:
                    data = pipe.read(65536)
                    if not data:
                        return
                    with lock:
                        remaining = max(0, maximum - logged[0])
                        if len(data) > remaining and not errors:
                            errors.append('Native acceptance exceeded its diagnostic budget.')
                        if remaining:
                            stream.write(data[:remaining])
                            logged[0] += min(len(data), remaining)
            except Exception:
                with lock:
                    errors.append('Native acceptance diagnostic capture failed.')
            finally:
                pipe.close()

        try:
            for pipe in (child.stdout, child.stderr):
                worker = threading.Thread(target=drain, args=(pipe,), daemon=True)
                threads.append(worker)
                worker.start()
            deadline = time.monotonic() + timeout
            result = child.wait(timeout=timeout)
            wait_owned_tree(child, deadline=deadline)
        finally:
            try:
                stop_owned_process(child)
            finally:
                for worker in threads:
                    if worker.ident is not None:
                        worker.join(timeout=10)
                for index, pipe in enumerate((child.stdout, child.stderr)):
                    if index >= len(threads) or threads[index].ident is None:
                        pipe.close()
                if any(worker.is_alive() for worker in threads):
                    raise RuntimeError('Owned native diagnostics did not settle after process cleanup.')
        if errors:
            raise RuntimeError(errors[0])
    if (result == 0) != success:
        raise RuntimeError(f'Native acceptance failed at {log.name}: exit {result}')
    return result


def wait_ready(path, child, seconds=30):
    deadline = time.monotonic() + seconds
    while not path.exists():
        if child.poll() is not None or time.monotonic() >= deadline:
            raise RuntimeError('The real installed application lease worker did not become ready.')
        time.sleep(0.05)


def inspect_shortcuts(root, report):
    folder = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Resonance'
    names = ('Resonance', 'Repair Resonance', 'Uninstall Resonance')
    if not all((folder / (name + '.lnk')).is_file() for name in names):
        raise RuntimeError('The per-user Start-menu shortcuts were not installed.')
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = report / 'inspect-shortcuts.ps1'
    script.write_text(
        "param([string]$Folder,[string]$Output)\n$ErrorActionPreference='Stop'\n"
        "$shell=New-Object -ComObject WScript.Shell\n$values=@{}\n"
        "foreach($name in @('Resonance','Repair Resonance','Uninstall Resonance')) { "
        "$values[$name]=$shell.CreateShortcut((Join-Path $Folder ($name+'.lnk'))).TargetPath }\n"
        "$values | ConvertTo-Json | Set-Content -Encoding utf8 $Output\n", encoding='utf-8')
    output = report / 'shortcuts.json'
    owned_process([powershell, '-NoProfile', '-NonInteractive', '-File', script,
                   '-Folder', folder, '-Output', output], cwd=report,
                  log=report / 'shortcuts-process.log', timeout=30)
    result = json.loads(output.read_text(encoding='utf-8-sig'))
    expected = {'Resonance': root / 'app/ResearchLibrary.exe',
                'Repair Resonance': root / 'maintenance/ResonanceSetup.exe',
                'Uninstall Resonance': root / 'unins000.exe'}
    if any(Path(result[name]).resolve() != path.resolve() for name, path in expected.items()):
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
    try:
        child = JobProcess([str(python).upper(), '-I', '-B', '-X', 'utf8', '-c', HOLD_APPLICATION,
                           str(ready), str(release)], cwd=work,
                          env=_environment(), stdin_pipe=False)
    except AudioCleanupPending as error:
        wait_cleanup_owner(error.cleanup_owner)
        raise
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
            try:
                stop_owned_process(child)
            finally:
                child.stdout.close()
                child.stderr.close()

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
