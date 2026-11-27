"""Real installer child diagnostics; no claim of a complete native installation."""
from concurrent.futures import ThreadPoolExecutor
import errno
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
import time
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
VALID_NAME = 'installation-20261127T120000.000001Z-' + 'a' * 32 + '.log'


@pytest.fixture
def installer(tmp_path):
    source = ROOT / 'packaging' / 'install_windows.py'
    module = ModuleType('installer_diagnostics_under_test')
    module.__file__ = str(source)
    exec(compile(source.read_bytes(), str(source), 'exec'), module.__dict__)
    program = tmp_path / 'Program Ω'
    program.mkdir()
    module.__file__ = str(program / 'install_windows.py')
    return module, program


def child_script(root, source):
    path = root / 'diagnostic-child.py'
    path.write_text(source, encoding='utf-8')
    return [sys.executable, '-I', '-B', '-X', 'utf8', str(path)]


def test_real_child_failure_retains_bounded_first_and_latest_utf8_output(installer, monkeypatch):
    module, root = installer
    command = child_script(root, """
import os, sys
os.write(1, 'BEGIN_ACTUAL_CHILD Київ 🚂\\n'.encode('utf-8'))
block = ('Ж🚂' * 512 + '\\n').encode('utf-8')
for _ in range(400):
    os.write(1, block)
os.write(2, b'\\xff\\x1b[31m')
os.write(2, '\\nFINAL_ACTUAL_FAILURE: відсутня бібліотека Ω\\n'.encode('utf-8'))
sys.exit(23)
""")
    mirrored = {'bytes': 0, 'first': False, 'last': False}

    def observe(text, **kwargs):
        mirrored['bytes'] += len(text.encode('utf-8'))
        mirrored['first'] |= 'BEGIN_ACTUAL_CHILD' in text
        mirrored['last'] |= 'FINAL_ACTUAL_FAILURE' in text

    monkeypatch.setattr(module, '_console', observe)
    log = module.InstallDiagnostics(root)
    try:
        log.phase('Installing reviewed offline packages')
        with pytest.raises(subprocess.CalledProcessError) as caught:
            log.run(command, timeout=10)
        assert caught.value.returncode == 23
    finally:
        log.close()
    raw = log.path.read_bytes()
    text = raw.decode('utf-8')
    assert len(raw) <= module.MAX_LOG_BYTES
    assert 'BEGIN_ACTUAL_CHILD Київ 🚂' in text
    assert 'FINAL_ACTUAL_FAILURE: відсутня бібліотека Ω' in text
    assert '[CHILD EXIT] 23' in text and 'CalledProcessError' in text
    assert 'bytes omitted' in text and 'Installing reviewed offline packages' in text
    assert '\ufffd' in text and '\\x1b[31m' in text and '\x1b' not in text
    assert mirrored['first'] and mirrored['last'] and mirrored['bytes'] > 1_000_000
    assert module.read_install_log(log.path) == text


def test_sparse_child_output_reaches_console_and_disk_before_exit(installer, monkeypatch):
    module, root = installer
    release = root / 'continue-child'
    command = child_script(root, """
from pathlib import Path
import sys, time
release = Path(sys.argv[1])
print('SPARSE_CHILD_READY', flush=True)
deadline = time.monotonic() + 8
while not release.exists() and time.monotonic() < deadline:
    time.sleep(.02)
if not release.exists():
    raise RuntimeError('The test did not release its real child.')
print('SPARSE_CHILD_FINISHED', flush=True)
""") + [str(release)]
    seen = Event()
    monkeypatch.setattr(module, '_console',
        lambda text, **kwargs: seen.set() if 'SPARSE_CHILD_READY' in text else None)
    log = module.InstallDiagnostics(root)
    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            future = workers.submit(log.run, command, timeout=10)
            try:
                assert seen.wait(4), 'Actual child output was not streamed before exit.'
                assert not future.done()
                deadline = time.monotonic() + 2
                persisted = False
                while time.monotonic() < deadline:
                    try:
                        persisted = 'SPARSE_CHILD_READY' in module.read_install_log(log.path)
                    except ValueError:
                        pass  # A concurrent bounded rewrite is intentionally refused.
                    if persisted:
                        break
                    time.sleep(.02)
                assert persisted, 'Sparse output remained only in memory while the child was running.'
            finally:
                release.touch()
                assert future.result(12).returncode == 0
    finally:
        log.close()
    assert 'SPARSE_CHILD_FINISHED' in module.read_install_log(log.path)


def test_attempt_creation_is_exclusive_and_rejects_path_escape(installer):
    module, root = installer
    occupied = root / VALID_NAME
    occupied.write_bytes(b'Previous attempt must remain unchanged.')
    with pytest.raises(FileExistsError):
        module.InstallDiagnostics(root, name=VALID_NAME)
    assert occupied.read_bytes() == b'Previous attempt must remain unchanged.'
    with pytest.raises(ValueError):
        module.InstallDiagnostics(root, name='../installation-escape.log')
    assert not (root.parent / 'installation-escape.log').exists()


def test_unwritable_log_prevents_starting_an_installation(installer, monkeypatch):
    module, root = installer
    started = []
    original_open = module.os.open

    def denied(path, flags, *args, **kwargs):
        if Path(path).parent == root and Path(path).name.startswith('installation-'):
            raise PermissionError(errno.EACCES, 'Synthetic unwritable program folder', str(path))
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, 'open', denied)
    monkeypatch.setattr(module, 'main', lambda **kwargs: started.append(True))
    assert module.entrypoint([]) == 1
    assert started == [] and not (root / '.venv').exists()


def test_closed_console_does_not_discard_actual_child_diagnostics(installer, monkeypatch):
    module, root = installer

    class ClosedConsole:
        def write(self, text): raise ValueError('I/O operation on closed console')
        def flush(self): raise AssertionError('A failed write must not flush')

    monkeypatch.setattr(module, 'sys', SimpleNamespace(stdout=ClosedConsole(), stderr=None))
    command = child_script(root, "import os\nos.write(1, 'VISIBLE_IN_FILE Київ\\n'.encode())\n")
    log = module.InstallDiagnostics(root)
    try:
        assert log.run(command, timeout=10).returncode == 0
    finally:
        log.close()
    assert 'VISIBLE_IN_FILE Київ' in module.read_install_log(log.path)


def test_failed_attempts_reopen_safely_without_incomplete_environment(installer, monkeypatch):
    module, root = installer
    command = child_script(root, """
import os, sys
os.write(2, b'ACTUAL_INSTALL_FAILURE\\n')
os.write(2, b'untrusted $(touch do-not-create) & exit 0\\n')
sys.exit(17)
""")
    attempts = []

    def install(*, diagnostics):
        attempts.append(diagnostics.path)
        (root / '.venv').mkdir(exist_ok=True)
        diagnostics.phase('Checking an actual child dependency')
        diagnostics.run(command, timeout=10)
        (root / '.venv' / '.installation-complete').touch(exist_ok=False)
        return 0

    monkeypatch.setattr(module, 'main', install)
    assert module.entrypoint([]) == 1
    original = attempts[0].read_bytes()
    assert module.entrypoint([]) == 1
    assert len(set(attempts)) == 2 and attempts[0].read_bytes() == original
    assert not (root / '.venv' / '.installation-complete').exists()
    for path in attempts:
        text = module.read_install_log(path)
        assert 'ACTUAL_INSTALL_FAILURE' in text and '[CHILD EXIT] 17' in text
        assert '[FAILED] Checking an actual child dependency' in text
    monkeypatch.setattr(module, 'main',
        lambda **kwargs: (_ for _ in ()).throw(AssertionError('Log viewing must not run the installer.')))
    displayed = []
    monkeypatch.setattr(module, '_console', lambda text, **kwargs: displayed.append(text))
    assert module.entrypoint(['--show-install-log']) == 0
    assert str(max(attempts, key=lambda path: path.name)) in ''.join(displayed)
    assert 'ACTUAL_INSTALL_FAILURE' in ''.join(displayed)
    assert not (root / 'do-not-create').exists()


def test_actual_child_timeout_is_bounded_and_child_is_reaped(installer, monkeypatch):
    module, root = installer
    command = child_script(root, "import time\nprint('TIMEOUT_CHILD', flush=True)\ntime.sleep(30)\n")
    processes = []
    original_popen = module.subprocess.Popen

    def capture(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        processes.append(child)
        return child

    monkeypatch.setattr(module.subprocess, 'Popen', capture)
    log = module.InstallDiagnostics(root)
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            log.run(command, timeout=.5)
    finally:
        log.close()
    assert time.monotonic() - started < 12
    assert processes and processes[0].poll() is not None
    assert 'TimeoutExpired' in module.read_install_log(log.path)


@pytest.mark.parametrize('damage', ['hardlink', 'oversize', 'invalid-utf8'])
def test_log_reader_rejects_linked_oversized_or_non_utf8_state(installer, damage):
    module, root = installer
    log = module.InstallDiagnostics(root)
    log.close()
    alias = root / 'additional-link'
    try:
        if damage == 'hardlink':
            os.link(log.path, alias)
        elif damage == 'oversize':
            log.path.write_bytes(b'x' * (module.MAX_LOG_BYTES + 1))
        else:
            log.path.write_bytes(b'bad utf8 \xff')
        with pytest.raises((ValueError, UnicodeError)):
            module.read_install_log(log.path)
    finally:
        alias.unlink(missing_ok=True)


def test_log_writer_does_not_modify_newly_linked_destination(installer):
    module, root = installer
    log = module.InstallDiagnostics(root)
    before = log.path.read_bytes()
    alias = root / 'new-link'
    try:
        os.link(log.path, alias)
        with pytest.raises(ValueError, match='diagnostic file changed'):
            log.append('MUST_NOT_BE_WRITTEN')
        assert alias.read_bytes() == before and log.path.read_bytes() == before
    finally:
        alias.unlink(missing_ok=True)
        log.close()


def test_reader_rejects_path_replacement_between_inspection_and_open(installer, monkeypatch):
    module, root = installer
    log = module.InstallDiagnostics(root)
    log.close()
    retained = root / 'original-log-retained'
    original_open = module.os.open
    replaced = False

    def replace_before_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if Path(path) == log.path and not replaced:
            replaced = True
            log.path.rename(retained)
            log.path.write_bytes(b'A replacement file is not the inspected log.')
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, 'open', replace_before_open)
    with pytest.raises(ValueError, match='changed while being read'):
        module.read_install_log(log.path)
    assert retained.is_file() and b'Resonance installation diagnostics' in retained.read_bytes()


def test_keyboard_interruption_keeps_phase_and_exception_status(installer, monkeypatch):
    module, root = installer

    def interrupted(*, diagnostics):
        diagnostics.phase('Preparing the installation')
        raise KeyboardInterrupt()

    monkeypatch.setattr(module, 'main', interrupted)
    assert module.entrypoint([]) == 1
    path, = root.glob('installation-*.log')
    text = module.read_install_log(path)
    assert '[FAILED] Preparing the installation' in text and 'KeyboardInterrupt' in text
    assert not (root / '.venv' / '.installation-complete').exists()


def test_actual_child_is_reaped_when_output_thread_cannot_start(installer, monkeypatch):
    module, root = installer
    command = child_script(root, "import time\ntime.sleep(30)\n")
    original_popen, children = module.subprocess.Popen, []

    def started(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        children.append(child)
        return child

    def unavailable_thread(self):
        raise RuntimeError('Synthetic output-thread start failure')

    monkeypatch.setattr(module.subprocess, 'Popen', started)
    monkeypatch.setattr(module.threading.Thread, 'start', unavailable_thread)
    log = module.InstallDiagnostics(root)
    try:
        with pytest.raises(RuntimeError, match='output-thread start failure'):
            log.run(command, timeout=10)
        assert children and all(child.poll() is not None for child in children)
        assert children[0].stdout.closed
    finally:
        log.close()
        # A failed assertion must also leave no actual child behind.
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
    assert children and all(child.poll() is not None for child in children)
    assert children[0].stdout.closed
    assert 'Synthetic output-thread start failure' in module.read_install_log(log.path)
