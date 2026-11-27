"""Narration reads through real library authorization and immutable originals."""
from hashlib import sha256
from itertools import count
from queue import Empty, Queue
from threading import get_ident
from types import SimpleNamespace
import json
import sys
import time

import pytest

from carr_library.application import LibraryApp
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.research.topic_tree import TopicTree
from carr_library.speech import ReadingDocument, Voice, VoiceRegistry
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.ui import speech_dialog as ui
from carr_library.speech.source import read_source_range


def test_reading_copy_keeps_library_original_and_saved_evidence(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Narration source')
    topic = app.create_topic(workspace, 'Evidence')
    text = 'Exact railway evidence.\r\nOriginal second line.'
    imported = app.add_text(workspace, 'A real synthetic source', text)
    hit, = app.search(workspace, 'railway evidence')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Keep this note.')
    original_evidence = app.evidence(workspace, topic)
    desk = ReadingDesk(app, workspace, imported['edition'], topic=topic)
    source = read_source_range(desk, 1, 1)
    assert source.text == text
    assert source.workspace == workspace and source.edition == imported['edition']
    assert source.revision == desk.revision
    assert source.positions[0]['text_sha256'] == sha256(text.encode('utf-8')).hexdigest()
    copy = ReadingDocument.from_narration_source(source)
    copy.set_text('An independently authored reading copy.')
    assert app.assets.resolve(imported['asset']).read_bytes() == text.encode('utf-8')
    assert app.evidence(workspace, topic) == original_evidence
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'railway evidence'
    assert copy.source_anchor['quotation_authority'] is False


def test_saved_lineage_distinguishes_identical_text_and_preserves_derived_separators(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    raw = 'Exact оригінал.\r\nŽltý e\u0301.\f\fNext original section.'
    sources, documents = [], []
    for number, prefix in enumerate((b'', b'\xef\xbb\xbf')):
        workspace = app.create_workspace(f'Source library {number}')
        original = tmp_path / f'original-{number}.txt'
        original.write_bytes(prefix + raw.encode('utf-8'))
        imported = app.import_file(workspace, original, title='Identical source text')
        desk = ReadingDesk(app, workspace, imported['edition'])
        source = read_source_range(desk, 1, len(desk.pages))
        copy = ReadingDocument.from_narration_source(source)
        anchor = copy.source_anchor
        assert (anchor['workspace'], anchor['edition'], anchor['revision'], anchor['asset_sha256']) == (
            workspace, desk.edition, desk.revision, imported['asset'])
        assert source.asset_sha256 == sha256(original.read_bytes()).hexdigest()
        assert [p['text_verified_at_read'] for p in anchor['positions']] == [True, True, True]
        assert [p['ordinal'] for p in anchor['positions']] == [1, 2, 3]
        assert all(p['kind'] == 'logical_segment' for p in anchor['positions'])
        assert anchor['quotation_authority'] is False
        for position, page in zip(anchor['positions'], raw.split('\f')):
            assert source.text[position['start']:position['end']] == page
            assert position['text_sha256'] == sha256(page.encode('utf-8')).hexdigest()
        for separator in anchor['derived_separators']:
            assert copy.source_span(separator['start'], separator['end']).start is None
            assert not copy.source_span(separator['start'], separator['end']).exact
        assert not copy.source_span(0, len(copy.text)).exact
        copy.replace(0, 5, 'Changed narration')
        output = tmp_path / f'source-{number}.reading.json'
        copy.save(output)
        reopened = ReadingDocument.load(output)
        assert reopened.source_anchor == anchor
        assert reopened.source_text == source.text and reopened.text == copy.text
        assert reopened.matches_source(source)
        assert reopened.undo() and reopened.text == source.text
        assert not reopened.source_span(0, len(reopened.text)).exact
        assert app.assets.resolve(imported['asset']).read_bytes() == prefix + raw.encode('utf-8')
        sources.append(source)
        documents.append(reopened)
    assert sources[0].text == sources[1].text
    for name in ('workspace', 'edition', 'revision', 'asset_sha256'):
        assert documents[0].source_anchor[name] != documents[1].source_anchor[name]
    assert not documents[0].matches_source(sources[1])
    assert not documents[1].matches_source(sources[0])


def test_source_anchor_retains_old_revision_without_reattaching_or_authorizing(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Source')
    imported = app.add_text(workspace, 'Source', 'Unchanged original text.')
    desk = ReadingDesk(app, workspace, imported['edition'])
    source = read_source_range(desk, 1, 1)
    copy = ReadingDocument.from_narration_source(source)
    path = tmp_path / 'old.reading.json'
    copy.save(path)
    app.update_metadata(workspace, imported['edition'], title='New catalog metadata')
    current = ReadingDesk(app, workspace, imported['edition'])
    current_source = read_source_range(current, 1, 1)
    reopened = ReadingDocument.load(path)
    assert reopened.source_text == current_source.text
    assert reopened.source_anchor['revision'] == source.revision != current.revision
    assert not reopened.matches_source(current_source)
    with pytest.raises(ValueError, match='revision changed'):
        read_source_range(desk, 1, 1)
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        read_source_range(current, 1, 1)
    # Retained user-owned work remains readable; it cannot authorize a source.
    assert ReadingDocument.load(path).source_anchor == reopened.source_anchor
    assert reopened.source_anchor['quotation_authority'] is False


def test_new_active_revision_rejects_stale_narration_source(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Source')
    imported = app.add_text(workspace, 'Source', 'A source for narration.')
    desk = ReadingDesk(app, workspace, imported['edition'])
    app.update_metadata(workspace, imported['edition'], title='New source metadata')
    with pytest.raises(ValueError):
        read_source_range(desk, 1, 1)


def test_source_range_guard_cancel_and_character_budget(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Source')
    imported = app.add_text(workspace, 'Source', 'A source for narration.')
    desk = ReadingDesk(app, workspace, imported['edition'])
    with pytest.raises(InterruptedError):
        read_source_range(desk, 1, 1, cancelled=lambda: True)
    with pytest.raises(ValueError, match='budget'):
        read_source_range(desk, 1, 1, max_characters=4)
    def denied():
        raise ValueError('Source authorization revoked.')
    with pytest.raises(ValueError, match='revoked'):
        read_source_range(desk, 1, 1, guard=denied)


@pytest.mark.parametrize('archive_during_picker', [False, True], ids=['authorized', 'late-topic-archive'])
@pytest.mark.parametrize('recovery_case', ['none', 'exact', 'legacy', 'foreign', 'revision'])
@pytest.mark.parametrize('voice_available', [True, False], ids=['voice', 'no-voice'])
def test_source_save_as_reauthorizes_after_picker(tmp_path, monkeypatch, archive_during_picker, recovery_case, voice_available):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic source')
    topic = app.create_topic(workspace, 'Bound topic')
    original = 'Exact original source.\r\nRetain Ukrainian: оригінал.'
    imported = app.add_text(workspace, 'Synthetic source book', original)
    desk = ReadingDesk(app, workspace, imported['edition'], topic=topic)
    source = read_source_range(desk, 1, 1)
    expected_editor = original
    store = ReaderDraftStore(app.root)
    prior_draft = None
    if recovery_case != 'none':
        if recovery_case == 'legacy':
            previous = ReadingDocument(original)
        elif recovery_case == 'foreign':
            other_workspace = app.create_workspace('Other authorized source')
            other = app.add_text(other_workspace, 'Identical source text', original)
            previous = ReadingDocument.from_narration_source(read_source_range(
                ReadingDesk(app, other_workspace, other['edition']), 1, 1))
        else:
            previous = ReadingDocument.from_narration_source(source)
        previous.set_text('Recovered reading edit.')
        store.save(ReaderResume(previous))
        prior_draft = store.path.read_bytes()
        if recovery_case == 'exact':
            expected_editor = previous.text
        elif recovery_case == 'revision':
            app.update_metadata(workspace, imported['edition'], title='New catalog metadata')
            desk = ReadingDesk(app, workspace, imported['edition'], topic=topic)
    output = tmp_path / 'reading.reading.json'
    main = get_ident()
    callbacks, controls = Queue(), []
    guard_calls = []
    statuses = []

    def scope():
        guard_calls.append(get_ident())
        with app.read(workspace) as db:
            TopicTree(db, workspace)._visible(topic)

    class Provider:
        name = 'Synthetic enumeration only'
        def voices(self):
            return (Voice('fixture:local', 'Fixture voice', 'fixture', 'en'),) if voice_available else ()
        def close(self):
            pass

    monkeypatch.setattr('carr_library.runtime_settings.voice_registry',
                        lambda *args, **kwargs: VoiceRegistry((Provider(),)))

    class Control:
        def __init__(self, parent=None, *args, **kwargs):
            self.parent = parent
            self.label = kwargs.get('label', next((a for a in args if isinstance(a, str)), ''))
            self.value = kwargs.get('value', kwargs.get('initial', ''))
            self.name, self.items, self.selection = '', [], -1
            self.enabled, self.destroyed, self.cursor = True, False, 0
            self.handlers = {}
            controls.append(self)
        def native(self):
            assert get_ident() == main
            assert not self.destroyed
        def SetName(self, value): self.native(); self.name = value
        def SetValue(self, value): self.native(); self.value = value
        def ChangeValue(self, value): self.SetValue(value)
        def GetValue(self): self.native(); return self.value
        def Set(self, values): self.native(); self.items = list(values)
        def SetSelection(self, value): self.native(); self.selection = value
        def GetSelection(self): self.native(); return self.selection
        def GetStringSelection(self): self.native(); return ''
        def SetInsertionPoint(self, value): self.native(); self.cursor = value
        def GetInsertionPoint(self): self.native(); return self.cursor
        def Enable(self, value=True): self.native(); self.enabled = value
        def Disable(self): self.Enable(False)
        def SetFocus(self): self.native(); assert self.enabled
        def Add(self, *args): self.native()
        def SetSizer(self, *args): self.native()
        def SetEscapeId(self, *args): self.native()
        def SetAcceleratorTable(self, *args): self.native()
        def Bind(self, event, callback, *args, **kwargs): self.native(); self.handlers[event] = callback
        def Start(self, *args): self.native()
        def Stop(self): self.native()
        def EndModal(self, result): self.native()
        def Destroy(self): self.native(); self.destroyed = True

    def named(label):
        return next(control for control in controls if control.label == label)

    class Dialog(Control):
        def ShowModal(self):
            self.native()
            deadline = time.monotonic() + 5
            while not named('Reload &voices').enabled:
                assert time.monotonic() < deadline, 'Canonical discovery did not complete.'
                try:
                    callback, args = callbacks.get(timeout=.02)
                except Empty:
                    continue
                callback(*args)
            editor = next(control for control in controls if control.name.startswith('Reading copy (editable'))
            assert editor.GetValue() == expected_editor
            statuses.append(next(c for c in controls if c.name == 'Speech and save status').GetValue())
            scope()
            named('Save &as').handlers['button'](None)
            statuses.append(next(c for c in controls if c.name == 'Speech and save status').GetValue())
            return 5101

    class FileDialog(Control):
        def __enter__(self): return self
        def __exit__(self, *args): self.Destroy()
        def ShowModal(self):
            self.native()
            if archive_during_picker:
                with app.topic_transaction(workspace) as tree:
                    tree.archive(topic)
            return 5100
        def GetPath(self): self.native(); return str(output)

    identifiers = count(10000)
    wx = SimpleNamespace(Dialog=Dialog, FileDialog=FileDialog, TextEntryDialog=FileDialog,
        TextCtrl=Control, StaticText=Control, Choice=Control, SpinCtrl=Control,
        Button=Control, BoxSizer=Control, Timer=Control,
        CallAfter=lambda callback, *args: callbacks.put((callback, args)),
        NewIdRef=lambda: next(identifiers), AcceleratorTable=lambda entries: entries,
        MessageBox=lambda *args, **kwargs: 4, ID_OK=5100, ID_CANCEL=5101,
        NOT_FOUND=-1, YES=2, NO=4, CANCEL=8, EVT_BUTTON='button', EVT_MENU='menu',
        EVT_CHOICE='choice', EVT_CLOSE='close', EVT_TIMER='timer')
    for constant in ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'LEFT',
            'TOP', 'RIGHT', 'EXPAND', 'ALL', 'ALIGN_CENTER_VERTICAL', 'ALIGN_RIGHT', 'TE_READONLY',
            'TE_MULTILINE', 'TE_DONTWRAP', 'FD_SAVE', 'FD_OPEN', 'FD_FILE_MUST_EXIST', 'YES_NO',
            'ICON_QUESTION', 'ACCEL_CTRL', 'ACCEL_SHIFT', 'WXK_F6', 'WXK_F7', 'WXK_F8'):
        setattr(wx, constant, 0)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    monkeypatch.setattr(ui, 'WavePlayer', lambda: SimpleNamespace(
        position_seconds=0, state='stopped', stop=lambda: None, close=lambda: None))
    parent = SimpleNamespace(library=app, report=statuses.append)

    ui.show_speech_reader(parent, source_guard=scope,
        source_loader=lambda cancelled: read_source_range(desk, 1, 1, guard=scope, cancelled=cancelled))
    assert app.assets.resolve(imported['asset']).read_bytes() == original.encode('utf-8')
    assert guard_calls
    if recovery_case in ('legacy', 'foreign', 'revision'):
        assert store.path.read_bytes() == prior_draft
        assert any('no matching source identity' in message for message in statuses)
    if archive_during_picker:
        with pytest.raises(ValueError):
            scope()
        assert not output.exists(), ('A source-bound reading project was written after its topic '
                                     'was archived in Save As: ' + repr(statuses))
    else:
        assert ReadingDocument.load(output).source_text == original
        assert ReadingDocument.load(output).text == expected_editor
        saved = json.loads(output.read_text(encoding='utf-8'))
        anchor = saved.get('source_anchor', {})
        assert (anchor.get('workspace'), anchor.get('edition'), anchor.get('revision'),
                anchor.get('asset_sha256')) == (workspace, desk.edition, desk.revision,
                                                imported['asset'])
