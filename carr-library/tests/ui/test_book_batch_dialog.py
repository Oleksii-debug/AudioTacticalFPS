"""Actual batch/dialog closures on the shared worker; stand-ins are not native evidence."""
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from carr_library.runtime_settings import load_voice_preferences, save_voice_preferences
from carr_library.studio.book_audio import SplitOptions, capture_plan
from carr_library.studio.book_batch import BookBatchService
from carr_library.studio.project import Role
from carr_library.ui import book_audio_dialog, book_batch_dialog

_spec = importlib.util.spec_from_file_location('book_batch_ui_handlers', Path(__file__).with_name('test_book_audio_dialog.py'))
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
handlers = _fixture.handlers


@pytest.fixture
def batch_ui(handlers, monkeypatch):
    e = handlers
    second = e.app.add_text(e.workspace, 'Second selected source', 'Second exact literal book.')
    choices = {'indices': (0, 1), 'single': 0}
    class Picker:
        def __init__(self, *args, **kwargs): pass
        def ShowModal(self): return e.wx.ID_OK
        def GetSelections(self): return choices['indices']
        def GetSelection(self): return choices['single']
        def Destroy(self): pass
    e.wx.MultiChoiceDialog = e.wx.SingleChoiceDialog = Picker
    available = tuple(SimpleNamespace(id=identity, name=identity, language='en', capabilities=('rate', 'volume'))
                      for identity in ('synthetic-listed-voice', 'second-listed-voice'))
    e.production._voices.voices = lambda: available
    settings = {
        e.imported['edition']: ('synthetic-listed-voice', 1, 72, 'wav'),
        second['edition']: ('second-listed-voice', -1, 52, 'flac'),
    }
    outputs = {}
    for edition in settings:
        outputs[edition] = e.output.parent / ('book-output-' + edition)
        outputs[edition].mkdir()
    details, reviewed = [], []
    def detail(frame, *, service, edition_id, on_reviewed, initial_plan, initial_output, **kwargs):
        details.append((edition_id, initial_plan, initial_output))
        class Child:
            def ShowModal(self):
                def work():
                    assert threading.get_ident() != e.ui_thread
                    voice, rate, volume, format = settings[edition_id]
                    plan = initial_plan or capture_plan(e.app, e.workspace, edition_id,
                        options=SplitOptions('document'), role=Role('narrator', 'Narrator', voice, 'en',
                        rate=rate, volume=volume), format=format)
                    value = service.review(plan, outputs[edition_id])
                    return plan, value
                def ready(value):
                    reviewed.append(value[0])
                    on_reviewed(*value)
                e.frame.run_job('Existing per-book review dialog fixture', work, done=ready)
                e.complete()
            def Destroy(self): pass
        return Child(), lambda: None
    monkeypatch.setattr(book_audio_dialog, 'create_book_audio_dialog', detail)
    start = len(e.state['widgets'])
    dialog, close = book_batch_dialog.create_book_batch_dialog(e.frame, service=e.service, wx_module=e.wx)
    widgets = e.state['widgets'][start:]
    def find(name):
        return next(widget for widget in widgets if widget.name == name)
    def press(name):
        control = find(name)
        assert control.enabled, name
        control.bindings[e.wx.EVT_BUTTON](None)
    yield SimpleNamespace(e=e, find=find, press=press, close=close, settings=settings, outputs=outputs,
                          details=details, reviewed=reviewed, choices=choices)
    close()


def choose_and_review(e):
    e.press('Choose library books')
    assert e.e.state['queue'] and not e.e.production.jobs()
    e.e.complete()
    select = e.find('Ordered books; review each book before Preview')
    assert len(select.choices) == 2
    for index in (0, 1):
        select.SetSelection(index)
        e.press('Review selected book')
    assert len(e.reviewed) == 2 and not e.e.production.jobs()


def preview(e):
    choose_and_review(e)
    e.press('Preview entire batch')
    assert e.e.state['queue']
    e.e.complete()
    assert 'Batch review SHA-256:' in e.find('Multiple book review and existing Studio checkpoints').value


def test_batch_real_review_prepare_order_and_per_item_settings_use_existing_worker(batch_ui):
    e = batch_ui
    choose_and_review(e)
    select = e.find('Ordered books; review each book before Preview')
    select.SetSelection(1)
    e.press('Move selected book up')
    e.press('Preview entire batch')
    e.e.complete()
    assert not e.e.production.jobs()
    e.press('Prepare batch items')
    assert len(e.e.state['approvals']) == 1 and not e.e.production.jobs()
    e.e.complete()
    batch, = BookBatchService(e.e.service).saved()
    assert [item.plan_id for item in batch.items] == [plan.id for plan in reversed(e.reviewed)]
    plans = BookBatchService(e.e.service).plans(batch)
    for plan in plans:
        expected = e.settings[plan.source['source_anchor']['edition']]
        assert (plan.role.voice_id, plan.role.rate, plan.role.volume, plan.format) == expected
    assert len(e.e.production.jobs()) == 2
    assert all(not list(path.iterdir()) for path in e.outputs.values())
    assert not e.find('Move selected book up').enabled and not e.find('Choose library books').enabled
    assert e.find('Run or resume batch').enabled
    assert 'PREPARED' in e.find('Multiple book review and existing Studio checkpoints').value


def test_batch_run_requires_explicit_frozen_review_and_keeps_borrowed_owner(batch_ui, monkeypatch):
    e = batch_ui
    preview(e)
    e.press('Prepare batch items')
    e.e.complete()
    calls = []
    def run(groups, identity, outputs, **options):
        assert threading.get_ident() != e.e.ui_thread
        options['guard']()
        assert not options['cancelled']()
        current = groups.review(groups.load(identity), outputs)
        assert options['approval'] == current.fingerprint
        calls.append((identity, tuple(outputs)))
        return {'status': 'paused', 'stored': groups.status(identity)}
    monkeypatch.setattr(BookBatchService, 'run', run)
    e.press('Run or resume batch')
    assert calls == [] and len(e.e.state['approvals']) == 2
    e.e.complete()
    assert len(calls) == 1 and 'Batch invocation: paused' in e.find(
        'Multiple book review and existing Studio checkpoints').value
    e.close()
    assert not e.e.service._closed.is_set() and not e.e.production._closed.is_set()


def test_mutated_title_cannot_reuse_batch_approval(batch_ui):
    e = batch_ui
    preview(e)
    e.find('Batch title').ChangeValue('Changed after exact preview')
    e.press('Prepare batch items')
    assert not e.e.state['approvals'] and not e.e.state['queue'] and not e.e.production.jobs()
    assert 'Preview this exact' in e.find('Multiple book review and existing Studio checkpoints').value


def test_queued_preview_freezes_inputs_and_rejects_later_ui_changes(batch_ui):
    e = batch_ui
    choose_and_review(e)
    e.press('Preview entire batch')
    e.find('Batch title').ChangeValue('Changed while source worker queued')
    e.e.complete()
    assert 'changed. Preview again' in e.find('Multiple book review and existing Studio checkpoints').value
    assert not e.find('Prepare batch items').enabled and not e.e.production.jobs()


def test_close_cancels_queued_source_review_without_closing_parent_service(batch_ui):
    e = batch_ui
    choose_and_review(e)
    e.press('Preview entire batch')
    e.close()
    result = e.e.complete()
    assert isinstance(result, InterruptedError)
    assert not e.e.production.jobs() and not e.e.service._closed.is_set()


def test_source_scope_change_blocks_queued_preparation(batch_ui):
    e = batch_ui
    preview(e)
    e.press('Prepare batch items')
    original = e.e.frame.active
    e.e.frame.active = (e.e.workspace, 'different-topic')
    try:
        result = e.e.complete()
        assert isinstance(result, ValueError) and not e.e.production.jobs()
    finally:
        e.e.frame.active = original


def test_cancel_before_queued_render_does_not_enter_production(batch_ui, monkeypatch):
    e = batch_ui
    preview(e)
    e.press('Prepare batch items')
    e.e.complete()
    observed = []
    def run(groups, identity, outputs, **options):
        observed.append((options['cancelled'](), options['cancel_action']()))
        return {'status': 'cancelled', 'stored': groups.status(identity)}
    monkeypatch.setattr(BookBatchService, 'run', run)
    e.press('Run or resume batch')
    e.press('Cancel batch')
    result = e.e.complete()
    assert isinstance(result, InterruptedError) and not observed
    assert len(e.e.production.jobs()) == 2
    assert all(not list(path.iterdir()) for path in e.outputs.values())


@pytest.mark.parametrize('requested', ['Pause batch', 'Cancel batch', 'close'])
def test_active_batch_stop_uses_existing_worker_and_retains_borrowed_service(batch_ui, monkeypatch, requested):
    e = batch_ui
    preview(e)
    e.press('Prepare batch items')
    e.e.complete()
    entered, release = threading.Event(), threading.Event()
    observed = []
    def run(groups, identity, outputs, **options):
        assert threading.get_ident() != e.e.ui_thread
        entered.set()
        assert release.wait(10)
        observed.append((options['cancelled'](), options['cancel_action']()))
        return {'status': 'cancelled' if options['cancel_action']() == 'cancel' else 'paused',
                'stored': groups.status(identity)}
    monkeypatch.setattr(BookBatchService, 'run', run)
    e.press('Run or resume batch')
    label, operation, done, failed = e.e.state['queue'].pop(0)
    values = []
    worker = threading.Thread(target=lambda: values.append(operation()))
    worker.start()
    try:
        assert entered.wait(10)
        e.close() if requested == 'close' else e.press(requested)
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and len(values) == 1
    e.e.frame.working = False
    done(values[0])
    assert observed == [(True, 'cancel' if requested == 'Cancel batch' else 'pause')]
    assert not e.e.service._closed.is_set() and not e.e.production._closed.is_set()
    assert all(not list(path.iterdir()) for path in e.outputs.values())


def test_saved_batch_reopens_references_and_does_not_resume(batch_ui):
    e = batch_ui
    preview(e)
    e.press('Prepare batch items')
    e.e.complete()
    batch, = BookBatchService(e.e.service).saved()
    e.press('New batch')
    assert not e.find('Ordered books; review each book before Preview').choices
    e.press('Saved batches')
    e.e.complete()  # Choose the immutable group; only this choice queues its jobs.
    assert e.e.state['queue'] and not e.find('Run or resume batch').enabled
    e.e.complete()
    assert len(e.find('Ordered books; review each book before Preview').choices) == 2
    assert not e.find('Run or resume batch').enabled
    assert 'No saved batch is automatically resumed' in e.find(
        'Multiple book review and existing Studio checkpoints').value
    e.find('Ordered books; review each book before Preview').SetSelection(0)
    e.press('Review selected book')
    assert e.details[-1][1].id == batch.items[0].plan_id


def test_existing_per_book_review_mode_captures_profile_without_jobs(handlers):
    e = handlers
    voice = SimpleNamespace(id='synthetic-listed-voice', name='Declared local fixture',
                            language='en', capabilities=('rate', 'volume'))
    e.production._voices.voices = lambda: (voice,)
    initial = load_voice_preferences(e.app.root / 'state')
    save_voice_preferences(e.app.root / 'state', {
        'favorites': [voice.id], 'defaults': {'en': voice.id},
        'profiles': {'Quiet profile': {'voice_id': voice.id, 'language': 'en', 'rate': 2, 'volume': 45}}},
        expected=initial)
    start = len(e.state['widgets'])
    captured = []
    dialog, close = book_audio_dialog.create_book_audio_dialog(e.frame, service=e.service, wx_module=e.wx,
        edition_id=e.imported['edition'], on_reviewed=lambda *value: captured.append(value),
        initial_output=e.output)
    widgets = e.state['widgets'][start:]
    find = lambda name: next(widget for widget in widgets if widget.name == name)
    def press(name):
        control = find(name)
        assert control.enabled
        control.bindings[e.wx.EVT_BUTTON](None)
    try:
        e.complete()  # Existing source_options runs on the shared worker.
        press('Discover local voices')
        e.complete()
        profile = find('Saved voice profile; copied into this review')
        profile.SetSelection(1)
        profile.bindings[e.wx.EVT_CHOICE](None)
        assert find('Voice rate, -10 to 10').value == '2'
        assert find('Voice volume, 0 to 100').value == '45'
        press('Preview split and names')
        e.complete()
        press('Use reviewed book')
        assert not captured and not e.production.jobs()
        e.complete()
        plan, reviewed = captured[0]
        assert (plan.role.rate, plan.role.volume) == (2, 45) and not reviewed.prepared
        assert not e.production.jobs() and not e.service.saved()
        assert not find('Run or resume saved items').enabled
        assert not e.service._closed.is_set()
    finally:
        close()



@pytest.mark.parametrize('changed', [None, 'volume', 'parent', 'destination'])
def test_actual_native_review_driver_checks_canonical_book_paths_and_settings(handlers, changed):
    """Run the real native-driver closure over genuine saved Book reviews."""
    import ast
    from carr_library.studio.book_batch import select_books

    e = handlers
    second_source = e.app.add_text(e.workspace, 'Native driver second book',
                                  'A separate exact second book.')
    first = capture_plan(e.app, e.workspace, e.imported['edition'],
        options=SplitOptions('document'),
        role=Role('narrator', 'Narrator', 'synthetic-listed-voice', 'en'))
    second = capture_plan(e.app, e.workspace, second_source['edition'],
        options=SplitOptions('document'),
        role=Role('narrator', 'Narrator', 'synthetic-listed-voice', 'en', volume=55),
        format='flac')
    spelling = e.output.parent / 'selected-path-spelling'
    spelling.mkdir()
    actual_output = e.output.parent / 'second-native-output'
    actual_output.mkdir()
    selected_output = spelling / '..' / actual_output.name
    assert selected_output.is_dir()
    assert str(selected_output) != str(selected_output.resolve())
    groups = BookBatchService(e.service)
    group = select_books((first, second), title='Native exact review contract')
    parents = (e.output, selected_output)
    previewed = groups.review(group, parents, plans=(first, second))
    prepared = groups.prepare(group, parents, plans=(first, second),
                              approval=previewed.fingerprint)
    displayed = book_batch_dialog.batch_review_text(group, (first, second), prepared)
    canonical = str(selected_output.resolve())
    assert prepared.books[1].output_parent == canonical
    assert 'Output parent: ' + canonical in displayed
    assert str(selected_output) not in displayed  # The old literal-path assertion fails here.
    assert '; volume 55' in displayed
    assert all(state['status'] == 'pending' and not state['artifacts']
               for state in e.production.jobs())
    assert not list(e.output.iterdir()) and not list(actual_output.iterdir())
    if changed == 'volume':
        displayed = displayed.replace('; volume 55', '; volume 54')
    elif changed == 'parent':
        displayed = displayed.replace('Output parent: ' + canonical,
                                      'Output parent: ' + str(e.output))
    elif changed == 'destination':
        displayed = displayed.replace('Output folder: ' + prepared.books[1].destinations[0],
                                      'Output folder: ' + str(e.output / 'unreviewed'))

    script = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_v3_smoke.py'
    tree = ast.parse(script.read_text(encoding='utf-8'), filename=str(script))
    batch_driver, = [node for node in ast.walk(tree)
                     if isinstance(node, ast.FunctionDef) and node.name == 'exercise_book_batch']
    reviewed, = [node for node in batch_driver.body
                 if isinstance(node, ast.FunctionDef) and node.name == 'reviewed']
    module = ast.fix_missing_locations(ast.Module(body=[reviewed], type_ignores=[]))
    status = SimpleNamespace(GetValue=lambda: displayed)
    frame = SimpleNamespace(working=False)
    clicked, scheduled, approvals = [], [], []

    def named(name, kind):
        assert name == 'Multiple book review and existing Studio checkpoints'
        return status

    def click(name):
        assert name == 'Run or resume batch'
        clicked.append(name)
        approvals.append('explicit native Run')
        frame.working = True

    rendered = object()
    environment = dict(frame=frame, named=named,
        wx=SimpleNamespace(TextCtrl=object, Window=SimpleNamespace(FindFocus=lambda: status)),
        voice=SimpleNamespace(id=first.role.voice_id),
        second_voice=SimpleNamespace(id=second.role.voice_id), second_output=selected_output,
        prepared=prepared, click=click, approvals=approvals,
        later=scheduled.append, rendered=rendered)
    exec(compile(module, str(script), 'exec'), environment)
    if changed is None:
        environment['reviewed']()
        assert clicked == ['Run or resume batch'] and approvals == ['explicit native Run']
        assert scheduled == [rendered]
    else:
        with pytest.raises(AssertionError):
            environment['reviewed']()
        assert not clicked and not scheduled and not approvals
        assert not frame.working
    assert not list(e.output.iterdir()) and not list(actual_output.iterdir())
    assert all(state['status'] == 'pending' and not state['artifacts']
               for state in e.production.jobs())


@pytest.mark.parametrize('stage, approvals, verified', [
    ('opened', 0, False), ('loaded', 0, False), ('reviewed', 0, False),
    ('rendered', 1, False), ('rendered', 2, True),
])
def test_native_batch_deadline_records_pending_phase_without_driving_work(
        tmp_path, monkeypatch, capsys, stage, approvals, verified):
    """The actual timer keeps its deadline and exposes the real held worker stack."""
    import ast
    import faulthandler
    import json
    import traceback

    script = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_v3_smoke.py'
    tree = ast.parse(script.read_text(encoding='utf-8'), filename=str(script))
    driver, = [node for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef) and node.name == 'exercise_book_batch']
    functions = [node for node in driver.body if isinstance(node, ast.FunctionDef)
                 and node.name in ('diagnose', 'later')]
    assert {node.name for node in functions} == {'diagnose', 'later'}
    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    entered, release = threading.Event(), threading.Event()

    def held_book_worker():
        entered.set()
        assert release.wait(10)

    worker = threading.Thread(target=held_book_worker, name='actual-book-diagnostic-barrier')
    pending, closed, calls, errors = [], [], [], []
    deadline = 100.0
    clock = [deadline - 1]
    frame = SimpleNamespace(working=True, status=SimpleNamespace(
        GetValue=lambda: 'Rendering reviewed books through existing Studio jobs'))
    controls = {
        'Multiple book review and existing Studio checkpoints': SimpleNamespace(
            GetValue=lambda: 'Book 2/2: exact retained source. ' + 'x' * 5000),
        'Ordered books; review each book before Preview': SimpleNamespace(GetCount=lambda: 2),
        'Run or resume batch': SimpleNamespace(IsEnabled=lambda: False),
        'Preview entire batch': SimpleNamespace(IsEnabled=lambda: False),
        'Close': SimpleNamespace(IsEnabled=lambda: True),
    }

    def named(name, kind):
        return controls[name]

    def callback():
        calls.append('must not start or resume work after deadline')
    callback.__name__ = stage
    environment = dict(frame=frame, dialog=SimpleNamespace(Close=lambda: closed.append(True)),
        named=named, wx=SimpleNamespace(TextCtrl=object, Choice=object, Button=object,
            CallLater=lambda delay, fn: pending.append((delay, fn))),
        monotonic=lambda: clock[0], deadline=deadline, approvals=['explicit Run'] * approvals,
        before={'accepted output': ('sha256', 1)} if verified else {}, passed=[],
        observed_phase=[None], gui_errors=errors, json=json, traceback=traceback)
    exec(compile(module, str(script), 'exec'), environment)
    dump = faulthandler.dump_traceback
    stacks = tmp_path / 'actual-worker-stacks.txt'
    worker.start()
    try:
        assert entered.wait(5)
        with stacks.open('w', encoding='utf-8') as output:
            monkeypatch.setattr(faulthandler, 'dump_traceback',
                lambda *, all_threads: dump(file=output, all_threads=all_threads))
            environment['later'](callback)
            assert pending[0][0] == 40 and not closed and not calls
            clock[0] = deadline
            pending.pop()[1]()
        assert not pending and not calls and closed == [True]
        assert len(errors) == 1
        assert 'TimeoutError: Native multi-book review/Run did not finish.' in errors[0]
        assert 'held_book_worker' in stacks.read_text(encoding='utf-8')
        records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert [record['native_book_batch'] for record in records] == ['STAGE', 'FAIL']
        assert all(record['stage'] == stage for record in records)
        failure = records[-1]
        assert failure['elapsed_seconds'] == 90 and failure['desktop_working']
        assert failure['run_approvals'] == approvals and failure['first_result_verified'] is verified
        assert failure['selected_books'] == 2 and failure['close_enabled']
        assert not failure['run_enabled'] and not failure['preview_enabled']
        assert len(failure['batch_status']) == 2048 and not failure['scenario_passed']
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()


def test_native_batch_diagnostic_failure_cannot_replace_primary_error_or_skip_close(monkeypatch):
    import ast
    import faulthandler
    import traceback

    script = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_v3_smoke.py'
    tree = ast.parse(script.read_text(encoding='utf-8'), filename=str(script))
    driver, = [node for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef) and node.name == 'exercise_book_batch']
    later, = [node for node in driver.body if isinstance(node, ast.FunctionDef) and node.name == 'later']
    module = ast.fix_missing_locations(ast.Module(body=[later], type_ignores=[]))
    pending, observed, closed, errors = [], [], [], []

    def diagnose(stage, *, failed=False):
        observed.append((stage, failed))
        if failed:
            raise OSError('injected diagnostic observation error')

    def render_callback():
        raise ValueError('original native callback failure')

    monkeypatch.setattr(faulthandler, 'dump_traceback',
                        lambda **kwargs: pytest.fail('unreached after diagnostic error'))
    environment = dict(diagnose=diagnose, monotonic=lambda: 1.0, deadline=90.0,
        gui_errors=errors, traceback=traceback,
        dialog=SimpleNamespace(Close=lambda: closed.append(True)),
        wx=SimpleNamespace(CallLater=lambda delay, fn: pending.append((delay, fn))))
    exec(compile(module, str(script), 'exec'), environment)
    environment['later'](render_callback)
    assert pending[0][0] == 40
    pending.pop()[1]()
    assert closed == [True] and not pending
    assert observed == [('render_callback', False), ('render_callback', True)]
    assert len(errors) == 2 and 'ValueError: original native callback failure' in errors[0]
    assert 'OSError: injected diagnostic observation error' in errors[1]


@pytest.mark.parametrize('observer_name, identity_name, event_name', [
    ('frame_destroyed', 'frame_destroy_id', 'destroyed'),
    ('bootstrap_closed', 'bootstrap_destroy_id', 'bootstrap_destroyed'),
])
def test_native_v3_destroy_observers_use_captured_id_not_python_wrapper(
        observer_name, identity_name, event_name):
    import ast

    script = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_v3_smoke.py'
    tree = ast.parse(script.read_text(encoding='utf-8'), filename=str(script))
    observer, = [node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name == observer_name]
    module = ast.fix_missing_locations(ast.Module(body=[observer], type_ignores=[]))
    seen, skipped = threading.Event(), []
    captured = [-31994]
    environment = {identity_name: captured, event_name: seen}
    exec(compile(module, str(script), 'exec'), environment)

    class Event:
        def __init__(self, identity):
            self.identity = identity
        def GetId(self):
            return self.identity
        def GetEventObject(self):
            pytest.fail('A destroy observer must not depend on the old Python wrapper.')
        def Skip(self):
            skipped.append(self.identity)

    environment[observer_name](Event(-31993))  # A child event must not satisfy owner destruction.
    assert not seen.is_set()
    environment[observer_name](Event(-31994))  # Same native owner, different/dead Python peer.
    assert seen.is_set() and skipped == [-31993, -31994]
    seen.clear()
    captured[0] = None
    environment[observer_name](Event(None))
    assert not seen.is_set() and skipped[-1] is None

    # Both IDs are captured from the real owner before the corresponding bind.
    main, = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main']
    owner_name = 'frame' if observer_name == 'frame_destroyed' else 'bootstrap'
    bind, = [node for node in ast.walk(main) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == 'Bind'
             and isinstance(node.func.value, ast.Name) and node.func.value.id == owner_name
             and any(isinstance(arg, ast.Name) and arg.id == observer_name for arg in node.args)]
    captured_assignment, = [node for node in ast.walk(main) if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                and target.value.id == identity_name for target in node.targets)]
    assert captured_assignment.lineno < bind.lineno
    assert isinstance(captured_assignment.value, ast.Call)
    assert ast.unparse(captured_assignment.value) == owner_name + '.GetId()'


@pytest.mark.parametrize('missing', ['frame-event', 'frame-wrapper', 'bootstrap-event', 'bootstrap-wrapper', None])
def test_native_v3_cleanup_requires_native_events_and_separately_deleted_wrappers(missing):
    import ast

    script = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_v3_smoke.py'
    tree = ast.parse(script.read_text(encoding='utf-8'), filename=str(script))
    poll, = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == 'poll_cleanup']
    module = ast.fix_missing_locations(ast.Module(body=[poll], type_ignores=[]))
    pending, exits, failures, releases = [], [], [], []
    destroyed, bootstrap_destroyed, loop_started, completed = (threading.Event() for _ in range(4))
    loop_started.set()
    if missing != 'frame-event':
        destroyed.set()
    if missing != 'bootstrap-event':
        bootstrap_destroyed.set()

    class Peer:
        def __init__(self, alive):
            self.alive = alive
            self.executor = SimpleNamespace(_threads=())
            self.progress_panel = SimpleNamespace(closed=True)
        def __bool__(self):
            return self.alive
        def Destroy(self):
            pytest.fail('Polling cannot replace the already requested native destruction.')

    result = {'checks': []}
    frame, bootstrap = Peer(missing == 'frame-wrapper'), Peer(missing == 'bootstrap-wrapper')
    future = SimpleNamespace(done=lambda: True, result=lambda: releases.append('actual owned release'))
    environment = dict(frame=frame, bootstrap=bootstrap, cleanup_executors=[],
        cleanup_futures=[future], destroyed=destroyed, bootstrap_destroyed=bootstrap_destroyed,
        loop_started=loop_started, completed=completed, bootstrap_closing=[True],
        frame_destroy_id=[-31994], bootstrap_destroy_id=[-31995],
        cleanup_deadline=[60.0], monotonic=lambda: 1.0, threading=threading,
        selected_progress=None, failures=failures, gui_errors=[], result=result,
        remember_failure=failures.append, app=SimpleNamespace(ExitMainLoop=lambda: exits.append(True)),
        wx=SimpleNamespace(CallLater=lambda delay, callback: pending.append((delay, callback))))
    exec(compile(module, str(script), 'exec'), environment)
    environment['poll_cleanup']()
    if missing is not None:
        assert len(pending) == 1 and pending[0][0] == 25
        assert not completed.is_set() and not exits and not releases and not failures
        assert 'native_cleanup' not in result
        # A missing native event/dead peer remains a genuine failure at the same deadline.
        environment['monotonic'] = lambda: 60.0
        pending.pop()[1]()
        assert completed.is_set() and exits == [True] and len(failures) == 1
        assert isinstance(failures[0], TimeoutError) and 'native_cleanup' not in result
        assert not releases
    else:
        assert completed.is_set() and exits == [True] and not pending and not failures
        assert releases == ['actual owned release']
        assert result['native_cleanup']['destroy_event'] is True
        assert result['native_cleanup']['bootstrap_destroy_event'] is True
        assert result['native_cleanup']['frame_wrapper_deleted'] is True
        assert result['native_cleanup']['bootstrap_wrapper_deleted'] is True
        assert result['native_cleanup']['frame_destroy_native_id'] == -31994
        assert result['native_cleanup']['bootstrap_destroy_native_id'] == -31995
