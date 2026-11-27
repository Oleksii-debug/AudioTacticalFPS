"""Real serial workers and canonical synthesis publication behind native handlers."""
import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.speech.runtime import Voice, VoiceRegistry
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.ui import reading_narration, reading_tools, speech_dialog


def closure(name, values):
    path = Path(speech_dialog.__file__)
    module = ast.parse(path.read_text(encoding='utf-8'))
    outer = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == 'show_speech_reader')
    callback = next(n for n in outer.body if isinstance(n, ast.FunctionDef) and n.name == name)
    bindings = '\n'.join('    ' + key + ' = values[' + repr(key) + ']' for key in values)
    state = '{' + ', '.join(repr(key) + ': ' + key for key in values) + '}'
    factory = ast.parse('def build(values):\n' + bindings
        + '\n    def change_document(value):\n        nonlocal document\n        document = value\n'
        + '\n    return ' + name + ', lambda: ' + state + ', change_document\n')
    factory.body[0].body.insert(-1, callback)
    namespace = dict(speech_dialog.__dict__)
    exec(compile(ast.fix_missing_locations(factory), str(path), 'exec'), namespace)
    return namespace['build'](values)


class Worker:
    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.pending, self.threads = [], []
    def run(self, work, done, *, on_failed=None):
        def actual():
            self.threads.append(get_ident())
            return work()
        future = self.pool.submit(actual)
        self.pending.append((future, done, on_failed))
        return True
    def finish(self):
        future, done, failed = self.pending.pop(0)
        try:
            value = future.result(timeout=5)
            done(value)
            return value
        except BaseException as exc:
            if failed is not None:
                failed(exc)
            else:
                raise
    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)


class Editor:
    """UTF-16 native positions with a hard owner-thread check."""
    def __init__(self, value):
        self.owner, self.value, self.selection, self.focus = get_ident(), value, (0, 0), 0
    def native(self): assert get_ident() == self.owner
    def GetValue(self): self.native(); return self.value
    def GetSelection(self): self.native(); return self.selection
    def GetStringSelection(self): return self.GetRange(*self.GetSelection())
    def GetInsertionPoint(self): self.native(); return self.selection[1]
    def GetLastPosition(self): self.native(); return len(self.value.encode('utf-16-le')) // 2
    def GetRange(self, first, last):
        self.native()
        return self.value.encode('utf-16-le')[first * 2:last * 2].decode('utf-16-le')
    def choose(self, first, last):
        self.native()
        self.selection = tuple(len(self.value[:at].encode('utf-16-le')) // 2 for at in (first, last))
    def SetInsertionPoint(self, value): self.native(); self.selection = (value, value)
    def ChangeValue(self, value): self.native(); self.value = value
    def SetFocus(self): self.native(); self.focus += 1


class Widget:
    def __init__(self, parent=None, *args, **kwargs):
        self.owner, self.dead, self.enabled = get_ident(), False, True
        self.value, self.selection, self.handlers = kwargs.get('value', ''), -1, {}
        self.rows, self.label, self.name = list(kwargs.get('choices', [])), kwargs.get('label', ''), ''
    def native(self): assert get_ident() == self.owner and not self.dead
    def SetName(self, value): self.native(); self.name = value
    def Wrap(self, value): self.native()
    def Add(self, *args, **kwargs): self.native()
    def SetSizer(self, value): self.native()
    def SetEscapeId(self, value): self.native()
    def SetFocus(self): self.native()
    def SetLabel(self, value): self.native(); self.label = value
    def Bind(self, event, handler): self.native(); self.handlers[event] = handler
    def Enable(self, value=True): self.native(); self.enabled = value
    def GetValue(self): self.native(); return self.value
    def ChangeValue(self, value): self.native(); self.value = value
    def Set(self, rows): self.native(); self.rows, self.selection = list(rows), -1
    def GetSelection(self): self.native(); return self.selection
    def SetSelection(self, value): self.native(); self.selection = value
    def EndModal(self, result): self.native()
    def __bool__(self): return not self.dead
    def Destroy(self):
        self.native()
        self.dead = True
        if 'destroy' in self.handlers:
            self.handlers['destroy'](SimpleNamespace(GetEventObject=lambda: self, Skip=lambda: None))


def wx_module():
    value = SimpleNamespace(Dialog=Widget, TextCtrl=Widget, StaticText=Widget, ListBox=Widget,
        Choice=Widget, BoxSizer=Widget, Button=Widget, EVT_BUTTON='press', EVT_CLOSE='close',
        EVT_CHOICE='choice', EVT_WINDOW_DESTROY='destroy', ID_CANCEL=2)
    for key in ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL',
                'ALL', 'EXPAND', 'LEFT', 'TE_READONLY', 'TE_MULTILINE'):
        setattr(value, key, 0)
    return value


@pytest.fixture
def policy_ui(tmp_path, monkeypatch):
    worker = Worker()
    doc = ReadingDocument('😀 Before [note] after.')
    editor = Editor(doc.text)
    editor.choose(doc.text.index('['), doc.text.index(']') + 1)
    parent, scope = SimpleNamespace(_closing=False), SimpleNamespace(allowed=True)
    store, saves, reports = ReaderDraftStore(tmp_path), [], []
    def guard():
        if not scope.allowed:
            raise ValueError('Source authorization changed.')
    def save(**kwargs):
        assert get_ident() == editor.owner
        store.save(ReaderResume(doc))
        saves.append(doc.to_dict())
    values = dict(alive=True, busy=False, source_loaded=True, parent=parent, dialog=object(),
        document=doc, editor=editor, playback_scope=guard, stop_for_reading_tab=Mock(),
        capabilities=Mock(), report=lambda message, **_kw: reports.append(message),
        sync_document=lambda: doc.set_text(editor.GetValue()), save_recovery=save,
        run=worker.run, cancel=Event())
    created = []
    monkeypatch.setattr(reading_tools, 'show_reading_tools',
                        lambda owner, **options: options['content_policy'](owner))
    def open_policy(owner, **options):
        created.append(reading_narration.create_reading_narration(owner, wx_module=wx_module(), **options))
    monkeypatch.setattr(reading_narration, 'show_reading_narration', open_policy)
    callback, current, change = closure('choose_reading_tools', values)
    callback()
    assert len(created) == 1 and not reports
    dialog = created[0]
    worker.finish()
    state = SimpleNamespace(worker=worker, document=doc, editor=editor, parent=parent, scope=scope,
        store=store, saves=saves, dialog=dialog, values=values, change_document=change)
    yield state
    if dialog.alive:
        dialog.close()
    worker.close()
    if not dialog.dead:
        dialog.Destroy()


def test_native_category_controls_apply_on_owner_and_keep_exact_unicode_selection(policy_ui, monkeypatch):
    state, dialog, doc = policy_ui, policy_ui.dialog, policy_ui.document
    owner, commits = get_ident(), []
    original = ReadingDocument.set_narration_policy
    def checked(self, value, **options):
        commits.append((self is doc, get_ident()))
        return original(self, value, **options)
    monkeypatch.setattr(ReadingDocument, 'set_narration_policy', checked)
    dialog.buttons['mark'].handlers['press']()
    state.worker.pending[0][0].result(timeout=5)
    assert doc.narration_policy is None  # The worker owns only a private draft.
    state.worker.finish()
    row = doc.narration_policy['regions'][0]
    assert doc.text[row['start']:row['end']] == '[note]'
    assert row['start'] == 9 and state.editor.selection[0] != row['start']
    assert all(thread == owner for current, thread in commits if current)
    assert any(not current and thread != owner for current, thread in commits)
    dialog.mode.SetSelection(1); dialog.buttons['apply'].handlers['press']()
    state.worker.finish()
    dialog.buttons['preview'].handlers['press']()
    state.worker.finish()
    assert dialog.preview.GetValue() == '😀 Before \n after.'
    assert dialog.preview_selection.provenance()['quotation_authority'] is False
    assert 'derived' in dialog.status.GetValue() and 'no text-follow' in dialog.status.GetValue()
    recovered = state.store.load().document
    assert recovered.to_dict() == doc.to_dict() and doc.source_text == '😀 Before [note] after.'
    dialog.mode.SetSelection(0); dialog.apply(); state.worker.finish()
    dialog.show_preview(); state.worker.finish()
    assert dialog.preview.GetValue() == doc.source_text and not doc.can_undo
    assert all(thread != owner for thread in state.worker.threads)


@pytest.mark.parametrize('change', ['text', 'policy', 'tab', 'source', 'owner_close'])
def test_queued_policy_result_cannot_mutate_a_new_source_text_policy_or_tab(policy_ui, change):
    state, dialog, doc = policy_ui, policy_ui.dialog, policy_ui.document
    dialog.mark()
    state.worker.pending[0][0].result(timeout=5)
    if change == 'text': doc.set_text('Changed reading copy.'); state.editor.ChangeValue(doc.text)
    elif change == 'policy': doc.mark_narration_region(0, 1, 'heading', 'Another review')
    elif change == 'tab': state.change_document(ReadingDocument(doc.source_text))
    elif change == 'source': state.scope.allowed = False
    else: state.parent._closing = True
    before = doc.to_dict()
    # Same worker has already finished; only owner-side adoption remains.
    future, done, failed = state.worker.pending.pop()
    try:
        done(future.result(timeout=5))
    except Exception as exc:
        failed(exc)
    assert doc.to_dict() == before and not state.saves
    assert 'changed' in dialog.status.GetValue().lower() or 'closing' in dialog.status.GetValue().lower()


def test_closed_policy_dialog_cancels_actual_worker_and_never_touches_destroyed_controls(policy_ui, monkeypatch):
    state, dialog = policy_ui, policy_ui.dialog
    entered, release = Event(), Event()
    real = ReadingDocument.from_dict.__func__
    def blocked(cls, value):
        if get_ident() != state.editor.owner:
            entered.set()
            assert release.wait(5)
        return real(cls, value)
    monkeypatch.setattr(ReadingDocument, 'from_dict', classmethod(blocked))
    dialog.mark()
    try:
        assert entered.wait(5)
        dialog.close()
        dialog.Destroy()
    finally:
        release.set()
    state.worker.finish()
    assert not dialog.alive and not state.saves and state.document.narration_policy is None


def test_reading_policy_lists_stale_labels_without_guessing_and_refuses_stale_skip_preview(policy_ui):
    state, dialog, doc = policy_ui, policy_ui.dialog, policy_ui.document
    dialog.mark(); state.worker.finish()
    dialog.mode.SetSelection(1); dialog.apply(); state.worker.finish()
    doc.set_text('Other visible text.')
    state.editor.ChangeValue(doc.text); state.editor.choose(0, 0)
    dialog.refresh(); state.worker.finish()
    assert 'text changed' in dialog.list.rows[0]
    dialog.show_preview(); state.worker.finish()
    assert 'Re-mark' in dialog.status.GetValue()
    dialog.mode.SetSelection(0); dialog.apply(); state.worker.finish()
    dialog.show_preview(); state.worker.finish()
    assert dialog.preview.GetValue() == 'Other visible text.'


class PCMProvider:
    def __init__(self):
        self.spoken, self.before_return = [], lambda: None
    def voices(self): return (Voice('fixture:policy', 'Synthetic PCM test', 'fixture', 'en'),)
    def render(self, text, voice, output, settings, cancelled):
        self.spoken.append(text)
        with wave.open(str(output), 'wb') as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(8000)
            audio.writeframes(b'\x10\x00' * 800)
        self.before_return()
    def close(self): pass


@pytest.fixture
def render_ui(tmp_path):
    worker, provider = Worker(), PCMProvider()
    registry = VoiceRegistry((provider,))
    doc = ReadingDocument('😀 Before [note] after.')
    doc.mark_narration_region(9, 15, 'footnote', 'Note')
    editor, parent = Editor(doc.text), SimpleNamespace(_closing=False)
    allowed = [True]
    def scope():
        if not allowed[0]: raise ValueError('Source authorization changed.')
    def selected(from_cursor=False):
        doc.set_text(editor.GetValue())
        return doc.text
    reports, player = [], Mock()
    values = dict(last_audio=None, last_reading_audio=None, playback_generation=0,
        alive=True, parent=parent, playback_scope=scope, source_guard=scope,
        registry=registry, voices=registry.voices(), selected_text=selected, document=doc, editor=editor,
        voice_choice=SimpleNamespace(GetSelection=lambda: 0), rate=SimpleNamespace(GetValue=lambda: 0),
        volume=SimpleNamespace(GetValue=lambda: 70), player=player, progress=None,
        audio_files=SimpleNamespace(name=str(tmp_path)), cancel=Event(), run=worker.run,
        report=lambda message, **kw: reports.append(message), capabilities=Mock(),
        start_playback=lambda callback: callback())
    callback, current, change = closure('speak', values)
    yield SimpleNamespace(document=doc, editor=editor, parent=parent, provider=provider,
        registry=registry, worker=worker, speak=callback, current=current, change_document=change,
        values=values, allowed=allowed, player=player, root=tmp_path, reports=reports)
    worker.close()
    registry.close()


@pytest.mark.parametrize('mode', ['whole', 'selection', 'cursor', 'save'])
def test_real_synthesis_uses_the_same_reviewed_projection_for_every_reader_entry(render_ui, mode):
    state, doc = render_ui, render_ui.document
    doc.set_narration_read('footnote', False)
    if mode == 'selection': state.editor.choose(9, len(doc.text))
    elif mode == 'cursor': state.editor.choose(9, 9)
    output = state.root / 'explicit.wav' if mode == 'save' else None
    state.speak(from_cursor=mode == 'cursor', destination=output)
    result, selection = state.worker.finish()
    first = 9 if mode in ('selection', 'cursor') else 0
    assert selection.text == doc.narration_selection(first).text
    assert ''.join(state.provider.spoken) == selection.text and '[note]' not in selection.text
    with wave.open(str(result.path), 'rb') as audio:
        assert audio.getnframes() == 800
    assert state.current()['last_reading_audio'] is None  # No fabricated contiguous follow.
    if output is None: state.player.play.assert_called_once_with(result.path)
    else: state.player.play.assert_not_called()
    assert doc.source_text == doc.text


def test_fully_skipped_selection_refuses_before_engine_or_output_creation(render_ui):
    state = render_ui
    state.document.set_narration_read('footnote', False)
    state.editor.choose(9, 15)
    output = state.root / 'must-not-exist.wav'
    state.speak(destination=output)
    with pytest.raises(ValueError, match='Empty narration'):
        state.worker.finish()
    assert not state.provider.spoken and not output.exists()
    state.player.play.assert_not_called()


@pytest.mark.parametrize('change', ['text', 'policy_same_text', 'source', 'tab', 'owner_close'])
def test_actual_speech_publication_rechecks_source_and_policy_after_provider_returns(render_ui, change):
    state = render_ui
    entered, release = Event(), Event()
    def barrier():
        entered.set()
        assert release.wait(5)
    state.provider.before_return = barrier
    output = state.root / 'revoked.wav'
    state.speak(destination=output)
    try:
        assert entered.wait(5)
        if change == 'text':
            state.document.replace(0, 1, 'Other')
            state.editor.ChangeValue(state.document.text)
        elif change == 'policy_same_text':
            # Both categories still Read; identical spoken bytes do not restore old consent.
            state.document.mark_narration_region(0, 1, 'heading', 'Another explicit annotation')
        elif change == 'source': state.allowed[0] = False
        elif change == 'tab': state.change_document(ReadingDocument(state.document.source_text))
        else: state.parent._closing = True
    finally:
        release.set()
    with pytest.raises((ValueError, InterruptedError), match='changed|closing|closed'):
        state.worker.finish()
    assert not output.exists() and not list(state.root.glob('.speech-*'))
    state.player.play.assert_not_called()


def test_policy_change_after_exact_completed_render_refuses_live_adoption_without_deleting_saved_output(render_ui):
    state = render_ui
    output = state.root / 'completed.wav'
    state.speak(destination=output)
    future, done, failed = state.worker.pending.pop()
    result, selection = future.result(timeout=5)
    raw = output.read_bytes()
    state.document.set_narration_read('footnote', False)
    done((result, selection))
    assert output.read_bytes() == raw and state.current()['last_audio'] is None
    assert 'WAV was saved:' in state.reports[-1] and str(output) in state.reports[-1]
    assert result.sha256 in state.reports[-1] and 'after publication' in state.reports[-1]
    assert 'changed' in state.reports[-1] and 'did not complete' not in state.reports[-1]
    state.player.play.assert_not_called()


def test_actual_reader_recovery_with_skip_never_saves_unfiltered_audio_position(tmp_path):
    doc = ReadingDocument('Before note after.')
    doc.mark_narration_region(7, 11, 'footnote', 'Note')
    doc.set_narration_read('footnote', False)
    store, editor = ReaderDraftStore(tmp_path), Editor(doc.text)
    values = dict(alive=True, draft_store=store, source_loaded=True, document=doc, editor=editor,
        last_recovery_signature=None, recovered=None, remembered_voice_id='', voices=(),
        voice_choice=SimpleNamespace(GetSelection=lambda: -1), rate=SimpleNamespace(GetValue=lambda: 0),
        volume=SimpleNamespace(GetValue=lambda: 80), last_audio=tmp_path / 'filtered.wav',
        player=SimpleNamespace(position_seconds=47.5), reader_tabs=None, save_path=None,
        sync_document=lambda: doc.set_text(editor.GetValue()))
    save, _, _ = closure('save_recovery', values)
    assert save(force=True)
    assert store.load().audio_seconds == 0 and store.load().document.narration_skips_enabled
    doc.set_narration_read('footnote', True)
    assert save(force=True)
    assert store.load().audio_seconds == 47.5


def test_direct_native_destroy_after_worker_completion_revokes_queued_policy_adoption(policy_ui):
    state, dialog = policy_ui, policy_ui.dialog
    before = state.document.to_dict()
    dialog.mark()
    state.worker.pending[0][0].result(timeout=5)
    assert state.document.narration_policy is None and not state.saves
    dialog.Destroy()  # No EVT_CLOSE/close() call; this is the native owner-destruction seam.
    assert not dialog.alive and dialog.dead and dialog.cancel_pending is None
    state.worker.finish()
    assert state.document.to_dict() == before and not state.saves and not state.store.path.exists()


def test_child_control_destroy_does_not_revoke_policy_owner(policy_ui):
    state, dialog = policy_ui, policy_ui.dialog
    skipped = Mock()
    dialog.destroyed(SimpleNamespace(GetEventObject=lambda: dialog.list, Skip=skipped))
    assert dialog.alive and not dialog.dead
    skipped.assert_called_once()
    dialog.mark(); state.worker.finish()
    assert state.document.narration_policy is not None and state.saves


def test_policy_modal_finally_destroys_even_when_close_raises(monkeypatch):
    events = []
    class Modal:
        alive = True
        def ShowModal(self): events.append('show')
        def close(self):
            self.alive = False
            events.append('close')
            raise RuntimeError('Injected close failure')
        def Destroy(self): events.append('destroy')
    monkeypatch.setattr(reading_narration, 'create_reading_narration', lambda *_a, **_k: Modal())
    with pytest.raises(RuntimeError, match='close failure'):
        reading_narration.show_reading_narration(object())
    assert events == ['show', 'close', 'destroy']


def test_actual_policy_close_still_ends_and_destroys_after_cancel_callback_failure(policy_ui, monkeypatch):
    state, dialog = policy_ui, policy_ui.dialog
    dialog.mark()
    state.worker.pending[0][0].result(timeout=5)
    cancel_pending = dialog.cancel_pending
    def broken_cancel():
        cancel_pending()
        raise RuntimeError('Injected cancel callback failure')
    dialog.cancel_pending = broken_cancel
    dialog.ShowModal = lambda: None
    ended, real_end = Mock(), dialog.EndModal
    def end(value):
        ended(value)
        real_end(value)
    dialog.EndModal = end
    monkeypatch.setattr(reading_narration, 'create_reading_narration', lambda *_a, **_k: dialog)
    with pytest.raises(RuntimeError, match='cancel callback failure'):
        reading_narration.show_reading_narration(object())
    assert dialog.dead and not dialog.alive and dialog.cancel_pending is None
    ended.assert_called_once()
    state.worker.finish()
    assert state.document.narration_policy is None and not state.saves


@pytest.mark.parametrize('change', ['editor_text', 'policy_same_text', 'tab', 'source', 'owner_close'])
def test_quiet_feedback_delay_rechecks_exact_projection_before_real_playback(render_ui, change):
    state, held = render_ui, []
    values = {**state.values, 'start_playback': lambda action: held.append(action)}
    speak, current, change_document = closure('speak', values)
    speak()
    result, selection = state.worker.finish()
    assert result.path.is_file() and len(held) == 1
    if change == 'editor_text':
        # The document has not synchronized this owner-only visible edit yet.
        state.editor.ChangeValue('Visible replacement before quiet feedback finished.')
    elif change == 'policy_same_text':
        state.document.mark_narration_region(0, 1, 'heading', 'Another explicit review')
    elif change == 'tab':
        change_document(ReadingDocument(state.document.source_text))
    elif change == 'source':
        state.allowed[0] = False
    else:
        state.parent._closing = True
    with pytest.raises((ValueError, InterruptedError), match='changed|closed'):
        held.pop()()
    state.player.play.assert_not_called()
    assert result.path.is_file()  # A completed private render is still owned by normal reader cleanup.
