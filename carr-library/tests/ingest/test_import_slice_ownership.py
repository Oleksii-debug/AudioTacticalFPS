"""Actual current-worker ownership and frozen-row reconciliation regressions."""
import ctypes
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

from filelock import FileLock, Timeout
import pytest

from carr_library.application import LibraryApp
from carr_library.audio import process
from carr_library.audio.models import AudioCleanupPending
from carr_library.ingest import worker, slices
from carr_library.ingest.resource_budget import ImportBudget, MIB


def _budget(**changes):
    return replace(ImportBudget(source_bytes=MIB, segment_bytes=MIB, segments=10,
        steps=11, seconds=30, memory_bytes=512*MIB, library_bytes=32*MIB,
        temporary_bytes=128*MIB), **changes)


def _plan(tmp_path, *, count=3, steps=11):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Exact captured slice')
    paths = []
    for number in range(count):
        path = tmp_path / ('source-%d.txt' % number)
        path.write_text('Source %d first.\fSource %d second.\fSource %d third.' %
                        (number, number, number), encoding='utf-8')
        paths.append(path)
    plan = app.plan_import_page(workspace, paths, import_budget=_budget(steps=steps))
    return app, workspace, plan, paths


def _instrument(monkeypatch, code):
    """Add a bounded observation/hook, then call the unchanged actual runner."""
    original = process.run_process
    calls = []
    module = Path(worker.__file__).resolve()
    expected = sha256(module.read_bytes()).hexdigest()
    proof = ("from pathlib import Path; from hashlib import sha256; "
             "import carr_library.ingest.worker as actual_worker; "
             "assert Path(actual_worker.__file__).resolve() == Path(" + repr(str(module)) + "); "
             "assert sha256(Path(actual_worker.__file__).read_bytes()).hexdigest() == " + repr(expected) + "; ")
    anchor = "runpy.run_module('carr_library.ingest.worker',run_name='__main__')"

    def run(args, **options):
        assert options['owned_tree'] is True and '-I' in args and '-B' in args
        assert options['watch'] and options['budget'].timeout <= 30
        args = list(args)
        index = args.index('-c') + 1
        assert args[index].count(anchor) == 1
        calls.append(dict(root=Path(options['watch'][0]), command=tuple(args)))
        args[index] = args[index].replace(anchor, proof + 'exec(' + repr(code) + '); ' + anchor)
        return original(args, **options)

    monkeypatch.setattr(process, 'run_process', run)
    return calls


def _alive(pid):
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # No such process.
                return False
            raise ctypes.WinError(error)
        try:
            result = kernel.WaitForSingleObject(handle, 0)
            if result not in (0, 258):
                raise ctypes.WinError(ctypes.get_last_error())
            return result == 258
        finally:
            assert kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path('/proc') / str(pid) / 'stat'
    if status.is_file():
        try:
            text = status.read_text()
        except FileNotFoundError:
            return False
        return text.rsplit(')', 1)[1].split()[0] not in ('Z', 'X')
    return True



def test_completed_real_leader_cannot_leave_a_source_holding_descendant(tmp_path, monkeypatch):
    app, workspace, plan, originals = _plan(tmp_path, count=1)
    marker = tmp_path / 'actual-child.json'
    late = tmp_path / 'must-not-publish-after-leader.txt'
    child = r"""
import json,os,pathlib,sys,time
held=pathlib.Path(sys.argv[1])
marker=pathlib.Path(sys.argv[2])
with held.open('rb') as source:
    assert source.read()==b'private native source'
    stage=marker.with_suffix('.new')
    with stage.open('w',encoding='utf-8') as output:
        json.dump({'pid':os.getpid(),'held':str(held)},output)
        output.flush();os.fsync(output.fileno())
    os.replace(stage,marker)
    time.sleep(12)
    pathlib.Path(sys.argv[3]).write_text('late native publication',encoding='utf-8')
"""
    hook = r"""
import os,pathlib,subprocess,sys,time
from carr_library.ingest import slices as selected_slices
actual=selected_slices.run_child
def observed(message):
    result=actual(message)
    held=pathlib.Path(os.environ['TMPDIR'])/'held-native-source.txt'
    held.write_bytes(b'private native source')
    environment=dict(os.environ)
    environment.pop('__PYVENV_LAUNCHER__',None)
    executable=getattr(sys,'_base_executable',None) or sys.executable
    descendant=subprocess.Popen([executable,'-I','-B','-c',CHILD,str(held),MARKER,LATE],
        env=environment,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    deadline=time.monotonic()+6
    while not pathlib.Path(MARKER).exists():
        assert descendant.poll() is None
        assert time.monotonic()<deadline
        time.sleep(.01)
    return result
selected_slices.run_child=observed
"""
    hook = hook.replace('CHILD', repr(child)).replace('MARKER', repr(str(marker))).replace('LATE', repr(str(late)))
    calls = _instrument(monkeypatch, hook)
    final = app.run_import_batch(workspace, plan['batch_id'])
    assert final['state'] == 'complete' and final['counts']['complete'] == 1, final
    assert len(calls) == 1
    actual = json.loads(marker.read_text(encoding='utf-8'))
    assert not _alive(actual['pid']), 'The actual owned native descendant is still alive.'
    assert not Path(actual['held']).exists() and not calls[0]['root'].exists()
    assert not late.exists()
    assert app.assets.resolve(plan['rows'][0]['manifest']['sha256']).read_bytes() == originals[0].read_bytes()


def test_cancel_after_first_actual_publication_recovers_only_its_attempt(tmp_path, monkeypatch):
    app, workspace, plan, originals = _plan(tmp_path, count=3)
    marker = tmp_path / 'first-committed'
    hook = r"""
import os,pathlib,time
from carr_library.application import LibraryApp
actual=LibraryApp._import_file
calls=0
def held(self,*args,**kwargs):
    global calls
    result=actual(self,*args,**kwargs)
    calls+=1
    if calls==1:
        assert result['state']=='complete'
        marker=pathlib.Path(MARKER);stage=marker.with_suffix('.new')
        stage.write_bytes(b'committed');os.replace(stage,marker)
        time.sleep(30)
    return result
LibraryApp._import_file=held
""".replace('MARKER', repr(str(marker)))
    calls = _instrument(monkeypatch, hook)
    partial = app.run_import_batch(workspace, plan['batch_id'], cancelled=marker.exists)
    assert marker.read_bytes() == b'committed' and len(calls) == 1
    assert partial['counts']['complete'] == 1 and partial['counts']['queued'] == 2, partial
    complete = partial['rows'][0]
    assert complete['result']['publication_recovered'] is True
    with app.read(workspace) as db:
        receipts = db.execute('SELECT request_id,attempt FROM import_publication').fetchall()
        assert len(receipts) == 1 and receipts[0]['request_id'] == complete['request_id']
    original = originals[0].read_bytes()
    originals[0].unlink()
    monkeypatch.undo()
    final = LibraryApp(app.root).run_import_batch(workspace, plan['batch_id'])
    assert final['counts']['complete'] == 3 and final['state'] == 'complete', final
    assert app.assets.resolve(complete['manifest']['sha256']).read_bytes() == original
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 3


def test_changed_middle_frozen_row_has_no_source_or_publication_side_effect(tmp_path, monkeypatch):
    app, workspace, plan, originals = _plan(tmp_path)
    middle = plan['rows'][1]['request_id']
    hook = r"""
from carr_library.application import LibraryApp
from carr_library.ingest import worker as selected_worker
actual=LibraryApp._import_file
calls=0
def change_after_first(self,*args,**kwargs):
    global calls
    result=actual(self,*args,**kwargs)
    calls+=1
    if calls==1:
        record=selected_worker.load_request(self.root,MIDDLE)
        record['metadata']['title']='Changed after aggregate capture'
        selected_worker._store(self.root,MIDDLE,record)
    return result
LibraryApp._import_file=change_after_first
""".replace('MIDDLE', repr(middle))
    calls = _instrument(monkeypatch, hook)
    result = app.run_import_batch(workspace, plan['batch_id'])
    assert len(calls) == 1 and result['counts']['complete'] == 1
    assert result['rows'][1]['state'] == 'failed'
    assert result['rows'][2]['state'] == 'queued'
    assert worker.load_request(app.root, middle)['metadata']['title'] == 'Changed after aggregate capture'
    for row in plan['rows'][1:]:
        assert not (app.assets_dir / (row['manifest']['sha256'] + '.txt')).exists()
    with app.read(workspace) as db:
        rows = db.execute('SELECT request_id FROM import_publication').fetchall()
        assert [row[0] for row in rows] == [plan['rows'][0]['request_id']]
    assert all(path.exists() for path in originals)


def test_same_attempt_result_rewrite_after_child_acknowledgement_is_refused(tmp_path, monkeypatch):
    app, workspace, plan, _ = _plan(tmp_path, count=1, steps=1)
    real_digest = slices._ordered_digest
    mutation = []

    def digest(values):
        result = real_digest(values)
        if values and type(values[0]) is dict and 'captured' in values[0] and not mutation:
            identity = values[0]['captured']['request_id']
            current = worker.load_request(app.root, identity)
            assert current['result']['state'] == 'pending'
            forged = dict(current['result'], state='complete', verified=True)
            worker.record_progress(app.root, identity, state='complete', result=forged)
            mutation.append(identity)
        return result

    monkeypatch.setattr(slices, '_ordered_digest', digest)
    result = app.run_import_batch(workspace, plan['batch_id'])
    assert mutation == [plan['rows'][0]['request_id']]
    assert result['counts']['complete'] == 0 and result['rows'][0]['state'] == 'failed'
    assert 'acknowledged import row changed' in result['rows'][0]['message']
    with app.read(workspace) as db:
        assert db.execute('SELECT 1 FROM revision').fetchone() is None
        assert db.execute('SELECT 1 FROM import_publication').fetchone() is None


def test_pending_native_ack_retains_batch_admission_and_private_input_until_same_owner_closes(
        tmp_path, monkeypatch):
    app, workspace, plan, _ = _plan(tmp_path, count=1)
    allowed = threading.Event()
    real_run = process.run_process
    private = []

    def observe(args, **options):
        private.append(Path(options['watch'][0]))
        return real_run(args, **options)
    monkeypatch.setattr(process, 'run_process', observe)

    if os.name == 'nt':
        from carr_library.audio.windows_job import JobProcess
        actual_close = JobProcess.close
        def close(owner):
            result = actual_close(owner)
            if not allowed.is_set():
                raise OSError('Fixture: native closure acknowledged only on retry.')
            return result
        monkeypatch.setattr(JobProcess, 'close', close)
    else:
        actual_wait = process._wait_posix_group
        def wait(*args, **kwargs):
            result = actual_wait(*args, **kwargs)
            if not allowed.is_set():
                raise OSError('Fixture: native closure acknowledged only on retry.')
            return result
        monkeypatch.setattr(process, '_wait_posix_group', wait)

    pending = None
    try:
        with pytest.raises(AudioCleanupPending) as caught:
            app.run_import_batch(workspace, plan['batch_id'])
        pending = caught.value.cleanup_owner
        assert pending.quiescent is False
        assert len(private) == 1 and (private[0] / 'request.json').is_file()
        with pytest.raises(Timeout):
            with FileLock(app.root / 'import-batches' / (plan['batch_id'] + '.lock'), timeout=0):
                pass
        row = plan['rows'][0]
        request_path = app.root / 'import-requests' / (row['request_id'] + '.json')
        before = request_path.read_bytes()
        saved = worker.load_request(app.root, row['request_id'])
        assert saved['state'] == 'running' and saved['result']['state'] == 'complete'
        competing = app.resume_import(workspace, row['request_id'], timeout=.1)
        assert competing['state'] == 'timed_out', competing
        assert request_path.read_bytes() == before
        assert not pending.quiescent and private[0].exists()
    finally:
        allowed.set()
        if pending is not None:
            assert pending.quiescent_event.wait(20), 'The same native cleanup owner did not close.'
    assert pending is not None and pending.quiescent
    assert not private[0].exists()
    with FileLock(app.root / 'import-batches' / (plan['batch_id'] + '.lock'), timeout=0):
        pass
    monkeypatch.undo()
    final = LibraryApp(app.root).run_import_batch(workspace, plan['batch_id'])
    assert final['state'] == 'complete' and final['counts']['complete'] == 1, final
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 1
