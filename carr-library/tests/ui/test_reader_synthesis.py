"""Native-option modal, actual reader callbacks and the real speech registry boundary."""
import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import get_ident
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from carr_library.speech import ReadingDocument, SpeechSettings, Voice, VoiceRegistry
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.ui import speech_dialog
from carr_library.ui.reader_synthesis import ReaderSynthesisControls


class Widget:
    widgets = []
    def __init__(self, parent=None, *args, **kwargs):
        self.owner, self.parent = get_ident(), parent
        self.value = kwargs.get('value', kwargs.get('initial', ''))
        self.label, self.name = kwargs.get('label', ''), ''
        self.handlers, self.children = {}, []
        self.enabled, self.destroyed = True, False
        if parent is not None:
            parent.children.append(self)
        self.widgets.append(self)
    def native(self):
        assert get_ident() == self.owner, 'Native widget accessed from the worker.'
        assert not self.destroyed, 'Destroyed native wrapper was used.'
    def GetId(self): self.native(); return id(self)
    def SetName(self, value): self.native(); self.name = value
    def SetToolTip(self, value): self.native(); self.tooltip = value
    def GetValue(self): self.native(); return self.value
    def SetValue(self, value): self.native(); self.value = value
    def ChangeValue(self, value): self.SetValue(value)
    def Enable(self, value=True): self.native(); self.enabled = value
    def Bind(self, event, handler): self.native(); self.handlers[event] = handler
    def Add(self, *args): self.native()
    def SetSizerAndFit(self, value): self.native()
    def SetEscapeId(self, value): self.native()
    def Destroy(self):
        self.native()
        self.destroyed = True
        callback = self.handlers.get('destroy')
        if callback:
            callback(SimpleNamespace(GetId=lambda: id(self), Skip=lambda: None))
        for child in self.children:
            if not child.destroyed:
                child.Destroy()


@pytest.fixture
def ui():
    Widget.widgets = []
    context = SimpleNamespace(alive=True, busy=False, allowed=True, driver=lambda view: view.close(),
        reports=[], events=[], before_error=None)
    class Dialog(Widget):
        modal = False
        def ShowModal(self):
            self.native(); self.modal = True
            context.driver(context.controls.view)
            assert self.destroyed or not self.modal, 'Test did not settle the real modal handler.'
            return 1
        def IsModal(self): self.native(); return self.modal
        def EndModal(self, code): self.native(); self.modal = False
    class Sizer:
        def __init__(self, *args): self.items = []
        def Add(self, *args): self.items.append(args)
    wx = SimpleNamespace(Dialog=Dialog, Button=Widget, CheckBox=Widget, SpinCtrl=Widget,
        TextCtrl=Widget, StaticText=Widget, BoxSizer=Sizer, ID_OK=1, ID_CANCEL=2,
        EVT_BUTTON='button', EVT_CHECKBOX='checkbox', EVT_SPINCTRL='spin',
        EVT_CLOSE='close', EVT_WINDOW_DESTROY='destroy')
    for name in ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL',
                 'ALL', 'EXPAND', 'TE_READONLY', 'TE_MULTILINE', 'ALIGN_RIGHT'):
        setattr(wx, name, 0)
    parent = Widget()
    def guard():
        if not context.allowed:
            raise ValueError('The source library changed.')
    def before():
        context.events.append('before')
        if context.before_error is not None:
            raise context.before_error
    controls = ReaderSynthesisControls(parent, alive=lambda: context.alive,
        busy=lambda: context.busy, guard=guard, before_apply=before,
        changed=lambda: context.events.append(('changed', context.controls.snapshot())),
        report=lambda value, **kwargs: context.reports.append(value), wx=wx)
    context.controls, context.wx, context.parent = controls, wx, parent
    context.voice = Voice('fixture:native', 'Native controls fixture', 'Synthetic PCM', 'uk',
                          ('rate', 'volume', 'pitch', 'punctuation'))
    controls.sync(context.voice, enabled=True)
    yield context
    if not parent.destroyed:
        parent.Destroy()


def choose(view, *, pitch=73, punctuation=True):
    view.pitch_enabled.SetValue(pitch is not None)
    if pitch is not None:
        view.pitch.SetValue(pitch)
    view.punctuation.SetValue(punctuation)
    view.refresh()


def test_opening_and_cancelling_preserve_defaults_without_reader_actions(ui):
    def drive(view):
        assert not view.pitch_enabled.GetValue() and not view.pitch.enabled
        assert view.punctuation.enabled and not view.punctuation.GetValue()
        choose(view)
        view.close()
    ui.driver = drive
    assert not ui.controls.show()
    assert ui.controls.snapshot() == (None, False)
    assert ui.controls.settings(-2, 64, ui.voice) == SpeechSettings(rate=-2, volume=64)
    assert not ui.events and not ui.reports


def test_explicit_apply_keeps_rate_volume_and_confirms_stop_before_acceptance(ui):
    def drive(view):
        choose(view)
        assert view.pitch.enabled and view.apply.enabled
        view.accept()
    ui.driver = drive
    assert ui.controls.show()
    assert ui.events == ['before', ('changed', (73, True))]
    assert ui.controls.settings(-2, 64, ui.voice) == SpeechSettings(
        rate=-2, volume=64, pitch=73, punctuation=True)


@pytest.mark.parametrize('capabilities, pitch_enabled, punctuation_enabled', [
    (('rate', 'volume'), False, False),
    (('rate', 'volume', 'pitch'), True, False),
    (('rate', 'volume', 'punctuation'), False, True),
])
def test_modal_exposes_only_selected_voice_capabilities(ui, capabilities, pitch_enabled, punctuation_enabled):
    voice = Voice('fixture:limited', 'Limited fixture', 'Synthetic PCM', 'uk', capabilities)
    assert ui.controls.sync(voice, enabled=True)
    def drive(view):
        assert view.pitch_enabled.enabled is pitch_enabled
        assert view.punctuation.enabled is punctuation_enabled
        assert not view.pitch.enabled
        assert view.apply.enabled  # Engine defaults are always an explicit valid request.
        view.close()
    ui.driver = drive
    assert not ui.controls.show()
    assert ui.controls.snapshot() == (None, False) and not ui.events


@pytest.mark.parametrize('options', [(0, False), (99, True), (None, True)])
def test_unavailable_recovered_options_remain_inert_until_explicit_reset(ui, options):
    ui.controls.restore(*options)
    voice = Voice('fixture:limited', 'Limited fixture', 'Synthetic PCM', 'uk')
    assert not ui.controls.sync(voice, enabled=True)
    assert ui.controls.snapshot() == options
    with pytest.raises(ValueError, match='does not support'):
        ui.controls.settings(0, 80, voice)
    assert 'reset native voice options' in ui.controls.explanation(voice)
    def drive(view):
        assert not view.pitch_enabled.enabled and not view.punctuation.enabled
        assert not view.apply.enabled
        view.reset_options()
        assert view.apply.enabled
        view.accept()
    ui.driver = drive
    assert ui.controls.show()
    assert ui.controls.snapshot() == (None, False)
    assert ui.controls.settings(0, 80, voice) == SpeechSettings(rate=0, volume=80)


@pytest.mark.parametrize('pitch,punctuation', [(True, False), (-1, False), (100, False),
    (1.5, False), ('50', False), (None, 1), (None, None), (None, 'false')])
def test_invalid_recovery_never_partially_changes_options(ui, pitch, punctuation):
    ui.controls.restore(20, True)
    with pytest.raises(ValueError):
        ui.controls.restore(pitch, punctuation)
    assert ui.controls.snapshot() == (20, True)


@pytest.mark.parametrize('change', ['source', 'busy', 'voice', 'restored'])
def test_modal_acceptance_rechecks_current_reader_and_voice(ui, change):
    def drive(view):
        choose(view)
        if change == 'source': ui.allowed = False
        elif change == 'busy': ui.busy = True
        elif change == 'voice':
            ui.controls.sync(Voice('fixture:new', 'New voice', 'Fixture', 'en'), enabled=True)
        else:
            ui.controls.restore(15, False)
        view.accept()
        assert not view.closed and view.result is None
        assert 'changed' in view.status.GetValue() or 'Finish' in view.status.GetValue()
        view.close()
    ui.driver = drive
    assert not ui.controls.show()
    assert ui.controls.snapshot() == ((15, False) if change == 'restored' else (None, False))
    assert not ui.events


def test_failed_native_stop_keeps_previous_options_and_audio_owner(ui):
    ui.controls.restore(11, False)
    ui.before_error = RuntimeError('Native audio cleanup is still pending.')
    def drive(view):
        choose(view)
        view.accept()
    ui.driver = drive
    assert not ui.controls.show()
    assert ui.controls.snapshot() == (11, False)
    assert ui.events == ['before']
    assert 'cleanup is still pending' in ui.reports[-1]


def test_owner_destroy_revokes_queued_handlers_without_touching_dead_wrapper(ui):
    def drive(view):
        choose(view)
        view.owner_destroyed(SimpleNamespace(GetId=lambda: view.pitch.GetId(), Skip=lambda: None))
        assert not view.closed
        view.window.Destroy()
        view.accept()
        view.refresh()
        view.reset_options()
        view.close()
    ui.driver = drive
    assert not ui.controls.show()
    assert ui.controls.view.destroyed and ui.controls.view.closed
    assert ui.controls.snapshot() == (None, False) and not ui.events


@pytest.mark.parametrize('boundary', ['closed', 'busy', 'source'])
def test_unavailable_reader_refuses_before_constructing_a_modal(ui, boundary):
    count = len(Widget.widgets)
    if boundary == 'closed': ui.alive = False
    elif boundary == 'busy': ui.busy = True
    else: ui.allowed = False
    assert not ui.controls.show()
    assert len(Widget.widgets) == count and not ui.events


def callbacks(names, factory, **values):
    source = Path(speech_dialog.__file__).resolve()
    outer = next(node for node in ast.parse(source.read_text(encoding='utf-8')).body
                 if isinstance(node, ast.FunctionDef) and node.name == 'show_speech_reader')
    found = {node.name: node for node in outer.body if isinstance(node, ast.FunctionDef)}
    wrapper = ast.parse(factory)
    body = wrapper.body[0].body
    index = next(i for i, node in enumerate(body) if isinstance(node, ast.Expr)
                 and isinstance(node.value, ast.Call) and getattr(node.value.func, 'id', None) == 'INSERT')
    body[index:index + 1] = [found[name] for name in names]
    environment = dict(speech_dialog.__dict__, SimpleNamespace=SimpleNamespace, **values)
    exec(compile(ast.fix_missing_locations(wrapper), str(source), 'exec'), environment)
    return environment['make']


@pytest.fixture
def reader(ui, tmp_path):
    text = 'Оригінал, з пунктуацією! Original source.'
    document = ReadingDocument(text)
    main_thread = get_ident()
    provider_calls, pending, reports = [], [], []
    class Provider:
        name = 'Actual registry with declared synthetic PCM only'
        def voices(self):
            return (ui.voice,)
        def render(self, content, voice, output, settings, cancelled):
            assert not cancelled()
            provider_calls.append((content, voice.id, settings, get_ident()))
            # Distinct actual PCM binds the fixture's requested settings, not
            # an acoustic-quality or real speech-model claim.
            sample = (1000 + (settings.pitch or 0) * 2 + int(settings.punctuation)).to_bytes(
                2, 'little', signed=True)
            with wave.open(str(output), 'wb') as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(8000)
                audio.writeframes(sample * 400)
    registry = VoiceRegistry([Provider()])
    player = SimpleNamespace(stop=Mock(), play=Mock(), position_seconds=0)
    root = tmp_path / 'library'
    draft = ReaderDraftStore(root, slot='synthesis-settings')
    selection = SimpleNamespace(value=0, GetSelection=lambda: selection.value)
    rate, volume = Widget(initial=-2), Widget(initial=64)
    editor = SimpleNamespace(GetValue=lambda: document.text, GetInsertionPoint=lambda: 0)
    def run(work, done, **kwargs):
        pending.append((work, done))
        return True
    make = callbacks(('speak', 'before_native_options_apply', 'save_recovery', 'capture_reading_tab'),
        """
def make():
    last_audio = last_reading_audio = last_recovery_signature = None
    playback_generation = 0
    alive, busy, source_loaded = True, False, True
    draft_store, reader_tabs, save_path = draft, None, None
    remembered_voice_id, reading_rules = '', ()
    synthesis_controls = controls
    voice_choice = selection
    voices = [voice]
    progress = None
    cancel = SimpleNamespace(is_set=lambda: False)
    audio_files = SimpleNamespace(name=str(audio_root))
    selected_text = lambda from_cursor=False: document.text
    reading_selection = lambda control: (0, len(document.text))
    reading_offset = lambda control, offset: offset
    sync_document = lambda: None
    playback_guard = guard
    report = lambda value, **kwargs: reports.append(value)
    start_playback = lambda action: action()
    INSERT()
    return SimpleNamespace(speak=speak, before=before_native_options_apply,
        save=save_recovery, capture=capture_reading_tab,
        state=lambda: (last_audio, last_reading_audio, playback_generation),
        voices=voices)
""", controls=ui.controls, voice=ui.voice, selection=selection, rate=rate,
        volume=volume, editor=editor, document=document, player=player, draft=draft,
        audio_root=tmp_path, guard=ui.controls.guard, reports=reports, run=run, registry=registry)
    actual = make()
    def finish():
        work, done = pending.pop(0)
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(work).result(timeout=5)
        done(result)
        return result
    result = SimpleNamespace(actual=actual, finish=finish, calls=provider_calls, pending=pending,
        registry=registry, player=player, document=document, draft=draft, reports=reports,
        main_thread=main_thread, rate=rate, volume=volume)
    yield result
    registry.close()


def test_actual_reader_worker_forwards_native_settings_to_registry_and_saved_wav(ui, reader, tmp_path):
    ui.controls.restore(73, True)
    original = reader.document.source_text.encode('utf-8')
    first = tmp_path / 'native.wav'
    reader.actual.speak(destination=first)
    assert not first.exists() and len(reader.pending) == 1
    result = reader.finish()
    assert result.path == first and first.is_file()
    assert reader.calls[0][:3] == (reader.document.text, ui.voice.id,
                                  SpeechSettings(rate=-2, volume=64, pitch=73, punctuation=True))
    assert reader.calls[0][3] != reader.main_thread
    assert reader.actual.state()[0] == first and reader.actual.state()[1].path == first
    assert not reader.player.play.called
    ui.controls.restore(None, False)
    second = tmp_path / 'defaults.wav'
    reader.actual.speak(destination=second)
    other = reader.finish()
    assert other.sha256 != result.sha256
    assert reader.calls[1][2] == SpeechSettings(rate=-2, volume=64)
    assert reader.document.source_text.encode('utf-8') == original
    assert not reader.document.can_undo


def test_unsupported_reader_request_never_queues_worker_or_stops_existing_audio(ui, reader, tmp_path):
    limited = Voice(ui.voice.id, 'Same exact identity, limited controls', 'Fixture', 'uk')
    reader.actual.voices[0] = limited
    ui.controls.sync(limited, enabled=True)
    ui.controls.restore(73, True)
    reader.actual.speak(destination=tmp_path / 'refused.wav')
    assert not reader.pending and not reader.calls and not reader.player.stop.called
    assert 'does not support' in reader.reports[-1]


@pytest.mark.parametrize('changed', ['pitch', 'punctuation', 'rate', 'volume', 'voice'])
def test_completed_wav_cannot_attach_to_another_current_synthesis_request(ui, reader, tmp_path, changed):
    ui.controls.restore(73, True)
    target = tmp_path / 'accepted-request.wav'
    reader.actual.speak(destination=target)
    if changed == 'pitch': ui.controls.restore(72, True)
    elif changed == 'punctuation': ui.controls.restore(73, False)
    elif changed == 'rate': reader.rate.SetValue(-1)
    elif changed == 'volume': reader.volume.SetValue(63)
    else:
        reader.actual.voices[0] = Voice('fixture:other', 'Other voice', 'Fixture', 'en')
    result = reader.finish()
    assert result.path.is_file() and len(reader.calls) == 1
    assert reader.actual.state()[:2] == (None, None)
    assert 'not attached' in reader.reports[-1]
    assert not reader.player.play.called


def test_explicit_options_apply_disarms_previous_render_before_adopting_new_settings(ui, reader, tmp_path):
    reader.actual.speak(destination=tmp_path / 'prior.wav')
    prior = reader.finish()
    ui.controls.before_apply = reader.actual.before
    ui.driver = lambda view: (choose(view), view.accept())
    assert ui.controls.show()
    assert reader.actual.state()[:2] == (None, None)
    assert prior.path.is_file() and ui.controls.snapshot() == (73, True)
    assert reader.player.stop.call_count == 2 and not reader.player.play.called


def test_unconfirmed_native_stop_retains_old_render_and_options(ui, reader, tmp_path):
    reader.actual.speak(destination=tmp_path / 'prior.wav')
    prior = reader.finish()
    ui.controls.before_apply = reader.actual.before
    reader.player.stop.side_effect = RuntimeError('Existing native owner remains pending.')
    ui.driver = lambda view: (choose(view), view.accept())
    assert not ui.controls.show()
    assert reader.actual.state()[0] == prior.path
    assert ui.controls.snapshot() == (None, False) and prior.path.is_file()


def test_actual_recovery_signature_and_tab_capture_bind_only_changed_options(ui, reader):
    assert reader.actual.save()
    _, old_token = reader.draft.load_with_token()
    ui.controls.restore(0, True)
    assert reader.actual.save()  # No text/rate/volume/cursor change; options must still persist.
    restored, token = reader.draft.load_with_token()
    assert token != old_token and (restored.pitch, restored.punctuation) == (0, True)
    captured, save_path = reader.actual.capture()
    assert save_path is None
    assert (captured.rate, captured.volume, captured.pitch, captured.punctuation) == (-2, 64, 0, True)
    ui.controls.restore(None, False)
    assert reader.actual.save()
    default, token_after = reader.draft.load_with_token()
    assert token_after != token and (default.pitch, default.punctuation) == (None, False)


@pytest.mark.parametrize('supported', [False, True])
def test_actual_reader_capability_handler_gates_every_synthesis_entry(ui, supported):
    voice = ui.voice if supported else Voice('fixture:limited', 'Limited', 'Fixture', 'uk')
    ui.controls.restore(73, True)
    controls = {name: Widget(initial=value) for name, value in (
        ('rate', -2), ('volume', 64), ('read', ''), ('cursor', ''), ('export_audio', ''),
        ('navigation_unit', ''), ('previous_unit', ''), ('current_unit', ''), ('next_unit', ''),
        ('find_button', ''), ('replace_button', ''), ('pronunciation_button', ''),
        ('preparation_button', ''), ('spelling_button', ''), ('appearance_button', ''),
        ('focus_button', ''), ('assistant_button', ''), ('progress_voice', ''),
        ('voice_preferences_button', ''))}
    spoken_units = {'word': Widget(), 'sentence': Widget()}
    reports = []
    make = callbacks(('capabilities',), """
def make():
    remembered_voice_id = ''
    alive, busy, source_loaded = True, False, True
    voices = [voice]
    voice_choice = SimpleNamespace(GetSelection=lambda: 0)
    synthesis_controls = options
    reader_tabs, progress = None, None
    reader_root = object()
    read_units = spoken_units
    output_controls = SimpleNamespace(sync=lambda: None)
    parent = SimpleNamespace()
    report = lambda value, **kwargs: reports.append(value)
    INSERT()
    return capabilities
""", voice=voice, options=ui.controls, spoken_units=spoken_units, reports=reports, **controls)
    make()()
    assert all(controls[name].enabled is supported for name in ('read', 'cursor', 'export_audio'))
    assert all(control.enabled is supported for control in spoken_units.values())
    assert ui.controls.button.enabled  # Explicit Reset remains reachable.
    assert controls['find_button'].enabled and controls['current_unit'].enabled
    assert (controls['rate'].GetValue(), controls['volume'].GetValue()) == (-2, 64)
    if not supported:
        assert 'does not support' in reports[-1]
    assert ui.controls.snapshot() == (73, True)



def test_modal_return_revokes_handlers_before_deferred_native_destroy(ui, monkeypatch):
    pending = {}
    original_destroy = ui.wx.Dialog.Destroy
    def deferred_destroy(window):
        window.native()
        window.modal = False
        # wx schedules a top-level window once; its destruction event is later.
        pending[id(window)] = window
    monkeypatch.setattr(ui.wx.Dialog, 'Destroy', deferred_destroy)
    ui.controls.restore(62, True)
    before = ui.controls.snapshot()
    def drive(view):
        choose(view, pitch=31, punctuation=False)
        view.window.Destroy()
        assert not view.destroyed
    ui.driver = drive
    assert not ui.controls.show()
    view = ui.controls.view
    assert view.closed and not view.destroyed and view.result is None
    assert ui.controls.snapshot() == before and not ui.events and not ui.reports
    # A queued callback can run after modal return but before the native event.
    view.accept()
    view.refresh()
    view.reset_options()
    view.close()
    assert ui.controls.snapshot() == before and not ui.events and len(pending) == 1
    original_destroy(pending.pop(id(view.window)))
    assert view.destroyed and view.closed and view.result is None
    assert ui.controls.snapshot() == before and not ui.events and not ui.reports
