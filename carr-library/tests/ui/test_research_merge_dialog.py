"""Real common4 work on one executor; controlled native answers are disclosed."""
import ast
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import inspect
import json
from pathlib import Path
from queue import Queue
import threading
from types import SimpleNamespace as N

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, INCOMPLETE
from carr_library import research_merge
from carr_library.ui import research_merge_dialog as ui


class Host:
    def __init__(self, library):
        self.library = library
        self.main_thread = threading.get_ident()
        self.working = self._closing = False
        self.alive = True
        self.cancel_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.pending = Queue()
        self.futures, self.calls, self.messages, self.callbacks = [], [], [], []
        self.duplicate = False
        self.admission_error = None
        self.after_submit_error = None

    def native(self):
        assert threading.get_ident() == self.main_thread, 'Native owner read on worker'
    def __bool__(self):
        self.native()
        return self.alive
    def IsBeingDeleted(self):
        self.native()
        return not self.alive
    def GetId(self):
        self.native()
        assert self.alive
        return 5037
    def report(self, message):
        self.native()
        self.messages.append(str(message))
    def run_job(self, title, operation, done=None, failed=None):
        self.native()
        if self.admission_error is not None:
            raise self.admission_error
        assert not self.working and not self._closing
        self.working = True
        self.cancel_event.clear()
        self.calls.append(title)
        future = self.executor.submit(operation)
        self.futures.append(future)
        self.callbacks.append((done, failed))
        def ready(completed):
            def deliver():
                self.working = False
                if self._closing:
                    return
                try:
                    result = completed.result()
                except Exception as error:
                    if failed is not None:
                        failed(error)
                else:
                    if done is not None:
                        done(result)
                        if self.duplicate:
                            done(result)
            self.pending.put(deliver)
        future.add_done_callback(ready)
        if self.after_submit_error is not None:
            raise self.after_submit_error
        return None
    def finish(self):
        self.pending.get(timeout=20)()
    def close(self):
        self._closing = True
        self.cancel_event.set()
        self.executor.shutdown(wait=True, cancel_futures=True)


class Answers:
    def __init__(self, left, right, parent):
        self.folder_answers = [left[0], right[0], parent]
        self.hash_answers = [left[1], right[1]]
        self.reviews, self.shown = [], []
        self.before_review = None
        self.cancel_review = False
        self.acknowledged = True
    def options(self, owner, action):
        assert action == 'restore'
        return N(budget=None, include_generated=False)
    def folder(self, title):
        return self.folder_answers.pop(0)
    def checksum(self, owner):
        return self.hash_answers.pop(0)
    def review(self, action, preview, owner):
        self.reviews.append(preview)
        if self.before_review:
            self.before_review()
        if self.cancel_review:
            return None
        rows = json.loads(preview.payload)['workspaces']
        return {row['id']: 'left' if row['left'] is not None else 'right' for row in rows}, self.acknowledged
    def show(self, title, body, owner):
        self.shown.append((title, body))


@pytest.fixture
def session(tmp_path):
    inputs, opened = [], []
    for label in ('left', 'right'):
        app = LibraryApp(tmp_path / (label + '-library'))
        opened.append(app)
        workspace = app.create_workspace(label)
        assert app.add_text(workspace, 'Owned source', 'Exact ' + label + ' Україна.')['state'] == 'complete'
        folder = tmp_path / (label + '-backup')
        inputs.append((folder, create_backup(app, folder), workspace))
    main = LibraryApp(tmp_path / 'current-library')
    opened.append(main)
    host = Host(main)
    answers = Answers(inputs[0], inputs[1], tmp_path)
    actions = ui.ResearchMergeActions(host, answers)
    before = {str(path): sha256(path.read_bytes()).hexdigest()
              for row in inputs for path in row[0].rglob('*') if path.is_file()}
    try:
        yield N(host=host, answers=answers, actions=actions, inputs=inputs, before=before)
    finally:
        actions.withdraw()
        host.close()
        assert {path: sha256(Path(path).read_bytes()).hexdigest() for path in before} == before
        for app in opened:
            assert app.close_search()


def complete(session):
    assert session.actions.start()
    session.host.finish()  # Genuine preview completed; same worker gets merge.
    assert len(session.host.calls) == 2
    session.host.finish()


def test_exact_review_and_public_merge_use_one_none_returning_executor_once(session):
    session.host.duplicate = True
    original_library = session.host.library
    complete(session)
    result = session.actions.result
    assert result is not None and len(session.answers.reviews) == 1 and len(session.answers.shown) == 1
    assert session.host.library is original_library and session.host._backup_review is None
    assert all(future.done() for future in session.host.futures)
    restored = LibraryApp(result.path)
    try:
        assert {row.id for row in restored.workspaces()} == {row[2] for row in session.inputs}
        for label, row in zip(('left', 'right'), session.inputs):
            hit, = restored.search(row[2], 'Exact ' + label + ' Україна.')
            assert restored.copy(row[2], hit.ref, hit.revision, 'quote') == 'Exact ' + label + ' Україна.'
    finally:
        assert restored.close_search()


@pytest.mark.parametrize('kind', ['cancel', 'no-ack', 'new-library', 'busy-unrelated'])
def test_rejected_or_revoked_review_never_admits_publication(session, kind):
    if kind == 'cancel':
        session.answers.cancel_review = True
    elif kind == 'no-ack':
        session.answers.acknowledged = False
    elif kind == 'new-library':
        session.answers.before_review = lambda: setattr(session.host, 'library', N(root=session.host.library.root))
    else:
        session.answers.before_review = lambda: setattr(session.host, 'working', True)
    assert session.actions.start()
    session.host.finish()
    assert len(session.host.calls) == 1 and not session.actions.action.destination.exists()
    assert session.host._backup_review is None and not session.answers.shown


@pytest.mark.parametrize('error_type', [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize('after_submit', [False, True])
def test_admission_baseexception_releases_only_own_token_and_cancels_actual_submitted_work(session, error_type, after_submit):
    error = error_type('Observed admission boundary')
    if after_submit:
        session.host.after_submit_error = error
    else:
        session.host.admission_error = error
        session.host.cancel_event.set()
    with pytest.raises(error_type):
        session.actions.start()
    assert session.actions.token is None and session.host._backup_review is None
    if after_submit:
        session.host.finish()
        assert session.host.futures[0].done()
    else:
        assert session.host.cancel_event.is_set() and not session.host.calls
    assert not session.actions.action.destination.exists() and not session.answers.reviews


def test_withdrawal_after_actual_uncommitted_graph_copy_revokes_only_owned_operation(session, monkeypatch):
    reached, release = threading.Event(), threading.Event()
    original = research_merge.ResearchContentMerge._copy_workspace
    def held(self, db, incoming, workspace, check):
        original(self, db, incoming, workspace, check)
        assert db.execute('SELECT count(*) FROM page').fetchone()[0] > 0
        reached.set()
        assert release.wait(10)
    monkeypatch.setattr(research_merge.ResearchContentMerge, '_copy_workspace', held)
    try:
        assert session.actions.start()
        session.host.finish()
        assert reached.wait(10)
        session.actions.withdraw()
        assert not session.host.cancel_event.is_set()
        release.set()
        session.host.finish()
        assert not session.actions.action.destination.exists()
        assert session.actions.result is None and not session.answers.shown
        assert session.host._backup_review is None
    finally:
        release.set()


def test_real_completed_merge_receipt_survives_dead_observer_and_late_callback(session, monkeypatch):
    reached, release = threading.Event(), threading.Event()
    original = research_merge.library_backup.restore_backup
    def held(source, destination, checksum, **kwargs):
        value = original(source, destination, checksum, **kwargs)
        if session.actions.action is not None and Path(destination) == session.actions.action.destination:
            assert not (value / INCOMPLETE).exists()
            reached.set()
            assert release.wait(10)
        return value
    monkeypatch.setattr(research_merge.library_backup, 'restore_backup', held)
    try:
        assert session.actions.start()
        session.host.finish()
        assert reached.wait(10)
        callback = session.host.callbacks[-1][0]
        session.actions.withdraw()
        session.host.alive = False
        release.set()
        session.host.finish()
        result = session.actions.result
        assert result is not None and result.path.is_dir()
        callback(result)
        assert not session.answers.shown and session.host._backup_review is None
        restored = LibraryApp(result.path)
        try:
            assert len(restored.workspaces()) == 2
        finally:
            assert restored.close_search()
    finally:
        release.set()


def test_busy_host_refusal_does_not_clear_or_cancel_an_unrelated_operation(session):
    session.host.working = True
    session.host.cancel_event.set()
    assert session.actions.start() is False
    assert session.host.cancel_event.is_set() and not session.host.calls


def extracted(name, **closure):
    # Exercise exact compiled product handlers; only native peers are controlled.
    module = ast.parse(inspect.getsource(ui))
    factory = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                   and node.name == 'create_merge_review')
    cls = next(node for node in factory.body if isinstance(node, ast.ClassDef))
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)
    namespace = dict(closure)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])),
                 'actual research-merge native handler', 'exec'), namespace)
    return namespace[name]


def test_exact_native_confirm_requires_unchanged_preview_choices_and_acknowledgment(session):
    left, right = session.inputs
    action = research_merge.ResearchContentMerge(*left[:2], *right[:2], session.host.library.root.parent / 'checked-output')
    preview = action.prepare()
    messages, ended = [], []
    status = N(SetLabel=messages.append, Wrap=lambda width: None)
    peer = N(alive=True, result=None, choices={left[2]: 'left', right[2]: 'right'},
             details=N(GetValue=lambda: preview.text), acknowledge=N(GetValue=lambda: False),
             status=status, GetClientSize=lambda: N(width=900), Layout=lambda: None,
             EndModal=ended.append)
    confirm = extracted('confirm', guard=lambda: None, preview=preview, action=action, wx=N(ID_OK=1))
    confirm(peer)
    assert peer.result is None and not ended and 'acknowledge' in messages[-1].lower()
    peer.acknowledge.GetValue = lambda: True
    peer.details.GetValue = lambda: preview.text + '\nchanged'
    confirm(peer)
    assert peer.result is None and not ended and 'changed' in messages[-1]
    peer.details.GetValue = lambda: preview.text
    confirm(peer)
    assert peer.result == (peer.choices, True) and ended == [1]
    assert not action.destination.exists()


def test_exact_native_destroy_handler_uses_cached_id_and_revokes_pure_result():
    handler = extracted('destroyed')
    peer = N(_native_id=41, alive=True, result=({'source': 'left'}, True))
    handler(peer, N(GetId=lambda: 42, Skip=lambda: None))
    assert peer.alive and peer.result is not None
    handler(peer, N(GetId=lambda: 41, Skip=lambda: None,
                    GetEventObject=lambda: pytest.fail('Native wrapper identity is not authority')))
    assert peer.alive is False and peer.result is None
