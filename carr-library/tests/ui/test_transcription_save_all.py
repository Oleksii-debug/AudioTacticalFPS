"""Real transcript stores and worker calls behind explicit native-control doubles.

The reused child fixture implements only the ASR file protocol. These tests
do not run a Whisper model or establish native Windows/NVDA acceptance.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty, Queue
import runpy
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.ui import transcription_dialog
from carr_library.ui.save_all import SaveCoordinator


@pytest.fixture
def native_view(tmp_path, monkeypatch):
    """Same explicit control/ownership checks as the existing Save All views."""
    from threading import get_ident
    owner = get_ident()
    state = SimpleNamespace(controls=[], drive=lambda dialog: None)
    class Control:
        def __init__(self, parent=None, *args, **kwargs):
            self.parent = parent
            self.label = kwargs.get('label', '')
            self.value = kwargs.get('value', '')
            self.items = list(kwargs.get('choices', ()))
            self.name, self.selection = '', -1
            self.enabled, self.destroyed = True, False
            self.handlers = {}
            state.controls.append(self)
        def native(self):
            assert get_ident() == owner, 'A control was used from a worker.'
            current = self
            while current is not None:
                assert not getattr(current, 'destroyed', False), 'A late callback touched a destroyed view.'
                current = getattr(current, 'parent', None)
        def SetName(self, value): self.native(); self.name = value
        def SetValue(self, value): self.native(); self.value = value
        def ChangeValue(self, value): self.SetValue(value)
        def GetValue(self): self.native(); return self.value
        def SetItems(self, values): self.native(); self.items = list(values)
        def SetSelection(self, value): self.native(); self.selection = value
        def GetSelection(self): self.native(); return self.selection
        def Enable(self, value=True): self.native(); self.enabled = value
        def SetFocus(self): self.native(); assert self.enabled
        def Add(self, *args): self.native()
        def SetSizer(self, *args): self.native()
        def Bind(self, event, callback, *args, **kwargs): self.native(); self.handlers[event] = callback
        def EndModal(self, result): self.native()
        def Destroy(self): self.native(); self.destroyed = True
    class Dialog(Control):
        def ShowModal(self):
            self.native()
            state.dialog = self
            state.drive(self)
            return 5101
    class CheckBox(Control):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault('value', False)
            super().__init__(*args, **kwargs)
    wx = SimpleNamespace(Dialog=Dialog, TextCtrl=Control, StaticText=Control, Choice=Control,
        Button=Control, BoxSizer=Control, GridSizer=Control, CheckBox=CheckBox,
        MessageBox=lambda *args, **kwargs: 4, ID_OK=5100, ID_CANCEL=5101, ID_CLOSE=5102,
        NOT_FOUND=-1, YES=2, NO=4, EVT_BUTTON='button', EVT_MENU='menu',
        EVT_CHOICE='choice', EVT_TEXT='text', EVT_CLOSE='close', CallAfter=lambda *args: None)
    for name in ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL',
                 'EXPAND', 'ALL', 'TE_READONLY', 'TE_MULTILINE', 'YES_NO', 'NO_DEFAULT'):
        setattr(wx, name, 0)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    state.app = LibraryApp(tmp_path / 'library')
    state.coordinator = SaveCoordinator()
    state.parent = SimpleNamespace(library=state.app, _save_coordinator=state.coordinator,
                                   report=lambda message: None)
    state.named = lambda prefix: next(control for control in state.controls if control.name.startswith(prefix))
    return state

@pytest.fixture
def transcript_view(native_view, tmp_path, monkeypatch):
    state = native_view
    wx = sys.modules['wx']
    monkeypatch.setattr(wx, 'GridSizer', wx.BoxSizer, raising=False)
    monkeypatch.setattr(wx, 'NO_DEFAULT', 0, raising=False)
    callbacks = Queue()
    monkeypatch.setattr(wx, 'CallAfter', lambda callback, *args: callbacks.put((callback, args)))
    workspace = state.app.create_workspace('Synthetic transcript edits')
    service = state.app.transcription(workspace)
    protocol = runpy.run_path(str(Path(__file__).resolve().parents[1] /
                                 'transcription/test_supplied_worker_bridge.py'))['SYNTHETIC_WORKER']
    worker = tmp_path / 'synthetic-worker.py'
    worker.write_text(protocol.replace('__MODE__', repr('complete')), encoding='utf-8')
    models = tmp_path / 'models'
    models.mkdir()
    service.configure(Path(sys.executable).resolve(), worker, models)
    originals = [tmp_path / 'Перший.wav', tmp_path / 'Другий.wav']
    for index, source in enumerate(originals):
        source.write_bytes(f'Synthetic protocol input {index}; no acoustic inference.'.encode())
    job = service.submit(originals, title='Editable derivative versions')
    result = service.run(job, timeout=30)
    assert result['status'] == 'completed' and result['done'] == 2
    def scoped_service(requested):
        assert requested == workspace
        return service
    monkeypatch.setattr(state.app, 'transcription', scoped_service)
    frame = state.parent
    frame.active, frame.working, frame._closing = (workspace, None), False, False
    frame.cancel_event = Event()
    state.pending, state.deliveries, state.progress_calls = [], [], []
    state.start_mode = None
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='transcript-save-test')

    def run_job(label, operation, done=None, failed=None, *, with_progress=False):
        mode, state.start_mode = state.start_mode, None
        if mode == 'raise':
            raise RuntimeError('Synthetic executor admission failure.')
        if mode == 'false':
            return False
        if mode == 'none':
            return None  # Same refusal value as canonical DesktopFrame.run_job.
        assert not frame.working
        frame.working = True
        frame.cancel_event.clear()
        state.pending.append((label, operation, done, failed, with_progress))
        return None  # Accepted canonical jobs also return None.

    frame.run_job = run_job

    def flush_ui():
        while True:
            try:
                callback, args = callbacks.get_nowait()
            except Empty:
                return
            callback(*args)

    def execute():
        label, operation, done, failed, with_progress = state.pending.pop(0)
        def observer(*values):
            state.progress_calls.append(values)
        future = pool.submit(operation, observer) if with_progress else pool.submit(operation)
        try:
            result = future.result(timeout=30)
        except Exception as error:
            callback, value = failed, error
        else:
            callback, value = done, result
        def deliver():
            frame.working = False
            if not frame._closing:
                callback(value)
        state.deliveries.append(deliver)

    def deliver():
        flush_ui()
        state.deliveries.pop(0)()
        flush_ui()

    def finish():
        execute()
        deliver()

    def ready():
        assert len(state.pending) == 1
        finish()
        assert state.named('Текст:').enabled

    def action(name):
        control = state.named(name)
        assert control.enabled, name
        return control.handlers['button'](SimpleNamespace())

    def edit(value):
        control = state.named('Текст:')
        control.SetValue(value)
        control.handlers['text'](SimpleNamespace(Skip=lambda: None))

    state.workspace, state.transcription, state.job = workspace, service, job
    state.originals, state.recognized = originals, service.text(job, result['sources'][0]['filename'])
    state.execute, state.deliver, state.finish = execute, deliver, finish
    state.flush_ui = flush_ui
    state.ready, state.action, state.edit = ready, action, edit
    state.open = lambda: transcription_dialog.show_transcription(frame)
    try:
        yield state
    finally:
        frame.cancel_event.set()
        pool.shutdown(wait=True, cancel_futures=True)


def test_tasks_handoff_selects_requested_saved_job_without_running_it(transcript_view, monkeypatch):
    state = transcript_view
    newer = state.transcription.submit(state.originals, title='Newer queued job')
    def no_recognition(*args, **kwargs):
        pytest.fail('Opening a saved job must not start or resume recognition.')
    monkeypatch.setattr(state.transcription, 'run', no_recognition)
    originals = {path: path.read_bytes() for path in state.originals}
    def drive(dialog):
        state.ready()
        jobs = state.named('Збережені завдання')
        assert jobs.GetSelection() == 1
        assert jobs.items[jobs.GetSelection()].startswith('Editable derivative versions — completed')
        assert state.named('Текст:').GetValue() == state.recognized
    state.drive = drive
    transcription_dialog.show_transcription(state.parent, job_id=state.job)
    assert state.transcription.get(newer)['status'] == 'ready'
    assert not (state.transcription._path(newer) / 'job_config.json').exists()
    assert {path: path.read_bytes() for path in originals} == originals


def test_tasks_handoff_rejects_archived_library_before_constructing_editor(transcript_view):
    state = transcript_view
    state.app.archive_workspace(state.workspace)
    with pytest.raises(ValueError, match='archived'):
        transcription_dialog.show_transcription(state.parent, job_id=state.job)
    assert not state.controls and not state.pending


@pytest.fixture
def folder_view(transcript_view, tmp_path, monkeypatch):
    state = transcript_view
    wx = sys.modules['wx']
    folder = tmp_path / 'Папка записів'
    folder.mkdir()
    for name in ('b.wav', 'a.MP3', 'notes.txt'):
        (folder / name).write_bytes(('Synthetic folder input ' + name).encode('utf-8'))
    state.folder, state.picker_result = folder, wx.ID_OK
    class Picker:
        def __init__(self, parent, message, **kwargs):
            assert parent is state.dialog
            assert 'без підпапок' in message
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ShowModal(self): return state.picker_result
        def GetPath(self): return str(state.folder)
    monkeypatch.setattr(wx, 'DirDialog', Picker, raising=False)
    monkeypatch.setattr(wx, 'DD_DIR_MUST_EXIST', 0, raising=False)
    return state


def test_named_folder_action_scans_and_copies_in_shared_worker_with_captured_options(folder_view, monkeypatch):
    from threading import get_ident
    from carr_library import transcription as module
    state, owner = folder_view, get_ident()
    original = {path.name: path.read_bytes() for path in state.folder.iterdir()}
    scanned, copied = [], []
    actual_scan, actual_copy = module.os.scandir, module._copy
    def scan(path):
        if isinstance(path, (str, Path)) and Path(path) == state.folder:
            scanned.append(get_ident())
            assert get_ident() != owner
        return actual_scan(path)
    def copy(*args, **kwargs):
        copied.append(get_ident())
        assert get_ident() != owner
        return actual_copy(*args, **kwargs)
    monkeypatch.setattr(module.os, 'scandir', scan)
    monkeypatch.setattr(module, '_copy', copy)
    def drive(dialog):
        state.ready()
        button = state.named('Додати папку записів')
        assert button.enabled and button.name == 'Додати папку записів'
        state.named('Назва нового завдання').SetValue('Captured folder title')
        state.named('Мова:').SetValue('uk')
        state.action('Додати папку записів')
        assert state.parent.working and state.pending[0][-1] is True
        assert state.named('Зупинити').enabled and not button.enabled
        assert not scanned and not copied
        assert len(state.transcription.list()) == 1
        state.named('Назва нового завдання').SetValue('Later control text')
        state.named('Мова:').SetValue('en')
        state.finish()
        created = [job for job in state.transcription.list() if job['id'] != state.job]
        assert len(created) == 1, state.named('Стан транскрипції').GetValue()
        job, = created
        assert job['title'] == 'Captured folder title' and job['settings']['language'] == 'uk'
        assert job['workspace'] == state.workspace and job['status'] == 'ready'
        assert [row['original_name'] for row in job['sources']] == ['a.MP3', 'b.wav']
        assert scanned and len(copied) == 2
        assert state.progress_calls[0][:2] == (None, None)
        assert state.progress_calls[-1][:2] == (2, 2)
        assert not state.parent.working and button.enabled
        assert not state.named('Зупинити').enabled
    state.drive = drive
    state.open()
    with actual_scan(state.folder) as entries:
        assert {entry.name: Path(entry.path).read_bytes() for entry in entries} == original


@pytest.mark.parametrize('stop', ['button', 'scope-change'])
def test_folder_handler_cancellation_and_scope_change_publish_no_job(folder_view, stop):
    state = folder_view
    other = state.app.create_workspace('Different active library') if stop == 'scope-change' else None
    original = {path.name: path.read_bytes() for path in state.folder.iterdir()}
    def drive(dialog):
        state.ready()
        state.action('Додати папку записів')
        assert len(state.pending) == 1 and state.parent.working
        if stop == 'button':
            state.action('Зупинити')
        else:
            state.parent.active = (other, None)
        state.finish()
        assert [job['id'] for job in state.transcription.list()] == [state.job]
        assert not list(state.transcription.jobs_root.glob('.pending-*'))
        assert not state.parent.working
        if stop == 'button':
            assert 'cancelled' in state.named('Стан транскрипції').GetValue()
            assert state.named('Додати папку записів').enabled
    state.drive = drive
    state.open()
    assert {path.name: path.read_bytes() for path in state.folder.iterdir()} == original


@pytest.mark.parametrize('rejected', ['empty', 'linked-folder'])
def test_folder_handler_reports_real_service_rejection_without_partial_job(folder_view, tmp_path, rejected):
    state = folder_view
    selected = tmp_path / 'Selected invalid folder'
    if rejected == 'empty':
        selected.mkdir()
    else:
        selected.symlink_to(state.folder, target_is_directory=True)
    state.folder = selected
    def drive(dialog):
        state.ready()
        state.action('Додати папку записів')
        state.finish()
        assert [job['id'] for job in state.transcription.list()] == [state.job]
        assert not list(state.transcription.jobs_root.glob('.pending-*'))
        message = state.named('Стан транскрипції').GetValue()
        assert ('no supported' in message) if rejected == 'empty' else ('links' in message)
        assert state.named('Додати папку записів').enabled
    state.drive = drive
    state.open()


def test_dismissed_folder_picker_does_not_start_background_work(folder_view):
    state = folder_view
    state.picker_result = sys.modules['wx'].ID_CANCEL
    def drive(dialog):
        state.ready()
        state.action('Додати папку записів')
        assert not state.pending and not state.parent.working
        assert [job['id'] for job in state.transcription.list()] == [state.job]
        assert state.named('Додати папку записів').enabled
    state.drive = drive
    state.open()


@pytest.mark.parametrize('newer_edit', [False, True])
def test_save_all_waits_for_actual_edit_revision_and_retains_newer_text(transcript_view, newer_edit):
    state = transcript_view
    originals = [path.read_bytes() for path in state.originals]
    def drive(dialog):
        state.ready()
        state.edit('Збережений знімок правок.')
        assert state.action('Зберегти всі відкриті правки')
        assert state.coordinator.busy and state.parent.working
        assert state.transcription.edits(state.job) == ()
        if newer_edit:
            state.edit('Новіші правки ще у вікні.')
        state.finish()
        edit, = state.transcription.edits(state.job)
        assert edit['text'] == 'Збережений знімок правок.'
        assert not state.coordinator.busy
        status = state.named('Стан транскрипції').GetValue()
        assert ('Unsaved changes remain' in status) if newer_edit else status == 'All 1 open drafts saved.'
        assert state.named('Зберегти правки').enabled is newer_edit
        assert state.named('Текст:').GetValue() == (
            'Новіші правки ще у вікні.' if newer_edit else 'Збережений знімок правок.')
        assert state.transcription.text(state.job, edit['source']) == state.recognized
        if not newer_edit:
            assert state.action('Зберегти всі відкриті правки')
            assert not state.pending and len(state.transcription.edits(state.job)) == 1
    state.drive = drive
    state.open()
    assert not state.coordinator.participants and not state.coordinator.busy
    assert [path.read_bytes() for path in state.originals] == originals


def test_reverting_to_loaded_text_is_clean_without_another_version(transcript_view):
    state = transcript_view
    def drive(dialog):
        state.ready()
        original = state.named('Текст:').GetValue()
        state.edit('Тимчасова правка.')
        state.edit(original)
        assert not state.named('Зберегти правки').enabled
        assert state.action('Зберегти всі відкриті правки')
        assert not state.pending and state.transcription.edits(state.job) == ()
        assert state.named('Стан транскрипції').GetValue().startswith('No unsaved drafts.')
    state.drive = drive
    state.open()


@pytest.mark.parametrize('admission', ['false', 'none', 'raise'])
def test_save_all_admission_failure_always_releases_completion(transcript_view, admission):
    state = transcript_view
    def drive(dialog):
        state.ready()
        state.edit('Правки залишаються доступними.')
        state.start_mode = admission
        assert state.action('Зберегти всі відкриті правки')
        assert not state.pending and not state.coordinator.busy
        assert state.named('Текст:').GetValue() == 'Правки залишаються доступними.'
        assert state.named('Зберегти правки').enabled
        assert state.transcription.edits(state.job) == ()
        assert 'Unsaved changes remain' in state.named('Стан транскрипції').GetValue()
    state.drive = drive
    state.open()


@pytest.mark.parametrize('failure', ['worker', 'cancel'])
def test_save_all_worker_failure_retains_draft_and_releases_completion(transcript_view, monkeypatch, failure):
    state = transcript_view
    if failure == 'worker':
        def unavailable(*args, **kwargs):
            raise OSError('Synthetic storage failure before publication.')
        monkeypatch.setattr(state.transcription, 'save_edit', unavailable)
    def drive(dialog):
        state.ready()
        state.edit('Незбережена редакція.')
        assert state.action('Зберегти всі відкриті правки')
        if failure == 'cancel':
            state.action('Зупинити')
        state.finish()
        assert not state.coordinator.busy
        assert state.transcription.edits(state.job) == ()
        assert state.named('Текст:').GetValue() == 'Незбережена редакція.'
        assert state.named('Зберегти правки').enabled
        assert 'Unsaved changes remain' in state.named('Стан транскрипції').GetValue()
    state.drive = drive
    state.open()


def test_save_all_completion_callback_error_does_not_deadlock(transcript_view, monkeypatch):
    state = transcript_view
    def drive(dialog):
        state.ready()
        state.edit('Записана перед помилкою інтерфейсу версія.')
        status = state.named('Стан транскрипції')
        change, called = status.ChangeValue, []
        def fail_once(value):
            if not called:
                called.append(True)
                raise RuntimeError('Synthetic native status failure.')
            change(value)
        monkeypatch.setattr(status, 'ChangeValue', fail_once)
        assert state.action('Зберегти всі відкриті правки')
        state.finish()
        assert called and not state.coordinator.busy
        edit, = state.transcription.edits(state.job)
        assert edit['text'] == 'Записана перед помилкою інтерфейсу версія.'
        assert 'Unsaved changes remain' in status.GetValue()
    state.drive = drive
    state.open()


@pytest.mark.parametrize('scope_change', ['workspace', 'library'])
def test_scope_switch_cancels_captured_save_without_writing_other_library(transcript_view, tmp_path, scope_change):
    state = transcript_view
    def drive(dialog):
        state.ready()
        state.edit('Правки належать початковій бібліотеці.')
        assert state.action('Зберегти всі відкриті правки')
        if scope_change == 'workspace':
            state.parent.active = (state.app.create_workspace('Other library'), None)
        else:
            state.parent.library = LibraryApp(tmp_path / 'other-data-root')
        state.finish()
        assert not state.coordinator.busy
        assert state.transcription.edits(state.job) == ()
        assert state.named('Текст:').GetValue() == 'Правки належать початковій бібліотеці.'
    state.drive = drive
    state.open()


def test_stale_selected_source_never_receives_an_old_save_baseline(transcript_view, monkeypatch):
    state = transcript_view
    def drive(dialog):
        state.ready()
        state.edit('Правки першого запису.')
        assert state.action('Зберегти всі відкриті правки')
        # An already queued selection event can arrive after controls were disabled.
        monkeypatch.setattr(sys.modules['wx'], 'MessageBox', lambda *a, **k: sys.modules['wx'].YES)
        sources = state.named('Запис для редагування')
        sources.SetSelection(1)
        sources.handlers['choice'](SimpleNamespace())
        state.finish()
        assert not state.coordinator.busy
        edit, = state.transcription.edits(state.job)
        assert edit['text'] == 'Правки першого запису.'
        assert state.named('Текст:').GetValue() == ''
        assert not state.named('Зберегти правки').enabled
        assert 'Unsaved changes remain' in state.named('Стан транскрипції').GetValue()
    state.drive = drive
    state.open()


@pytest.mark.parametrize('commit_first', [False, True])
@pytest.mark.parametrize('parent_closing', [False, True])
def test_closed_modal_releases_captured_coordinator_before_late_callback(transcript_view, commit_first, parent_closing):
    state = transcript_view
    successor = SaveCoordinator()
    captured_key = []
    def drive(dialog):
        state.ready()
        state.edit('Окрема версія перед закриттям.')
        assert state.action('Зберегти всі відкриті правки')
        if commit_first:
            state.execute()  # Durable receipt exists; its UI delivery is held.
        key, = state.coordinator.participants
        captured_key.append(key)
        successor.register(key, 'Another owner', dirty=lambda: False, save=lambda callback: None)
        state.parent._save_coordinator = successor
        state.parent._closing = parent_closing
        dialog.EndModal(sys.modules['wx'].ID_CLOSE)
    state.drive = drive
    state.open()
    assert not state.coordinator.busy and not state.coordinator.participants
    assert list(successor.participants) == captured_key
    if not commit_first:
        state.execute()
    state.deliver()
    assert len(state.transcription.edits(state.job)) == int(commit_first)
    assert not successor.busy


@pytest.mark.parametrize('indeterminate', [False, True])
def test_run_uses_shared_progress_facade_and_keeps_reported_count_explanation(transcript_view,
                                                                           monkeypatch, indeterminate):
    import carr_library.transcription as transcription

    if indeterminate:
        monkeypatch.setattr(transcription, '_READ_LIVE_WORKER_STATUS', False)
    state = transcript_view
    class NoDirectProgressAccess:
        @property
        def event(self):
            raise AssertionError('The UI must use the shared progress facade.')
    state.parent.operation_progress = NoDirectProgressAccess()
    fresh = state.transcription.submit(state.originals, title='Run through shared facade')
    def drive(dialog):
        assert not state.pending  # The fresh job has no transcript to read yet.
        assert state.named('Збережені завдання').items[0].startswith('Run through shared facade')
        status = state.named('Стан транскрипції')
        shown = []
        actual_set_value = status.SetValue
        def capture(value):
            shown.append(value)
            actual_set_value(value)
        monkeypatch.setattr(status, 'SetValue', capture)
        state.action('Запустити / продовжити')
        assert state.pending[0][-1] is True
        state.execute()
        state.flush_ui()
        assert 'Кількість файлів повідомляє рушій' in state.named('Стан транскрипції').GetValue()
        if not transcription._READ_LIVE_WORKER_STATUS:
            assert state.progress_calls[0][:2] == (None, None)
            assert any('Поточна кількість оброблених файлів невідома' in value for value in shown)
            assert all('None' not in value for value in shown)
        state.deliver()
        assert state.progress_calls
        if state.pending:  # The completed job refreshes its verified transcript.
            state.finish()
        assert state.transcription.verify(fresh)['status'] == 'completed'
        assert not state.parent.working
    state.drive = drive
    state.open()


def test_without_installed_coordinator_disables_save_all_but_keeps_manual_save(transcript_view):
    state = transcript_view
    state.parent._save_coordinator = None
    def drive(dialog):
        state.ready()
        assert not state.named('Зберегти всі відкриті правки').enabled
        state.edit('Звичайне збереження без загального координатора.')
        state.action('Зберегти правки')
        state.finish()
        edit, = state.transcription.edits(state.job)
        assert edit['text'] == 'Звичайне збереження без загального координатора.'
        assert not state.named('Зберегти правки').enabled
    state.drive = drive
    state.open()
    assert not state.coordinator.participants and not state.coordinator.busy


def test_transcription_command_uses_library_menu_after_file_menu_insertion(monkeypatch):
    wx = SimpleNamespace(ID_ANY=-1, NOT_FOUND=-1, EVT_MENU=object())
    monkeypatch.setitem(sys.modules, 'wx', wx)
    class Menu:
        def __init__(self): self.labels = []
        def Append(self, identity, label):
            self.labels.append(label)
            return object()
    file_menu, library_menu = Menu(), Menu()
    menus = SimpleNamespace(FindMenu=lambda label: {'File': 0, 'Library': 1}.get(label, -1),
                            GetMenu=lambda index: (file_menu, library_menu)[index])
    frame = SimpleNamespace(menu_bar=menus, Bind=lambda *args: None)
    transcription_dialog.install_transcription_actions(frame)
    assert file_menu.labels == []
    assert library_menu.labels == ['Transcription: local audio and video...']
