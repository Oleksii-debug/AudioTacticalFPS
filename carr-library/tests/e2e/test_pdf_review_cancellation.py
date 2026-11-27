"""Cancelled review actions must not publish or revoke a source attestation."""
import threading
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import worker


@pytest.fixture
def pdf_result(tmp_path):
    from reportlab.pdfgen.canvas import Canvas

    source = tmp_path / 'synthetic-review.pdf'
    canvas = Canvas(str(source))
    canvas.drawString(72, 700, 'Synthetic railway source for cancellation checks.')
    canvas.save()
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic review tests')
    result = app.import_file(workspace, source, title='Synthetic source')
    assert result['state'] == 'complete', result
    hit, = app.search(workspace, 'railway')
    assert hit.verified is False
    return app, workspace, hit


def _reviews(app, workspace):
    with app.read(workspace) as db:
        return tuple(db.execute('SELECT active FROM source_page_review WHERE workspace=?',
                                (workspace,)).fetchall())


def test_cancel_during_pdf_render_never_records_review(pdf_result, monkeypatch):
    app, workspace, hit = pdf_result
    event = threading.Event()
    original = worker.isolated_render_fingerprint

    def render_then_cancel(*args, **kwargs):
        result = original(*args, **kwargs)
        event.set()
        return result

    monkeypatch.setattr(worker, 'isolated_render_fingerprint', render_then_cancel)
    with pytest.raises(InterruptedError, match='no change was saved'):
        app.review_pdf_page(workspace, hit.ref, hit.revision, 'Synthetic reviewer',
                            confirmed=True, cancelled=event.is_set)
    assert _reviews(app, workspace) == ()
    assert app.search(workspace, 'railway')[0].verified is False


def _cancel_on_sql(app, monkeypatch, prefix, event):
    original = app.write

    @contextmanager
    def monitored_write(workspace):
        with original(workspace) as db:
            def changed(statement):
                if statement.upper().startswith(prefix):
                    event.set()
            db.set_trace_callback(changed)
            try:
                yield db
            finally:
                db.set_trace_callback(None)

    monkeypatch.setattr(app, 'write', monitored_write)


def test_cancel_inside_review_transaction_rolls_back_insert(pdf_result, monkeypatch):
    app, workspace, hit = pdf_result
    event = threading.Event()
    _cancel_on_sql(app, monkeypatch, 'INSERT INTO SOURCE_PAGE_REVIEW', event)

    with pytest.raises(InterruptedError, match='no change was saved'):
        app.review_pdf_page(workspace, hit.ref, hit.revision, 'Synthetic reviewer',
                            confirmed=True, cancelled=event.is_set)
    assert event.is_set()
    assert _reviews(app, workspace) == ()


def test_cancel_inside_revocation_transaction_preserves_prior_review(pdf_result, monkeypatch):
    app, workspace, hit = pdf_result
    app.review_pdf_page(workspace, hit.ref, hit.revision, 'Synthetic reviewer', confirmed=True)
    assert tuple(row[0] for row in _reviews(app, workspace)) == (1,)
    event = threading.Event()
    _cancel_on_sql(app, monkeypatch, 'UPDATE SOURCE_PAGE_REVIEW', event)

    with pytest.raises(InterruptedError, match='no change was saved'):
        app.revoke_pdf_review(workspace, hit.edition_id, hit.revision, 1,
                              cancelled=event.is_set)
    assert event.is_set()
    assert tuple(row[0] for row in _reviews(app, workspace)) == (1,)


def _render_barrier(monkeypatch, marker, *, hold=True):
    """Hold the actual renderer after its work, before its reply is published."""
    original = subprocess.Popen
    children = []

    def launch(command, *args, **kwargs):
        command = list(command)
        # Windows owns sys._base_executable directly, retaining the selected
        # venv through __PYVENV_LAUNCHER__. Match the trusted action, not argv[0].
        if '-c' not in command:
            return original(command, *args, **kwargs)
        index = command.index('-c') + 1
        if index >= len(command) or "runpy.run_module('carr_library.ingest.worker'" not in command[index]:
            return original(command, *args, **kwargs)
        stream = kwargs['stdin']
        position = stream.tell()
        try:
            message = json.load(stream)
        finally:
            stream.seek(position)
        if message.get('action') != 'render':
            return original(command, *args, **kwargs)
        injection = f'''
import json, os, time
from pathlib import Path
from carr_library.citations import pdf_review_attestation as _review
from carr_library.ingest import worker as _worker
_original_render = _review.render_fingerprint
def _held_render(*args, **kwargs):
    result = _original_render(*args, **kwargs)
    # Keep the real review temporary file open until the supervisor reaps us.
    with open({message['source']!r}, 'rb'):
        marker = Path({str(marker)!r})
        pending = marker.with_suffix('.pending')
        pending.write_text(json.dumps(dict(pid=os.getpid(), module=_worker.__file__)), encoding='utf-8')
        os.replace(pending, marker)
        if {hold!r}:
            time.sleep(30)
    return result
_review.render_fingerprint = _held_render
'''
        command[index] = command[index].replace('runpy.run_module(',
            'exec(' + repr(injection) + '); runpy.run_module(', 1)
        process = original(command, *args, **kwargs)
        child = dict(process=process, source=Path(message['source']))
        children.append(child)
        deadline = monotonic() + 15
        try:
            while not marker.exists():
                assert process.poll() is None, 'Actual renderer exited before its acknowledgement.'
                assert monotonic() < deadline, 'Actual renderer did not acknowledge within 15 seconds.'
                sleep(.01)
            receipt = json.loads(marker.read_bytes())
            assert receipt['pid'] == process.pid, (receipt, process.pid)
            assert Path(receipt['module']).samefile(worker.__file__)
            child['acknowledged_at'] = monotonic()
        except BaseException:
            worker._stop(process)
            raise
        return process

    monkeypatch.setattr(worker.subprocess, 'Popen', launch)
    return children


@pytest.mark.parametrize('preflight_delay', (0, .3), ids=('ordinary-preflight', 'slow-preflight'))
def test_cancel_stops_running_isolated_renderer(pdf_result, monkeypatch, tmp_path, preflight_delay):
    app, workspace, hit = pdf_result
    marker = tmp_path / 'render-acknowledged.json'
    children = _render_barrier(monkeypatch, marker)
    original = worker.isolated_render_fingerprint

    def preflight(*args, **kwargs):
        # Deliberately exceed the old .2 second cancellation clock before Popen.
        sleep(preflight_delay)
        assert not marker.exists()
        assert kwargs['cancelled']() is False
        return original(*args, **kwargs)

    monkeypatch.setattr(worker, 'isolated_render_fingerprint', preflight)
    try:
        with pytest.raises(InterruptedError, match='cancelled'):
            app.review_pdf_page(workspace, hit.ref, hit.revision, 'Synthetic reviewer',
                                confirmed=True, cancelled=marker.exists)
        child, = children
        assert child['process'].poll() is not None
        assert child['process'].wait(timeout=1) == child['process'].returncode
        assert monotonic() - child['acknowledged_at'] < 5
        assert not child['source'].parent.exists()
        assert _reviews(app, workspace) == ()
        assert app.search(workspace, 'railway')[0].verified is False
    finally:
        for child in children:
            if child['process'].poll() is None:
                worker._stop(child['process'])


def test_acknowledged_renderer_without_cancellation_still_records_real_review(pdf_result, monkeypatch, tmp_path):
    app, workspace, hit = pdf_result
    marker = tmp_path / 'completed-render.json'
    children = _render_barrier(monkeypatch, marker, hold=False)
    try:
        app.review_pdf_page(workspace, hit.ref, hit.revision, 'Synthetic reviewer',
                            confirmed=True, cancelled=lambda: False)
        child, = children
        assert marker.exists() and child['process'].wait(timeout=1) == 0
        assert not child['source'].parent.exists()
        assert tuple(row[0] for row in _reviews(app, workspace)) == (1,)
    finally:
        for child in children:
            if child['process'].poll() is None:
                worker._stop(child['process'])


def test_review_cancelled_before_launch_creates_no_renderer(pdf_result, monkeypatch, tmp_path):
    app, workspace, hit = pdf_result
    marker = tmp_path / 'must-not-launch.json'
    children = _render_barrier(monkeypatch, marker)
    with pytest.raises(InterruptedError, match='cancelled'):
        app.review_pdf_page(workspace, hit.ref, hit.revision, 'Synthetic reviewer',
                            confirmed=True, cancelled=lambda: True)
    assert children == [] and not marker.exists()
    assert _reviews(app, workspace) == ()


def test_render_barrier_does_not_intercept_other_python_children(monkeypatch, tmp_path):
    marker = tmp_path / 'not-a-renderer.json'
    children = _render_barrier(monkeypatch, marker)
    result = subprocess.run([sys.executable, '-I', '-B', '-c', "print('unrelated child')"],
                            capture_output=True, text=True, check=True, timeout=5)
    assert result.stdout.strip() == 'unrelated child'
    assert children == [] and not marker.exists()
