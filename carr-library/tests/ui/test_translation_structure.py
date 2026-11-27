"""Actual source capture and provider controller behind controllable UI owners."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.translation_structure import capture_text, source_text
from carr_library.ui.translation_dialog import TranslationController
from carr_library.ui.translation_sources import TranslationSourcesDialog
from tests.assistant.test_translation import ready, text_calls


class Control:
    def __init__(self, value='', selected=0):
        self.value, self.selection, self.items, self.enabled = value, selected, [], True
    def GetValue(self): return self.value
    def ChangeValue(self, value): self.value = value
    def GetSelection(self): return self.selection
    def SetSelection(self, index): self.selection = index
    def SetItems(self, items): self.items = list(items)
    def Enable(self, enabled=True): self.enabled = bool(enabled)


@pytest.fixture
def picker(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Translation source choices')
    queue, disposed = deque(), []
    executor = ThreadPoolExecutor(max_workers=1)
    frame = SimpleNamespace(library=app, active=(workspace, None),
        working=False, busy=False, _closing=False, cancel_event=Event())
    def run_job(label, operation, done, failed):
        if frame.working:
            return False
        frame.working = True
        future = executor.submit(operation)
        def completed(task):
            def apply():
                frame.working = False
                try:
                    done(task.result())
                except Exception as exc:
                    failed(exc)
            queue.append(apply)
        future.add_done_callback(completed)
        return True
    frame.run_job = run_job
    view = object.__new__(TranslationSourcesDialog)
    view.wx, view.frame = SimpleNamespace(ID_CLOSE=1, ID_OK=2), frame
    view.library, view.scope, view.workspace = app, frame.active, workspace
    view.cancel = Event()
    view.alive, view.busy = True, False
    view.rows, view.selections, view.history = (), [], []
    view.after, view.has_next, view.result, view.query_value = None, False, None, None
    view.dialog = SimpleNamespace(EndModal=disposed.append)
    for name in ('filter', 'books', 'kind', 'first', 'last', 'selected', 'status'):
        setattr(view, name, Control())
    view.first.value = '1'
    view.markdown = Control(False)
    view.buttons = {key: Control() for key in ('search','previous','next','add','remove','up','down','prepare','close')}
    def drain():
        deadline = monotonic() + 30
        while frame.working or queue:
            if monotonic() >= deadline:
                raise AssertionError('The actual source-capture worker did not settle.')
            if queue:
                queue.popleft()()
            else:
                sleep(.001)
    yield view, app, workspace, drain, disposed
    view.cancel.set()
    executor.shutdown(wait=True, cancel_futures=True)
    drain()


def test_current_catalog_pages_and_selected_order_reach_the_exact_immutable_sources(picker):
    view, app, workspace, drain, disposed = picker
    for index in range(61):
        app.add_text(workspace, f'Book {index:02}', f'Exact text {index}.')
    view.load(None, [])
    drain()
    first_ids = [row.edition_id for row in view.rows]
    assert len(first_ids) == 50 and view.has_next
    view.books.selection = 3
    view.add()
    expected = [view.rows[3].title]
    view.next()
    drain()
    assert len(view.rows) == 11 and not view.has_next
    assert not set(first_ids).intersection(row.edition_id for row in view.rows)
    view.books.selection = 0
    view.add()
    expected.append(view.rows[0].title)
    view.selected.selection = 1
    view.move(-1)
    view.capture()
    assert view.busy and not view.buttons['add'].enabled
    drain()
    assert disposed == [2] and not view.alive
    assert [item['title'] for item in view.result['documents']] == list(reversed(expected))
    assert all(item['context']['source_anchor']['quotation_authority'] is False
               for item in view.result['documents'])


def test_changed_filter_cannot_reuse_another_catalog_cursor(picker):
    view, app, workspace, drain, disposed = picker
    app.add_text(workspace, 'Original source', 'Exact text.')
    view.load(None, [])
    drain()
    before = view.rows
    view.has_next = True
    view.filter.value = 'Different'
    view.next()
    assert not view.busy and view.rows == before
    assert 'changed title filter' in view.status.value


def test_source_range_and_document_are_distinct_current_capture_choices(picker):
    view, app, workspace, drain, disposed = picker
    app.add_text(workspace, 'Three positions', 'First.\fSecond.\fThird.')
    view.load(None, [])
    drain()
    view.kind.selection = 1
    view.first.value, view.last.value = '2', '3'
    view.add()
    view.capture()
    drain()
    item, = view.result['documents']
    assert item['kind'] == 'range'
    assert ''.join(section['text'] for section in item['sections']) == 'Second.\n\nThird.'
    assert [position['ordinal'] for position in item['context']['source_anchor']['positions']] == [2, 3]


def test_current_source_guard_rejects_changed_library_before_capture(picker):
    view, app, workspace, drain, disposed = picker
    app.add_text(workspace, 'Exact source', 'Unchanged.')
    view.load(None, [])
    drain()
    view.add()
    view.frame.active = ('another-workspace', None)
    view.capture()
    assert not view.busy and view.result is None and not disposed
    assert 'library changed' in view.status.value


def test_structure_and_source_context_are_bound_to_the_current_controller_review(ready):
    service, profile, vault, transport = ready
    controller = TranslationController(service.root, service=service)
    document = capture_text('# Original\n\nText.', markdown=True)
    value = source_text(document)
    controller.preview(profile, value, 'en', 'uk', '1', title='Review', structure=document)
    changed = deepcopy(document)
    changed['documents'][0]['title'] = 'A different reviewed document identity'
    with pytest.raises(PermissionError):
        controller.submit(profile, value, 'en', 'uk', '1', title='Review',
                          structure=changed, approved=True)
    assert not text_calls(transport) and service.list() == ()
    identity = controller.submit(profile, value, 'en', 'uk', '1', title='Review',
                                 structure=document, approved=True)
    assert service.jobs.load(identity)['structure'] == document and not text_calls(transport)


def test_shared_operation_cancel_stops_the_real_retained_source_capture(picker):
    view, app, workspace, drain, disposed = picker
    app.add_text(workspace, 'Cancellable source', 'Exact source text.')
    view.load(None, [])
    drain()
    view.add()
    view.frame.cancel_event.set()
    view.capture()
    drain()
    assert not view.result and not disposed
    assert 'cancelled' in view.status.value.lower()


def translation_run_callbacks():
    """Execute the real native closures with only the wx widget boundary replaced."""
    import ast
    from decimal import Decimal
    from pathlib import Path
    from carr_library.ui import translation_dialog
    source = Path(translation_dialog.__file__)
    show = next(node for node in ast.parse(source.read_text(encoding='utf-8')).body
                if isinstance(node, ast.FunctionDef) and node.name == 'show_translation')
    helpers = {node.name: node for node in show.body if isinstance(node, ast.FunctionDef)}
    factory = ast.parse('''
def callbacks(frame, controller, service, chosen, document, wx, completed):
    alive, busy, current_job = True, False, None
    scoped_library, structured_source = frame.library, document
    cancel = Event()
    status = Control()
    dialog = SimpleNamespace(EndModal=lambda code: None)
    job_title, rate, glossary = Control('Worker source'), Control(''), Control('')
    values = lambda: chosen
    widget_threads = []
    controls = lambda: widget_threads.append(get_ident())
    def state():
        return current_job, busy, status.value, widget_threads
    return run, close, state
''').body[0]
    factory.body[-1:-1] = [helpers[name] for name in ('report', 'stopped', 'begin', 'run', 'close')]
    namespace = dict(Decimal=Decimal, Path=Path, Event=Event, Control=Control,
                     SimpleNamespace=SimpleNamespace)
    from threading import get_ident
    namespace['get_ident'] = get_ident
    exec(compile(ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[])),
                 str(source), 'exec'), namespace)
    return namespace['callbacks']


@pytest.mark.parametrize('outcome', ['cancel-before-commit', 'resume-failure', 'inspection-failure'])
def test_actual_translation_submit_uses_shared_worker_and_keeps_committed_identity(
        ready, monkeypatch, outcome):
    from threading import get_ident
    from carr_library.assistant.translation_structure import capture_library
    service, profile, vault, transport = ready
    app = LibraryApp(service.root)
    workspace = app.create_workspace('Worker-bound immutable source')
    book = app.add_text(workspace, 'Retained original', 'Exact retained paragraph.')
    document = capture_library(app, [{'workspace': workspace, 'edition': book['edition'],
        'kind': 'document', 'first': 1, 'last': None}])
    chosen = (profile, source_text(document), 'en', 'uk', '1')
    controller = TranslationController(app.root, service=service)
    controller.preview(*chosen, title='Worker source', structure=document)
    entered, release = Event(), Event()
    main_thread = get_ident()
    original_check = service._verify_structure_sources
    def checked(value, *, cancelled=lambda: False):
        assert get_ident() != main_thread
        entered.set()
        assert release.wait(10), 'Test worker release was not delivered.'
        return original_check(value, cancelled=cancelled)
    monkeypatch.setattr(service, '_verify_structure_sources', checked)
    if outcome == 'resume-failure':
        def unavailable(*args, **kwargs):
            raise RuntimeError('Synthetic failure after a durable queue commit.')
        monkeypatch.setattr(service, 'resume', unavailable)
    callbacks, results = deque(), []
    frame = SimpleNamespace(library=app, cancel_event=Event(), working=False, _closing=False)
    executor = ThreadPoolExecutor(max_workers=1)
    def run_job(label, operation, done, failed):
        if frame.working:
            return False
        frame.working = True
        future = executor.submit(operation)
        def settled(task):
            def apply():
                frame.working = False
                try:
                    done(task.result())
                except Exception as error:
                    failed(error)
            callbacks.append(apply)
        future.add_done_callback(settled)
        return True
    frame.run_job = run_job
    wx = SimpleNamespace(YES=1, YES_NO=2, NO_DEFAULT=4, ICON_QUESTION=8, ID_CLOSE=42,
                         MessageBox=lambda *a, **k: 1,
                         CallAfter=lambda action, *a: callbacks.append(lambda: action(*a)))
    def completed(result):
        assert get_ident() == main_thread
        results.append(result)
        if outcome == 'inspection-failure':
            raise OSError('Synthetic final inspection failure.')
    run, close, state = translation_run_callbacks()(
        frame, controller, service, chosen, document, wx, completed)
    try:
        run(None)
        assert frame.working and entered.wait(3)
        assert state()[0] is None and state()[1] is True
        assert not text_calls(transport)
        if outcome == 'cancel-before-commit':
            frame.cancel_event.set()
        release.set()
        until = monotonic() + 20
        while frame.working or callbacks:
            assert monotonic() < until, 'Translation worker did not settle.'
            if callbacks:
                callbacks.popleft()()
            else:
                sleep(.001)
        identity, busy, message, widget_threads = state()
        assert not busy and all(thread == main_thread for thread in widget_threads)
        if outcome == 'cancel-before-commit':
            assert identity is None and service.list() == () and not text_calls(transport)
            assert 'cancel' in message.lower()
        else:
            assert identity and identity in message and 'Synthetic' in message
            saved = service.jobs.load(identity)
            assert saved['status'] == ('queued' if outcome == 'resume-failure' else 'complete')
            assert len(text_calls(transport)) == (0 if outcome == 'resume-failure' else 1)
    finally:
        release.set()
        frame.cancel_event.set()
        executor.shutdown(wait=True, cancel_futures=True)
        close()
