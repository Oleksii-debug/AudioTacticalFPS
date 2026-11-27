"""Execute real reader focus/render closures with exact selected text snapshots."""
import ast
from hashlib import sha256
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.speech.runtime import SpeechResult
from carr_library.ui import reading_focus, speech_dialog


class Editor:
    def __init__(self, text, selection=(0, 0), cursor=0):
        self.value, self.selection, self.cursor = text, selection, cursor
        self.focus = 0
    def GetValue(self): return self.value
    def GetSelection(self): return self.selection
    def GetStringSelection(self): return self.value[slice(*self.selection)]
    def GetInsertionPoint(self): return self.cursor
    def GetLastPosition(self): return len(self.value)
    def GetRange(self, first, last): return self.value[first:last]
    def SetInsertionPoint(self, value): self.cursor = value
    def SetFocus(self): self.focus += 1


def extract(name, values):
    path = Path(speech_dialog.__file__)
    module = ast.parse(path.read_text(encoding='utf-8'))
    outer = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == 'show_speech_reader')
    callback = next(n for n in outer.body if isinstance(n, ast.FunctionDef) and n.name == name)
    assignments = '\n'.join('    ' + key + ' = values[' + repr(key) + ']' for key in values)
    getter = '{' + ', '.join(repr(key) + ': ' + key for key in values) + '}'
    factory = ast.parse('def build(values):\n' + assignments
        + '\n    return ' + name + ', lambda: ' + getter + '\n')
    factory.body[0].body.insert(-1, callback)
    namespace = dict(speech_dialog.__dict__)
    exec(compile(ast.fix_missing_locations(factory), str(path), 'exec'), namespace)
    return namespace['build'](values)


@pytest.mark.parametrize('action', ['open', 'goto', 'changed', 'source', 'closing', 'busy'])
def test_actual_focus_hook_rechecks_snapshot_and_changes_cursor_only_explicitly(tmp_path, monkeypatch, action):
    doc, parent = ReadingDocument('😀 Original first. Другий рядок.'), SimpleNamespace(_closing=False)
    editor = Editor(doc.text, cursor=2)
    saved, report, show = Mock(), Mock(), Mock()
    scope, source = Mock(), Mock()
    values = dict(alive=True, busy=action == 'busy', parent=parent, document=doc, editor=editor,
        playback_scope=scope, source_guard=source, sync_document=lambda: doc.set_text(editor.value),
        last_reading_audio=None, player=SimpleNamespace(path=None), dialog=object(),
        pause_audio=Mock(), stop_audio=Mock(), save_recovery=saved, report=report)
    def opened(_parent, **options):
        show()
        assert options['audio'] is None and options['cursor'] == 2
        options['guard']()
        if action == 'goto': options['goto'](12)
        elif action == 'changed':
            doc.set_text('Changed text.')
            options['goto'](2)
        elif action == 'source':
            source.side_effect = ValueError('Source authorization changed.')
            options['goto'](2)
        elif action == 'closing':
            parent._closing = True
            options['goto'](2)
    monkeypatch.setattr(reading_focus, 'show_reading_focus', opened)
    handler, _ = extract('choose_reading_focus', values)
    handler()
    assert editor.focus == 1
    if action == 'goto':
        assert editor.cursor == 12
        saved.assert_called_once_with(force=True)
    else:
        assert editor.cursor == 2
        saved.assert_not_called()
    if action in ('changed', 'source', 'closing', 'busy'): report.assert_called_once()
    else: report.assert_not_called()
    if action == 'busy': show.assert_not_called()
    else: show.assert_called_once()
    assert doc.source_text == '😀 Original first. Другий рядок.'


@pytest.mark.parametrize('selection,cursor,from_cursor,expected', [
    ((0, 0), 0, False, (0, None)),
    ((2, 10), 0, False, (2, 10)),
    ((0, 0), 12, True, (12, None)),
])
def test_real_speak_binds_only_the_exact_native_selected_rendered_text(tmp_path, selection, cursor, from_cursor, expected):
    text = '😀 Original first. Другий рядок. End'
    doc, editor = ReadingDocument(text), Editor(text, selection, cursor)
    work, produced = [], []
    def synthesize(content, voice_id, destination, **kwargs):
        result = SpeechResult(Path(destination), 'f' * 64, 2, 24000, 1, 48000, voice_id,
                              sha256(content.encode('utf-8')).hexdigest(), 1)
        produced.append(content)
        return result
    def selected(from_cursor=False):
        return text[cursor:] if from_cursor else editor.GetStringSelection() or text
    def run(callback, done, **kwargs):
        work.append((callback, done))
        return True
    player, report = Mock(), Mock()
    values = dict(last_audio=None, last_reading_audio=None, playback_generation=0,
        registry=SimpleNamespace(synthesize=synthesize), voices=(SimpleNamespace(id='fixture:exact'),),
        source_guard=Mock(), selected_text=selected, document=doc, editor=editor,
        voice_choice=SimpleNamespace(GetSelection=lambda: 0),
        rate=SimpleNamespace(GetValue=lambda: 0), volume=SimpleNamespace(GetValue=lambda: 71),
        player=player, progress=None, audio_files=SimpleNamespace(name=str(tmp_path)),
        cancel=Event(), run=run, report=report, capabilities=Mock(),
        start_playback=lambda callback: callback())
    handler, current = extract('speak', values)
    handler(from_cursor=from_cursor)
    assert len(work) == 1
    callback, done = work.pop()
    done(callback())
    binding = current()['last_reading_audio']
    first, last = (expected[0], len(text) if expected[1] is None else expected[1])
    assert (binding.start, binding.end) == (first, last)
    assert produced == [text[first:last]]
    assert binding.document is doc and binding.frames == 48000
    assert doc.text == doc.source_text == text


def test_late_render_cannot_attach_timing_to_a_changed_reading_copy(tmp_path):
    doc, editor, work = ReadingDocument('Original selected text.'), Editor('Original selected text.'), []
    def run(callback, done, **kwargs): work.append((callback, done)); return True
    text = doc.text
    result = SpeechResult(tmp_path / 'render.wav', 'f' * 64, 1, 24000, 1, 24000,
                          'fixture:exact', sha256(text.encode('utf-8')).hexdigest(), 1)
    values = dict(last_audio=None, last_reading_audio=None, playback_generation=0,
        registry=SimpleNamespace(synthesize=lambda *_a, **_k: result),
        voices=(SimpleNamespace(id='fixture:exact'),), source_guard=Mock(),
        selected_text=lambda _from_cursor: text, document=doc, editor=editor,
        voice_choice=SimpleNamespace(GetSelection=lambda: 0), rate=SimpleNamespace(GetValue=lambda: 0),
        volume=SimpleNamespace(GetValue=lambda: 71), player=Mock(), progress=None,
        audio_files=SimpleNamespace(name=str(tmp_path)), cancel=Event(), run=run, report=Mock(),
        capabilities=Mock(), start_playback=lambda callback: callback())
    handler, current = extract('speak', values)
    handler()
    doc.set_text('Different reading revision.')
    callback, done = work.pop()
    done(callback())
    assert current()['last_reading_audio'] is None
    assert doc.source_text == text
