"""Cancelled review actions must not publish or revoke a source attestation."""
import threading
from contextlib import contextmanager
import subprocess
import sys
from time import monotonic

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


def test_cancel_stops_running_isolated_renderer(pdf_result, monkeypatch):
    app, workspace, hit = pdf_result
    original = subprocess.Popen
    children = []

    def delayed_renderer(command, *args, **kwargs):
        # subprocess.run also calls this shared Popen on Windows for taskkill.
        # Replace only the isolated renderer, so its real process-tree cleanup
        # remains exercised and is not replaced by a second sleeping child.
        if not (isinstance(command, (list, tuple)) and command[0] == sys.executable
                and '-c' in command
                and "runpy.run_module('carr_library.ingest.worker'" in command[command.index('-c') + 1]):
            return original(command, *args, **kwargs)
        child = original([sys.executable, '-I', '-c', 'import time; time.sleep(30)'],
                         *args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(worker.subprocess, 'Popen', delayed_renderer)
    started = monotonic()
    try:
        with pytest.raises(InterruptedError, match='cancelled'):
            app.review_pdf_page(workspace, hit.ref, hit.revision, 'Synthetic reviewer',
                                confirmed=True, cancelled=lambda: monotonic() - started > 0.2)
        assert len(children) == 1 and children[0].poll() is not None
        assert monotonic() - started < 5
        assert _reviews(app, workspace) == ()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

