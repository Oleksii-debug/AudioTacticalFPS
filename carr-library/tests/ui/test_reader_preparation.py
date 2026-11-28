"""Faithful modal lifetime plus actual borrowed-thread preparation and source guards."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import sys
import threading

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.speech.reading_tools import ReadingSnapshot
from carr_library.ui.reader_preparation import ReaderPreparationDialog
from carr_library.ui.keyboard_settings import voice_reader_commands


class Host:
    def __init__(self):
        self.document = ReadingDocument('😀 12 + 3')
        self.range = (0, 0)
        self.main = threading.get_ident()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.future = self.callbacks = None
        self.running = self.dead = self.revoked = self.stopped = False
        self.applied = []
        self.reports = []

    def gui(self):
        assert threading.get_ident() == self.main, 'Native document callbacks must stay on the GUI thread.'

    def guard(self):
        self.gui()
        if self.revoked or self.dead:
            raise ValueError('The current reading source was revoked.')

    def capture(self):
        self.guard()
        return ReadingSnapshot.capture(self.document)

    def selection(self):
        self.gui()
        return self.range

    def select(self, start, end):
        self.gui()
        self.range = start, end

    def verify(self, snapshot):
        self.guard()
        snapshot.check(self.document)

    def apply(self, snapshot, start, end, replacement, *, label):
        self.verify(snapshot)
        self.applied.append((start, end, replacement))
        return self.document.replace(start, end, replacement, label=label)

    def submit(self, work, done, *, on_error):
        self.gui()
        if self.running:
            return False
        self.running = True
        self.stopped = False
        self.callbacks = done, on_error
        self.future = self.pool.submit(work)
        return True

    def finish(self):
        self.gui()
        done, error = self.callbacks
        self.callbacks = None
        self.running = False
        try:
            value = self.future.result(timeout=5)
        except Exception as exc:
            error(exc)
        else:
            done(value)

    def alive(self):
        return not self.dead

    def busy(self):
        return self.running

    def cancelled(self):
        return self.stopped

    def report(self, text, *, focus=False):
        self.gui()
        self.reports.append(text)

    def voice(self):
        pytest.fail('Voice language must not silently choose a preparation profile.')


@pytest.fixture
def view(monkeypatch):
    host = Host()
    wx = SimpleNamespace(on_show=lambda dialog: None)
    for index, name in enumerate(('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL',
            'HORIZONTAL', 'TE_MULTILINE', 'TE_READONLY', 'HSCROLL', 'EXPAND', 'ALL',
            'LEFT', 'RIGHT', 'TOP')):
        setattr(wx, name, 1 << index)
    wx.ID_ANY, wx.ID_CANCEL = -1, 5101
    wx.EVT_CHOICE, wx.EVT_CHECKBOX, wx.EVT_BUTTON = 'choice', 'check', 'button'
    wx.EVT_CLOSE, wx.EVT_WINDOW_DESTROY = 'close', 'destroy'
    counter = iter(range(100, 10000))

    class Widget:
        def __init__(self, parent=None, *args, value='', choices=(), label='', **kwargs):
            self.parent, self.value, self.choices, self.label = parent, value, list(choices), label
            self.id = args[0] if args and isinstance(args[0], int) and args[0] != -1 else next(counter)
            self.dead = False
            self.enabled = True
            self.selection = 0
            self.name = ''
            self.bindings = {}
            self.destroy_calls = 0

        def live(self):
            host.gui()
            node = self
            while isinstance(node, Widget):
                assert not node.dead, 'A callback touched a deleted native wrapper.'
                node = node.parent

        def SetName(self, name):
            self.live(); self.name = name
        def GetId(self):
            self.live(); return self.id
        def SetValue(self, value):
            self.live(); self.value = value
        def ChangeValue(self, value):
            self.SetValue(value)
        def GetValue(self):
            self.live(); return self.value
        def SetSelection(self, value):
            self.live(); self.selection = value
        def GetSelection(self):
            self.live(); return self.selection
        def Enable(self, value=True):
            self.live(); self.enabled = value
        def SetFocus(self):
            self.live()
        def Bind(self, event, handler):
            self.live(); self.bindings[event] = handler
        def SetSizer(self, value):
            self.live()
        def SetEscapeId(self, value):
            self.live()
        def EndModal(self, value):
            self.live(); self.modal_result = value
        def ShowModal(self):
            self.live(); wx.on_show(self); return wx.ID_CANCEL
        def Destroy(self):
            self.live()
            self.destroy_calls += 1
            self.dead = True
            # Real wx/SIP can invalidate the original wrapper before this event.
            event = SimpleNamespace(GetId=lambda: self.id, Skip=lambda: None,
                GetEventObject=lambda: pytest.fail('Never inspect the deleted event object.'))
            callback = self.bindings.get(wx.EVT_WINDOW_DESTROY)
            if callback:
                callback(event)

    class Sizer:
        def __init__(self, *args, **kwargs): pass
        def Add(self, *args, **kwargs): host.gui()

    for name in ('Dialog', 'TextCtrl', 'StaticText', 'Choice', 'CheckBox', 'Button'):
        setattr(wx, name, Widget)
    wx.BoxSizer = wx.WrapSizer = Sizer
    monkeypatch.setitem(sys.modules, 'wx', wx)
    controller = ReaderPreparationDialog(None, host)
    try:
        yield host, controller, wx
    finally:
        if not controller.destroyed:
            controller.dialog.Destroy()
        host.pool.shutdown(wait=True, cancel_futures=True)


def choose(controller, language=1):
    controller.language.SetSelection(language)
    controller.options['numbers'].SetValue(True)
    controller.changed()


def test_language_and_transformations_require_explicit_choices(view):
    host, controller, _ = view
    assert controller.language.GetSelection() == 0
    assert not any(field.GetValue() for field in controller.options.values())
    assert not controller.preview_button.enabled and not controller.apply_button.enabled
    assert controller.preview() is False and host.future is None
    assert host.document.text == '😀 12 + 3' and not host.applied


def test_real_borrowed_worker_previews_then_one_explicit_apply_and_undo(view, monkeypatch):
    host, controller, _ = view
    import carr_library.ui.reader_preparation as module
    original = module.preview_preparation
    threads = []
    def observed(*args, **kwargs):
        threads.append(threading.get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'preview_preparation', observed)
    choose(controller, language=2)
    assert controller.preview()
    host.finish()
    assert threads and all(thread != host.main for thread in threads)
    assert host.document.text == '😀 12 + 3' and not host.applied
    assert controller.apply_button.enabled
    controller.accept()
    assert host.document.text == '😀 один два + три'
    assert len(host.applied) == 1 and len(host.document._edits) == 1
    assert host.document.source_text == '😀 12 + 3'
    assert host.document.undo() and host.document.text == '😀 12 + 3'


@pytest.mark.parametrize('mutation', ['document-edit', 'same-text-other-tab', 'source-revocation'])
def test_stale_worker_result_cannot_be_accepted(view, mutation):
    host, controller, _ = view
    choose(controller)
    assert controller.preview()
    if mutation == 'document-edit':
        host.document.set_text('Other 45')
    elif mutation == 'same-text-other-tab':
        host.document = ReadingDocument(host.document.text)
    else:
        host.revoked = True
    expected = host.document.to_dict()
    host.finish()
    assert controller.session.preview is None and not controller.apply_button.enabled
    controller.accept()
    assert host.document.to_dict() == expected and not host.applied


def test_open_dialog_cannot_adopt_another_identical_tab_before_preview(view):
    host, controller, _ = view
    choose(controller)
    host.document = ReadingDocument(host.document.text)
    assert controller.preview() is False and host.future is None and not host.applied
    assert 'changed' in controller.status.value


def test_option_change_invalidates_queued_completion(view):
    host, controller, _ = view
    choose(controller)
    assert controller.preview()
    controller.options['symbols'].SetValue(True)
    controller.changed()
    host.finish()
    assert controller.session.preview is None and not controller.apply_button.enabled
    assert not host.applied and host.document.text == '😀 12 + 3'


def test_reject_and_cancel_never_apply_a_preview(view):
    host, controller, _ = view
    choose(controller)
    assert controller.preview()
    host.finish()
    controller.reject()
    controller.accept()
    assert not host.applied
    assert controller.preview()
    host.finish()
    controller.cancel()
    controller.accept()
    assert not host.applied and host.document.text == '😀 12 + 3'
    assert not host.stopped  # A tool does not clear or invent the owner's stop state.


def test_direct_destroy_revokes_exact_queued_result_without_native_calls(view):
    host, controller, wx = view
    choose(controller)
    assert controller.preview()
    host.future.result(timeout=5)  # The real result exists, GUI delivery is held.
    wx.on_show = lambda dialog: dialog.Destroy()
    controller.show()
    assert controller.destroyed and controller.session.closed
    host.finish()
    assert controller.dialog.destroy_calls == 1
    assert controller.session.preview is None and not host.applied


def test_child_destroy_event_does_not_close_the_modal_owner(view):
    _, controller, _ = view
    seen = []
    event = SimpleNamespace(GetId=lambda: controller._window_id + 1, Skip=lambda: seen.append('skip'),
                            GetEventObject=lambda: pytest.fail('No native owner dereference required.'))
    controller._destroyed(event)
    assert seen == ['skip'] and not controller.destroyed and not controller.session.closed


def test_close_while_worker_pending_does_not_transfer_or_apply_result(view):
    host, controller, _ = view
    choose(controller)
    assert controller.preview()
    controller.close()
    host.finish()
    assert controller.session.closed and not host.applied
    assert host.document.text == '😀 12 + 3'


def test_selected_scope_and_second_explicit_preview_use_current_derivative(view):
    host, controller, _ = view
    controller.scope = (2, 4)
    controller.selected_only.SetValue(True)
    choose(controller)
    assert controller.preview()
    host.finish()
    controller.accept()
    assert host.document.text == '😀 one two + 3'
    assert host.range == (2, 9)
    controller.selected_only.SetValue(False)
    controller.changed()
    assert controller.preview()
    host.finish()
    controller.accept()
    assert host.document.text == '😀 one two + three'
    assert len(host.applied) == 2


def test_existing_busy_worker_is_not_replaced(view):
    host, controller, _ = view
    choose(controller)
    host.running = True
    assert controller.preview() is False
    assert host.future is None and not host.applied


def test_preparation_is_one_empty_default_action_in_the_existing_voice_reader_registry():
    commands = voice_reader_commands()
    selected = [command for command in commands if command.identity == 'voice_reader.prepare']
    assert len(selected) == 1 and selected[0].shortcut == ''
    assert selected[0].context == 'voice_reader'
    assert next(command for command in commands if command.identity == 'voice_reader.read').shortcut == 'F6'


class HistoryText:
    """TextCtrl.ChangeValue has no native Undo entry; only model history is used."""
    def __init__(self, owner, value=''):
        self.owner, self.value, self.reads = owner, value, 0

    def GetValue(self):
        assert not self.owner.dead_controls, 'A destroyed native editor was read.'
        self.reads += 1
        return self.value

    def ChangeValue(self, value):
        assert not self.owner.dead_controls, 'A destroyed native editor was written.'
        self.value = value

    def SetFocus(self):
        assert not self.owner.dead_controls, 'A destroyed native editor was focused.'
        self.owner.focus = self

    def Undo(self):
        pytest.fail('Reading edits must not use a second, native-only history.')

    Redo = Undo


class HistoryKey:
    def __init__(self, key, modifiers=2):
        self.key, self.modifiers, self.skipped = key, modifiers, False

    def GetKeyCode(self): return self.key
    def GetModifiers(self): return self.modifiers
    def Skip(self): self.skipped = True


def history_binding(state):
    """Execute the installed reader's actual handlers over the real document model."""
    import ast
    from pathlib import Path
    from carr_library.ui import speech_dialog
    source = Path(speech_dialog.__file__)
    module = ast.parse(source.read_text(encoding='utf-8'))
    reader = next(node for node in module.body
                  if isinstance(node, ast.FunctionDef) and node.name == 'show_speech_reader')
    names = {'sync_document', 'history', 'history_key'}
    handlers = [node for node in reader.body
                if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in handlers} == names
    wrapper = ast.parse("""
def bind(state):
    document, editor = state.document, state.editor
    source_status, recovery_notice = state.source_status, ''
    alive, dialog_destroyed, busy, source_loaded = True, False, False, True
    playback_generation = 10
    last_audio = last_reading_audio = state.old_audio
    player, progress = state.player, state.progress
    playback_guard, report, wx = state.guard, state.report, state.wx

    def change(*, document_value=None, editor_value=None, alive_value=None,
               destroyed_value=None, busy_value=None, loaded_value=None):
        nonlocal document, editor, alive, dialog_destroyed, busy, source_loaded
        if document_value is not None: document = document_value
        if editor_value is not None: editor = editor_value
        if alive_value is not None: alive = alive_value
        if destroyed_value is not None: dialog_destroyed = destroyed_value
        if busy_value is not None: busy = busy_value
        if loaded_value is not None: source_loaded = loaded_value

    def retained():
        return playback_generation, last_audio, last_reading_audio

    return SimpleNamespace(key=history_key, history=history, change=change, retained=retained)
""")
    wrapper.body[0].body[-1:-1] = handlers
    namespace = {'SimpleNamespace': SimpleNamespace, '__name__': speech_dialog.__name__,
                 '__package__': speech_dialog.__package__}
    exec(compile(ast.fix_missing_locations(wrapper), str(source), 'exec'), namespace)
    return namespace['bind'](state)


@pytest.fixture
def history_view():
    state = SimpleNamespace(document=ReadingDocument('😀 Original source 12 + 3'),
        dead_controls=False, revoked=False, guards=0, messages=[], stops=0,
        refreshes=0, stop_error=None, old_audio=object(), focus=None)
    state.editor = HistoryText(state, state.document.text)
    state.source_status = HistoryText(state)
    state.focus = state.editor

    def guard():
        state.guards += 1
        if state.revoked:
            raise ValueError('The retained reading source was revoked.')

    def stop():
        state.stops += 1
        if state.stop_error is not None:
            raise state.stop_error

    def refresh():
        state.refreshes += 1

    state.guard = guard
    state.report = lambda value, **kwargs: state.messages.append(value)
    state.player = SimpleNamespace(stop=stop)
    state.progress = SimpleNamespace(refresh_audio_state=refresh)
    state.wx = SimpleNamespace(MOD_CONTROL=2, Window=SimpleNamespace(FindFocus=lambda: state.focus))
    state.handlers = history_binding(state)
    return state


def test_native_undo_redo_reuses_one_accepted_reading_edit_and_retains_original(history_view):
    value = history_view
    document = value.document
    original = document.source_text
    assert document.replace(18, 20, 'twelve', label='Accepted explicit number preview')
    accepted = document.text
    value.editor.ChangeValue(accepted)
    undo, redo = HistoryKey(ord('Z')), HistoryKey(ord('Y'))
    value.handlers.key(undo)
    assert not undo.skipped and document.text == original and value.editor.value == original
    assert document is value.document and document.source_text == original
    assert value.handlers.retained() == (11, None, None)
    value.handlers.key(redo)
    assert not redo.skipped and document.text == accepted and value.editor.value == accepted
    assert document.source_text == original and value.handlers.retained() == (12, None, None)
    assert value.stops == value.refreshes == 2 and value.guards == 2
    # One more Redo is a no-op, without another player action or history record.
    assert value.handlers.history(forward=True) is False
    assert value.stops == 2 and document.text == accepted
    assert document.undo() and document.text == original and not document.can_undo


def test_keyboard_undo_first_captures_current_typed_text_in_the_same_history(history_view):
    value = history_view
    original = value.document.text
    value.editor.value = original + ' A typed continuation.'
    value.handlers.key(HistoryKey(ord('Z')))
    assert value.document.text == original and value.editor.value == original
    value.handlers.key(HistoryKey(ord('Y')))
    assert value.document.text == original + ' A typed continuation.'
    assert value.document.source_text == original


def test_history_key_tracks_the_current_manual_tab_without_touching_an_inactive_copy(history_view):
    value = history_view
    value.document.replace(0, 1, 'First edited')
    first = value.document.text
    second = ReadingDocument('Second retained source')
    second.replace(0, 6, 'Changed')
    editor = HistoryText(value, second.text)
    value.handlers.change(document_value=second, editor_value=editor)
    value.focus = editor
    value.handlers.key(HistoryKey(ord('Z')))
    assert second.text == 'Second retained source' and editor.value == second.text
    assert value.document.text == first and value.editor.reads == 0


@pytest.mark.parametrize('key,modifiers', [(ord('Z'), 0), (ord('Z'), 3), (ord('Z'), 6),
    (ord('Y'), 10), (ord('A'), 2), (ord('S'), 2), (13, 0), (322, 0)])
def test_unrelated_and_modified_chords_keep_the_native_editing_path(history_view, key, modifiers):
    value = history_view
    event = HistoryKey(key, modifiers)
    value.handlers.key(event)
    assert event.skipped and not value.guards and not value.editor.reads and not value.stops


@pytest.mark.parametrize('nested', ['preparation-field', 'search-field', 'another-reader'])
def test_history_chords_in_other_controls_never_dispatch_the_readers_history(history_view, nested):
    value = history_view
    value.focus = HistoryText(value, nested)
    event = HistoryKey(ord('Z'))
    value.handlers.key(event)
    assert event.skipped and not value.guards and not value.editor.reads and not value.stops


@pytest.mark.parametrize('condition', ['busy', 'loading', 'revoked'])
def test_fixed_history_chord_does_not_bypass_current_reader_admission(history_view, condition):
    value = history_view
    value.document.replace(0, 1, 'Accepted')
    value.editor.value = value.document.text
    before = value.document.to_dict()
    if condition == 'busy':
        value.handlers.change(busy_value=True)
    elif condition == 'loading':
        value.handlers.change(loaded_value=False)
    else:
        value.revoked = True
    event = HistoryKey(ord('Z'))
    value.handlers.key(event)
    assert not event.skipped, 'Denied reading history must not fall through to a native-only Undo.'
    assert value.document.to_dict() == before and value.editor.reads == 0
    assert not value.stops and value.messages
    assert value.handlers.retained() == (10, value.old_audio, value.old_audio)


@pytest.mark.parametrize('condition', ['closed', 'destroyed'])
def test_queued_history_key_never_queries_focus_or_controls_after_owner_revocation(history_view, condition):
    value = history_view
    value.dead_controls = True
    if condition == 'closed':
        value.handlers.change(alive_value=False)
    else:
        value.handlers.change(destroyed_value=True)
    value.wx.Window.FindFocus = lambda: pytest.fail('Destroyed reader queried native focus.')
    event = HistoryKey(ord('Z'))
    value.handlers.key(event)
    assert event.skipped and not value.guards and not value.messages and not value.stops


def test_history_denies_mutation_while_playback_silence_is_unconfirmed(history_view):
    value = history_view
    value.document.replace(0, 1, 'Accepted')
    value.editor.value = value.document.text
    before = value.document.to_dict()
    value.stop_error = RuntimeError('Native playback Stop is unconfirmed.')
    event = HistoryKey(ord('Z'))
    value.handlers.key(event)
    assert not event.skipped and value.document.to_dict() == before
    assert value.stops == 1 and value.refreshes == 0
    assert value.handlers.retained() == (11, value.old_audio, value.old_audio)
    assert any('Stop is unconfirmed' in message for message in value.messages)


def test_fixed_undo_redo_remain_reserved_from_configurable_reader_shortcuts():
    from carr_library.keybindings import validate_bindings
    for key in ('Ctrl+Z', 'Ctrl+Y'):
        with pytest.raises(ValueError):
            validate_bindings(voice_reader_commands(), {'voice_reader.prepare': key})
