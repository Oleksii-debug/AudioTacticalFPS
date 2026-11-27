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
from carr_library.speech import ReadingDocument, SourceSpan, Voice, VoiceRegistry
from carr_library.speech.session import ReaderDraftStore, ReaderResume, open_source_draft
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
def test_source_save_as_reauthorizes_after_picker_without_draft(tmp_path, monkeypatch, archive_during_picker):
    _exercise_reader_project(tmp_path, monkeypatch, archive_during_picker=archive_during_picker)


@pytest.mark.parametrize('open_case', ['legacy', 'foreign', 'legacy-foreign'])
def test_bound_reader_rejects_unrelated_open_project(tmp_path, monkeypatch, open_case):
    _exercise_reader_project(tmp_path, monkeypatch, open_case=open_case)


@pytest.mark.parametrize('open_case', ['exact', 'legacy-origin'])
def test_bound_reader_accepts_matching_open_project(tmp_path, monkeypatch, open_case):
    _exercise_reader_project(tmp_path, monkeypatch, open_case=open_case)


@pytest.mark.parametrize('open_case', ['foreign', 'text'])
def test_standalone_reader_autosaves_same_title_project_identity(tmp_path, monkeypatch, open_case):
    _exercise_reader_project(tmp_path, monkeypatch, open_case=open_case, standalone=True)


@pytest.mark.parametrize('save_action', ['autosave', 'save', 'save-as'])
def test_reader_reports_saved_draft_when_recovery_discovery_fails(tmp_path, monkeypatch, save_action):
    _exercise_reader_project(tmp_path, monkeypatch, recovery_failure_action=save_action)


def test_save_as_reports_portable_success_when_later_recovery_write_fails(tmp_path, monkeypatch):
    _exercise_reader_project(tmp_path, monkeypatch, recovery_failure_action='save-as', fail_draft=True)


def _exercise_reader_project(tmp_path, monkeypatch, *, archive_during_picker=False,
                             open_case=None, standalone=False, recovery_failure_action=None, fail_draft=False):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic source')
    topic = app.create_topic(workspace, 'Bound topic')
    original = 'Exact original source.\r\nRetain Ukrainian: оригінал.'
    imported = app.add_text(workspace, 'Synthetic source book', original)
    desk = ReadingDesk(app, workspace, imported['edition'], topic=topic)
    source = read_source_range(desk, 1, 1)
    store = ReaderDraftStore(app.root)
    if standalone:
        previous = (ReadingDocument(original, title=source.title) if open_case == 'text'
                    else source.reading_document())
        store.save(ReaderResume(previous))
    candidate = None
    candidate_path = tmp_path / 'opened.reading.json'
    if open_case is not None:
        if open_case in ('foreign', 'legacy-foreign'):
            other_workspace = app.create_workspace('Another retained source')
            other = app.add_text(other_workspace, source.title, original)
            candidate = read_source_range(
                ReadingDesk(app, other_workspace, other['edition']), 1, 1).reading_document()
        elif open_case == 'text':
            candidate = ReadingDocument('Different source text with the same title.', title=source.title)
        elif open_case == 'legacy':
            candidate = ReadingDocument(original, title=source.title)
        else:
            candidate = source.reading_document()
        if not standalone:
            candidate.set_text('Portable reading edit.')
        candidate.save(candidate_path)
        if open_case in ('legacy-origin', 'legacy-foreign'):
            candidate_path.write_text(json.dumps(_legacy_origin_record(candidate)), encoding='utf-8')
        candidate_bytes = candidate_path.read_bytes()
    expected_final = candidate.text if standalone or open_case in ('exact', 'legacy-origin') else original
    if recovery_failure_action:
        expected_final = 'User edits that must survive the recovery failure.'
        from carr_library.speech import session
        atomic_json = session.atomic_json

        def fail_recovery_metadata(path, value, *, replace=False):
            suffix = '.reader.json' if fail_draft else '.reader-links.json'
            if path.name.endswith(suffix):
                raise OSError('Synthetic recovery metadata failure.')
            return atomic_json(path, value, replace=replace)

        monkeypatch.setattr(session, 'atomic_json', fail_recovery_metadata)
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
            if standalone:
                self.handlers['timer'](None)
                assert store.load().document.source_origin == previous.source_origin
            if candidate is not None:
                named('&Open text / project').handlers['button'](None)
                assert editor.GetValue() == expected_final
                if not standalone and open_case not in ('exact', 'legacy-origin'):
                    message = next(c for c in controls if c.name == 'Speech and save status').GetValue()
                    assert 'standalone voice reader' in message
                assert candidate_path.read_bytes() == candidate_bytes
            if standalone:
                self.handlers['timer'](None)
                resumed = store.load().document
                assert resumed.source_sha256 == candidate.source_sha256
                assert resumed.source_origin == candidate.source_origin
            if recovery_failure_action:
                editor.ChangeValue(expected_final)
                if recovery_failure_action == 'autosave':
                    self.handlers['timer'](None)
                elif recovery_failure_action == 'save':
                    named('&Save reading copy').handlers['button'](None)
                else:
                    named('Save &as').handlers['button'](None)
                message = next(c for c in controls if c.name == 'Speech and save status').GetValue()
                assert 'was not saved' not in message and 'could not be autosaved' not in message
                if fail_draft:
                    assert 'Reading copy and undo history saved:' in message
                    assert 'The recovery draft could not be updated.' in message
                    assert store.for_source(source).load() is None
                else:
                    assert 'Reading draft saved.' in message
                    assert 'Automatic source recovery could not be updated' in message
                    saved_draft = store.for_source(source).load()
                    assert saved_draft.document.text == expected_final
                    assert saved_draft.document.source_origin == source.origin
                    assert saved_draft.document.undo() and saved_draft.document.text == original
            if recovery_failure_action != 'save-as':
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
        def GetPath(self):
            self.native()
            return str(candidate_path if self.label.startswith('Open ') else output)

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
        source_loader=None if standalone else
            lambda cancelled: read_source_range(desk, 1, 1, guard=scope, cancelled=cancelled))
    assert app.assets.resolve(imported['asset']).read_bytes() == original.encode('utf-8')
    assert guard_calls
    if archive_during_picker:
        with pytest.raises(ValueError):
            scope()
        assert not output.exists(), ('A source-bound reading project was written after its topic '
                                     'was archived in Save As: ' + repr(statuses))
    else:
        saved = ReadingDocument.load(output)
        assert saved.source_text == (candidate.source_text if standalone else original)
        assert saved.text == expected_final
        expected_origin = candidate.source_origin if standalone or open_case in ('exact', 'legacy-origin') else source.origin
        assert saved.source_origin == expected_origin



def _two_equal_sources(tmp_path):
    """Actual retained originals with equal extraction, different bytes/IDs."""
    app = LibraryApp(tmp_path / 'library')
    text = 'Перший розділ e\u0301.\r\nOriginal line.\fDruhý oddiel.'
    sources = []
    for index, prefix in enumerate((b'', b'\xef\xbb\xbf')):
        path = tmp_path / f'source-{index}.txt'
        path.write_bytes(prefix + text.encode('utf-8'))
        workspace = app.create_workspace(f'Library {index}')
        imported = app.import_file(workspace, path, title='Same source title')
        desk = ReadingDesk(app, workspace, imported['edition'])
        source = read_source_range(desk, 1, len(desk.pages))
        sources.append((source, imported, desk))
    assert sources[0][0].text == sources[1][0].text
    assert sources[0][0].asset_sha256 != sources[1][0].asset_sha256
    return app, sources


def test_distinct_editions_keep_portable_identity_after_edit_save_and_reopen(tmp_path):
    app, sources = _two_equal_sources(tmp_path)
    saved = []
    for index, (source, imported, desk) in enumerate(sources):
        original = app.assets.resolve(imported['asset']).read_bytes()
        document = source.reading_document()
        document.replace(0, 6, 'Читання')
        path = tmp_path / f'narration-{index}.reading.json'
        document.save(path)
        reopened = ReadingDocument.load(path)
        origin = reopened.source_origin
        assert reopened.source_bound
        assert reopened.to_dict()['schema'] == 'carr.reading-copy.v2'
        assert origin == source.origin
        assert (origin['workspace'], origin['edition'], origin['revision'], origin['asset_sha256']) == (
            desk.workspace, desk.edition, desk.revision, imported['asset'])
        assert origin['quotation_authority'] is False
        assert reopened.source_text == source.text
        assert reopened.text == document.text
        assert reopened.undo() and reopened.text == source.text
        assert reopened.redo() and reopened.text == document.text
        assert app.assets.resolve(imported['asset']).read_bytes() == original
        saved.append(origin)
    assert saved[0] != saved[1]


def test_derived_page_separators_are_not_exact_source_spans(tmp_path):
    _, sources = _two_equal_sources(tmp_path)
    source = sources[0][0]
    document = source.reading_document()
    first, second = source.origin['positions']
    assert source.origin['derived_separators'] == [{'start': first['end'], 'end': second['start']}]
    assert document.source_span(first['end'], second['start']) == SourceSpan(None, None, False)
    assert document.source_span(first['start'], first['end']).exact
    assert not document.source_span(first['start'], second['end']).exact
    reopened = ReadingDocument.from_dict(document.to_dict())
    assert reopened.source_span(first['end'], second['start']) == SourceSpan(None, None, False)


def test_same_text_does_not_rebind_a_different_or_legacy_source(tmp_path):
    _, sources = _two_equal_sources(tmp_path)
    first, second = sources[0][0], sources[1][0]
    recovered = first.reading_document()
    recovered.set_text('Saved independently authored narration.')
    before = recovered.to_dict()
    with pytest.raises(ValueError, match='source identity'):
        second.reading_document(recovered)
    assert recovered.to_dict() == before
    legacy = ReadingDocument(first.text)
    legacy.set_text('Legacy edits preserved.')
    legacy_record = legacy.to_dict()
    assert legacy_record['schema'] == 'carr.reading-copy.v1'
    with pytest.raises(ValueError, match='source identity'):
        first.reading_document(legacy)
    assert legacy.to_dict() == legacy_record
    assert not ReadingDocument.from_dict(legacy_record).source_bound


def test_observed_verification_change_does_not_reuse_a_draft(tmp_path):
    from dataclasses import replace
    _, sources = _two_equal_sources(tmp_path)
    source = sources[0][0]
    positions = tuple({**row, 'text_verified_at_capture': not row['text_verified_at_capture']}
                      for row in source.positions)
    changed = replace(source, positions=positions)
    document = source.reading_document()
    with pytest.raises(ValueError, match='source identity'):
        changed.reading_document(document)
    changed_document = changed.reading_document()
    assert changed_document.source_origin['quotation_authority'] is False
    assert changed_document.source_anchor['positions'] == list(positions)
    assert [row['text_verified'] for row in changed_document.source_origin['positions']] == [
        row['text_verified_at_capture'] for row in positions]


def test_source_origin_is_defensively_copied(tmp_path):
    _, sources = _two_equal_sources(tmp_path)
    source = sources[0][0]
    document = source.reading_document()
    expected = document.source_origin
    exposed = document.source_origin
    exposed['edition'] = 'other-edition'
    exposed['positions'][0]['text_verified'] = not exposed['positions'][0]['text_verified']
    exposed['derived_separators'][0]['end'] += 1
    exported = document.to_dict()
    exported['source_anchor']['workspace'] = 'different-workspace'
    assert document.source_origin == expected == source.origin


@pytest.mark.parametrize('damage', ['unknown', 'authority', 'asset', 'offset', 'ordinal',
    'verified', 'separator', 'positions', 'missing_origin'])
def test_imported_source_identity_is_bounded_and_untrusted(tmp_path, damage):
    _, sources = _two_equal_sources(tmp_path)
    document = sources[0][0].reading_document()
    record = document.to_dict()
    record.pop('source_anchor')
    record['source_origin'] = document.source_origin
    origin = record['source_origin']
    if damage == 'unknown':
        origin['path'] = '/not-an-authorized-source'
    elif damage == 'authority':
        origin['quotation_authority'] = True
    elif damage == 'asset':
        origin['asset_sha256'] = 'invalid'
    elif damage == 'offset':
        origin['positions'][0]['start'] = 1
    elif damage == 'ordinal':
        origin['positions'][0]['ordinal'] = True
    elif damage == 'verified':
        origin['positions'][0]['text_verified'] = 1
    elif damage == 'separator':
        origin['derived_separators'][0]['end'] += 1
    elif damage == 'positions':
        origin['positions'] = origin['positions'] * 5001
    else:
        record['source_origin'] = None
    with pytest.raises(ValueError):
        ReadingDocument.from_dict(record)


@pytest.mark.parametrize('linked', [False, True])
def test_legacy_origin_v2_migrates_without_changing_discovery_or_history(tmp_path, linked):
    """Read actual earlier session bytes, retaining their old hashed identity."""
    from dataclasses import replace
    from carr_library.durable_state import encode

    app, sources = _two_equal_sources(tmp_path)
    source = sources[0][0]
    source = replace(source, positions=tuple({**row, 'text_verified_at_capture': i == 0}
                                            for i, row in enumerate(source.positions)))
    document = source.reading_document()
    document.replace(0, 3, 'Стар')
    edited = document.text
    document.set_text(edited + '\nA later edit retained for redo.')
    assert document.undo()
    record = document.to_dict()
    anchor = record.pop('source_anchor')
    old_origin = {key: anchor[key] for key in ('workspace', 'edition', 'revision',
                  'asset_sha256', 'quotation_authority')}
    old_origin.update(schema='carr.narration-source.v1', positions=[{
        'ordinal': row['ordinal'], 'kind': row['kind'], 'index': row['index'],
        'start': row['start'], 'end': row['end'], 'text_sha256': row['text_sha256'],
        'text_verified': row['text_verified_at_capture']} for row in anchor['positions']],
        derived_separators=[{'start': a['end'], 'end': b['start']}
                           for a, b in zip(anchor['positions'], anchor['positions'][1:])])
    record['source_origin'] = old_origin
    family = sha256(encode({'workspace': old_origin['workspace'], 'edition': old_origin['edition'],
        'first': old_origin['positions'][0]['ordinal'],
        'last': old_origin['positions'][-1]['ordinal']})).hexdigest()
    identity = sha256(encode(old_origin)).hexdigest()
    previous = ReaderDraftStore(app.root, slot=f'source-v2:{family}:{identity}')
    raw = encode({'schema': 'carr.reader-session.v1', 'document': record,
                  'voice_id': 'flite:slt', 'rate': 1, 'volume': 65, 'cursor': 3,
                  'audio_seconds': 2.5})
    previous.path.write_bytes(raw)
    if linked:
        (previous.root / (family + '.reader-links.json')).write_bytes(encode({
            'schema': 'carr.reader-recovery-links.v1', 'copies': [previous.key]}))

    resumed = previous.load()
    assert resumed.document.source_anchor == anchor
    assert resumed.document.source_origin == old_origin == source.origin
    assert resumed.document.source_bound and resumed.document.text == edited
    assert (resumed.voice_id, resumed.rate, resumed.volume, resumed.cursor,
            resumed.audio_seconds) == ('flite:slt', 1, 65, 3, 2.5)
    assert resumed.document.can_undo and resumed.document.can_redo
    assert resumed.document.redo() and resumed.document.text.endswith('retained for redo.')
    assert resumed.document.undo() and resumed.document.text == edited
    assert resumed.document.undo() and resumed.document.text == source.text
    separator, = old_origin['derived_separators']
    assert resumed.document.source_span(separator['start'], separator['end']) == SourceSpan(None, None, False)
    assert resumed.document.redo() and resumed.document.text == edited
    normalized = resumed.document.to_dict()
    assert normalized['source_anchor'] == anchor and 'source_origin' not in normalized
    assert normalized['edits'] == record['edits'] and normalized['revision'] == record['revision']

    fresh = ReaderDraftStore(app.root, slot='original-source-reader')
    assert fresh.for_source(source).key == previous.key
    reopened, selected, detached = open_source_draft(source, fresh,
        choose=lambda: pytest.fail('The unchanged explicit source must reopen its old identity slot.'))
    assert selected.key == previous.key and not detached
    assert reopened.document.text == edited and reopened.document.source_anchor == anchor
    summary, = fresh.list_saved().copies
    recovered, copied = fresh.open_saved(summary)
    assert copied.path != previous.path
    assert recovered.document.source_origin == old_origin and recovered.document.can_redo
    assert previous.path.read_bytes() == raw
    assert ReadingDocument(source.text, source_origin=old_origin).source_anchor == anchor


@pytest.mark.parametrize('version', ['carr.reading-copy.v1', 'carr.reading-copy.v2'])
def test_portable_document_rejects_ambiguous_dual_source_formats(tmp_path, version):
    _, sources = _two_equal_sources(tmp_path)
    document = sources[0][0].reading_document()
    record = document.to_dict()
    record.update(schema=version, source_origin=document.source_origin)
    with pytest.raises(ValueError, match='version or fields'):
        ReadingDocument.from_dict(record)


def _legacy_draft(app, source):
    legacy = ReaderDraftStore(app.root, slot='old-book-reader')
    document = ReadingDocument(source.text, title='Saved older narration')
    document.set_text(source.text + '\nOriginal saved author edits.')
    legacy.save(ReaderResume(document, 'flite:slt', rate=1, volume=65, cursor=3, audio_seconds=2))
    return legacy, legacy.path.read_bytes()


@pytest.mark.parametrize('selection', [0, 1, None])
def test_legacy_recovery_requires_explicit_choice_and_preserves_old_file(tmp_path, selection):
    app, sources = _two_equal_sources(tmp_path)
    source = sources[0][0]
    legacy, before = _legacy_draft(app, source)
    if selection is None:
        with pytest.raises(InterruptedError, match='kept unchanged'):
            open_source_draft(source, legacy, choose=lambda: None)
    else:
        resumed, selected, detached = open_source_draft(source, legacy, choose=lambda: selection)
        assert detached is (selection == 0)
        if detached:
            assert resumed.document.text.endswith('Original saved author edits.')
            assert not resumed.document.source_bound
            assert selected.path != legacy.path
            assert selected.load().document.text == resumed.document.text
        else:
            assert resumed.document.source_origin == source.origin
            assert resumed.document.text == source.text
    assert legacy.path.read_bytes() == before


def test_current_source_choice_reauthorizes_after_modal(tmp_path):
    app, sources = _two_equal_sources(tmp_path)
    source = sources[0][0]
    legacy, before = _legacy_draft(app, source)
    permitted = True
    def guard():
        if not permitted:
            raise ValueError('Source authorization revoked.')
    def choose():
        nonlocal permitted
        permitted = False
        return 1
    with pytest.raises(ValueError, match='revoked'):
        open_source_draft(source, legacy, choose=choose, guard=guard)
    assert legacy.path.read_bytes() == before
    assert len(tuple(legacy.root.glob('*.reader.json'))) == 1


@pytest.mark.parametrize('change', ['selected_snapshot', 'latest_link'])
@pytest.mark.parametrize('selection', [0, 1, None])
def test_source_recovery_choice_preserves_newer_saved_work_from_another_session(
        tmp_path, change, selection):
    app, sources = _two_equal_sources(tmp_path)
    source = sources[0][0]
    legacy, _ = _legacy_draft(app, source)
    current = legacy.for_source(source)
    concurrent = {}

    def saved_files():
        return {path.name: path.read_bytes() for path in legacy.root.glob('*.json')}

    def choose():
        # A separate reader saves while the native recovery choice is open.
        second_session = ReaderDraftStore(app.root, slot='old-book-reader')
        newer = second_session.load()
        newer.document.set_text('LATEST edits saved by another reader')
        if change == 'selected_snapshot':
            second_session.save(newer)
        else:
            current.separate_copy(newer)
        concurrent.update(saved_files())
        return selection

    if selection == 0:
        with pytest.raises(ValueError, match='draft changed'):
            open_source_draft(source, legacy, choose=choose)
    elif selection is None:
        with pytest.raises(InterruptedError, match='kept unchanged'):
            open_source_draft(source, legacy, choose=choose)
    else:
        resumed, _, detached = open_source_draft(source, legacy, choose=choose)
        assert not detached and resumed.document.source_origin == source.origin
        assert resumed.document.text == source.text
    assert saved_files() == concurrent
    fresh = ReaderDraftStore(app.root, slot='old-book-reader')
    resumed, _, detached = open_source_draft(source, fresh, choose=lambda: 0)
    assert detached and resumed.document.text == 'LATEST edits saved by another reader'


def test_source_recovery_rejects_a_snapshot_changed_while_it_was_loaded(tmp_path, monkeypatch):
    app, sources = _two_equal_sources(tmp_path)
    source = sources[0][0]
    legacy, _ = _legacy_draft(app, source)
    load = ReaderDraftStore.load
    changed = False

    def load_then_save_newer(store):
        nonlocal changed
        resumed = load(store)
        if store.key == legacy.key and not changed:
            changed = True
            newer = ReaderResume(ReadingDocument.from_dict(resumed.document.to_dict()))
            newer.document.set_text('LATEST concurrent save during initial load')
            store.save(newer)
        return resumed

    monkeypatch.setattr(ReaderDraftStore, 'load', load_then_save_newer)
    with pytest.raises(ValueError, match='draft changed'):
        open_source_draft(source, legacy,
            choose=lambda: pytest.fail('A changed snapshot must not reach the choice.'))
    assert legacy.load().document.text == 'LATEST concurrent save during initial load'
    assert len(tuple(legacy.root.glob('*.reader.json'))) == 1


def test_fresh_reader_discovers_latest_edited_standalone_recovery(tmp_path):
    app, sources = _two_equal_sources(tmp_path)
    source = sources[0][0]
    legacy, before = _legacy_draft(app, source)
    resumed, separate, detached = open_source_draft(source, legacy, choose=lambda: 0)
    assert detached and not resumed.document.source_bound
    resumed.document.set_text('Latest separately recovered reading edits.')
    separate.save(resumed)
    fresh = ReaderDraftStore(app.root, slot='old-book-reader')
    reopened, newest, detached = open_source_draft(source, fresh, choose=lambda: 0)
    assert detached and reopened.document.text == resumed.document.text
    assert not reopened.document.source_bound and newest.path != separate.path
    assert legacy.path.read_bytes() == before
    assert separate.load().document.text == resumed.document.text


def test_fresh_reader_discovers_bound_draft_after_source_revision_change(tmp_path):
    app, sources = _two_equal_sources(tmp_path)
    source, imported, desk = sources[0]
    old = ReaderDraftStore(app.root, slot='previous-revision-slot')
    resumed, saved, detached = open_source_draft(source, old,
        choose=lambda: pytest.fail('A new source has no recovery conflict.'))
    resumed.document.set_text('Bound draft edits before metadata revision.')
    saved.save(resumed)
    before = saved.path.read_bytes()
    app.update_metadata(desk.workspace, imported['edition'], title='New revision')
    updated = ReadingDesk(app, desk.workspace, imported['edition'])
    current = read_source_range(updated, 1, len(updated.pages))
    assert current.origin != source.origin
    fresh = ReaderDraftStore(app.root, slot='new-revision-slot')
    reopened, _, detached = open_source_draft(current, fresh, choose=lambda: 0)
    assert detached and reopened.document.text == resumed.document.text
    assert reopened.document.source_origin == source.origin
    assert saved.path.read_bytes() == before


def test_bound_draft_and_recovery_links_survive_complete_library_backup(tmp_path):
    from carr_library.library_backup import restore_backup
    app, sources = _two_equal_sources(tmp_path)
    source, imported, desk = sources[0]
    legacy = ReaderDraftStore(app.root, slot='source-session')
    resumed, current, _ = open_source_draft(source, legacy,
        choose=lambda: pytest.fail('A new source has no recovery conflict.'))
    resumed.document.set_text('Saved source-bound narration edits.')
    current.save(resumed)
    backup = tmp_path / 'backup'
    checksum = app.backup(backup)
    restored_root = tmp_path / 'restored'
    restore_backup(backup, restored_root, expected_manifest_sha256=checksum)
    restored = LibraryApp(restored_root)
    restored_desk = ReadingDesk(restored, desk.workspace, imported['edition'])
    restored_source = read_source_range(restored_desk, 1, len(restored_desk.pages))
    reopened, _, detached = open_source_draft(restored_source,
        ReaderDraftStore(restored_root, slot='a-fresh-session'),
        choose=lambda: pytest.fail('Exact source identity must reuse the bound draft.'))
    assert not detached
    assert reopened.document.text == resumed.document.text
    assert reopened.document.source_origin == source.origin
    assert restored.assets.resolve(imported['asset']).read_bytes() == app.assets.resolve(imported['asset']).read_bytes()


def test_generic_recovery_of_bound_snapshot_keeps_history_without_rebinding(tmp_path):
    app, sources = _two_equal_sources(tmp_path)
    source = sources[0][0]
    document = source.reading_document()
    document.set_text('Saved source-bound edit.')
    original = ReaderDraftStore(app.root, slot='unidentified-old-revision-slot')
    original.save(ReaderResume(document, 'flite:slt', volume=65, cursor=3))
    before = original.path.read_bytes()
    finder = ReaderDraftStore(app.root)
    summary, = finder.list_saved().copies
    assert summary.source_recorded and 'source identity recorded' in summary.label
    recovered, separate = finder.open_saved(summary)
    assert recovered.document.source_origin == source.origin
    assert recovered.document.text == document.text
    assert recovered.document.undo() and recovered.document.text == source.text
    assert original.path.read_bytes() == before
    assert separate.path != original.path


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


@pytest.mark.parametrize('mutation', ['authority', 'hash', 'range', 'kind', 'separator', 'extra', 'null',
    'missing', 'verified', 'ordinal', 'position-extra', 'positions', 'both', 'v1-anchor'])
def test_portable_source_anchor_rejects_inconsistent_data(tmp_path, mutation):
    _, sources = _two_equal_sources(tmp_path)
    source = sources[0][0]
    data = _canonical_anchor_record(source.reading_document())
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
    elif mutation == 'null':
        data['source_anchor'] = None
    elif mutation == 'missing':
        del anchor['positions'][0]['text_verified_at_capture']
    elif mutation == 'verified':
        anchor['positions'][0]['text_verified_at_capture'] = 1
    elif mutation == 'ordinal':
        anchor['positions'][0]['ordinal'] = True
    elif mutation == 'position-extra':
        anchor['positions'][0]['text_verified'] = True
    elif mutation == 'positions':
        anchor['positions'] *= 5001
    elif mutation == 'both':
        data['source_origin'] = source.origin
    else:
        data['schema'] = 'carr.reading-copy.v1'
    before = json.dumps(data, sort_keys=True)
    with pytest.raises(ValueError):
        ReadingDocument.from_dict(data)
    assert json.dumps(data, sort_keys=True) == before

def _canonical_anchor_record(document):
    """Canonical184 persisted format remains the current serialization."""
    record = document.to_dict()
    assert 'source_anchor' in record and 'source_origin' not in record
    return record


def _legacy_origin_record(document):
    """Exact earlier95 producer shape, retained for compatibility reading."""
    record = document.to_dict()
    record.pop('source_anchor')
    record['source_origin'] = document.source_origin
    return record


def test_canonical_anchor_v2_reopens_history_without_rewriting_old_project(tmp_path):
    _, sources = _two_equal_sources(tmp_path)
    source, other = sources[0][0], sources[1][0]
    document = source.reading_document()
    document.replace(0, 6, 'Reading')
    document.replace(7, 7, 'only ')
    assert document.undo()
    record = _canonical_anchor_record(document)
    path = tmp_path / 'canonical-anchor.reading.json'
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    before = path.read_bytes()

    reopened = ReadingDocument.load(path)
    assert path.read_bytes() == before
    assert reopened.source_origin == source.origin
    assert reopened.source_origin['quotation_authority'] is False
    assert reopened.source_sha256 == document.source_sha256
    assert reopened.text == document.text and reopened.revision == document.revision
    assert source.reading_document(reopened) is reopened
    with pytest.raises(ValueError, match='source identity'):
        other.reading_document(reopened)
    assert reopened.undo() and reopened.text == source.text
    first, second = source.origin['positions']
    assert reopened.source_span(first['end'], second['start']) == SourceSpan(None, None, False)
    assert not reopened.source_span(0, len(reopened.text)).exact
    assert reopened.redo() and reopened.text == document.text
    assert reopened.redo() and 'only ' in reopened.text
    portable = tmp_path / 'canonical-roundtrip.reading.json'
    reopened.save(portable)
    written = json.loads(portable.read_text(encoding='utf-8'))
    assert written['schema'] == 'carr.reading-copy.v2'
    assert 'source_origin' not in written and written['source_anchor'] == source.source_anchor
    assert path.read_bytes() == before


def test_canonical_anchor_recovery_and_backup_keep_original_saved_bytes(tmp_path):
    from carr_library.library_backup import restore_backup

    app, sources = _two_equal_sources(tmp_path)
    source = sources[0][0]
    document = source.reading_document()
    document.set_text('Saved canonical edits with retained source identity.')
    store = ReaderDraftStore(app.root, slot='canonical-book-reader')
    store.save(ReaderResume(document, 'fixture:voice', rate=2, volume=65, cursor=3))
    record = json.loads(store.path.read_bytes())
    record['document'] = _canonical_anchor_record(document)
    store.path.write_text(json.dumps(record, ensure_ascii=False) + '\n', encoding='utf-8')
    before = store.path.read_bytes()

    recovered, selected, detached = open_source_draft(source, store,
        choose=lambda: pytest.fail('An exact old-format source needs no conflict choice.'))
    assert not detached and selected.path != store.path
    assert recovered.document.source_origin == source.origin
    assert recovered.document.text == document.text
    assert (recovered.voice_id, recovered.rate, recovered.volume, recovered.cursor) == ('fixture:voice', 2, 65, 3)
    assert store.path.read_bytes() == before
    summary, = store.list_saved().copies
    assert summary.source_recorded
    copied, separate_store = store.open_saved(summary)
    assert separate_store.path != store.path and copied.document.source_origin == source.origin
    assert copied.document.text == document.text
    assert copied.document.undo() and copied.document.text == source.text
    assert store.path.read_bytes() == before

    backup = tmp_path / 'canonical-draft-backup'
    manifest = app.backup(backup)
    destination = tmp_path / 'canonical-draft-restored'
    restore_backup(backup, destination, expected_manifest_sha256=manifest)
    restored_store = ReaderDraftStore(destination, slot='canonical-book-reader')
    restored = restored_store.load()
    assert restored_store.path.read_bytes() == before
    assert restored.document.source_origin == source.origin and restored.document.text == document.text
    assert restored.document.undo() and restored.document.text == source.text


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
        copy = source.reading_document()
        anchor = copy.source_anchor
        assert (anchor['workspace'], anchor['edition'], anchor['revision'], anchor['asset_sha256']) == (
            workspace, desk.edition, desk.revision, imported['asset'])
        assert source.asset_sha256 == sha256(original.read_bytes()).hexdigest()
        assert [p['text_verified_at_capture'] for p in anchor['positions']] == [True, True, True]
        assert [p['ordinal'] for p in anchor['positions']] == [1, 2, 3]
        assert all(p['kind'] == 'logical_segment' for p in anchor['positions'])
        assert anchor['quotation_authority'] is False
        for position, page in zip(anchor['positions'], raw.split('\f')):
            assert source.text[position['start']:position['end']] == page
            assert position['text_sha256'] == sha256(page.encode('utf-8')).hexdigest()
        for first, second in zip(anchor['positions'], anchor['positions'][1:]):
            assert copy.source_span(first['end'], second['start']).start is None
            assert not copy.source_span(first['end'], second['start']).exact
        assert not copy.source_span(0, len(copy.text)).exact
        copy.replace(0, 5, 'Changed narration')
        output = tmp_path / f'source-{number}.reading.json'
        copy.save(output)
        reopened = ReadingDocument.load(output)
        assert reopened.source_anchor == anchor
        assert reopened.source_text == source.text and reopened.text == copy.text
        assert reopened.source_anchor == source.reading_document().source_anchor
        assert reopened.undo() and reopened.text == source.text
        assert not reopened.source_span(0, len(reopened.text)).exact
        assert app.assets.resolve(imported['asset']).read_bytes() == prefix + raw.encode('utf-8')
        sources.append(source)
        documents.append(reopened)
    assert sources[0].text == sources[1].text
    for name in ('workspace', 'edition', 'revision', 'asset_sha256'):
        assert documents[0].source_anchor[name] != documents[1].source_anchor[name]
    assert documents[0].source_anchor != sources[1].reading_document().source_anchor
    assert documents[1].source_anchor != sources[0].reading_document().source_anchor


def test_source_anchor_retains_old_revision_without_reattaching_or_authorizing(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Source')
    imported = app.add_text(workspace, 'Source', 'Unchanged original text.')
    desk = ReadingDesk(app, workspace, imported['edition'])
    source = read_source_range(desk, 1, 1)
    copy = source.reading_document()
    path = tmp_path / 'old.reading.json'
    copy.save(path)
    app.update_metadata(workspace, imported['edition'], title='New catalog metadata')
    current = ReadingDesk(app, workspace, imported['edition'])
    current_source = read_source_range(current, 1, 1)
    reopened = ReadingDocument.load(path)
    assert reopened.source_text == current_source.text
    assert reopened.source_anchor['revision'] == source.revision != current.revision
    assert reopened.source_anchor != current_source.reading_document().source_anchor
    with pytest.raises(ValueError, match='revision changed'):
        read_source_range(desk, 1, 1)
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        read_source_range(current, 1, 1)
    # Retained user-owned work remains readable; it cannot authorize a source.
    assert ReadingDocument.load(path).source_anchor == reopened.source_anchor
    assert reopened.source_anchor['quotation_authority'] is False


@pytest.mark.parametrize('archive_during_picker', [False, True], ids=['authorized', 'late-topic-archive'])
@pytest.mark.parametrize('recovery_case', ['none', 'exact', 'legacy', 'foreign', 'revision', 'pre-release'])
@pytest.mark.parametrize('voice_available', [True, False], ids=['voice', 'no-voice'])
def test_source_save_as_reauthorizes_after_picker(tmp_path, monkeypatch, archive_during_picker, recovery_case, voice_available):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic source')
    topic = app.create_topic(workspace, 'Bound topic')
    original = 'Exact original source.\r\nRetain Ukrainian: оригінал.'
    imported = app.add_text(workspace, 'Synthetic source book', original)
    desk = ReadingDesk(app, workspace, imported['edition'], topic=topic)
    source = read_source_range(desk, 1, 1)
    from carr_library.ui.research_actions import NativeDialogs
    choices = []

    def choose_current(_self, _parent, _title, _message, options):
        assert options == ('Open saved draft as a separate reading copy',
                           'Read the current source and keep the saved draft')
        choices.append(1)
        return 1

    monkeypatch.setattr(NativeDialogs, 'choose', choose_current)
    expected_editor = original
    store = ReaderDraftStore(app.root)
    prior_draft = None
    if recovery_case != 'none':
        if recovery_case == 'legacy':
            previous = ReadingDocument(original)
        elif recovery_case == 'foreign':
            other_workspace = app.create_workspace('Other authorized source')
            other = app.add_text(other_workspace, 'Identical source text', original)
            previous = read_source_range(
                ReadingDesk(app, other_workspace, other['edition']), 1, 1).reading_document()
        else:
            previous = source.reading_document()
        previous.set_text('Recovered reading edit.')
        store.save(ReaderResume(previous))
        if recovery_case == 'pre-release':
            record = json.loads(store.path.read_text(encoding='utf-8'))
            anchor = record['document']['source_anchor']
            anchor['schema'] = 'carr.narration-source.v1'
            anchor['derived_separators'] = []
            for position in anchor['positions']:
                position['text_verified_at_read'] = position.pop('text_verified_at_capture')
            store.path.write_text(json.dumps(record), encoding='utf-8')
        prior_draft = store.path.read_bytes()
        if recovery_case in ('exact', 'pre-release'):
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
    assert choices == ([1] if recovery_case in ('legacy', 'foreign', 'revision') else [])
    if recovery_case in ('legacy', 'foreign', 'revision'):
        assert store.path.read_bytes() == prior_draft
        assert any('no matching source lineage' in message for message in statuses)
    if recovery_case == 'pre-release':
        assert store.path.read_bytes() == prior_draft
        assert any('pre-release reading-project format' in message for message in statuses)
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
