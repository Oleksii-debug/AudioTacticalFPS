"""Real stored evidence behind background handlers; native controls are headless."""
import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.citations.anchors import format_citation


class Control:
    def __init__(self,value=''):
        self.owner = get_ident()
        self.value,self.items,self.selection = value,[],0
        self.focused = False

    def check(self):
        assert get_ident() == self.owner,'Native control accessed from worker thread'

    def GetValue(self):
        self.check()
        return self.value

    def SetValue(self,value):
        self.check()
        self.value = value

    def GetSelection(self):
        self.check()
        return self.selection

    def SetSelection(self,selection):
        self.check()
        self.selection = selection

    def Clear(self):
        self.check()
        self.items = []
        self.selection = -1

    def Append(self,value):
        self.check()
        self.items.append(value)

    def Enable(self,value=True):
        self.check()

    def SetFocus(self):
        self.check()
        self.focused = True


@pytest.fixture
def evidence(tmp_path):
    root = Path(__file__).resolve().parents[2] / 'src/carr_library/ui'
    env = {'wx':SimpleNamespace(Panel=object,YES=1,YES_NO=2,NO_DEFAULT=4,ICON_QUESTION=8,
                               MessageBox=Mock(return_value=1)),
           'replace':replace,'format_citation':format_citation}
    for name in ('evidence_panel.py','desktop_evidence_panel.py'):
        tree = ast.parse((root / name).read_text(encoding='utf-8'))
        nodes = [node for node in tree.body if isinstance(node,ast.ClassDef)]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),str(root / name),'exec'),env)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Background evidence')
    topic = app.create_topic(workspace,'Research')
    app.add_text(workspace,'Source','Exact original passage.')
    hit, = app.search(workspace,'Exact')
    identity = app.save_hit(workspace,topic,hit.ref,hit.revision,'Saved note')
    current = object.__new__(env['DesktopEvidencePanel'])
    current.workspace,current.topic = workspace,topic
    current.transaction = lambda: app.evidence_transaction(workspace)
    current.rows = app.evidence(workspace,topic)
    current.displayed = current.rows[0]
    for name in ('items','page_choice','quote','citation','note','status','save_button'):
        setattr(current,name,Control())
    current.note.SetValue('Saved note')
    current.quote.SetValue('Exact')
    current._desktop_ready,current._evidence_epoch = True,0
    current.frame = SimpleNamespace(working=False,_closing=False,cancel_event=Event(),
        evidence=current,active=(workspace,topic),run_job=Mock(),report=Mock())
    return current,app,identity


def execute(current):
    _,operation,complete,failed = current.frame.run_job.call_args.args
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(operation)
        try:
            result = future.result()
        except Exception as error:
            failed(error)
            return False
    complete(result)
    return True


def test_refresh_and_selection_resolve_sources_outside_the_ui_thread(evidence):
    current,app,_ = evidence
    current.refresh()
    assert current.quote.value == 'Exact'
    assert execute(current)
    assert current.note.value == 'Saved note'
    assert current.rows == app.evidence(current.workspace,current.topic)
    current.select()
    assert current.frame.run_job.call_count == 2
    assert execute(current)


def test_queued_initial_load_does_not_touch_a_closed_frame(evidence):
    current,_,_ = evidence
    current.frame._closing = True
    current.refresh = Mock()
    current._initial_refresh()
    current.refresh.assert_not_called()


def test_note_commits_in_worker_and_updates_ui_only_after_commit(evidence):
    current,app,_ = evidence
    current.note.SetValue('Draft to save')
    current.save_note()
    assert app.evidence(current.workspace,current.topic)[0].note == 'Saved note'
    assert execute(current)
    assert app.evidence(current.workspace,current.topic)[0].note == 'Draft to save'
    assert current.displayed.note == 'Draft to save' and not current._dirty()
    assert current.note.focused


def test_commit_failure_preserves_draft_and_allows_retry(evidence):
    current,app,_ = evidence
    transaction = current.transaction

    @contextmanager
    def rejected():
        with transaction() as service:
            yield service
            raise RuntimeError('Commit rejected')

    current.transaction = rejected
    current.note.SetValue('Keep this draft')
    current.save_note()
    assert not execute(current)
    assert current.note.value == 'Keep this draft' and current.displayed.note == 'Saved note'
    assert current.quote.value == '' and 'retained' in current.status.value
    assert app.evidence(current.workspace,current.topic)[0].note == 'Saved note'
    current.transaction = transaction
    current.save_note()
    assert execute(current)
    assert app.evidence(current.workspace,current.topic)[0].note == 'Keep this draft'


@pytest.mark.parametrize('change',['scope','epoch','closed'])
def test_late_refresh_never_replaces_closed_or_changed_view(evidence,change):
    current,_,_ = evidence
    current.refresh()
    _,operation,complete,_ = current.frame.run_job.call_args.args
    with ThreadPoolExecutor(max_workers=1) as pool:
        snapshot = pool.submit(operation).result()
    if change == 'scope':
        current.frame.active = ('other','topic')
    elif change == 'epoch':
        current._evidence_epoch += 1
    else:
        current.frame._closing = True
    before = current.rows,current.displayed,current.note.value,current.status.value
    complete(snapshot)
    assert (current.rows,current.displayed,current.note.value,current.status.value) == before


def test_cancelled_refresh_keeps_the_existing_draft(evidence):
    current,_,_ = evidence
    current.note.SetValue('Draft')
    current.refresh(discard=True)
    current.frame.cancel_event.set()
    assert not execute(current)
    assert current.note.value == 'Draft' and 'retained' in current.status.value


def test_cancelled_selection_restores_draft_row_for_immediate_save_retry(evidence):
    current,app,_ = evidence
    hit, = app.search(current.workspace,'Exact')
    other_id = app.save_hit(current.workspace,current.topic,hit.ref,hit.revision,'Other saved note')
    other = next(row for row in app.evidence(current.workspace,current.topic) if row.evidence_id == other_id)
    current.rows = (current.displayed,other)
    current.items.SetSelection(1)
    current.note.SetValue('Keep and retry this draft')
    current.select()
    current.frame.cancel_event.set()
    assert not execute(current)
    assert current.items.GetSelection() == 0 and current._selected() == current.displayed
    assert current.note.value == 'Keep and retry this draft'
    current.frame.cancel_event.clear()
    current.save_note()
    assert execute(current)
    saved = next(row for row in app.evidence(current.workspace,current.topic)
                 if row.evidence_id == current.displayed.evidence_id)
    assert saved.note == 'Keep and retry this draft'
    assert next(row for row in app.evidence(current.workspace,current.topic)
                if row.evidence_id == other_id).note == 'Other saved note'


def test_newer_draft_is_not_replaced_by_an_earlier_refresh(evidence):
    current,_,_ = evidence
    current.refresh()
    _,operation,complete,_ = current.frame.run_job.call_args.args
    with ThreadPoolExecutor(max_workers=1) as pool:
        snapshot = pool.submit(operation).result()
    current.note.SetValue('A newer draft')
    complete(snapshot)
    assert current.note.value == 'A newer draft'
    assert 'newer unsaved note' in current.status.value
