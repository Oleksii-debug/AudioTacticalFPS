"""Real current library/CSL/files with an explicitly controlled Word boundary.

These cases qualify the protocol and ownership contracts, never installed Office.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace as N
from zipfile import ZipFile

import pytest

from carr_library.application import LibraryApp
from carr_library.academic_integration import word_com as word
from carr_library.academic_integration.documents import (
    CitationParagraph, _PART, _NAMESPACE, _read_citation_docx_bytes,
    _citation_xml, _read_citation_xml, read_citation_docx, export_citation_docx,
)
from carr_library.academic_integration.formatting import CitationCluster, CitationRequest, CitationError
from carr_library.ui.academic_library_dialog import AcademicController
from carr_library.ui.citation_formatting import CitationFormattingController


class ControlledDocument:
    """Only Word is controlled; real source checks, CSL and publication run."""
    next_window = 100

    def __init__(self, path):
        self.path = Path(path).resolve()
        self.state = read_citation_docx(self.path)
        self.saved, self.editable, self.managed = True, True, False
        self.generation, self.applied, self.adopted = 0, [], 0
        self.before_paragraphs = lambda: None
        self.selection_supported = True
        type(self).next_window += 1
        self.window = type(self).next_window

    def owner(self):
        return (7, self.window, str(self.path))

    def snapshot(self):
        return deepcopy(self.state)

    def require_editable(self, *, saved=False):
        if not self.editable or (saved and not self.saved):
            raise ValueError('Save an editable document first.')

    def signature(self, *, selection=True):
        return sha256(json.dumps([self.state, self.generation, self.managed],
                                 sort_keys=True).encode()).hexdigest()

    def preflight(self, snapshot):
        self.require_editable()
        assert snapshot == self.state

    def adopt(self, snapshot, *, cancelled):
        word._check(cancelled)
        self.preflight(snapshot)
        self.managed = True
        self.adopted += 1

    def capture(self, snapshot, *, insertion=False):
        if not self.managed or snapshot != self.state:
            raise ValueError('The actual controlled visible citations differ from metadata.')
        if insertion and not self.selection_supported:
            raise ValueError('Place a collapsed caret in ordinary managed body text.')
        anchors = tuple((identity, index * 100 + 10, index * 100 + 20)
                        for index, identity in enumerate(word._ids(snapshot)))
        return dict(ids=word._ids(snapshot), anchors=anchors, caret=10_000)

    def paragraphs(self, snapshot, *, insertion=None):
        self.before_paragraphs()
        result = [CitationParagraph(row['text'], tuple(row['citation_ids']))
                  for row in snapshot['document']['paragraphs']]
        if insertion is not None:
            result.append(CitationParagraph('Explicit additional prose 😀', (insertion[1],)))
        return tuple(result)

    def apply(self, previous, snapshot, *, insertion, cancelled):
        word._check(cancelled)
        assert self.state == previous
        assert _read_citation_xml(_citation_xml(snapshot).decode()) == snapshot
        self.state = deepcopy(snapshot)
        self.applied.append((previous, snapshot, insertion))


class ControlledApplication:
    def __init__(self, source):
        self.document = ControlledDocument(source)
        self.opens = []

    def active(self):
        return self.document

    def open_copy(self, path):
        self.opens.append(Path(path))
        self.document = ControlledDocument(path)
        return self.document


@pytest.fixture
def research(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Word controlled-contract library')
    academic = AcademicController(app, workspace)
    source = app.add_text(workspace, 'Real retained source', 'Exact source wording 😀.')
    row = academic.create(dict(title='Synthetic methods 😀', authors=('Smith, Jane',),
        year='2024', publisher='Test Press', pages='1-200', edition_id=source['edition'],
        notes='Private notes never sent to Word', abstract='Private abstract'))
    other = academic.create(dict(title='Second source', authors=('Reader, Ana',), year='2025'))
    formatter = CitationFormattingController(app, workspace)
    yield N(app=app, academic=academic, row=row, other=other, formatter=formatter, source=source)
    assert app.close_search()


def prepare(research, tmp_path, *, style='apa', locator=False):
    snapshot = research.formatter.load()
    item = CitationRequest(research.row.id, '8' if locator else None,
                           locator_verified=locator)
    cluster = CitationCluster((item,), 'original-occurrence',
                              1 if style == 'chicago-notes' else None)
    result = research.formatter.render(snapshot, style=style, citations=(cluster,))
    source = tmp_path / 'original.docx'
    export_citation_docx(result, source,
        paragraphs=[CitationParagraph('Original body 😀', ('original-occurrence',))])
    payload = source.read_bytes()
    application = ControlledApplication(source)
    @contextmanager
    def connector():
        yield application
    bridge = word.WordBridge(research.formatter, snapshot, result, connector=connector)
    return bridge, application, source, payload


def test_actual_current_schema_copy_preserves_original_and_retained_source(research, tmp_path):
    retained = research.app.assets.resolve(research.source['asset']).read_bytes()
    bridge, app, source, original = prepare(research, tmp_path)
    target = tmp_path / 'copy.docx'
    copy = bridge.connect(target)
    assert copy.original == source and copy.original_sha256 == sha256(original).hexdigest()
    assert source.read_bytes() == target.read_bytes() == original
    assert app.opens == [target] and app.document.adopted == 1
    inspected = bridge.inspect(copy)
    assert inspected.citation_ids == ('original-occurrence',)
    assert inspected.quotation_verified is False and inspected.saved_to_disk is False
    assert research.app.assets.resolve(research.source['asset']).read_bytes() == retained
    assert b'Private notes' not in original
    assert 'Private notes' not in json.dumps(app.document.state)
    assert app.document.state['schema'] == 1 and 'records' not in app.document.state


@pytest.mark.parametrize('style', ['apa', 'chicago-notes'])
def test_actual_csl_insert_edit_refresh_keep_current_interchange_and_stable_occurrences(research, tmp_path, style):
    bridge, app, source, original = prepare(research, tmp_path, style=style)
    copy = bridge.connect(tmp_path / 'copy.docx')
    inserted = bridge.change(copy, items=(CitationRequest(research.other.id, '12',
                              locator_verified=True),), insert=True)
    new_id = inserted.citation_ids[-1]
    assert inserted.citation_ids[0] == 'original-occurrence' and new_id != 'original-occurrence'
    edited = bridge.change(copy, occurrence_id=new_id,
        items=(CitationRequest(research.row.id, '14', locator_verified=True),))
    refreshed = bridge.change(copy)
    assert refreshed.citation_ids == edited.citation_ids == inserted.citation_ids
    state = app.document.state
    assert state['clusters'][-1]['items'][0]['locator'] == '14'
    assert state['clusters'][-1]['locator_evidence'] == [
        {'reference_id': research.row.id, 'locator_verified': True}]
    assert len(app.document.applied) == 3
    assert any('😀' in row['text'] for row in state['document']['paragraphs'])
    assert source.read_bytes() == original and copy.path.read_bytes() == original
    if state['style']['kind'] == 'note':
        assert [row['note_index'] for row in state['clusters']] == [1, 2]
    # It is the same schema and renderer accepted by the existing document code.
    word._validated(state, lambda: False)
    assert all(value.quotation_verified is False for value in (inserted, edited, refreshed))


def test_copy_collision_unsaved_and_initial_cancel_have_no_open_or_overwrite(research, tmp_path):
    bridge, app, source, original = prepare(research, tmp_path)
    target = tmp_path / 'copy.docx'
    target.write_bytes(b'foreign file')
    with pytest.raises(FileExistsError):
        bridge.connect(target)
    assert target.read_bytes() == b'foreign file'
    app.document.saved = False
    with pytest.raises(ValueError, match='Save'):
        bridge.connect(tmp_path / 'unsaved.docx')
    app.document.saved = True
    with pytest.raises(InterruptedError):
        bridge.connect(tmp_path / 'cancelled.docx', cancelled=lambda: True)
    assert not app.opens and source.read_bytes() == original
    assert not (tmp_path / 'unsaved.docx').exists()
    assert not (tmp_path / 'cancelled.docx').exists()


@pytest.mark.parametrize('change', ['bytes', 'equal-bytes-inode', 'host-window'])
def test_actual_create_only_checkpoint_rechecks_original_bytes_identity_and_window(
        research, tmp_path, monkeypatch, change):
    bridge, app, source, original = prepare(research, tmp_path)
    actual_writer = word.write_binary_export
    def changed(*args, **kwargs):
        if change == 'bytes':
            source.write_bytes(b'new external source bytes')
        elif change == 'equal-bytes-inode':
            replacement = tmp_path / 'replacement.docx'
            replacement.write_bytes(original)
            replacement.replace(source)
        else:
            app.document.window += 1
        return actual_writer(*args, **kwargs)
    monkeypatch.setattr(word, 'write_binary_export', changed)
    with pytest.raises(ValueError, match='original Word document changed'):
        bridge.connect(tmp_path / 'copy.docx')
    assert not (tmp_path / 'copy.docx').exists() and not app.opens


@pytest.mark.parametrize('reason', ['selection', 'reference', 'workspace', 'document', 'window'])
def test_stale_source_selection_scope_or_host_prevents_mutation(research, tmp_path, reason):
    bridge, app, _, _ = prepare(research, tmp_path)
    copy = bridge.connect(tmp_path / 'copy.docx')
    if reason == 'selection':
        app.document.selection_supported = False
    elif reason == 'reference':
        app.document.before_paragraphs = lambda: research.academic.update(research.row, {'title': 'Changed'})
    elif reason == 'workspace':
        app.document.before_paragraphs = lambda: research.app.archive_workspace(research.formatter.workspace)
    elif reason == 'window':
        app.document.before_paragraphs = lambda: setattr(app.document, 'window', app.document.window + 1)
    else:
        app.document.before_paragraphs = lambda: setattr(app.document, 'generation', app.document.generation + 1)
    with pytest.raises(ValueError):
        bridge.change(copy, items=(CitationRequest(research.row.id),), insert=True)
    assert not app.document.applied


def test_changed_linked_source_revokes_saved_locator_review_and_does_not_rebind(research, tmp_path):
    bridge, app, source, original = prepare(research, tmp_path, locator=True)
    copy = bridge.connect(tmp_path / 'copy.docx')
    new_source = research.app.add_text(research.formatter.workspace, 'Another source', 'Other original bytes.')
    research.academic.update(research.row, {'edition_id': new_source['edition']})
    with pytest.raises(ValueError, match='references changed'):
        bridge.change(copy)
    assert not app.document.applied and source.read_bytes() == original
    assert app.document.state['clusters'][0]['items'][0]['locator'] == '8'


def test_foreign_or_recreated_capability_and_other_active_document_are_refused(research, tmp_path):
    bridge, app, source, _ = prepare(research, tmp_path)
    copy = bridge.connect(tmp_path / 'copy.docx')
    with pytest.raises(ValueError, match='capability'):
        bridge.inspect(replace(copy))
    app.document = ControlledDocument(source)
    with pytest.raises(ValueError, match='same connected Word'):
        bridge.change(copy)
    assert not app.document.applied


def test_new_window_with_same_path_and_metadata_is_not_the_connected_document(research, tmp_path):
    bridge, app, _, _ = prepare(research, tmp_path)
    copy = bridge.connect(tmp_path / 'copy.docx')
    app.document = ControlledDocument(copy.path)
    app.document.managed = True
    with pytest.raises(ValueError, match='same connected Word'):
        bridge.change(copy)
    assert not app.document.applied


@pytest.mark.parametrize('items', [
    ({'id': 'arbitrary'},),
    (CitationRequest('foreign-reference'),),
    (CitationRequest('placeholder', '4', locator_verified=False),),
])
def test_unknown_or_unreviewed_locator_requests_never_write(research, tmp_path, items):
    bridge, app, _, _ = prepare(research, tmp_path)
    copy = bridge.connect(tmp_path / 'copy.docx')
    if isinstance(items[0], CitationRequest) and items[0].reference_id == 'placeholder':
        items = (replace(items[0], reference_id=research.row.id),)
    with pytest.raises((ValueError, CitationError)):
        bridge.change(copy, items=items, insert=True)
    assert not app.document.applied


def test_rehashed_foreign_metadata_even_with_same_ids_never_gains_authority(research, tmp_path):
    bridge, app, _, _ = prepare(research, tmp_path)
    copy = bridge.connect(tmp_path / 'copy.docx')
    app.document.state['document']['title'] = 'Foreign rehashed replacement'
    assert _read_citation_xml(_citation_xml(app.document.state).decode()) == app.document.state
    with pytest.raises(word.WordRecoveryRequired, match='issued action'):
        bridge.change(copy)
    assert not app.document.applied


def test_copied_snapshot_or_nonissued_result_cannot_create_a_bridge(research, tmp_path):
    bridge, _, _, _ = prepare(research, tmp_path)
    foreign = CitationFormattingController(research.app, research.formatter.workspace)
    snapshot = foreign.load()
    with pytest.raises(ValueError):
        word.WordBridge(foreign, snapshot, bridge.result)
    with pytest.raises(ValueError):
        word.WordBridge(research.formatter, bridge.snapshot, replace(bridge.result))


def test_saved_docx_must_match_current_issued_formatting_result(research, tmp_path):
    bridge, app, source, _ = prepare(research, tmp_path)
    other_result = research.formatter.render(bridge.snapshot, style='ieee',
        citations=(CitationCluster((CitationRequest(research.row.id),), 'original-occurrence'),))
    other = word.WordBridge(research.formatter, bridge.snapshot, other_result, connector=bridge.connector)
    with pytest.raises(CitationError, match='current reviewed formatting'):
        other.connect(tmp_path / 'copy.docx')
    assert not app.opens and not (tmp_path / 'copy.docx').exists()


def test_uncertain_committed_host_action_blocks_replay_then_exact_inspection_recovers(research, tmp_path):
    bridge, app, source, original = prepare(research, tmp_path)
    copy = bridge.connect(tmp_path / 'copy.docx')
    actual_apply = app.document.apply
    def lost_ack(*args, **kwargs):
        actual_apply(*args, **kwargs)
        raise OSError('Controlled lost COM acknowledgement after actual state change')
    app.document.apply = lost_ack
    with pytest.raises(word.WordRecoveryRequired, match='Do not repeat'):
        bridge.change(copy, items=(CitationRequest(research.other.id),), insert=True)
    assert bridge.recovery_required and len(app.document.applied) == 1
    with pytest.raises(word.WordRecoveryRequired, match='Inspect'):
        bridge.change(copy)
    assert len(app.document.applied) == 1
    inspected = bridge.inspect(copy)
    assert len(inspected.citation_ids) == 2 and not bridge.recovery_required
    assert source.read_bytes() == original and copy.path.read_bytes() == original


def test_partial_host_action_stays_blocked_without_automatic_undo_or_retry(research, tmp_path):
    bridge, app, _, _ = prepare(research, tmp_path)
    copy = bridge.connect(tmp_path / 'copy.docx')
    def partial(*args, **kwargs):
        app.document.state['bibliography'][0]['text'] = 'Partial native state'
        raise RuntimeError('Controlled partial host mutation')
    app.document.apply = partial
    with pytest.raises(word.WordRecoveryRequired):
        bridge.change(copy, items=(CitationRequest(research.other.id),), insert=True)
    assert bridge.recovery_required
    with pytest.raises(word.WordRecoveryRequired, match='issued action'):
        bridge.inspect(copy)
    assert bridge.recovery_required and not app.document.applied


def test_late_cancellation_after_confirmed_host_write_is_completed_not_rollback(research, tmp_path):
    bridge, app, _, _ = prepare(research, tmp_path)
    copy = bridge.connect(tmp_path / 'copy.docx')
    cancelled = []
    actual_apply = app.document.apply
    def completed(*args, **kwargs):
        actual_apply(*args, **kwargs)
        cancelled.append(True)
    app.document.apply = completed
    result = bridge.change(copy, items=(CitationRequest(research.other.id),), insert=True,
                           cancelled=lambda: bool(cancelled))
    assert result.status == 'complete' and result.changed and not bridge.recovery_required
    assert len(app.document.applied) == 1


def test_explicit_inspect_accepts_exact_prior_snapshot_after_user_native_undo(research, tmp_path):
    bridge, app, _, _ = prepare(research, tmp_path)
    copy = bridge.connect(tmp_path / 'copy.docx')
    before = deepcopy(app.document.state)
    bridge.change(copy, items=(CitationRequest(research.other.id),), insert=True)
    # Controlled Word boundary only: actual Office Undo is separately qualified.
    app.document.state = before
    assert bridge.inspect(copy).citation_ids == ('original-occurrence',)
    assert not bridge.recovery_required


def test_library_revocation_inside_apply_preserves_original_and_requires_inspection(research, tmp_path):
    bridge, app, source, original = prepare(research, tmp_path)
    copy = bridge.connect(tmp_path / 'copy.docx')
    actual_apply = app.document.apply
    def revoked(*args, **kwargs):
        research.app.archive_workspace(research.formatter.workspace)
        return actual_apply(*args, **kwargs)
    app.document.apply = revoked
    with pytest.raises(word.WordRecoveryRequired):
        bridge.change(copy, items=(CitationRequest(research.other.id),), insert=True)
    assert not app.document.applied and source.read_bytes() == original
    assert bridge.recovery_required


def test_advisory_progress_cannot_change_real_publication_outcome(research, tmp_path, monkeypatch):
    from carr_library import progress
    def unavailable(**kwargs):
        raise RuntimeError('Observer detached')
    monkeypatch.setattr(progress, 'current_progress', lambda: N(update=unavailable))
    bridge, app, source, original = prepare(research, tmp_path)
    copy = bridge.connect(tmp_path / 'copy.docx')
    result = bridge.change(copy, items=(CitationRequest(research.other.id),), insert=True)
    assert result.changed and len(app.document.applied) == 1 and source.read_bytes() == original


def replace_zip(payload, *, rename=False, duplicate=False, addition=None, xml=None):
    target = BytesIO()
    with ZipFile(BytesIO(payload)) as original, ZipFile(target, 'w') as result:
        for info in original.infolist():
            name = 'customXml/item19.xml' if rename and info.filename == _PART else info.filename
            raw = xml if xml is not None and info.filename == _PART else original.read(info)
            result.writestr(name, raw)
        if duplicate:
            result.writestr('customXml/item20.xml', original.read(_PART))
        if addition:
            result.writestr(*addition)
    return target.getvalue()


def test_word_renamed_utf16_part_retains_current_schema_and_canonical_checksum(research, tmp_path):
    _, _, _, original = prepare(research, tmp_path)
    saved = _read_citation_docx_bytes(original)
    xml = _citation_xml(saved).decode().replace("encoding='UTF-8'", "encoding='UTF-16'")
    encoded = xml.encode('utf-16')
    assert _read_citation_docx_bytes(replace_zip(original, rename=True, xml=encoded)) == saved
    assert _read_citation_xml(xml) == saved
    with pytest.raises(CitationError, match='unique'):
        _read_citation_docx_bytes(replace_zip(original, duplicate=True))
    with pytest.raises(CitationError, match='duplicate|aliased'):
        _read_citation_docx_bytes(replace_zip(original, addition=(_PART.upper(), encoded)))


@pytest.mark.parametrize('addition', [
    ('word/vbaProject.bin', b'active'),
    ('word/embeddings/object.bin', b'embedded'),
    ('word/header91.xml', b'<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:instrText>DDEAUTO</w:instrText></w:hdr>'),
    ('word/_rels/external.xml.rels', b'<Relationships><Relationship TargetMode="External" Target="https://example.invalid/x"/></Relationships>'),
])
def test_active_or_external_docx_content_refused_before_word_open(research, tmp_path, addition):
    _, _, _, original = prepare(research, tmp_path)
    with pytest.raises(ValueError, match='active|external'):
        word._safe_package(replace_zip(original, addition=addition))


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16'])
def test_declared_entities_never_enter_native_metadata_codec(encoding):
    xml = ('<?xml version="1.0" encoding="' + encoding + '"?>'
        '<!DOCTYPE citations [<!ENTITY private SYSTEM "file:///not-read">]>'
        '<citations xmlns="' + _NAMESPACE + '" schema="1" sha256="' + '0' * 64 + '">&private;</citations>')
    from carr_library.academic_integration.documents import _read_citation_xml_bytes
    with pytest.raises(CitationError, match='entities'):
        _read_citation_xml_bytes(xml.encode(encoding))


def test_snapshot_checksum_and_xml_byte_budget_are_strict(research, tmp_path):
    _, _, _, original = prepare(research, tmp_path)
    saved = _read_citation_docx_bytes(original)
    xml = _citation_xml(saved).decode()
    assert _read_citation_xml(xml) == saved
    with pytest.raises(CitationError, match='integrity'):
        _read_citation_xml(xml.replace('Original body', 'Tampered body'))
    with pytest.raises(CitationError, match='budget'):
        _read_citation_xml('x' * 16_000_001)


@pytest.mark.parametrize('path', ['NUL.docx', r'C:\source.docx:stream', r'\\server\source.docx'])
def test_device_stream_and_network_paths_refused_before_any_probe(path):
    with pytest.raises(ValueError, match='local'):
        word._path(path)


def test_offsets_and_current_writer_bookmarks_use_the_exact_existing_contract(research, tmp_path):
    assert word._units('A😀Б') == 4 and word._plain('<i>A😀Б</i>') == 'A😀Б'
    _, _, _, original = prepare(research, tmp_path)
    saved = _read_citation_docx_bytes(original)
    name = word._original_bookmark(saved, 'original-occurrence')
    with ZipFile(BytesIO(original)) as archive:
        assert name.encode() in archive.read('word/document.xml')
    assert name == word._bookmark('citation:original-occurrence:1')


def test_platform_absence_never_claims_office_qualification(monkeypatch):
    monkeypatch.setattr(word, 'os', N(name='posix'))
    with pytest.raises(word.WordUnavailable, match='Windows'):
        with word._running_word():
            pytest.fail('No Office connector should be returned.')


@pytest.mark.parametrize('fail_at', ['start', 'body', 'end'])
def test_named_native_undo_group_never_undoes_unrelated_host_actions(fail_at):
    events = []
    def start(name):
        events.append(('start', name))
        if fail_at == 'start':
            raise OSError('Ambiguous start')
    def end():
        events.append(('end',))
        if fail_at == 'end':
            raise OSError('Ambiguous end')
    def forbidden(*args):
        pytest.fail('The bridge must never call user Word Undo automatically.')
    native = word._ComDocument(N(UndoRecord=N(StartCustomRecord=start, EndCustomRecord=end)),
                               N(Undo=forbidden))
    native.require_editable = lambda **kwargs: None
    with pytest.raises(word.WordRecoveryRequired, match='no automatic Undo'):
        with native._undo('Controlled action'):
            if fail_at == 'body':
                events.append(('write',))
                raise OSError('Partial body')
    assert sum(value[0] == 'start' for value in events) == 1
    assert sum(value[0] == 'end' for value in events) == (0 if fail_at == 'start' else 1)


@pytest.mark.parametrize('mode', ['available', 'not-running', 'unexpected-com-error', 'changed-mode'])
def test_sta_is_explicit_on_actual_worker_and_refused_before_attach_on_changed_mode(monkeypatch, mode):
    from concurrent.futures import ThreadPoolExecutor
    import sys
    import threading
    from types import ModuleType
    calls, owner = [], threading.get_ident()
    runtime = ModuleType('comtypes')
    runtime.__path__ = []
    class ComError(Exception):
        def __init__(self, code):
            self.hresult = code
            super().__init__('Controlled COM error ' + str(code))
    runtime.COMError = ComError
    runtime.COINIT_APARTMENTTHREADED = 2
    def initialize(flag):
        calls.append(('initialize', threading.get_ident(), flag))
        if mode == 'changed-mode':
            raise ComError(0x80010106)
    runtime.CoInitializeEx = initialize
    runtime.CoUninitialize = lambda: calls.append(('uninitialize', threading.get_ident()))
    client = ModuleType('comtypes.client')
    def attach(name, *, dynamic):
        calls.append(('attach', threading.get_ident(), name, dynamic))
        assert dynamic is True
        if mode not in ('available', 'changed-mode'):
            raise ComError(0x800401e3 if mode == 'not-running' else 0x80004005)
        return object()
    client.GetActiveObject = attach
    monkeypatch.setattr(word, '_RUNTIME', (runtime, attach))
    monkeypatch.setitem(sys.modules, 'comtypes', runtime)
    monkeypatch.setitem(sys.modules, 'comtypes.client', client)
    monkeypatch.setattr(word, 'os', N(name='nt'))
    def operation():
        with word._running_word() as application:
            assert isinstance(application, word._WordApplication)
            return 'completed-boundary'
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(operation)
        if mode == 'available':
            assert future.result(timeout=3) == 'completed-boundary'
        elif mode == 'not-running':
            with pytest.raises(word.WordUnavailable):
                future.result(timeout=3)
        else:
            with pytest.raises(RuntimeError, match='Controlled COM error'):
                future.result(timeout=3)
    assert [row[0] for row in calls] == (
        ['initialize'] if mode == 'changed-mode' else ['initialize', 'attach', 'uninitialize'])
    assert len({row[1] for row in calls}) == 1 and calls[0][1] != owner


@pytest.mark.parametrize('outcome', ['success', 'exception', 'cancel'])
def test_all_session_document_proxies_detach_before_balanced_sta_release(monkeypatch, outcome):
    from concurrent.futures import ThreadPoolExecutor
    from types import ModuleType
    import sys
    import threading
    import weakref
    wrappers, sessions, weak_objects, events = [], [], [], []
    owner = threading.get_ident()
    class Document:
        def Activate(self):
            events.append('activate')
        def Close(self, *args, **kwargs):
            pytest.fail('Never close a user document.')
    class Documents:
        Count = 1
        def Open(self, **kwargs):
            document = Document()
            weak_objects.append(weakref.ref(document))
            return document
    class Application:
        def __init__(self):
            self.ActiveDocument = Document()
            weak_objects.append(weakref.ref(self.ActiveDocument))
            self.Documents = Documents()
        def Quit(self, *args, **kwargs):
            pytest.fail('Never quit user Word.')
    runtime = ModuleType('comtypes')
    runtime.__path__ = []
    runtime.COINIT_APARTMENTTHREADED = 2
    runtime.COMError = type('ControlledComError', (Exception,), {})
    runtime.CoInitializeEx = lambda flag: events.append('initialize')
    def uninitialize():
        assert threading.get_ident() != owner
        assert len(wrappers) == 2
        assert all(document.doc is None and document.app is None for document in wrappers)
        assert sessions[0].application is None and not sessions[0]._documents
        assert all(reference() is None for reference in weak_objects)
        events.append('uninitialize')
    runtime.CoUninitialize = uninitialize
    client = ModuleType('comtypes.client')
    def attach(name, *, dynamic):
        assert name == 'Word.Application' and dynamic
        application = Application()
        weak_objects.append(weakref.ref(application))
        return application
    client.GetActiveObject = attach
    monkeypatch.setattr(word, '_RUNTIME', (runtime, attach))
    monkeypatch.setitem(sys.modules, 'comtypes', runtime)
    monkeypatch.setitem(sys.modules, 'comtypes.client', client)
    monkeypatch.setattr(word, 'os', N(name='nt'))
    def operation():
        with word._running_word() as application:
            sessions.append(application)
            wrappers.extend([application.active(), application.open_copy('controlled-copy.docx')])
            if outcome == 'exception':
                raise RuntimeError('Controlled operation failed')
            if outcome == 'cancel':
                word._check(lambda: True)
            return 'completed'
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(operation)
        if outcome == 'success':
            assert future.result(timeout=3) == 'completed'
        else:
            with pytest.raises(RuntimeError if outcome == 'exception' else InterruptedError):
                future.result(timeout=3)
    assert events == ['initialize', 'activate', 'uninitialize']
    sessions[0]._release()


def test_runtime_first_import_is_owned_by_main_and_worker_never_imports_it(monkeypatch):
    import builtins
    import sys
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from types import ModuleType
    calls, main = [], threading.get_ident()
    runtime = ModuleType('comtypes')
    runtime.__path__ = []
    runtime.COINIT_APARTMENTTHREADED = 2
    runtime.COMError = type('ControlledComError', (Exception,), {})
    runtime.CoInitializeEx = lambda flag: calls.append(('worker-init', threading.get_ident()))
    runtime.CoUninitialize = lambda: calls.append(('worker-uninit', threading.get_ident()))
    client = ModuleType('comtypes.client')
    client.GetActiveObject = lambda name, **kwargs: object()
    real_import = builtins.__import__
    def importing(name, *args, **kwargs):
        if name == 'comtypes':
            calls.append(('first-import', threading.get_ident()))
            monkeypatch.setitem(sys.modules, 'comtypes', runtime)
            return runtime
        if name == 'comtypes.client':
            return client
        return real_import(name, *args, **kwargs)
    monkeypatch.delitem(sys.modules, 'comtypes', raising=False)
    monkeypatch.delitem(sys.modules, 'comtypes.client', raising=False)
    monkeypatch.setattr(word, '_RUNTIME', None)
    monkeypatch.setattr(word, 'os', N(name='nt'))
    monkeypatch.setattr(builtins, '__import__', importing)
    word.prepare_word_runtime()
    word.prepare_word_runtime()
    def operation():
        with word._running_word():
            return True
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(operation).result(timeout=3)
    assert [row[0] for row in calls] == ['first-import', 'worker-init', 'worker-uninit']
    assert calls[0][1] == main and calls[1][1] == calls[2][1] != main


def test_worker_cannot_trigger_first_import_and_unowned_prior_import_is_refused(monkeypatch):
    import sys
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(word, '_RUNTIME', None)
    monkeypatch.setattr(word, 'os', N(name='nt'))
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(word.WordUnavailable, match='main thread'):
            executor.submit(word.prepare_word_runtime).result(timeout=3)
    monkeypatch.setitem(sys.modules, 'comtypes', object())
    with pytest.raises(word.WordUnavailable, match='outside'):
        word.prepare_word_runtime()
    assert word._RUNTIME is None


def test_real_export_has_one_standard_datastore_identity_without_changing_payload(research, tmp_path):
    from defusedxml import ElementTree
    from uuid import UUID
    _, _, _, original = prepare(research, tmp_path)
    namespace = 'http://schemas.openxmlformats.org/officeDocument/2006/customXml'
    with ZipFile(BytesIO(original)) as archive:
        properties = ElementTree.fromstring(archive.read('customXml/carr-citations-props.xml'))
        assert properties.tag == '{%s}datastoreItem' % namespace
        UUID(properties.get('{%s}itemID' % namespace).strip('{}'))
        assert properties[0][0].get('{%s}uri' % namespace) == _NAMESPACE
        links = ElementTree.fromstring(archive.read('customXml/_rels/carr-citations.xml.rels'))
        assert len(links) == 1 and links[0].get('Type').endswith('/customXmlProps')
        assert links[0].get('Target') == 'carr-citations-props.xml'
    assert word._safe_package(original) == _read_citation_docx_bytes(original)


@pytest.mark.parametrize('damage', ['missing', 'duplicate-identity', 'foreign-namespace', 'wrong-schema'])
def test_word_datastore_aliases_and_foreign_namespace_never_reach_open(research, tmp_path, damage):
    from lxml import etree
    _, _, _, original = prepare(research, tmp_path)
    namespace = 'http://schemas.openxmlformats.org/officeDocument/2006/customXml'
    with ZipFile(BytesIO(original)) as archive:
        parts = {info.filename: archive.read(info) for info in archive.infolist()}
    own = etree.fromstring(parts['customXml/carr-citations-props.xml'])
    if damage == 'missing':
        del parts['customXml/_rels/carr-citations.xml.rels']
    elif damage == 'wrong-schema':
        own[0][0].set('{%s}uri' % namespace, 'urn:foreign')
        parts['customXml/carr-citations-props.xml'] = etree.tostring(own)
    else:
        foreign_name = next(name for name in parts if name.startswith('customXml/')
                            and name.count('/') == 1 and name.endswith('.xml')
                            and name != 'customXml/carr-citations-props.xml'
                            and etree.fromstring(parts[name]).tag == '{%s}datastoreItem' % namespace)
        foreign = etree.fromstring(parts[foreign_name])
        if damage == 'duplicate-identity':
            foreign.set('{%s}itemID' % namespace, own.get('{%s}itemID' % namespace))
        else:
            # Default python-docx bibliography custom XML has a real separate
            # datastore part; it cannot masquerade as our citation metadata.
            foreign[0][0].set('{%s}uri' % namespace, _NAMESPACE)
        parts[foreign_name] = etree.tostring(foreign)
    output = BytesIO()
    with ZipFile(output, 'w') as archive:
        for name, raw in parts.items():
            archive.writestr(name, raw)
    with pytest.raises(ValueError, match='datastore'):
        word._safe_package(output.getvalue())
