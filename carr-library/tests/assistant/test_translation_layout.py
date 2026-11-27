"""Actual DOCX/package/retained-job tests with the existing synthetic gateway.

No network service, Word application, layout fidelity or translation quality is
claimed. Format workers are real isolated child interpreters in source and
installed runs; their imported origin is bound by the production protocol.
"""
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE

from carr_library.application import LibraryApp
from carr_library.assistant import translation_layout as layout_module
from carr_library.assistant import translation_layout_worker as worker
from carr_library.assistant.models import canonical
from carr_library.assistant.profiles import ProfileStore
from carr_library.assistant.providers import Gateway
from carr_library.assistant.translation import TranslationService
from carr_library.assistant.translation_documents import run_documents, TranslationBatchRunError
from carr_library.assistant.translation_layout import capture_layout, review_layout, prepare_layout, export_layout
from carr_library.audio.models import AudioCancelled
from carr_library.durable_state import atomic_json, encode, read_json
from carr_library.library_backup import create_backup, restore_backup
from carr_library.operations.broker import ResourceBroker
from carr_library.research.export_file import capture_export_destination

_spec = importlib.util.spec_from_file_location('docx_layout_test_transport',
    Path(__file__).with_name('test_translation_local.py'))
_transport = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_transport)
ready = _transport.ready


def hyperlink(paragraph, value):
    relation = paragraph.part.relate_to('https://example.invalid/retained-reference',
                                        RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    link = OxmlElement('w:hyperlink')
    link.set(qn('r:id'), relation)
    run, node = OxmlElement('w:r'), OxmlElement('w:t')
    node.text = value
    run.append(node)
    link.append(run)
    paragraph._p.append(link)


def docx_bytes(*, paragraphs=1, rich=True):
    document = Document()
    for number in range(paragraphs):
        paragraph = document.add_paragraph(style='Heading 1' if number == 0 else 'Normal')
        paragraph.add_run('Original Ukrainian Ω ' + str(number) + ' ').bold = True
        paragraph.add_run('second styled span.').italic = True
    if rich:
        table = document.add_table(rows=2, cols=2)
        table.style = 'Table Grid'
        for row_index, row in enumerate(table.rows):
            for column, cell in enumerate(row.cells):
                cell.text = 'Cell ' + str(row_index) + ':' + str(column)
        link = document.add_paragraph()
        hyperlink(link, 'Complete linked paragraph.')
        document.add_paragraph('')
        document.sections[0].header.paragraphs[0].text = 'Untranslated header stays exact.'
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def replace_members(data, changes):
    result = BytesIO()
    with ZipFile(BytesIO(data)) as original, ZipFile(result, 'w') as output:
        for info in original.infolist():
            output.writestr(info, changes.pop(info.filename, original.read(info)))
        for name, raw in changes.items():
            output.writestr(name, raw)
    return result.getvalue()


@pytest.fixture
def bound(tmp_path, ready):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Explicit formatted source')
    source = tmp_path / 'Selected Ω.docx'
    raw = docx_bytes()
    source.write_bytes(raw)
    result = app._import_file(workspace, source, title='Retained DOCX Ω',
        authors=('Synthetic original author',), work_id='docx-layout-work', edition_id='docx-layout-edition')
    assert result['state'] == 'complete' and result['verified'] is False
    directory = app.root / 'state/translation'
    service = TranslationService(directory, library=app, gateway=Gateway(directory / 'provider'))
    ProfileStore(app.root / 'state/providers').save(ready.profile)
    service.refresh_languages(ready.profile, authorization=_transport.discovery(ready.profile))
    return SimpleNamespace(app=app, workspace=workspace, edition='docx-layout-edition',
        source=source, original=raw, service=service, profile=ready.profile, state=ready.state)


def captured(case):
    return capture_layout(case.app, case.workspace, case.edition)


def prepared(case, layout):
    review = review_layout(case.service, case.profile, layout, 'en', 'uk', '0')
    return prepare_layout(case.service, case.profile, layout, 'en', 'uk', '0',
                          approved_review=review['review_sha256'])['jobs']


def completed(case):
    layout = captured(case)
    identities = prepared(case, layout)
    assert run_documents(case.service, identities)['status'] == 'complete'
    return layout, identities


def test_actual_worker_maps_only_exact_retained_paragraphs_and_simple_cells(bound, monkeypatch):
    actual = layout_module.run_process
    invocations = []
    def observed(args, **kwargs):
        invocations.append((tuple(args), kwargs))
        assert args[1:5] == ['-I', '-B', '-X', 'utf8']
        assert Path(args[-2]).resolve() == Path(worker.__file__).resolve()
        assert kwargs['owned_windows_job'] is (os.name == 'nt')
        assert kwargs['env']['PYTHONDONTWRITEBYTECODE'] == '1'
        return actual(args, **kwargs)
    monkeypatch.setattr(layout_module, 'run_process', observed)
    layout = captured(bound)
    assert len(invocations) == 1
    assert layout.mapping['selected'] == 6 and len(layout.mapping['records']) == 7
    assert [row['locator'] for row in layout.mapping['records']][:5] == [
        [1], [2, 1, 1, 1], [2, 1, 2, 1], [2, 2, 1, 1], [2, 2, 2, 1]]
    for row, selected in zip((r for r in layout.mapping['records'] if r['selected']), layout.selections):
        assert selected.document.source_text == row['text']
        assert selected.document.source_anchor['positions'][0]['ordinal'] == row['ordinal']
        assert selected.document.source_anchor['quotation_authority'] is False
    assert all(not item.document.source_anchor['positions'][0]['text_verified_at_capture']
               for item in layout.selections)
    assert bound.source.read_bytes() == bound.original


def test_review_requires_no_provider_request_and_binds_visible_layout_limitations(bound):
    layout = captured(bound)
    before = len(bound.state['calls'])
    review = review_layout(bound.service, bound.profile, layout, 'en', 'uk', '0')
    assert len(bound.state['calls']) == before and review['provider_calls_started'] is False
    assert review['layout']['mapping_sha256'] == layout.mapping_sha256
    assert 'first textual run' in review['layout']['notice']
    identities = prepare_layout(bound.service, bound.profile, layout, 'en', 'uk', '0',
                                approved_review=review['review_sha256'])['jobs']
    assert len(identities) == 6 and len(bound.state['calls']) == before
    assert all(bound.service.get(identity)['kind'] == 'translation/2' for identity in identities)
    with pytest.raises(PermissionError, match='exact DOCX'):
        prepare_layout(bound.service, bound.profile, layout, 'auto', 'uk', '0',
                       approved_review=review['review_sha256'])
    assert len(bound.service.jobs.identities()) == 6


def test_actual_completed_docx_preserves_tables_styles_relationships_and_unselected_parts(bound, tmp_path):
    layout, identities = completed(bound)
    target = tmp_path / 'Translated Ω.docx'
    approval = capture_export_destination(target, private_root=bound.app.root)
    result = export_layout(bound.service, layout, identities, target, approval=approval)
    assert result['status'] == 'complete' and result['quotation_authority'] is False
    assert result['source_sha256'] == sha256(bound.original).hexdigest()
    assert result['output_sha256'] == sha256(target.read_bytes()).hexdigest()
    assert result['changed_members'] == ['word/document.xml']
    original, exported = Document(BytesIO(bound.original)), Document(target)
    assert exported.paragraphs[0].style.style_id == original.paragraphs[0].style.style_id
    assert exported.paragraphs[0].runs[0].bold is True
    assert exported.paragraphs[0].runs[1].italic is True and exported.paragraphs[0].runs[1].text == ''
    assert exported.paragraphs[0].text.startswith('DERIVED: Original Ukrainian Ω')
    assert exported.tables[0].style.style_id == original.tables[0].style.style_id
    assert len(exported.tables[0].rows) == 2 and len(exported.tables[0].columns) == 2
    assert all(cell.text.startswith('DERIVED: Cell') for row in exported.tables[0].rows for cell in row.cells)
    assert exported.paragraphs[-1].text == ''
    assert exported.paragraphs[1].hyperlinks[0].address == original.paragraphs[1].hyperlinks[0].address
    with ZipFile(BytesIO(bound.original)) as before, ZipFile(target) as after:
        assert before.namelist() == after.namelist()
        for name in before.namelist():
            if name != 'word/document.xml':
                assert before.read(name) == after.read(name), name
    assert bound.source.read_bytes() == bound.original
    assert bound.app.assets.resolve(layout.asset_sha256).read_bytes() == bound.original
    assert ResourceBroker.for_app(bound.app.root).snapshot()['leases'] == []


@pytest.mark.parametrize('construct', ['field', 'footnote', 'endnote', 'drawing', 'tracked-change',
                                       'mixed-hyperlink', 'nested-table', 'signature', 'macro'])
def test_unsupported_source_shapes_are_refused_by_unchanged_canonical_admission(construct):
    document = Document()
    paragraph = document.add_paragraph('Explicit original.')
    if construct in ('field', 'footnote', 'endnote', 'drawing'):
        tag = {'field': 'w:fldChar', 'footnote': 'w:footnoteReference',
               'endnote': 'w:endnoteReference', 'drawing': 'w:drawing'}[construct]
        paragraph.runs[0]._r.append(OxmlElement(tag))
    elif construct == 'tracked-change':
        paragraph._p.append(OxmlElement('w:ins'))
    elif construct == 'mixed-hyperlink':
        hyperlink(paragraph, 'Separate linked span.')
    elif construct == 'nested-table':
        table = document.add_table(rows=1, cols=1)
        table.cell(0, 0).add_table(rows=1, cols=1).cell(0, 0).text = 'Nested original.'
    raw = BytesIO()
    document.save(raw)
    data = raw.getvalue()
    if construct == 'signature':
        data = replace_members(data, {'_xmlsignatures/sig1.xml': b'<signature/>'})
    if construct == 'macro':
        data = replace_members(data, {'word/vbaProject.bin': b'NOT EXECUTABLE SYNTHETIC'})
    before = sha256(data).hexdigest()
    with pytest.raises(ValueError):
        worker.describe(data)
    assert sha256(data).hexdigest() == before


def test_mixed_hyperlink_fails_real_capture_before_jobs_or_translation(bound):
    document = Document(BytesIO(bound.original))
    hyperlink(document.paragraphs[0], 'Mixed reference.')
    raw = BytesIO()
    document.save(raw)
    path = bound.source.with_name('mixed.docx')
    path.write_bytes(raw.getvalue())
    bound.app._import_file(bound.workspace, path, title='Mixed source',
        work_id='mixed-work', edition_id='mixed-edition')
    before = len(bound.state['calls'])
    with pytest.raises(ValueError, match='Mixed hyperlink'):
        capture_layout(bound.app, bound.workspace, 'mixed-edition')
    assert not bound.service.jobs.identities() and len(bound.state['calls']) == before


def test_actual_document_with_more_than2048_nonempty_units_is_refused_before_job_creation(bound):
    path = bound.source.with_name('too-many.docx')
    path.write_bytes(docx_bytes(paragraphs=2049, rich=False))
    bound.app._import_file(bound.workspace, path, title='Larger explicit document',
        work_id='larger-work', edition_id='larger-edition')
    before = len(bound.state['calls'])
    with pytest.raises(ValueError, match='at most 2,048'):
        capture_layout(bound.app, bound.workspace, 'larger-edition')
    assert not bound.service.jobs.identities() and len(bound.state['calls']) == before


def test_changed_same_size_worker_request_is_refused_by_its_actual_decoded_snapshot(bound, monkeypatch):
    actual = layout_module.run_process
    reached = []
    def replaced(args, **kwargs):
        path = Path(args[-4])
        original = path.read_bytes()
        value = read_json(path)
        value['source_sha256'] = ('0' if value['source_sha256'][0] != '0' else '1') + value['source_sha256'][1:]
        changed = encode(value)
        assert len(changed) == len(original)
        path.write_bytes(changed)
        reached.append(True)
        return actual(args, **kwargs)
    monkeypatch.setattr(layout_module, 'run_process', replaced)
    with pytest.raises(ValueError, match='request changed'):
        captured(bound)
    assert reached == [True] and not bound.service.jobs.identities()


def test_foreign_worker_origin_is_rejected_before_source_parsing(bound, monkeypatch):
    actual = layout_module._command
    def changed(*args, **kwargs):
        command, environment = actual(*args, **kwargs)
        command[-2] = str(Path(worker.__file__).with_name('not-the-imported-worker.py'))
        return command, environment
    monkeypatch.setattr(layout_module, '_command', changed)
    with pytest.raises(ValueError, match='origin differs'):
        captured(bound)
    assert not bound.service.jobs.identities()


def test_mutated_mapping_cannot_authorize_another_body_or_layout(bound):
    layout = captured(bound)
    value = layout.mapping
    value['records'][0]['locator'] = [2]
    changed = replace(layout, mapping_json=canonical(value))
    with pytest.raises(ValueError, match='mapping changed'):
        review_layout(bound.service, bound.profile, changed, 'en', 'uk', '0')
    assert not bound.service.jobs.identities()


def test_actual_partial_jobs_must_finish_before_format_publication_and_resume_without_duplicates(bound, tmp_path):
    layout = captured(bound)
    identities = prepared(bound, layout)
    stop = []
    with pytest.raises(TranslationBatchRunError):
        run_documents(bound.service, identities, cancelled=lambda: bool(stop),
                      progress=lambda *args: stop.append(True))
    completed_ids = [identity for identity in identities if bound.service.get(identity)['status'] == 'complete']
    assert completed_ids and len(completed_ids) < len(identities)
    target = tmp_path / 'partial.docx'
    with pytest.raises(ValueError, match='Finish'):
        export_layout(bound.service, layout, identities, target)
    assert not target.exists()
    before = len(bound.state['calls'])
    assert run_documents(bound.service, identities)['status'] == 'complete'
    assert len(bound.state['calls']) - before == len(identities) - len(completed_ids)
    assert export_layout(bound.service, layout, identities, target)['status'] == 'complete'


def test_changed_profile_cannot_resume_prepared_formatted_requests(bound):
    layout = captured(bound)
    identities = prepared(bound, layout)
    store = ProfileStore(bound.app.root / 'state/providers')
    store.save(replace(bound.profile, name='Changed selected account'))
    before = len(bound.state['calls'])
    with pytest.raises(PermissionError, match='profile changed'):
        run_documents(bound.service, identities)
    assert len(bound.state['calls']) == before
    assert all(not bound.service.get(identity)['receipts'] for identity in identities)


@pytest.mark.parametrize('fault', ['source', 'same-byte-competitor', 'parent', 'private-target'])
def test_final_protected_publication_keeps_source_destination_and_no_clobber_authority(bound, tmp_path, monkeypatch, fault):
    layout, identities = completed(bound)
    parent = tmp_path / 'external'
    parent.mkdir()
    target = parent / 'translated.docx'
    if fault == 'private-target':
        target = bound.app.root / 'private.docx'
        with pytest.raises(ValueError):
            export_layout(bound.service, layout, identities, target)
        assert not target.exists()
        return
    approval = capture_export_destination(target, private_root=bound.app.root)
    actual = layout_module.write_binary_export
    competitor = []
    def boundary(path, data, **kwargs):
        if fault == 'source':
            retained = bound.app.assets.resolve(layout.asset_sha256)
            retained.write_bytes(bound.original + b'changed')
        elif fault == 'same-byte-competitor':
            Path(path).write_bytes(data)
            competitor.append(data)
        else:
            parent.rename(tmp_path / 'replaced-external')
            parent.mkdir()
        return actual(path, data, **kwargs)
    monkeypatch.setattr(layout_module, 'write_binary_export', boundary)
    with pytest.raises((ValueError, FileExistsError)):
        export_layout(bound.service, layout, identities, target, approval=approval)
    if competitor:
        assert target.read_bytes() == competitor[0]
    else:
        assert not target.exists()
    assert bound.source.read_bytes() == bound.original


def test_late_cancel_after_actual_publisher_return_preserves_completed_receipt(bound, tmp_path, monkeypatch):
    layout, identities = completed(bound)
    target = tmp_path / 'committed.docx'
    stopped = []
    actual = layout_module.write_binary_export
    def published(*args, **kwargs):
        result = actual(*args, **kwargs)
        stopped.append(True)
        return result
    monkeypatch.setattr(layout_module, 'write_binary_export', published)
    result = export_layout(bound.service, layout, identities, target, cancelled=lambda: bool(stopped))
    assert stopped == [True] and result['status'] == 'complete'
    assert result['output_sha256'] == sha256(target.read_bytes()).hexdigest()


def test_same_path_library_replacement_revokes_captured_layout_before_worker(bound, tmp_path, monkeypatch):
    layout = captured(bound)
    root = bound.app.root
    root.rename(tmp_path / 'prior-library')
    root.mkdir()
    def forbidden(*args, **kwargs):
        pytest.fail('A stale library selection reached the parser.')
    monkeypatch.setattr(layout_module, 'run_process', forbidden)
    with pytest.raises(ValueError, match='directory changed'):
        review_layout(bound.service, bound.profile, layout, 'en', 'uk', '0')


def test_actual_child_cancellation_reaps_worker_before_lease_and_staging_release(bound, tmp_path, monkeypatch):
    from carr_library.audio import process as process_module
    marker = tmp_path / 'worker-ready.json'
    actual_command = layout_module._command
    def blocked(*args, **kwargs):
        command, environment = actual_command(*args, **kwargs)
        bootstrap_index = command.index('-c') + 1
        command[bootstrap_index] = (
            'import json,os,time; from pathlib import Path; '
            'Path(' + repr(str(marker)) + ').write_text(json.dumps({"pid":os.getpid()}),encoding="utf-8"); '
            'time.sleep(60); ' + command[bootstrap_index])
        return command, environment
    monkeypatch.setattr(layout_module, '_command', blocked)
    processes = []
    if os.name == 'nt':
        from carr_library.audio import windows_job
        actual_process = windows_job.JobProcess
        def capture(*args, **kwargs):
            value = actual_process(*args, **kwargs)
            processes.append(value)
            return value
        monkeypatch.setattr(windows_job, 'JobProcess', capture)
    else:
        actual_process = process_module.subprocess.Popen
        def capture(*args, **kwargs):
            value = actual_process(*args, **kwargs)
            processes.append(value)
            return value
        monkeypatch.setattr(process_module.subprocess, 'Popen', capture)
    with pytest.raises(AudioCancelled):
        capture_layout(bound.app, bound.workspace, bound.edition, cancelled=marker.exists)
    assert marker.is_file() and len(processes) == 1
    assert processes[0].returncode is not None
    assert ResourceBroker.for_app(bound.app.root).snapshot()['leases'] == []
    assert not list((bound.app.root / 'runtime').glob('.docx-layout-*'))


def test_actual_full_restore_exports_completed_layout_offline_after_deleting_original_and_prior_root(bound, tmp_path, monkeypatch):
    layout, identities = completed(bound)
    baseline = tmp_path / 'baseline.docx'
    export_layout(bound.service, layout, identities, baseline)
    expected = baseline.read_bytes()
    receipt = create_backup(bound.app, tmp_path / 'backup')
    bound.source.unlink()
    shutil.rmtree(bound.app.root)
    destination = restore_backup(tmp_path / 'backup', tmp_path / 'restored', receipt)
    app = LibraryApp(destination)
    store = ProfileStore(app.root / 'state/providers')
    store.remove(bound.profile.id)
    directory = app.root / 'state/translation'
    service = TranslationService(directory, library=app, gateway=Gateway(directory / 'provider'))
    def forbidden(*args, **kwargs):
        pytest.fail('Offline formatted export attempted a provider request or credential lookup.')
    monkeypatch.setattr(service.gateway, 'request', forbidden)
    monkeypatch.setattr(service.gateway.vault, 'get', forbidden)
    fresh = capture_layout(app, bound.workspace, bound.edition)
    with pytest.raises(ValueError, match='directory changed'):
        export_layout(service, layout, identities, tmp_path / 'stale.docx')
    result = export_layout(service, fresh, identities, tmp_path / 'restored-output.docx')
    assert result['status'] == 'complete'
    assert (tmp_path / 'restored-output.docx').read_bytes() == expected
    assert app.assets.resolve(fresh.asset_sha256).read_bytes() == bound.original


def test_explicit_saved_job_selection_orders_only_the_chosen_source_positions(bound):
    layout = captured(bound)
    identities = prepared(bound, layout)
    before = list(bound.state['calls'])
    result = layout_module.order_layout_jobs(bound.service, layout, tuple(reversed(identities)))
    assert result == tuple(identities)
    assert bound.state['calls'] == before
    assert all(bound.service.get(identity)['status'] != 'complete' for identity in result)


@pytest.mark.parametrize('mode', ['missing', 'duplicate', 'other-settings', 'combined-range'])
def test_explicit_saved_job_selection_cannot_substitute_foreign_or_incomplete_approval(bound, mode):
    from carr_library.assistant.translation_documents import capture_document, review_documents, prepare_documents
    layout = captured(bound)
    identities = list(prepared(bound, layout))
    if mode == 'missing':
        identities.pop()
    elif mode == 'duplicate':
        identities[-1] = identities[0]
    elif mode == 'other-settings':
        review = review_layout(bound.service, bound.profile, layout, 'auto', 'uk', '0')
        different = prepare_layout(bound.service, bound.profile, layout, 'auto', 'uk', '0',
                                   approved_review=review['review_sha256'])['jobs']
        identities[-1] = different[-1]
    else:
        selected = (capture_document(bound.app, bound.workspace, bound.edition, first=1, last=2),)
        review = review_documents(bound.service, bound.profile, selected, 'en', 'uk', '0')
        different = prepare_documents(bound.service, bound.profile, selected, 'en', 'uk', '0',
                                      approved_review=review['review_sha256'])['jobs']
        identities[0] = different[0]
    before = list(bound.state['calls'])
    with pytest.raises(ValueError):
        layout_module.order_layout_jobs(bound.service, layout, identities)
    assert bound.state['calls'] == before


def test_export_rejects_forged_layout_even_when_source_paragraph_jobs_are_authentic(bound):
    layout, identities = completed(bound)
    mapping = json.loads(layout.mapping_json)
    mapping['records'][0]['locator'] = [999]
    forged = replace(layout, mapping_json=canonical(mapping))
    output = bound.source.parent / 'forged-mapping.docx'
    before = list(bound.state['calls'])
    with pytest.raises(ValueError, match='mapping differs'):
        export_layout(bound.service, forged, identities, output)
    assert not output.exists() and bound.state['calls'] == before
    assert bound.source.read_bytes() == bound.original
