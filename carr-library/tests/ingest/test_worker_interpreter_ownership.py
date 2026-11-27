"""Interpreter ownership, with real worker publication separate from launch models.

The native case uses the current Python and its actual dependencies. The Windows
desktop CI runs it from its installed venv; the real child PID must belong to
Popen itself, while its venv package identity and isolated flags stay unchanged.
"""
import json
import ntpath
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import filelock
import pytest

from carr_library.application import LibraryApp
from carr_library.extract.page_ocr import Config
from carr_library.ingest import worker
from carr_library.ingest.writer_guard import exclusive_writer


@pytest.mark.parametrize(('platform', 'prefix', 'base', 'redirect'), [
    ('nt', r'C:\selected-venv', r'C:\Python311\python.exe', True),
    ('nt', r'C:\Python311', r'C:\Python311\python.exe', False),
    ('nt', r'C:\selected-venv', r'c:\SELECTED-VENV\scripts\PYTHON.EXE', False),
    ('nt', r'C:\selected-venv', None, False),
    ('posix', r'C:\selected-venv', r'C:\Python311\python.exe', False),
])
def test_launch_contract_preserves_isolation_and_only_bypasses_windows_venv(
        monkeypatch, platform, prefix, base, redirect):
    """Modeled platform boundary; this does not qualify native process behavior."""
    selected = r'C:\selected-venv\Scripts\python.exe'
    inherited = {'PYTHONEXECUTABLE': 'unselected-python.exe',
                 '__PYVENV_LAUNCHER__': 'unselected-venv.exe',
                 'PYTHONPATH': 'untrusted-module-directory', 'PATH': 'preserved'}
    before = dict(inherited)
    observed = []

    def launch(command, **options):
        observed.append((command, options))
        options['stdout'].write(b'{"result":{"state":"launch-contract-only"}}')
        options['stdout'].flush()
        return SimpleNamespace(returncode=0, poll=lambda: 0)

    monkeypatch.setattr(worker, 'os', SimpleNamespace(
        name=platform, path=ntpath, environ=inherited, fstat=os.fstat))
    monkeypatch.setattr(worker, 'sys', SimpleNamespace(
        executable=selected, prefix=prefix, base_prefix=r'C:\Python311',
        _base_executable=base))
    monkeypatch.setattr(worker, 'subprocess', SimpleNamespace(
        Popen=launch, CREATE_NO_WINDOW=0x08000000, CREATE_NEW_PROCESS_GROUP=0x200))
    assert worker._invoke({'contract': 'only'}, timeout=1)['state'] == 'launch-contract-only'
    (command, options), = observed
    assert command[0] == (base if redirect else selected)
    assert command[1:6] == ['-I', '-B', '-X', 'utf8', '-c']
    assert "runpy.run_module('carr_library.ingest.worker'" in command[6]
    package_root = Path(worker.__file__).resolve().parents[2]
    expected_roots = [str(package_root)]
    if (package_root.parent / 'bootstrap/vendor/cairn_extractors/hasher.py').is_file():
        expected_roots.append(str(package_root.parent))
    assert command[7:] == expected_roots
    environment = options['env']
    assert environment['PATH'] == before['PATH'] and environment['PYTHONPATH'] == before['PYTHONPATH']
    assert environment['PYTHONUTF8'] == '1' and environment['PYTHONIOENCODING'] == 'utf-8'
    if redirect:
        assert 'PYTHONEXECUTABLE' not in environment
        assert environment['__PYVENV_LAUNCHER__'] == selected
    else:
        assert all(environment[key] == value for key, value in before.items())
    assert inherited == before, 'A child launch must not alter the parent environment.'
    if platform == 'nt':
        assert options['creationflags'] == 0x08000200
    else:
        assert options['start_new_session'] is True


def test_actual_worker_pid_venv_and_lock_release_before_commit_recovery(tmp_path, monkeypatch):
    """Only a post-commit scheduling barrier is injected into the real child."""
    app = LibraryApp(tmp_path / 'library')
    source = tmp_path / 'Owned interpreter.txt'
    source.write_text('A real source retained before cancelled acknowledgement.', encoding='utf-8')
    workspace = app.create_workspace('Actual interpreter ownership')
    marker = tmp_path / 'committed-interpreter.json'
    untrusted = tmp_path / 'untrusted-pythonpath'
    untrusted.mkdir()
    (untrusted / 'carr_untrusted_interpreter_probe.py').write_text('UNTRUSTED = True\n')
    (tmp_path / 'carr_untrusted_interpreter_probe.py').write_text('UNTRUSTED = True\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PYTHONPATH', str(untrusted))
    windows_venv = os.name == 'nt' and sys.prefix != sys.base_prefix
    if windows_venv:
        monkeypatch.setenv('PYTHONEXECUTABLE', str(tmp_path / 'unselected-python.exe'))
        monkeypatch.setenv('__PYVENV_LAUNCHER__', str(tmp_path / 'unselected-venv/python.exe'))
    injection = f'''
import importlib.util, json, os, sys, time
from pathlib import Path
import filelock
from carr_library.application import _CatalogPublisher
from carr_library.ingest import worker as imported_worker
original_publish = _CatalogPublisher.publish_revision
def committed_with_lock(self, *args, **kwargs):
    result = original_publish(self, *args, **kwargs)
    marker = Path({str(marker)!r})
    temporary = marker.with_suffix('.pending')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(dict(pid=os.getpid(), executable=sys.executable, prefix=sys.prefix,
                       base_executable=sys._base_executable, dependency=filelock.__file__,
                       module=imported_worker.__file__, isolated=sys.flags.isolated,
                       ignore_environment=sys.flags.ignore_environment,
                       no_user_site=sys.flags.no_user_site, utf8=sys.flags.utf8_mode,
                       bytecode=sys.dont_write_bytecode,
                       untrusted=importlib.util.find_spec('carr_untrusted_interpreter_probe') is not None), stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, marker)
    time.sleep(30)
    return result
_CatalogPublisher.publish_revision = committed_with_lock
'''
    popen = subprocess.Popen
    processes = []

    def launch(command, **options):
        command = list(command)
        if '-c' not in command:
            return popen(command, **options)
        index = command.index('-c') + 1
        if index >= len(command) or "runpy.run_module('carr_library.ingest.worker'" not in command[index]:
            return popen(command, **options)
        command[index] = command[index].replace('runpy.run_module(',
            'exec(' + repr(injection) + '); runpy.run_module(', 1)
        process = popen(command, **options)
        processes.append(process)
        # Separate fixture startup from the unchanged production timeout. The
        # child publishes the complete JSON only after the real SQL commit.
        deadline = time.monotonic() + 15
        try:
            while not marker.exists():
                assert process.poll() is None, 'Actual worker exited before its commit barrier.'
                assert time.monotonic() < deadline, 'Actual worker did not reach its commit barrier.'
                time.sleep(.01)
        except BaseException:
            worker._stop(process)
            raise
        return process

    monkeypatch.setattr(worker.subprocess, 'Popen', launch)
    planned = worker.prepare_request(app.root, workspace, source,
        dict(title=None, authors=(), language=None, edition=None, volume=None,
             collections=(), work_id=None, edition_id=None), Config())
    try:
        result = app.resume_import(workspace, planned['request_id'], cancelled=marker.exists, timeout=20)
        assert marker.exists(), result
        runtime = json.loads(marker.read_text(encoding='utf-8'))
        process, = processes
        assert runtime['pid'] == process.pid, (runtime, process.pid)
        assert process.poll() is not None
        assert os.path.normcase(runtime['executable']) == os.path.normcase(sys.executable)
        assert os.path.normcase(runtime['prefix']) == os.path.normcase(sys.prefix)
        assert Path(runtime['base_executable']).resolve() == Path(sys._base_executable).resolve()
        assert Path(runtime['dependency']).resolve() == Path(filelock.__file__).resolve()
        assert Path(runtime['module']).resolve() == Path(worker.__file__).resolve()
        assert runtime['isolated'] == runtime['ignore_environment'] == runtime['no_user_site'] == runtime['utf8'] == 1
        assert runtime['bytecode'] is True and runtime['untrusted'] is False
        assert result['state'] == 'complete' and result['publication_recovered'] is True, result
        with exclusive_writer(app.root, timeout=0):
            pass
        with app.read(workspace) as db:
            receipt = db.execute('SELECT request_id,attempt FROM import_publication').fetchone()
            saved = worker.load_request(app.root, planned['request_id'])
            assert receipt[0] == planned['request_id'] and receipt[1] == saved['attempt']
            assert saved['result'] == result
            assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
    finally:
        for process in processes:
            if process.poll() is None:
                worker._stop(process)
