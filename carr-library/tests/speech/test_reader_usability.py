"""Reader GUI glue with actual documents/rules/workers and deterministic native doubles."""
from concurrent.futures import Future
from itertools import count
from pathlib import Path
from threading import Thread, get_ident
from types import SimpleNamespace
import json
import sys
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.research.topic_tree import TopicTree
from carr_library.speech import PronunciationRule, ReadingDocument, Voice, VoiceRegistry
from carr_library.speech.source import read_source_range
from carr_library.ui import speech_dialog, reading_shortcuts, keyboard_settings
from carr_library.ui.pronunciation_dialog import caret_offset, native_caret_position, text_at_caret


class NativeRange:
    """Windows-like CRLF/UTF-16 positions; never Python string indices."""
    def __init__(self, text, position):
        self.native = text.replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-16-le')
        self.position = position
    def GetValue(self): return self.native.decode('utf-16-le').replace('\r\n', '\n')
    def GetInsertionPoint(self): return self.position
    def GetLastPosition(self): return len(self.native) // 2
    def GetRange(self, start, end):
        return self.native[2 * start:2 * end].decode('utf-16-le').replace('\r\n', '\n')


@pytest.mark.parametrize('text,prefix,line,paragraph', [
    ('🚂 first\nsecond e\u0301 line\n\nnext', '🚂 first\nsec', 'second e\u0301 line', '🚂 first\nsecond e\u0301 line'),
    ('one\r\ntwo\r\n \t\r\nthree\r\nfour', 'one\r\ntwo\r\n \t\r\nthr', 'three', 'three\nfour'),
    ('one\n\nnext', 'one\n', '', ''),
    ('last 🚂', 'last 🚂', 'last 🚂', 'last 🚂'),
    ('', '', '', ''),
])
def test_native_caret_ranges_preserve_unicode_and_explicit_paragraph_boundaries(text, prefix, line, paragraph):
    native_prefix = prefix.replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-16-le')
    editor = NativeRange(text, len(native_prefix) // 2)
    assert text_at_caret(editor) == line
    assert text_at_caret(editor, paragraph=True) == paragraph


@pytest.mark.parametrize('text', ['first\r\n🚂 e\u0301 line\r\nlast', '🚂🚂', 'one\n\nnext', ''])
def test_recovered_caret_roundtrips_native_crlf_and_surrogate_offsets(text):
    editor = NativeRange(text, 0)
    for offset in range(len(editor.GetValue()) + 1):
        position = native_caret_position(editor, offset)
        assert editor.GetRange(0, position) == editor.GetValue()[:offset]
        editor.position = position
        assert caret_offset(editor) == offset


@pytest.fixture
def reader_ui(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Pronunciation and reading controls')
    topic = app.create_topic(workspace, 'Bound reading source')
    raw = '🚂 First line.\r\nDr. Smith has 12 kg.\r\n\r\nNext paragraph.\r\nFinal line.'
    book = app.add_text(workspace, 'Immutable speech fixture', raw)
    desk = ReadingDesk(app, workspace, book['edition'], topic=topic)
    source = read_source_range(desk, 1, 1)
    main = get_ident()
    state = SimpleNamespace(controls=[], queue=[], jobs=[], requests=[], played=[], closed=[],
        paths=[], chunks=[], clipboard='Independent clipboard text.', clipboard_reads=0, clipboard_opens=0,
        clipboard_closes=0, clipboard_hook=lambda: None, render_hook=lambda: None,
        scenario=lambda env: None,
        dictionary_scenario=lambda env: None, notices=[])
    state.parent = SimpleNamespace(library=app, _closing=False, report=state.notices.append)
    state.app, state.source, state.raw, state.tmp_path = app, source, raw, tmp_path
    state.workspace, state.topic = workspace, topic

    class Executor:
        def __init__(self, **kwargs): self.shutdowns = []
        def submit(self, callback, *args, **kwargs):
            future = Future()
            state.jobs.append((future, callback, args, kwargs))
            return future
        def shutdown(self, **kwargs): self.shutdowns.append(kwargs)

    def drain():
        while state.queue:
            function, args = state.queue.pop(0)
            function(*args)

    def complete(deliver=True):
        future, callback, args, kwargs = state.jobs.pop(0)
        def work():
            if future.set_running_or_notify_cancel():
                try: future.set_result(callback(*args, **kwargs))
                except BaseException as exc: future.set_exception(exc)
        thread = Thread(target=work)
        thread.start(); thread.join(12)
        assert not thread.is_alive(), 'The bounded reader worker did not finish.'
        if deliver: drain()
        return future

    state.drain, state.complete = drain, complete

    class Control:
        def __init__(self, parent=None, *args, **kwargs):
            self.parent = parent
            self.label = kwargs.get('label', next((a for a in args if isinstance(a, str)), ''))
            self.title = kwargs.get('title', '')
            self.value = kwargs.get('value', kwargs.get('initial', ''))
            self.name, self.items, self.selection, self.cursor = '', [], -1, 0
            self.enabled, self.destroyed = True, False
            self.handlers, self.menu_handlers, self.table = {}, {}, ()
            state.controls.append(self)
        def native(self):
            assert get_ident() == main, 'A worker touched a native control.'
            assert not self.destroyed, 'A late callback touched a destroyed control.'
        def SetName(self, value): self.native(); self.name = value
        def GetName(self): self.native(); return self.name
        def SetLabel(self, value): self.native(); self.label = value
        def GetValue(self): self.native(); return self.value
        def SetValue(self, value): self.native(); self.value = value
        def ChangeValue(self, value): self.SetValue(value)
        def Set(self, values): self.native(); self.items = list(values)
        def SetSelection(self, value): self.native(); self.selection = value
        def GetSelection(self): self.native(); return self.selection
        def GetStringSelection(self): self.native(); return ''
        def SetInsertionPoint(self, value): self.native(); self.cursor = value
        def GetInsertionPoint(self): self.native(); return self.cursor
        def GetLastPosition(self):
            self.native(); return len(self.value.replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-16-le')) // 2
        def GetRange(self, start, end):
            self.native(); return NativeRange(self.value, self.cursor).GetRange(start, end)
        def Enable(self, enabled=True): self.native(); self.enabled = enabled
        def Disable(self): self.Enable(False)
        def IsEnabled(self): self.native(); return self.enabled
        def SetFocus(self): self.native(); assert self.enabled
        def Wrap(self, *args): self.native()
        def Add(self, *args): self.native()
        def SetSizer(self, *args): self.native()
        def SetEscapeId(self, *args): self.native()
        def SetAcceleratorTable(self, entries): self.native(); self.table = tuple(entries)
        def Bind(self, event, callback, *args, **kwargs):
            self.native()
            (self.menu_handlers if 'id' in kwargs else self.handlers)[kwargs.get('id', event)] = callback
        def Unbind(self, event, *, id): self.native(); self.menu_handlers.pop(id, None)
        def Start(self, *args): self.native()
        def Stop(self): self.native()
        def EndModal(self, code): self.native()
        def Destroy(self):
            if self.destroyed: return
            self.native()
            for child in list(state.controls):
                if child.parent is self: child.Destroy()
            self.destroyed = True

    class Dialog(Control):
        def ShowModal(self):
            self.native()
            if self.title == 'Pronunciation dictionary':
                state.dictionary_scenario(state)
            else:
                assert self.title == 'Voice reader and reading copy'
                state.voice_dialog = self
                complete()
                state.scenario(state)
            return 5101

    class Picker(Control):
        def __enter__(self): return self
        def __exit__(self, *args): self.Destroy()
        def ShowModal(self):
            self.native()
            self.path = state.paths.pop(0)
            return 5101 if self.path is None else 5100
        def GetPath(self): self.native(); return str(self.path)

    class Clipboard:
        def Open(self): state.clipboard_opens += 1; return True
        def GetData(self, data):
            state.clipboard_reads += 1
            data.value = state.clipboard
            state.clipboard_hook()
            return True
        def Close(self): state.clipboard_closes += 1

    class TextData:
        def __init__(self): self.value = ''
        def GetText(self): return self.value

    wx = SimpleNamespace(Dialog=Dialog, FileDialog=Picker, TextEntryDialog=Picker,
        TextCtrl=Control, StaticText=Control, Choice=Control, SpinCtrl=Control,
        CheckBox=lambda parent, **kwargs: Control(parent, value=False, **kwargs),
        Button=Control, BoxSizer=Control, WrapSizer=Control, Timer=Control,
        TheClipboard=Clipboard(), TextDataObject=TextData,
        CallAfter=lambda callback, *args: state.queue.append((callback, args)),
        CallLater=lambda milliseconds, callback: state.queue.append((callback, ())),
        NewIdRef=lambda: next(identifiers), AcceleratorTable=tuple,
        MessageBox=lambda *args, **kwargs: 4, ID_OK=5100, ID_CANCEL=5101,
        NOT_FOUND=-1, YES=2, NO=4, CANCEL=8, EVT_BUTTON='button', EVT_MENU='menu',
        EVT_CHOICE='choice', EVT_CLOSE='close', EVT_TIMER='timer')
    identifiers = count(10000)
    for name in ('DEFAULT_DIALOG_STYLE','RESIZE_BORDER','VERTICAL','HORIZONTAL','LEFT',
            'TOP','RIGHT','EXPAND','ALL','ALIGN_CENTER_VERTICAL','ALIGN_RIGHT','TE_READONLY',
            'TE_MULTILINE','TE_DONTWRAP','FD_SAVE','FD_OPEN','FD_FILE_MUST_EXIST','YES_NO',
            'ICON_QUESTION'):
        setattr(wx, name, 0)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    monkeypatch.setattr(reading_shortcuts, 'accelerator_tuple',
                        lambda shortcut, identifier: (shortcut, 0, int(identifier)))
    monkeypatch.setattr(keyboard_settings, 'accelerator_tuple',
                        lambda shortcut, identifier: (shortcut, 0, int(identifier)))
    monkeypatch.setattr(speech_dialog, 'ThreadPoolExecutor', Executor)

    class Provider:
        name = 'Explicit synthetic speech fixture'
        def voices(self):
            return (Voice('fixture:voice', 'Fixture', 'fixture', 'en', capabilities=('rate', 'volume')),)
        def render(self, text, voice, destination, settings, cancelled):
            assert get_ident() != main
            if cancelled(): raise InterruptedError('Fixture speech cancelled.')
            state.chunks.append(text)
            with wave.open(str(destination), 'wb') as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(8000)
                output.writeframes(b'\0\0' * 800)
            state.render_hook()
        def close(self): state.closed.append(True)
    monkeypatch.setattr('carr_library.runtime_settings.voice_registry',
                        lambda *args, **kwargs: VoiceRegistry([Provider()]))
    actual_synthesize = VoiceRegistry.synthesize
    def synthesize(registry, text, *args, **kwargs):
        state.requests.append(text)
        return actual_synthesize(registry, text, *args, **kwargs)
    monkeypatch.setattr(VoiceRegistry, 'synthesize', synthesize)
    player = SimpleNamespace(state='stopped', position_seconds=0,
        stop=lambda: None, close=lambda: None, play=lambda path: state.played.append(Path(path)),
        pause=lambda: None, resume=lambda: None)
    monkeypatch.setattr(speech_dialog, 'WavePlayer', lambda: player)

    def find(name):
        matches = [c for c in state.controls if c.name == name and not c.destroyed]
        assert len(matches) == 1, (name, [c.name for c in state.controls if not c.destroyed])
        return matches[0]
    def click(name):
        control = find(name)
        assert control.IsEnabled(), name
        control.handlers['button'](None)
    def export_project(name='result.reading.json'):
        path = tmp_path / name
        state.paths.append(path); click('Save reading copy as')
        return ReadingDocument.load(path)
    def guard():
        with app.read(workspace) as db:
            TopicTree(db, workspace)._visible(topic)
    def run(scenario):
        state.scenario = scenario
        speech_dialog.show_speech_reader(state.parent, source_guard=guard,
            source_loader=lambda cancelled: read_source_range(desk, 1, 1, guard=guard, cancelled=cancelled))
        while state.jobs: complete()
        drain()
        assert app.assets.resolve(book['asset']).read_bytes() == raw.encode('utf-8')
    state.find, state.click, state.export_project, state.run = find, click, export_project, run
    yield state


def test_explicit_clipboard_and_native_line_paragraph_reuse_speech_without_rebinding_source(reader_ui):
    def scenario(e):
        assert e.clipboard_reads == 0 and not e.requests
        editor = e.find('Reading copy (editable; Ctrl+S saves)')
        prefix = '🚂 First line.\r\nDr. '
        editor.SetInsertionPoint(len(prefix.encode('utf-16-le')) // 2)
        e.click('Read current logical line'); e.complete()
        assert ''.join(e.requests) == 'Dr. Smith has 12 kg.'
        e.requests.clear()
        e.click('Read current paragraph'); e.complete()
        assert ''.join(e.requests) == '🚂 First line.\nDr. Smith has 12 kg.'
        e.requests.clear()
        e.click('Read clipboard once'); e.complete()
        assert ''.join(e.requests) == e.clipboard
        assert (e.clipboard_reads, e.clipboard_opens, e.clipboard_closes) == (1, 1, 1)
        assert editor.GetValue() == e.raw
        saved = e.export_project()
        assert saved.source_anchor == e.source.reading_document().source_anchor
        assert saved.source_text == saved.text == e.raw
    reader_ui.run(scenario)


def test_dictionary_controls_validate_edit_remove_and_roundtrip_existing_format(reader_ui):
    def dictionary(e):
        e.find('Pattern to pronounce').SetValue('Dr.')
        e.find('Spoken replacement').SetValue('Doctor')
        e.find('Exact language code; empty means any').SetValue('en')
        e.click('Add rule')
        e.find('Spoken replacement').SetValue('Doktor')
        e.click('Save rule changes')
        export = e.tmp_path / 'rules.json'
        e.paths.append(export); e.click('Export dictionary JSON'); e.complete()
        record = json.loads(export.read_text())
        assert record['schema'] == 'carr.pronunciations.v1'
        assert record['rules'][0]['replacement'] == 'Doktor'
        e.click('Remove selected rule')
        assert e.find('Pronunciation rules').items == []
        e.paths.append(export); e.click('Import dictionary JSON'); e.complete()
        assert len(e.find('Pronunciation rules').items) == 1
        e.find('Pronunciation rules').SetSelection(0)
        e.find('Pattern to pronounce').SetValue('')
        e.click('Save rule changes')
        assert 'nonempty' in e.find('Dictionary operation status').GetValue()
        e.click('Close dictionary')
    reader_ui.dictionary_scenario = dictionary
    reader_ui.run(lambda e: e.click('Pronunciation dictionary'))


def test_dictionary_regex_apply_is_one_undoable_source_bound_edit(reader_ui):
    def dictionary(e):
        e.find('Pattern to pronounce').SetValue(r'(\d+) kg')
        e.find('Spoken replacement').SetValue(r'\1 kilograms')
        e.find('Use regular expression').SetValue(True)
        e.find('Exact language code; empty means any').SetValue('en')
        e.click('Add rule'); e.click('Apply to reading copy')
        assert e.find('Reading copy (editable; Ctrl+S saves)').GetValue() == e.raw
        e.complete()
        assert '12 kilograms' in e.find('Reading copy (editable; Ctrl+S saves)').GetValue()
        e.click('Close dictionary')
    def scenario(e):
        e.click('Pronunciation dictionary')
        saved = e.export_project()
        assert saved.revision == 1 and saved.source_text == e.raw
        assert saved.source_anchor == e.source.reading_document().source_anchor
        changed = saved.text.index('kilograms')
        assert saved.source_span(changed, changed + len('kilograms')).exact is False
        undo = next(c for c in e.controls if c.label == '&Undo reading edit' and not c.destroyed)
        undo.handlers['button'](None)
        assert e.find('Reading copy (editable; Ctrl+S saves)').GetValue() == e.raw
    reader_ui.dictionary_scenario = dictionary
    reader_ui.run(scenario)


@pytest.mark.parametrize('boundary', ['before-worker', 'after-worker', 'closed-dictionary', 'edited-copy'])
def test_dictionary_cancel_and_stale_copy_never_commit_a_late_replacement(reader_ui, boundary):
    def dictionary(e):
        e.find('Pattern to pronounce').SetValue('Dr.')
        e.find('Spoken replacement').SetValue('Doctor')
        e.click('Add rule'); e.click('Apply to reading copy')
        if boundary == 'after-worker':
            e.complete(deliver=False); e.click('Cancel dictionary operation'); e.drain()
        elif boundary == 'before-worker':
            e.click('Cancel dictionary operation'); e.complete()
        elif boundary == 'closed-dictionary':
            e.click('Close dictionary'); e.complete()
        else:
            e.find('Reading copy (editable; Ctrl+S saves)').SetValue('User edited before completion.')
            e.complete()
        expected = 'User edited before completion.' if boundary == 'edited-copy' else e.raw
        assert e.find('Reading copy (editable; Ctrl+S saves)').GetValue() == expected
        if boundary != 'closed-dictionary': e.click('Close dictionary')
    reader_ui.dictionary_scenario = dictionary
    reader_ui.run(lambda e: e.click('Pronunciation dictionary'))


@pytest.mark.parametrize('changed', ['object', 'root', 'closing'])
def test_queued_clipboard_work_rechecks_library_identity_before_synthesis(reader_ui, changed):
    def scenario(e):
        e.click('Read clipboard once')
        original_root = e.app.root
        if changed == 'object': e.parent.library = LibraryApp(e.app.root)
        elif changed == 'root': e.app.root = e.tmp_path / 'changed-root'
        else: e.parent._closing = True
        e.complete()
        assert e.requests == [] and e.played == []
        e.app.root = original_root
    reader_ui.run(scenario)


def test_clipboard_change_during_native_read_is_rejected_and_handle_is_closed(reader_ui):
    def scenario(e):
        e.clipboard_hook = lambda: setattr(e.parent, '_closing', True)
        e.click('Read clipboard once')
        assert not e.jobs and not e.requests and e.clipboard_closes == 1
    reader_ui.run(scenario)


def test_busy_clipboard_command_cannot_read_or_clear_pending_stop(reader_ui):
    def scenario(e):
        e.click('Read current logical line')
        e.click('Stop speech operation')
        e.find('Read clipboard once').handlers['button'](None)
        assert e.clipboard_reads == 0
        e.complete()
        assert not e.requests and not e.played
    reader_ui.run(scenario)


def test_close_with_pending_speech_rejects_late_playback_and_disposes_owned_voice(reader_ui):
    def scenario(e):
        e.click('Read clipboard once')
        # Returning from the actual modal function starts its real cleanup path.
    reader_ui.run(scenario)
    assert not reader_ui.requests and not reader_ui.played
    assert reader_ui.closed == [True]


@pytest.mark.parametrize('target', ['existing', 'private'])
def test_dictionary_export_preserves_existing_files_and_private_state(reader_ui, target):
    def dictionary(e):
        path = (e.tmp_path / 'existing.json') if target == 'existing' else e.app.root / 'private.json'
        if target == 'existing': path.write_bytes(b'keep exact')
        e.paths.append(path)
        e.click('Export dictionary JSON')
        assert not e.jobs
        assert path.read_bytes() == b'keep exact' if target == 'existing' else not path.exists()
        e.click('Close dictionary')
    reader_ui.dictionary_scenario = dictionary
    reader_ui.run(lambda e: e.click('Pronunciation dictionary'))


def test_cancelled_dictionary_export_publishes_no_file(reader_ui):
    def dictionary(e):
        path = e.tmp_path / 'cancelled.json'
        e.paths.append(path); e.click('Export dictionary JSON')
        e.click('Cancel dictionary operation'); e.complete()
        assert not path.exists()
        e.click('Close dictionary')
    reader_ui.dictionary_scenario = dictionary
    reader_ui.run(lambda e: e.click('Pronunciation dictionary'))


@pytest.mark.parametrize('changed', ['object', 'closing', 'source-archive'])
def test_completed_synthesis_rechecks_current_source_before_late_playback(reader_ui, changed):
    def scenario(e):
        e.click('Read clipboard once')
        e.complete(deliver=False)
        assert e.requests == [e.clipboard] and not e.played
        if changed == 'object':
            e.parent.library = LibraryApp(e.app.root)
        elif changed == 'closing':
            e.parent._closing = True
        else:
            with e.app.topic_transaction(e.workspace) as tree:
                tree.archive(e.topic)
        e.drain()
        assert not e.played
    reader_ui.run(scenario)


@pytest.mark.parametrize('changed', [False, True])
def test_saved_wav_rechecks_source_before_publication(reader_ui, changed):
    def scenario(e):
        destination = e.tmp_path / 'narrated.wav'
        e.paths.append(destination)
        if changed:
            def revoke():
                with e.app.topic_transaction(e.workspace) as tree:
                    tree.archive(e.topic)
            e.render_hook = revoke
        button = next(c for c in e.controls if c.label == 'Save &WAV' and not c.destroyed)
        button.handlers['button'](None)
        future = e.complete()
        assert e.chunks, 'The source change must happen during actual synthesis.'
        if changed:
            assert future.exception() is not None
            assert not destination.exists()
        else:
            assert future.exception() is None
            with wave.open(str(destination), 'rb') as result:
                assert result.getnframes() > 0
        assert not list(e.tmp_path.glob('.speech-*'))
    reader_ui.run(scenario)


def test_dictionary_export_rechecks_a_new_destination_after_queueing(reader_ui):
    def dictionary(e):
        path = e.tmp_path / 'competing.json'
        e.paths.append(path)
        e.click('Export dictionary JSON')
        path.write_bytes(b'other writer owns this result')
        e.complete()
        assert path.read_bytes() == b'other writer owns this result'
        assert 'changed' in e.find('Dictionary operation status').GetValue().lower()
        e.click('Close dictionary')
    reader_ui.dictionary_scenario = dictionary
    reader_ui.run(lambda e: e.click('Pronunciation dictionary'))


def test_voice_reader_commands_use_the_shared_keymap_and_keep_stop_available(reader_ui):
    manager = keyboard_settings.KeyboardManager()
    reader_ui.parent.keyboard_commands = manager
    def scenario(e):
        adapter, = tuple(manager._adapters)
        assert adapter.context == 'voice_reader'
        manager.apply_overrides({'voice_reader.line': 'Ctrl+Shift+L'})
        assert adapter.shortcut('voice_reader.line') == 'Ctrl+Shift+L'
        assert 'Ctrl+Shift+L' in e.find('Read current logical line').label
        assert adapter.invoke('voice_reader.line')
        assert not adapter.invoke('voice_reader.clipboard')
        assert adapter.invoke('voice_reader.stop')
        e.complete()
        assert not e.requests and not e.played and e.clipboard_reads == 0
        with pytest.raises(ValueError):
            manager.apply_overrides({'voice_reader.line': 'Ctrl+C'})
        assert adapter.shortcut('voice_reader.line') == 'Ctrl+Shift+L'
    reader_ui.run(scenario)
    assert not tuple(manager._adapters)


def test_dictionary_requires_explicit_save_of_a_changed_rule_form_before_apply(reader_ui):
    def dictionary(e):
        e.find('Pattern to pronounce').SetValue('Dr.')
        e.find('Spoken replacement').SetValue('Doctor')
        e.click('Add rule')
        e.find('Spoken replacement').SetValue('Professor')
        e.click('Apply to reading copy')
        assert not e.jobs and e.find('Reading copy (editable; Ctrl+S saves)').GetValue() == e.raw
        assert 'Save the edited rule' in e.find('Dictionary operation status').GetValue()
        e.click('Save rule changes')
        e.click('Apply to reading copy'); e.complete()
        assert 'Professor Smith' in e.find('Reading copy (editable; Ctrl+S saves)').GetValue()
        e.click('Close dictionary')
    reader_ui.dictionary_scenario = dictionary
    reader_ui.run(lambda e: e.click('Pronunciation dictionary'))
