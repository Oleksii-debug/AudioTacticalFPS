"""Typed native-handler contracts; no private runtime or inference is simulated."""
from collections import deque
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from carr_library.transcription_receipt import DEFAULT_SETTINGS
from carr_library.ui import transcription_dialog as ui


class Control:
    def __init__(self, value=None):
        self.value, self.selection, self.items, self.enabled = value, -1, [], True
    def GetValue(self):
        return self.value
    def SetValue(self, value):
        self.value = value
    ChangeValue = SetValue
    def GetSelection(self):
        return self.selection
    def SetSelection(self, value):
        self.selection = value
    def SetItems(self, values):
        self.items, self.selection = list(values), -1
    def Enable(self, value=True):
        self.enabled = bool(value)
    def SetInsertionPoint(self, value):
        pass


class Frame:
    def __init__(self, root):
        self.library = SimpleNamespace(root=root)
        self.cancel_event = Event()
        self.working, self._closing = False, False
        self.calls, self.pending = [], None
        self.operation_progress = SimpleNamespace(request_cancel=Mock())
    def run_job(self, title, operation, *, done, failed, **options):
        self.working = True
        self.calls.append((title, options))
        self.pending = operation, done, failed
    def finish(self, *, deliver=True):
        operation, done, failed = self.pending
        try:
            result = operation()
        except Exception as exc:
            self.working = False
            if deliver:
                failed(exc)
        else:
            self.working = False
            if deliver:
                done(result)


def session_fixture(tmp_path):
    frame, queue, reports, closed = Frame(tmp_path), deque(), [], []
    session = ui.TranscriptionSession(frame,
        dispatch=lambda function, *args: queue.append((function, args)),
        changed=Mock(), report=reports.append, close=lambda: closed.append(True), request_stop=Mock())
    def pump():
        while queue:
            function, args = queue.popleft()
            function(*args)
    return session, frame, pump, reports, closed


def fields():
    result = {name: Control() for name in DEFAULT_SETTINGS}
    ui.apply_settings(result, dict(DEFAULT_SETTINGS))
    return result


def test_all_seventeen_native_settings_remain_typed_and_language_is_explicit():
    controls = fields()
    controls['language'].SetSelection(tuple(code for code, _ in ui._CHOICES['language']).index('uk'))
    controls['task'].SetSelection(1)
    controls['beam_size'].SetValue(4)
    controls['parallel_files'].SetValue(3)
    controls['output_vtt'].SetValue(True)
    result = ui.settings_from_controls(controls)
    assert len(result) == 17 and result['language'] == 'uk'
    assert result['task'] == 'translate' and result['translation_target'] == 'en'
    assert result['beam_size'] == 4 and type(result['parallel_files']) is int
    assert result['output_vtt'] is True and result['output_json'] is False


@pytest.mark.parametrize('name,value', [('beam_size', True), ('parallel_files', 21),
    ('model', 'unsafe model'), ('vad_filter', 'yes')])
def test_native_value_boundary_reuses_the_service_contract(name, value):
    controls = fields()
    controls[name].SetValue(value)
    with pytest.raises(ValueError):
        ui.settings_from_controls(controls)


def test_unselected_native_choice_never_borrows_a_previous_setting():
    controls = fields()
    controls['device'].SetSelection(-1)
    with pytest.raises(ValueError, match='Choose'):
        ui.settings_from_controls(controls)


def test_modal_close_requests_stop_and_waits_for_its_operation(tmp_path):
    session, frame, pump, reports, closed = session_fixture(tmp_path)
    delivered = Mock()
    def operation():
        assert session.stopped()
        raise InterruptedError('Synthetic acknowledged stop.')
    assert session.submit('Preparing', operation, delivered, kind='prepare')
    assert session.request_close() is False
    assert session.cancel.is_set() and not closed and session.busy
    session.request_stop.assert_called_once()
    frame.finish()
    pump()
    assert closed == [True] and session.closed and not session.busy
    delivered.assert_not_called()


def test_settled_worker_releases_modal_even_if_main_frame_drops_changed_root_callback(tmp_path):
    session, frame, pump, reports, closed = session_fixture(tmp_path)
    delivered = Mock()
    assert session.submit('Reading jobs', lambda: (), delivered, kind='list')
    frame.library = SimpleNamespace(root=tmp_path / 'another')
    frame.finish(deliver=False)
    assert not closed
    pump()
    assert closed == [True] and session.closed
    delivered.assert_not_called()


def test_changed_library_cannot_start_an_operation(tmp_path):
    session, frame, pump, reports, closed = session_fixture(tmp_path)
    frame.library = SimpleNamespace(root=tmp_path)
    assert session.submit('Wrong root', Mock(), Mock(), kind='run') is False
    assert not frame.calls and closed == [True]


def test_already_running_shared_operation_is_never_replaced(tmp_path):
    session, frame, pump, reports, closed = session_fixture(tmp_path)
    frame.working = True
    assert session.submit('Another', Mock(), Mock(), kind='run') is False
    assert not frame.calls and not session.busy and not closed


def test_submission_failure_unlocks_controls_and_keeps_dialog_open(tmp_path):
    session, frame, pump, reports, closed = session_fixture(tmp_path)
    frame.run_job = Mock(side_effect=RuntimeError('Synthetic executor unavailable.'))
    assert session.submit('Cannot start', Mock(), Mock(), kind='list') is False
    assert not session.busy and not closed
    assert 'executor unavailable' in reports[-1]


def test_late_callbacks_cannot_publish_into_newer_operation(tmp_path):
    session, frame, pump, reports, closed = session_fixture(tmp_path)
    first, second = Mock(), Mock()
    session.submit('First', lambda: 1, first, kind='list')
    stale = frame.pending[1]
    frame.finish()
    pump()
    session.submit('Second', lambda: 2, second, kind='list')
    stale('stale result')
    assert session.busy and first.call_args.args == (1,)
    frame.finish()
    pump()
    second.assert_called_once_with(2)


class Service:
    def __init__(self, root):
        self.library_root, self.calls, self.prepared = root, [], {}
    def prepare(self, sources, manifest, settings, *, title, cancelled):
        assert not cancelled()
        self.calls.append(('prepare', tuple(sources), manifest, dict(settings)))
        self.prepared = dict(id='a' * 32, title=title, settings=dict(settings),
            runtime_manifest_sha256='b' * 64,
            inputs=[dict(relative='Україна.wav', bytes=32)])
        return self.prepared['id']
    def definition(self, identity, *, verify_inputs, cancelled):
        assert identity == self.prepared['id'] and not verify_inputs and not cancelled()
        return self.prepared
    def run(self, identity, manifest, *, cancelled, progress):
        self.calls.append(('run', identity, manifest))
        return dict(input_count=1, output_count=1,
                    outputs=[dict(path='Україна.txt', format='txt', bytes=12)])
    def completed(self, identity, *, cancelled):
        self.calls.append(('verify', identity))
        return dict(input_count=1, output_count=1,
                    outputs=[dict(path='Україна.txt', format='txt', bytes=12)])
    def control(self, identity, action):
        self.calls.append((action, identity))


def view_fixture(tmp_path):
    frame, queue = Frame(tmp_path), deque()
    view = object.__new__(ui.TranscriptionDialog)
    view.frame, view.root, view.service = frame, tmp_path, Service(tmp_path)
    view.wx = SimpleNamespace(CallAfter=lambda callback, *args: queue.append((callback, args)))
    view.sources, view.manifest_path, view.current_job = (), '', None
    view.runtime_profile = None
    view.definition = view.summary = None
    view.rows, view.pause_requested = (), False
    view.fields = fields()
    for name in ('title', 'manifest', 'runtime_identity', 'source_list', 'jobs_list', 'outputs', 'status'):
        setattr(view, name, Control('Synthetic job' if name == 'title' else ''))
    view.buttons = {name: Control() for name in ('files', 'folder', 'remove', 'manifest', 'profiles', 'engine_settings', 'new',
        'refresh', 'load', 'prepare', 'start', 'pause', 'resume', 'stop', 'verify', 'reading', 'close')}
    view.session = ui.TranscriptionSession(frame, dispatch=view.wx.CallAfter,
        changed=view.controls, report=view.report, close=Mock(), request_stop=view._request_stop)
    def finish():
        frame.finish()
        while queue:
            callback, args = queue.popleft()
            callback(*args)
    return view, finish


def test_prepare_freezes_exact_settings_and_never_starts_worker_until_separate_action(tmp_path):
    view, finish = view_fixture(tmp_path)
    view.add_paths((tmp_path / 'Україна.wav',))
    view.manifest_path = str(tmp_path / 'selected-manifest.json')
    view.fields['beam_size'].SetValue(3)
    view.prepare()
    assert view.service.calls == []  # Submission does not run on the event thread.
    finish()
    assert [call[0] for call in view.service.calls] == ['prepare']
    assert view.current_job == 'a' * 32 and view.definition['settings']['beam_size'] == 3
    assert all(not control.enabled for control in view.fields.values())
    assert not view.buttons['prepare'].enabled and view.buttons['start'].enabled
    assert view.runtime_identity.value == 'b' * 64
    with pytest.raises(ValueError, match='New job'):
        view.add_paths((tmp_path / 'another.wav',))
    view.start()
    finish()
    assert [call[0] for call in view.service.calls] == ['prepare', 'run']
    assert view.summary['output_count'] == 1 and view.buttons['reading'].enabled


def test_new_job_reenables_settings_without_modifying_prepared_job(tmp_path):
    view, finish = view_fixture(tmp_path)
    view.add_paths((tmp_path / 'original.wav',))
    view.manifest_path = str(tmp_path / 'runtime.json')
    view.prepare()
    finish()
    original = dict(view.service.prepared['settings'])
    view.new()
    assert view.current_job is None and view.manifest_path == '' and view.sources == ()
    assert all(control.enabled for control in view.fields.values())
    assert view.service.prepared['settings'] == original
    assert ui.settings_from_controls(view.fields) == dict(DEFAULT_SETTINGS)


def test_pause_resume_and_stop_apply_only_to_the_current_running_job(tmp_path):
    view, finish = view_fixture(tmp_path)
    view.current_job = 'a' * 32
    view.session.busy, view.session.kind = True, 'prepare'
    view.pause()
    view.resume()
    assert not view.service.calls
    view.session.kind = 'run'
    view.pause()
    assert view.pause_requested and view.buttons['resume'].enabled
    view.resume()
    assert not view.pause_requested and view.buttons['pause'].enabled
    view.session.stop()
    assert [row[0] for row in view.service.calls] == ['pause', 'resume', 'stop']
    assert view.session.cancel.is_set() and not view.buttons['stop'].enabled


def test_verify_outputs_uses_existing_receipt_service_without_runtime_chooser(tmp_path):
    view, finish = view_fixture(tmp_path)
    view.current_job = 'a' * 32
    assert view.manifest_path == ''
    view.verify()
    finish()
    assert view.service.calls == [('verify', 'a' * 32)]
    assert view.summary['outputs'][0]['path'] == 'Україна.txt'


def test_stale_progress_after_completed_or_new_job_does_not_touch_status(tmp_path):
    view, finish = view_fixture(tmp_path)
    view.status.value = 'Current status'
    view._progress(0, 1, 1, 'old')
    assert view.status.value == 'Current status'
    view.session.busy, view.session.generation = True, 2
    view._progress(1, 1, 1, 'old generation')
    assert view.status.value == 'Current status'


def test_native_error_summary_does_not_include_oserror_source_filename():
    error = OSError(13, 'Permission denied', '/private/original-person.wav')
    message = ui._error_text(error)
    assert 'Permission denied' in message and 'original-person' not in message


@pytest.mark.parametrize('manager_installed', [False, True], ids=['before-keyboard', 'after-keyboard'])
def test_installation_registers_one_shared_command_without_starting_work(monkeypatch, manager_installed):
    from carr_library.ui.command_registry import CommandRegistry, CommandSpec
    registry = CommandRegistry((CommandSpec('library.existing', 'Existing', 'main', 'Ctrl+J'),))
    overrides = {'library.existing': 'Ctrl+Shift+J'}
    menu = SimpleNamespace(items=[])
    def append(identifier, caption):
        item = SimpleNamespace(identifier=100 + len(menu.items), caption=caption)
        menu.items.append(item)
        return item
    menu.Append = append
    frame = SimpleNamespace(menu_bar=SimpleNamespace(GetMenuCount=lambda: 1,
        GetMenuLabel=lambda index: '&Tools', GetMenu=lambda index: menu), Bind=Mock())
    registered = []
    if manager_installed:
        def register(spec, item):
            registry.register(spec)
            registry.validate(overrides)
            registered.append((spec, item))
        frame.keyboard_commands = SimpleNamespace(register_menu_command=register)
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_ANY=-1, EVT_MENU=object()))
    open_dialog = Mock()
    monkeypatch.setattr(ui, 'show_transcription', open_dialog)
    actions = ui.install_transcription_actions(frame)
    assert ui.install_transcription_actions(frame) is actions
    rows = registered if manager_installed else frame._pending_keyboard_commands
    assert len(menu.items) == len(rows) == 1
    assert rows[0][0].command_id == 'transcription.local' and rows[0][0].default_shortcut == ''
    assert overrides == {'library.existing': 'Ctrl+Shift+J'}
    open_dialog.assert_not_called()
    actions.open()
    open_dialog.assert_called_once_with(frame)


@pytest.mark.parametrize('failed', [False, True], ids=['verified-copy', 'changed-output'])
def test_reading_action_reuses_existing_editor_only_after_service_reverification(tmp_path, monkeypatch, failed):
    from carr_library.reader import narration
    from carr_library.ui import narration_editor
    view, finish = view_fixture(tmp_path)
    view.dialog = object()
    view.current_job = 'a' * 32
    view.summary = {'outputs': [{'path': 'Україна.txt'}]}
    view.outputs.SetSelection(0)
    original = object()
    view.service.reading_copy = Mock(side_effect=ValueError('The output changed.') if failed else None,
                                     return_value=original)
    store = object()
    factory, editor = Mock(return_value=store), Mock()
    monkeypatch.setattr(narration, 'NarrationStore', factory)
    monkeypatch.setattr(narration_editor, 'open_narration_editor', editor)
    view.reading()
    editor.assert_not_called()
    finish()
    view.service.reading_copy.assert_called_once_with('a' * 32, 'Україна.txt', cancelled=view.session.stopped)
    if failed:
        editor.assert_not_called()
        factory.assert_not_called()
    else:
        factory.assert_called_once_with(tmp_path)
        editor.assert_called_once_with(view.dialog, store, original, guard=view.session.guard)


def test_saved_runtime_profile_is_readmitted_before_prepare_and_start(tmp_path, monkeypatch):
    from carr_library import runtime_settings
    view, finish = view_fixture(tmp_path)
    view.sources = (str(tmp_path / 'selected.wav'),)
    view.manifest_path = str(tmp_path / 'installed' / 'manifest.json')
    view.runtime_profile = ('c' * 32, 'd' * 64)
    admission = Mock(return_value=SimpleNamespace(manifest=SimpleNamespace(
        path=Path(view.manifest_path), manifest_sha256='d' * 64)))
    monkeypatch.setattr(runtime_settings, 'EngineProfileStore',
                        lambda root: SimpleNamespace(admit=admission))
    view.prepare()
    finish()
    view.start()
    finish()
    assert [call[0] for call in view.service.calls] == ['prepare', 'run']
    assert admission.call_count == 2
    assert all(call.args == ('c' * 32,) and call.kwargs['engine'] == 'supplied-whisper'
               for call in admission.call_args_list)


@pytest.mark.parametrize('failure', ['disabled', 'changed-pin', 'changed-path'])
def test_changed_saved_profile_cannot_enter_supervisor(tmp_path, monkeypatch, failure):
    from carr_library import runtime_settings
    view, finish = view_fixture(tmp_path)
    view.sources = (str(tmp_path / 'selected.wav'),)
    view.manifest_path = str(tmp_path / 'installed' / 'manifest.json')
    view.runtime_profile = ('c' * 32, 'd' * 64)
    admission = Mock()
    if failure == 'disabled':
        admission.side_effect = ValueError('The selected profile is disabled.')
    else:
        admission.return_value = SimpleNamespace(manifest=SimpleNamespace(
            path=Path(view.manifest_path) if failure != 'changed-path' else tmp_path / 'other.json',
            manifest_sha256='e' * 64 if failure == 'changed-pin' else 'd' * 64))
    monkeypatch.setattr(runtime_settings, 'EngineProfileStore',
                        lambda root: SimpleNamespace(admit=admission))
    view.prepare()
    finish()
    assert view.service.calls == [] and view.current_job is None
    assert 'disabled' in view.status.value or 'selection changed' in view.status.value


def test_new_job_drops_machine_profile_selection_without_rebinding_outputs(tmp_path):
    view, finish = view_fixture(tmp_path)
    view.runtime_profile = ('c' * 32, 'd' * 64)
    view.manifest_path = str(tmp_path / 'installed.json')
    view.new()
    assert view.runtime_profile is None and not view.manifest_path


def test_saved_runtime_picker_shows_only_enabled_transcription_profiles(tmp_path, monkeypatch):
    from carr_library import runtime_settings
    view, finish = view_fixture(tmp_path)
    view.dialog = object()
    active = SimpleNamespace(id='c' * 32, name='Installed tiny', engine='supplied-whisper', enabled=True)
    disabled = SimpleNamespace(id='e' * 32, name='Disabled', engine='supplied-whisper', enabled=False)
    speech = SimpleNamespace(id='f' * 32, name='Speech voice', engine='piper', enabled=True)
    reviewed = SimpleNamespace(manifest=SimpleNamespace(
        path=tmp_path / 'installed.json', manifest_sha256='d' * 64, settings={'model': 'tiny'}))
    store = SimpleNamespace(snapshot=lambda: SimpleNamespace(profiles=(active, disabled, speech)),
                            admit=Mock(return_value=reviewed))
    monkeypatch.setattr(runtime_settings, 'EngineProfileStore', lambda root: store)
    picker = SimpleNamespace(ShowModal=lambda: 1, GetSelection=lambda: 0, Destroy=Mock())
    choices = Mock(return_value=picker)
    view.wx.SingleChoiceDialog, view.wx.ID_OK = choices, 1
    view.choose_profile()
    assert choices.call_args.args[-1] == ['Installed tiny']
    assert not view.manifest_path  # No active selection until current assets pass.
    finish()
    assert view.runtime_profile == ('c' * 32, 'd' * 64)
    assert view.fields['model'].value == 'tiny'
    assert view.manifest_path == str(reviewed.manifest.path)
    picker.Destroy.assert_called_once()
