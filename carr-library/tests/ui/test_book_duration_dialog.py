"""Actual worker/source publication through named widget stand-ins; not native evidence."""
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from carr_library.studio.book_audio import SplitOptions, capture_plan
from carr_library.studio.project import Role
from carr_library.studio import measured_audio
from carr_library.ui import book_duration_dialog as ui

_spec = importlib.util.spec_from_file_location('measured_ui_handlers', Path(__file__).with_name('test_book_audio_dialog.py'))
_handlers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_handlers)
handlers = _handlers.handlers
_spec = importlib.util.spec_from_file_location('measured_ui_tone_fixture',
    Path(__file__).parents[1] / 'studio' / 'test_book_audio.py')
_tones = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tones)


@pytest.fixture
def measured(handlers):
    e = handlers
    voices = _tones.ToneVoices()
    e.production._voices = voices
    plan = capture_plan(e.app, e.workspace, e.imported['edition'], options=SplitOptions('document'),
                        role=Role('narrator', 'Narrator', 'synthetic-tone', 'en'))
    preliminary = e.service.review(plan, e.output)
    ready = e.service.prepare(plan, e.output, approval=preliminary.fingerprint)
    assert e.service.run(plan.id, e.output, approval=ready.fingerprint)[0]['status'] == 'complete'
    dialog, close = ui.create_book_duration_dialog(e.frame, service=e.service, plan=plan, wx_module=e.wx)
    output = e.output.parent / 'native-handler-parts.zip'
    e.find('Maximum measured part milliseconds').ChangeValue('70')
    e.find('Reviewed measured-parts ZIP').ChangeValue(str(output))
    yield SimpleNamespace(e=e, voices=voices, plan=plan, dialog=dialog, close=close, output=output)
    close()
    assert voices.closed == 0


def preview(m):
    m.e.press('Preview measured parts')
    assert m.e.state['queue'] and not m.output.exists()
    result = m.e.complete()
    assert not isinstance(result, Exception), str(result)
    return result


def test_actual_worker_preview_publish_and_borrowed_owner_survive_child_close(measured, monkeypatch):
    m, e = measured, measured.e
    original, calls = e.service.review_duration, []
    def review(*args, **kwargs):
        calls.append(threading.get_ident())
        assert calls[-1] != e.ui_thread
        return original(*args, **kwargs)
    monkeypatch.setattr(e.service, 'review_duration', review)
    before = tuple(m.voices.received)
    candidate = preview(m)
    assert calls and e.find('Publish reviewed parts').enabled
    text = e.find('Measured audio preview and publication status').value
    assert '4800 frames' in text and 'whole-line' in text and candidate['approval_sha256'] in text
    assert not m.output.exists() and tuple(m.voices.received) == before
    e.press('Publish reviewed parts')
    assert len(e.state['approvals']) == 1 and 'starts no speech' in e.state['approvals'][0]
    e.complete()
    assert m.output.exists() and measured_audio.inspect(m.output)['content'] == candidate['content']
    assert tuple(m.voices.received) == before
    assert 'Publication receipt:' in e.find('Measured audio preview and publication status').value
    m.close()
    assert not e.service._closed.is_set() and not e.production._closed.is_set()


@pytest.mark.parametrize('field,value', [
    ('Maximum measured part milliseconds', '40'),
    ('Measured part name template: index, book, part', '{index:04d} - Changed'),
    ('Reviewed measured-parts ZIP', 'unreviewed.zip'),
])
def test_changed_fields_cannot_publish_a_previous_measured_review(measured, field, value):
    m, e = measured, measured.e
    preview(m)
    e.find(field).ChangeValue(value)
    e.press('Publish reviewed parts')
    assert not e.state['queue'] and not e.state['approvals'] and not m.output.exists()
    assert 'Preview the exact' in e.find('Measured audio preview and publication status').value


def test_queue_uses_frozen_controls_and_refuses_late_widget_change(measured):
    m, e = measured, measured.e
    e.press('Preview measured parts')
    e.find('Maximum measured part milliseconds').ChangeValue('40')
    e.complete()
    assert not e.find('Publish reviewed parts').enabled
    assert 'changed' in e.find('Measured audio preview and publication status').value
    assert not m.output.exists()


def test_declining_publication_confirmation_starts_no_worker(measured):
    m, e = measured, measured.e
    preview(m)
    e.wx.MessageBox = lambda *args: e.wx.ID_CANCEL
    e.press('Publish reviewed parts')
    assert not e.state['queue'] and not m.output.exists()


def test_active_scope_change_refuses_publication_before_confirmation(measured):
    m, e = measured, measured.e
    preview(m)
    e.frame.active = ('another-library', None)
    e.press('Publish reviewed parts')
    assert not e.state['queue'] and not e.state['approvals'] and not m.output.exists()
    assert 'no longer active' in e.find('Measured audio preview and publication status').value


def test_child_close_stops_actual_partition_and_never_closes_parent_service(measured, monkeypatch):
    m, e = measured, measured.e
    preview(m)
    entered, release = threading.Event(), threading.Event()
    original = measured_audio.package
    def package(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(measured_audio, 'package', package)
    e.press('Publish reviewed parts')
    _, operation, done, failed = e.state['queue'].pop(0)
    result = []
    def work():
        try:
            result.append(operation())
        except Exception as exc:
            result.append(exc)
    thread = threading.Thread(target=work)
    thread.start()
    try:
        assert entered.wait(10)
        m.close()
    finally:
        release.set()
        thread.join(10)
        e.frame.working = False
    assert not thread.is_alive() and len(result) == 1 and isinstance(result[0], Exception)
    assert not m.output.exists() and not list(m.output.parent.glob('.pending-measured*'))
    assert not e.service._closed.is_set() and not e.production._closed.is_set()
    assert m.voices.closed == 0
