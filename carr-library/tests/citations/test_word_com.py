"""Real CSL/database/files with a controlled Word boundary, never Office qualification."""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace as N
from zipfile import ZipFile

import pytest

from carr_library.application import LibraryApp
from carr_library.citations.bibliography_workbench import BibliographyWorkbench, records_for_style
from carr_library.citations.documents import (
    CitationParagraph, _PART, _read_citation_bytes, citation_document, citation_xml,
    read_citation_docx, read_citation_xml,
)
from carr_library.citations import word_com as word
from carr_library.ui.academic_library_dialog import AcademicController


class ControlledDocument:
    """Only the external Word boundary is replaced; publication and CSL are real."""
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.state = read_citation_docx(self.path)
        self.saved, self.editable, self.managed = True, True, False
        self.generation, self.applied, self.adopted = 0, [], 0
        self.before_paragraphs = lambda: None
        self.selection_supported = True

    def snapshot(self):
        return deepcopy(self.state)

    def require_editable(self, *, saved=False):
        if not self.editable or (saved and not self.saved):
            raise ValueError('Save an editable document first.')

    def signature(self):
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
        assert self.managed and snapshot == self.state
        if insertion and not self.selection_supported:
            raise ValueError('Place a collapsed caret in ordinary managed body text.')
        anchors = tuple((identity, index * 100 + 10, index * 100 + 20)
                        for index, identity in enumerate(snapshot['citation_ids']))
        return dict(ids=tuple(snapshot['citation_ids']), anchors=anchors, caret=10_000)

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
        # Real renderer has produced this same schema and immutable reference IDs.
        assert read_citation_xml(citation_xml(snapshot)) == snapshot
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
    controller = AcademicController(app, workspace)
    row = controller.create(dict(title='Synthetic methods 😀', authors=('Smith, Jane',),
                                 year='2024', publisher='Test Press', pages='1-200'))
    other = controller.create(dict(title='Second source', authors=('Reader, Ana',), year='2025'))
    workbench = BibliographyWorkbench(controller)
    preview = workbench.preview(name_mode='family-given')
    return N(app=app, controller=controller, row=row, other=other, workbench=workbench, preview=preview)


def prepare(research, tmp_path, *, style='apa', locator=False):
    records, _ = records_for_style(research.workbench.check(research.preview), name_mode='family-given')
    item = {'id': research.row.id}
    if locator:
        item.update(locator='8', label='page')
    clusters = [dict(id='original-occurrence', items=[item], note_index=1 if style != 'apa' else None)]
    payload = citation_document(records, style=style, citations=clusters,
        paragraphs=[CitationParagraph('Original body 😀', ('original-occurrence',))])
    source = tmp_path / 'original.docx'
    source.write_bytes(payload)
    application = ControlledApplication(source)

    @contextmanager
    def connector():
        yield application

    bridge = word.WordBridge(research.workbench, connector=connector)
    return bridge, application, source, payload


def test_explicit_copy_is_exact_and_original_never_saved(research, tmp_path):
    bridge, app, source, original = prepare(research, tmp_path)
    target = tmp_path / 'copy.docx'
    connected = bridge.connect(target)
    assert connected.original == source and connected.original_sha256 == sha256(original).hexdigest()
    assert source.read_bytes() == target.read_bytes() == original
    assert app.opens == [target] and app.document.adopted == 1
    assert bridge.inspect(connected).citation_ids == ('original-occurrence',)
    assert bridge.inspect(connected).quotation_verified is False


@pytest.mark.parametrize('style', ['apa', 'chicago-note'])
def test_real_csl_insert_edit_and_refresh_preserve_occurrence_and_provenance(research, tmp_path, style):
    bridge, app, source, original = prepare(research, tmp_path, style=style)
    connected = bridge.connect(tmp_path / 'copy.docx')
    inserted = bridge.change(connected, preview=research.preview,
        items=[{'id': research.other.id, 'locator': '12', 'label': 'page'}], insert=True)
    new_id = inserted.citation_ids[-1]
    assert inserted.citation_ids[0] == 'original-occurrence' and new_id != 'original-occurrence'
    assert app.document.state['clusters'][-1]['items'][0]['locator'] == '12'
    edited = bridge.change(connected, preview=research.preview, occurrence_id=new_id,
        items=[{'id': research.row.id, 'locator': '14', 'label': 'page'}])
    refreshed = bridge.change(connected, preview=research.preview)
    assert refreshed.citation_ids == edited.citation_ids == inserted.citation_ids
    snapshot = app.document.state
    assert snapshot['quotation_verified'] is snapshot['visible_edits_verified'] is False
    assert source.read_bytes() == original and (tmp_path / 'copy.docx').read_bytes() == original
    assert len(app.document.applied) == 3
    assert any('😀' in row['text'] for row in snapshot['document']['paragraphs'])
    if snapshot['style']['kind'] == 'note':
        assert [row['note_index'] for row in snapshot['clusters']] == [1, 2]


def test_copy_collision_unsaved_source_and_initial_cancel_do_not_open_word_copy(research, tmp_path):
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
    assert app.opens == [] and source.read_bytes() == original
    assert not (tmp_path / 'unsaved.docx').exists() and not (tmp_path / 'cancelled.docx').exists()


def test_source_change_at_real_publication_checkpoint_refuses_copy(research, tmp_path, monkeypatch):
    bridge, app, source, _ = prepare(research, tmp_path)
    actual_writer = word.write_binary_export

    def changed(*args, **kwargs):
        source.write_bytes(b'new external source bytes')
        return actual_writer(*args, **kwargs)

    monkeypatch.setattr(word, 'write_binary_export', changed)
    with pytest.raises(ValueError, match='original Word document changed'):
        bridge.connect(tmp_path / 'copy.docx')
    assert source.read_bytes() == b'new external source bytes'
    assert not (tmp_path / 'copy.docx').exists() and not app.opens


@pytest.mark.parametrize('reason', ['selection', 'reference', 'workspace', 'document'])
def test_stale_authority_or_unsupported_selection_prevents_word_mutation(research, tmp_path, reason):
    bridge, app, _, _ = prepare(research, tmp_path)
    connected = bridge.connect(tmp_path / 'copy.docx')
    if reason == 'selection':
        app.document.selection_supported = False
    elif reason == 'reference':
        app.document.before_paragraphs = lambda: research.controller.update(research.row, {'title': 'Changed'})
    elif reason == 'workspace':
        app.document.before_paragraphs = lambda: research.app.archive_workspace(research.controller.workspace)
    else:
        app.document.before_paragraphs = lambda: setattr(app.document, 'generation', app.document.generation + 1)
    with pytest.raises(ValueError):
        bridge.change(connected, preview=research.preview, items=[{'id': research.row.id}], insert=True)
    assert app.document.applied == []


def test_changed_source_with_retained_locator_needs_explicit_item_review(research, tmp_path):
    bridge, app, _, _ = prepare(research, tmp_path, locator=True)
    connected = bridge.connect(tmp_path / 'copy.docx')
    research.controller.update(research.row, {'title': 'Reviewed new title'})
    current = research.workbench.preview(name_mode='family-given')
    with pytest.raises(ValueError, match='locators'):
        bridge.change(connected, preview=current)
    assert not app.document.applied
    result = bridge.change(connected, preview=current, occurrence_id='original-occurrence',
        items=[{'id': research.row.id, 'locator': '9', 'label': 'page'}])
    assert result.citation_ids == ('original-occurrence',)
    assert app.document.state['clusters'][0]['items'][0]['locator'] == '9'


def test_other_active_document_and_original_session_cannot_be_mutated(research, tmp_path):
    bridge, app, source, _ = prepare(research, tmp_path)
    connected = bridge.connect(tmp_path / 'copy.docx')
    app.document = ControlledDocument(source)
    with pytest.raises(ValueError, match='Activate'):
        bridge.change(connected)
    with pytest.raises(ValueError, match='explicit new'):
        bridge.inspect(word.WordCopy(source, source, connected.original_sha256))
    assert app.document.applied == []


def replace_zip(payload, *, rename=False, duplicate=False, addition=None):
    target = BytesIO()
    with ZipFile(BytesIO(payload)) as original, ZipFile(target, 'w') as result:
        for info in original.infolist():
            name = 'customXml/item19.xml' if rename and info.filename == _PART else info.filename
            result.writestr(name, original.read(info))
        if duplicate:
            result.writestr('customXml/item20.xml', original.read(_PART))
        if addition:
            result.writestr(*addition)
    return target.getvalue()


def test_saved_word_customxml_part_rename_keeps_existing_regeneration_contract(research, tmp_path):
    _, _, _, original = prepare(research, tmp_path)
    renamed = replace_zip(original, rename=True)
    assert _read_citation_bytes(renamed) == _read_citation_bytes(original)
    with pytest.raises(ValueError, match='unique'):
        _read_citation_bytes(replace_zip(original, duplicate=True))


@pytest.mark.parametrize('addition', [
    ('word/vbaProject.bin', b'active'),
    ('word/embeddings/object.bin', b'embedded'),
    ('word/header91.xml', b'<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:instrText>DDEAUTO</w:instrText></w:hdr>'),
    ('word/_rels/external.xml.rels', b'<Relationships><Relationship TargetMode="External" Target="https://example.invalid/x"/></Relationships>'),
])
def test_active_docx_content_is_refused_before_word_open(research, tmp_path, addition):
    _, _, _, original = prepare(research, tmp_path)
    with pytest.raises(ValueError, match='active|external'):
        word._safe_package(replace_zip(original, addition=addition))


def test_snapshot_checksum_and_xml_budget_remain_strict(research, tmp_path, monkeypatch):
    _, _, _, original = prepare(research, tmp_path)
    snapshot = _read_citation_bytes(original)
    xml = citation_xml(snapshot)
    assert read_citation_xml(xml) == snapshot
    with pytest.raises(ValueError, match='integrity'):
        read_citation_xml(xml.replace('Original body', 'Tampered body'))
    from carr_library.citations import documents
    monkeypatch.setattr(documents, '_MAX_METADATA', 128)
    with pytest.raises(ValueError, match='budget'):
        read_citation_xml(xml)


@pytest.mark.parametrize('path', ['NUL.docx', r'C:\source.docx:stream', r'\\server\source.docx'])
def test_windows_device_stream_and_network_paths_refused_before_probe(path):
    with pytest.raises(ValueError, match='local'):
        word._path(path)


def test_word_offsets_use_utf16_units_not_unicode_codepoints():
    assert word._units('A😀Б') == 4
    assert word._plain('<i>A😀Б</i>') == 'A😀Б'


def test_platform_unavailable_never_claims_office_qualification(monkeypatch):
    monkeypatch.setattr(word, 'os', N(name='posix'))
    with pytest.raises(word.WordUnavailable, match='Windows'):
        with word._running_word():
            pytest.fail('No Office connector should be returned.')


@pytest.mark.parametrize('mutated,undo_succeeds', [(False, True), (True, True), (True, False)])
def test_named_undo_only_rolls_back_this_operation_and_reports_recovery_failure(mutated, undo_succeeds):
    """Native COM is controlled here; this is explicitly not an Office PASS."""
    events, state = [], {'version': 0}
    record = N(IsRecordingCustomRecord=False,
        StartCustomRecord=lambda name: events.append(('start', name)),
        EndCustomRecord=lambda: events.append(('end',)))
    def undo(count):
        events.append(('undo', count))
        if undo_succeeds:
            state['version'] = 0
        return undo_succeeds
    native = word._ComDocument(N(UndoRecord=record), N(Undo=undo))
    native.require_editable = lambda **kwargs: None
    native.signature = lambda **kwargs: str(state['version'])
    native._part = lambda: N(XML='unread-but-retained-original-metadata')
    # No metadata load is needed when the exact old state has already returned.
    native.snapshot = lambda: {}
    expected = word.WordRecoveryRequired if mutated and not undo_succeeds else RuntimeError
    # A valid snapshot is only needed by the fallback metadata comparison.
    from unittest.mock import patch
    with patch.object(word, 'read_citation_xml', return_value={}):
        with pytest.raises(expected):
            with native._undo('Controlled one operation'):
                if mutated:
                    state['version'] = 1
                raise RuntimeError('specific operation failure')
    assert events[:2] == [('start', 'Controlled one operation'), ('end',)]
    assert ('undo', 1) in events if mutated else not any(row[0] == 'undo' for row in events)
    if mutated and undo_succeeds:
        assert state['version'] == 0


def test_word_datastore_properties_use_existing_namespace_and_snapshot(research, tmp_path):
    from defusedxml import ElementTree
    from uuid import UUID
    _, _, _, original = prepare(research, tmp_path)
    namespace = 'http://schemas.openxmlformats.org/officeDocument/2006/customXml'
    with ZipFile(BytesIO(original)) as archive:
        properties = ElementTree.fromstring(archive.read('customXml/carr-citations-props.xml'))
        assert properties.tag == '{%s}datastoreItem' % namespace
        UUID(properties.get('{%s}itemID' % namespace).strip('{}'))
        assert properties[0][0].get('{%s}uri' % namespace) == 'urn:carr:citations:1'
        relationships = ElementTree.fromstring(archive.read('customXml/_rels/carr-citations.xml.rels'))
        assert any(row.get('Type').endswith('/customXmlProps')
                   and row.get('Target') == 'carr-citations-props.xml' for row in relationships)
    assert _read_citation_bytes(original)['citation_ids'] == ['original-occurrence']


def test_word_renamed_utf16_metadata_is_parsed_by_namespace(research, tmp_path):
    _, _, _, original = prepare(research, tmp_path)
    saved = _read_citation_bytes(original)
    output = BytesIO()
    with ZipFile(BytesIO(original)) as source, ZipFile(output, 'w') as target:
        for info in source.infolist():
            if info.filename != _PART:
                target.writestr(info.filename, source.read(info))
        target.writestr('customXml/item91.xml', citation_xml(saved).encode('utf-16'))
    assert _read_citation_bytes(output.getvalue()) == saved


def test_advisory_progress_failure_cannot_undo_a_real_copy_or_citation_update(research, tmp_path, monkeypatch):
    from carr_library import progress
    def unavailable(**kwargs):
        raise RuntimeError('Observer was detached')
    monkeypatch.setattr(progress, 'current_progress', lambda: N(update=unavailable))
    bridge, app, source, original = prepare(research, tmp_path)
    connected = bridge.connect(tmp_path / 'copy.docx')
    result = bridge.change(connected, preview=research.preview, items=[{'id': research.other.id}], insert=True)
    assert result.changed and len(app.document.applied) == 1
    assert source.read_bytes() == original and (tmp_path / 'copy.docx').read_bytes() == original


def test_library_revocation_at_native_apply_checkpoint_prevents_write(research, tmp_path):
    bridge, app, _, _ = prepare(research, tmp_path)
    connected = bridge.connect(tmp_path / 'copy.docx')
    original_apply = app.document.apply
    def revoked(*args, **kwargs):
        research.app.archive_workspace(research.controller.workspace)
        return original_apply(*args, **kwargs)
    app.document.apply = revoked
    with pytest.raises(ValueError):
        bridge.change(connected, preview=research.preview, items=[{'id': research.other.id}], insert=True)
    assert app.document.applied == []


@pytest.mark.parametrize('mode', ['available', 'not-running', 'unexpected-com-error'])
def test_com_apartment_lives_entirely_in_actual_worker_and_always_uninitializes(monkeypatch, mode):
    from concurrent.futures import ThreadPoolExecutor
    import sys
    import threading
    from types import ModuleType
    calls = []
    owner = threading.get_ident()
    pythoncom = ModuleType('pythoncom')
    class ComError(Exception):
        def __init__(self, code):
            self.hresult = code
    pythoncom.com_error = ComError
    pythoncom.COINIT_APARTMENTTHREADED = 2
    pythoncom.CoInitializeEx = lambda flag: calls.append(('initialize', threading.get_ident(), flag))
    pythoncom.CoUninitialize = lambda: calls.append(('uninitialize', threading.get_ident()))
    parent = ModuleType('win32com')
    parent.__path__ = []
    client = ModuleType('win32com.client')
    def attach(name):
        calls.append(('attach', threading.get_ident(), name))
        if mode != 'available':
            raise ComError(0x800401e3 if mode == 'not-running' else 0x80004005)
        return object()
    client.GetActiveObject = attach
    for name, module in (('pythoncom', pythoncom), ('win32com', parent), ('win32com.client', client)):
        monkeypatch.setitem(sys.modules, name, module)
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
            with pytest.raises(ComError):
                future.result(timeout=3)
    assert [row[0] for row in calls] == ['initialize', 'attach', 'uninitialize']
    assert len({row[1] for row in calls}) == 1 and calls[0][1] != owner


def test_rehashed_replacement_snapshot_cannot_drop_connected_source_ids(research, tmp_path):
    bridge, app, _, _ = prepare(research, tmp_path)
    connected = bridge.connect(tmp_path / 'copy.docx')
    records = app.document.state['records']
    replacement = citation_document(records, citations=[
        {'id': 'foreign-occurrence', 'items': [{'id': research.other.id}], 'note_index': None}],
        paragraphs=[CitationParagraph('Foreign replacement', ('foreign-occurrence',))])
    app.document.state = _read_citation_bytes(replacement)
    with pytest.raises(ValueError, match='connected source identities'):
        bridge.change(connected, preview=research.preview)
    assert not app.document.applied
