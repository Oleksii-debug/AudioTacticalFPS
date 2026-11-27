"""Existing reader-menu flow; controlled picker/consent answers, no native claim."""
from collections import deque
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.integrations import liblouis
from carr_library.ui.reading_desk import ReadingActions
from carr_library.ui import braille_actions


@pytest.fixture
def action(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Reader BRF actions')
    imported = app.add_text(workspace, 'Selected original', 'Unchanged source for a separate derivative.')
    output, archive = tmp_path / 'chosen.brf', tmp_path / 'selected.zip'
    queue, reports, native, inspections, questions = deque(), [], [], [], []
    state = {'range': '1-1', 'table': 0, 'consent': True, 'after_question': lambda: None}
    panel = SimpleNamespace(workspace=workspace, ready=True, rows={imported['edition']: object()},
        _selection=lambda: (None, None, imported['edition']))
    frame = SimpleNamespace(library=app, active=(workspace, None), working=False, busy=False,
        _closing=False, cancel_event=Event(), book=SimpleNamespace(GetPage=lambda i: panel),
        report=reports.append)
    def run_job(title, work, done):
        assert not frame.working
        frame.working = True
        frame.cancel_event.clear()
        queue.append((title, work, done))
    frame.run_job = run_job
    dialogs = SimpleNamespace(text=lambda *a, **k: state['range'],
                              choose=lambda *a, **k: state['table'])
    actions = ReadingActions(frame, dialogs=dialogs)
    monkeypatch.setattr(braille_actions, '_path', lambda frame, *, archive: (
        tmp_path / 'selected.zip' if archive else output))
    def inspection(path, *, cancelled):
        assert not cancelled()
        inspections.append(path)
        return {'version': '3.39.0', 'native_execution': 'NOT_RUN'}
    monkeypatch.setattr(braille_actions, 'inspect_archive', inspection)
    def render(value, table, selected, stage, *, cancelled):
        assert not cancelled()
        native.append((value, table, selected))
        return b'ABC', {'status': 'PASS', 'synthetic': True}
    monkeypatch.setattr(liblouis, 'render_selected', render)
    # Only the explicit final review question is simulated. Existing-file
    # approval is still captured by the real export destination helper.
    wx = SimpleNamespace(YES=2, NO=8, YES_NO=10, NO_DEFAULT=128, ICON_QUESTION=2048)
    def question(message, title, style, parent):
        questions.append((message, title, style))
        state['after_question']()
        return wx.YES if state['consent'] else wx.NO
    wx.MessageBox = question
    monkeypatch.setitem(sys.modules, 'wx', wx)
    def step(*, before_done=lambda: None):
        _, work, done = queue.popleft()
        try:
            result = work()
        except Exception as error:
            frame.working = False
            reports.append(str(error))
            return error
        frame.working = False
        before_done()
        done(result)
        return result
    return SimpleNamespace(app=app, frame=frame, actions=actions, state=state, output=output,
        archive=archive, queue=queue, reports=reports, native=native, inspections=inspections,
        questions=questions, step=step, wx=wx)


def test_existing_menu_action_defers_source_inspection_and_native_export(action):
    e = action
    e.actions.export_braille()
    assert len(e.queue) == 1 and not e.native and not e.inspections
    e.step()
    assert len(e.queue) == 1 and not e.native and not e.inspections
    e.step()
    assert len(e.queue) == 1 and len(e.questions) == 1 and not e.native
    assert e.questions[0][2] & e.wx.NO_DEFAULT
    e.step()
    assert not e.queue and len(e.native) == 1 and e.output.read_bytes() == b'ABC'
    assert any(str(e.output) in message for message in e.reports)


@pytest.mark.parametrize('cancel', ['range', 'table', 'consent'])
def test_cancelled_existing_dialog_flow_never_starts_native_work(action, cancel):
    e = action
    if cancel in ('range', 'table'):
        e.state[cancel] = None
    else:
        e.state['consent'] = False
    e.actions.export_braille()
    while e.queue:
        e.step()
    assert not e.native and not e.output.exists()


def test_changed_original_library_after_review_suppresses_late_confirmation(action, tmp_path):
    e = action
    e.actions.export_braille()
    e.step()
    replacement = LibraryApp(tmp_path / 'other-library')
    e.step(before_done=lambda: setattr(e.frame, 'library', replacement))
    assert not e.queue and not e.questions and not e.native and not e.output.exists()


def test_stop_during_native_review_question_cannot_be_cleared_by_a_new_job(action):
    e = action
    e.actions.export_braille()
    e.step()
    e.state['after_question'] = e.frame.cancel_event.set
    e.step()
    assert not e.queue and not e.native and not e.output.exists()
    assert e.frame.cancel_event.is_set()


def test_destination_appearing_during_review_is_preserved(action):
    e = action
    e.actions.export_braille()
    e.step()
    e.state['after_question'] = lambda: e.output.write_bytes(b'appeared after path capture')
    e.step()
    assert len(e.queue) == 1
    e.step()
    assert not e.native and e.output.read_bytes() == b'appeared after path capture'


def test_busy_or_closing_reader_does_not_open_a_picker(action, monkeypatch):
    e = action
    monkeypatch.setattr(braille_actions, '_path',
                        lambda *a, **k: pytest.fail('A blocked action opened a picker.'))
    e.frame.working = True
    e.actions.export_braille()
    e.frame.working = False
    e.frame._closing = True
    e.actions.export_braille()
    assert not e.queue and not e.native
