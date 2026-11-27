"""Narration reads through real library authorization and immutable originals."""
from hashlib import sha256
from itertools import count
from queue import Empty, Queue
from threading import get_ident
from types import SimpleNamespace
import sys
import time

import pytest

from carr_library.application import LibraryApp
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.research.topic_tree import TopicTree
from carr_library.speech import ReadingDocument, Voice, VoiceRegistry
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
    copy = source.reading_document()
    copy.set_text('An independently authored reading copy.')
    assert app.assets.resolve(imported['asset']).read_bytes() == text.encode('utf-8')
    assert app.evidence(workspace, topic) == original_evidence
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'railway evidence'


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
def test_source_save_as_reauthorizes_after_picker(tmp_path, monkeypatch, archive_during_picker):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic source')
    topic = app.create_topic(workspace, 'Bound topic')
    original = 'Exact original source.\r\nRetain Ukrainian: оригінал.'
    imported = app.add_text(workspace, 'Synthetic source book', original)
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
            return (Voice('fixture:local', 'Fixture voice', 'fixture', 'en'),)
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
            while not named('Read &selection / all (F6)').enabled:
                assert time.monotonic() < deadline, 'Canonical discovery did not complete.'
                try:
                    callback, args = callbacks.get(timeout=.02)
                except Empty:
                    continue
                callback(*args)
            editor = next(control for control in controls if control.name.startswith('Reading copy (editable'))
            assert editor.GetValue() == original
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
    if archive_during_picker:
        with pytest.raises(ValueError):
            scope()
        assert not output.exists(), ('A source-bound reading project was written after its topic '
                                     'was archived in Save As: ' + repr(statuses))
    else:
        reopened = ReadingDocument.load(output)
        assert reopened.source_text == original
        assert reopened.source_anchor == read_source_range(desk, 1, 1).reading_document().source_anchor
        assert reopened.source_anchor['quotation_authority'] is False


def test_source_lineage_is_distinct_and_survives_save_and_backup(tmp_path):
    from carr_library.library_backup import restore_backup
    from carr_library.speech.session import ReaderDraftStore, ReaderResume

    app = LibraryApp(tmp_path / 'library')
    raw = 'Київ — e\u0301\r\n\fDruhý oddiel'
    first = app.create_workspace('First research library')
    second = app.create_workspace('Second research library')
    one = app.add_text(first, 'Identical title', raw)
    two = app.add_text(second, 'Identical title', raw)
    desk = ReadingDesk(app, first, one['edition'])
    other = ReadingDesk(app, second, two['edition'])
    doc = read_source_range(desk, 1, len(desk.pages)).reading_document()
    other_doc = read_source_range(other, 1, len(other.pages)).reading_document()
    assert doc.source_text == other_doc.source_text
    assert doc.source_anchor != other_doc.source_anchor
    anchor = doc.source_anchor
    assert (anchor['workspace'], anchor['edition'], anchor['revision'], anchor['asset_sha256']) == (
        first, one['edition'], desk.revision, one['asset'])
    assert anchor['quotation_authority'] is False
    assert len(anchor['positions']) == 2
    a, b = anchor['positions']
    assert a['kind'] == b['kind'] == 'logical_segment'
    assert doc.source_text[a['end']:b['start']] == '\n\n'
    assert not doc.source_span(a['end'], b['start']).exact
    assert not doc.source_span(0, len(doc.source_text)).exact
    doc.replace(0, 4, 'Edited')
    assert doc.source_anchor == anchor
    path = tmp_path / 'portable.reading.json'
    doc.save(path)
    reopened = ReadingDocument.load(path)
    assert reopened.source_anchor == anchor and reopened.text == doc.text
    assert reopened.undo() and reopened.text == doc.source_text
    assert reopened.source_anchor == anchor
    copied = reopened.source_anchor
    copied['positions'][0]['start'] = 99
    assert reopened.source_anchor == anchor
    store = ReaderDraftStore(app.root, slot='synthetic-bound-source')
    store.save(ReaderResume(doc))
    backup = tmp_path / 'backup'
    manifest = app.backup(backup)
    destination = tmp_path / 'restored'
    restore_backup(backup, destination, expected_manifest_sha256=manifest)
    restored = ReaderDraftStore(destination, slot='synthetic-bound-source').load().document
    assert restored.source_anchor == anchor and restored.text == doc.text
    restored_app = LibraryApp(destination)
    restored_desk = ReadingDesk(restored_app, first, one['edition'])
    assert read_source_range(restored_desk, 1, len(restored_desk.pages)).reading_document().source_anchor == anchor
    assert ReadingDocument.from_dict(ReadingDocument(doc.source_text).to_dict()).source_anchor is None


@pytest.mark.parametrize('mutation', ['authority', 'hash', 'range', 'kind', 'separator', 'extra', 'null'])
def test_portable_source_anchor_rejects_inconsistent_data(tmp_path, mutation):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Source')
    imported = app.add_text(workspace, 'Source', 'A source\fSecond segment')
    desk = ReadingDesk(app, workspace, imported['edition'])
    doc = read_source_range(desk, 1, len(desk.pages)).reading_document()
    data = doc.to_dict()
    anchor = data['source_anchor']
    if mutation == 'authority':
        anchor['quotation_authority'] = True
    elif mutation == 'hash':
        anchor['positions'][0]['text_sha256'] = '0' * 64
    elif mutation == 'range':
        anchor['positions'][0]['start'] = 1
    elif mutation == 'kind':
        anchor['positions'][0]['kind'] = 'printed_page'
    elif mutation == 'separator':
        anchor['positions'][1]['start'] -= 1
    elif mutation == 'extra':
        anchor['printed_page'] = 'iv'
    else:
        data['source_anchor'] = None
    with pytest.raises(ValueError):
        ReadingDocument.from_dict(data)
