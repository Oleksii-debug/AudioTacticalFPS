"""Real processor, scoped database and Word/HTML output acceptance."""
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import socket
from zipfile import ZipFile

import pytest

from carr_library.application import LibraryApp
from carr_library.bibliography_cli import main
from carr_library.citations.bibliography_workbench import (
    BibliographyWorkbench, docx_document, html_document, records_for_style,
)
from carr_library.ui.academic_library_dialog import AcademicController


@pytest.fixture
def research(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Academic local library')
    controller = AcademicController(app, workspace)
    reference = controller.create(dict(title='Synthetic source methods', authors=('Smith, Jane',),
        year='2020', publisher='Example Press', notes='PrivateNoteNeverInBibliography', pages='11-99'))
    return controller, reference, BibliographyWorkbench(controller)


def test_real_apa_keeps_name_mode_explicit_and_never_infers_quotation_page(research):
    controller, row, workbench = research
    literal = workbench.preview(identity=row.id)
    explicit = workbench.preview(identity=row.id, name_mode='family-given')
    assert 'Smith, Jane' in literal.bibliography
    assert 'Smith, J.' in explicit.bibliography
    assert '(Smith, 2020)' in explicit.citations
    assert '11-99' not in explicit.citations and 'PrivateNote' not in explicit.explanation
    assert controller.get(row.id) == row


@pytest.mark.parametrize('mode', ['literal', 'family-given'])
def test_corporate_name_marker_remains_literal(mode):
    rows, warnings = records_for_style(json.dumps([dict(id='org', type='book', title='Report',
        author=[dict(literal='{Research and Development, Inc.}')])]), name_mode=mode)
    assert rows[0]['author'] == [dict(literal='Research and Development, Inc.')]
    assert warnings


def test_explicit_name_mode_does_not_guess_unseparated_names():
    rows, _ = records_for_style(json.dumps([dict(id='x', type='book', title='Source',
        author=[dict(literal='Jane Smith'), dict(literal='Smith, Jane, Jr.')])]), name_mode='family-given')
    assert rows[0]['author'] == [dict(literal='Jane Smith'), dict(family='Smith', given='Jane', suffix='Jr.')]


def test_changed_reference_revoked_workspace_and_cross_scope_prevent_stale_exports(research, tmp_path):
    controller, row, workbench = research
    preview = workbench.preview(identity=row.id)
    edited = controller.update(row, {'title': 'Revised metadata'})
    with pytest.raises(ValueError, match='changed'):
        workbench.copy_text(preview)
    with pytest.raises(ValueError, match='changed'):
        workbench.export(preview, tmp_path / 'stale.txt')
    assert not (tmp_path / 'stale.txt').exists()
    preview = workbench.preview(identity=edited.id)
    other = controller.library.create_workspace('Other library')
    foreign = BibliographyWorkbench(AcademicController(controller.library, other))
    with pytest.raises(ValueError, match='another library'):
        foreign.copy_text(preview)
    controller.library.archive_workspace(controller.workspace)
    with pytest.raises(ValueError):
        workbench.copy_text(preview)


def test_all_active_formatting_excludes_archived_foreign_and_personal_notes(research):
    controller, row, workbench = research
    archived = controller.create(dict(title='ArchivedOnlyMarker'))
    controller.archive(archived)
    other = controller.library.create_workspace('Other library')
    AcademicController(controller.library, other).create(dict(title='ForeignOnlyMarker'))
    preview = workbench.preview()
    assert len(preview.result.entries) == 1 and row.title in preview.bibliography
    assert all(marker not in preview.explanation for marker in ('ArchivedOnlyMarker', 'ForeignOnlyMarker', 'PrivateNote'))


def test_real_word_and_html_output_keep_italics_but_never_active_metadata(research, tmp_path):
    controller, row, workbench = research
    row = controller.update(row, {'title': '<script>alert(1)</script> Source <b>methods</b>'})
    preview = workbench.preview(identity=row.id, name_mode='family-given')
    html = workbench.export(preview, tmp_path / 'references.html', format='html')
    assert '<script>' not in html and 'Content-Security-Policy' in html and 'href=' not in html
    assert '<i>' in html and 'Example Press' in html
    word = workbench.export(preview, tmp_path / 'references.docx', format='docx')
    from docx import Document
    document = Document(BytesIO(word))
    assert document.paragraphs[0].text == 'Bibliography'
    assert any(run.italic for paragraph in document.paragraphs for run in paragraph.runs)
    assert any('Example Press' in paragraph.text for paragraph in document.paragraphs)
    with ZipFile(BytesIO(word)) as archive:
        for name in archive.namelist():
            if name.endswith('.rels'):
                assert b'TargetMode="External"' not in archive.read(name)
    assert controller.get(row.id) == row


def test_private_destination_and_cancelled_export_preserve_files(research, tmp_path):
    controller, row, workbench = research
    preview = workbench.preview(identity=row.id)
    private = controller.library.root / 'untouched.txt'
    private.write_text('original')
    with pytest.raises(ValueError, match='outside'):
        workbench.export(preview, private)
    assert private.read_text() == 'original'
    existing = tmp_path / 'existing.txt'
    existing.write_text('existing')
    with pytest.raises(TimeoutError, match='cancelled'):
        workbench.export(preview, existing, cancelled=lambda: True)
    assert existing.read_text() == 'existing'


def test_actual_processor_remains_offline(research, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError('A bibliography formatter must not open a socket')
    monkeypatch.setattr(socket, 'socket', no_network)
    monkeypatch.setattr(socket, 'create_connection', no_network)
    controller, row, workbench = research
    assert row.title in workbench.preview(identity=row.id).bibliography


def test_cli_preview_commit_duplicates_and_nonoverwriting_format(research, tmp_path, capsys):
    controller, _, _ = research
    source = tmp_path / 'incoming.bib'
    source.write_text('@book{localkey, author={Doe, Jane}, title={CLI synthetic reference}, year={2026}}')
    common = ['--data-dir', str(controller.library.root), '--workspace', controller.workspace]
    args = ['import', *common, '--input', str(source)]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)['result'] == 'PREVIEW_ONLY'
    assert len(controller.list()) == 1
    assert main([*args, '--commit']) == 0
    saved = json.loads(capsys.readouterr().out)['result']['imported_ids']
    assert len(saved) == 1 and len(controller.list()) == 2
    assert main([*args, '--commit', '--skip-duplicates']) == 0
    assert json.loads(capsys.readouterr().out)['result']['imported_ids'] == []
    output = tmp_path / 'formatted.txt'
    format_args = ['format', *common, '--reference', saved[0], '--names', 'family-given',
                   '--output', str(output)]
    assert main(format_args) == 0
    capsys.readouterr()
    assert 'Doe, J.' in output.read_text() and 'CLI synthetic reference' in output.read_text()
    before = output.read_bytes()
    assert main(format_args) == 1
    assert 'exists' in capsys.readouterr().err and output.read_bytes() == before


def test_cli_requires_existing_library_and_exposes_offline_styles(tmp_path, capsys):
    assert main(['styles']) == 0
    styles = json.loads(capsys.readouterr().out)
    assert {style['id'] for style in styles} >= {'apa', 'ieee'}
    missing = tmp_path / 'not-created'
    assert main(['format', '--data-dir', str(missing), '--workspace', 'unknown']) == 1
    assert not missing.exists()


def test_bibtex_import_uses_existing_atomic_scope_and_preserves_source(research, tmp_path):
    controller, row, _ = research
    app = controller.library
    source = app.add_text(controller.workspace, 'Original', 'Quotation must not change.')
    hit, = app.search(controller.workspace, 'Quotation')
    imported = controller.preview(b'@book{a,title={First}}\n@book{b,title={Second}}', 'bibtex')
    assert len(imported.entries) == 2
    from types import MappingProxyType
    bad = replace(imported.entries[1], fields=MappingProxyType(
        {**imported.entries[1].fields, 'edition_id': 'unavailable-source'}))
    invalid = replace(imported, entries=(imported.entries[0], bad))
    with pytest.raises(ValueError):
        controller.import_selected(invalid, (0, 1))
    assert controller.list() == (row,)
    assert app.copy(controller.workspace, hit.ref, hit.revision, 'quote') == 'Quotation'


@pytest.mark.parametrize('mode,value', [('guess','Name'), ('family-given','Family, , Suffix'),
                                       ('family-given','One,Two,Three,Four')])
def test_invalid_name_convention_fails_without_changing_data(mode, value):
    snapshot = json.dumps([dict(id='one', type='book', title='Title', author=[dict(literal=value)])])
    with pytest.raises(ValueError):
        records_for_style(snapshot, name_mode=mode)


@pytest.mark.parametrize('format', ('html', 'docx'))
@pytest.mark.parametrize('existing', (False, True))
def test_destination_approval_preserves_other_work_created_during_real_render(
        research, tmp_path, monkeypatch, format, existing):
    from carr_library.citations import csl
    from carr_library.research.export_file import capture_export_destination

    controller, row, workbench = research
    preview = workbench.preview(identity=row.id, style='harvard')
    destination = tmp_path / ('bibliography.' + format)
    if existing:
        destination.write_bytes(b'Old export explicitly selected for replacement')
    approval = capture_export_destination(destination, private_root=controller.library.root)
    render = csl.render
    changed = b'Another program saved its work while CSL was rendering'

    def real_render_then_competing_save(*args, **options):
        rendered = render(*args, **options)
        destination.write_bytes(changed)
        return rendered

    monkeypatch.setattr(csl, 'render', real_render_then_competing_save)
    with pytest.raises(ValueError, match='destination changed'):
        workbench.export(preview, destination, format=format, approval=approval,
                         require_absent=approval.require_absent)

    assert destination.read_bytes() == changed
    assert controller.get(row.id) == row
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('format', ['html', 'docx'])
@pytest.mark.parametrize('existing', [False, True])
def test_direct_workbench_export_captures_before_render_without_supplied_approval(
        research, tmp_path, monkeypatch, format, existing):
    from carr_library.citations import csl

    controller, row, workbench = research
    preview = workbench.preview(identity=row.id)
    destination = tmp_path / ('direct.' + format)
    if existing:
        destination.write_bytes(b'Previously selected export')
    render = csl.render
    independent = b'Another program saved this during real formatting'

    def render_then_external_write(*args, **kwargs):
        result = render(*args, **kwargs)
        destination.write_bytes(independent)
        return result

    monkeypatch.setattr(csl, 'render', render_then_external_write)
    with pytest.raises(ValueError, match='destination changed'):
        workbench.export(preview, destination, format=format)
    assert destination.read_bytes() == independent
    assert controller.get(row.id) == row
    assert not list(tmp_path.glob('.research-export-*'))


@pytest.mark.parametrize('existing', [False, True])
def test_cli_overwrite_captures_before_real_preview(
        research, tmp_path, monkeypatch, capsys, existing):
    controller, row, _ = research
    destination = tmp_path / 'cli-overwrite.txt'
    if existing:
        destination.write_bytes(b'Previously selected export')
    preview = BibliographyWorkbench.preview
    independent = b'Another program saved this while the CLI preview was rendered'

    def preview_then_external_write(workbench, **kwargs):
        result = preview(workbench, **kwargs)
        destination.write_bytes(independent)
        return result

    monkeypatch.setattr(BibliographyWorkbench, 'preview', preview_then_external_write)
    result = main(['format', '--data-dir', str(controller.library.root),
                   '--workspace', controller.workspace, '--reference', row.id,
                   '--output', str(destination), '--overwrite'])
    assert result == 1
    captured = capsys.readouterr()
    assert 'destination changed' in captured.err and not captured.out
    assert destination.read_bytes() == independent
    assert controller.get(row.id) == row
    assert not list(tmp_path.glob('.research-export-*'))


def test_cli_explicit_overwrite_still_publishes_unchanged_approved_target(
        research, tmp_path, capsys):
    controller, row, _ = research
    destination = tmp_path / 'cli-overwrite.txt'
    destination.write_bytes(b'Previously selected export')
    result = main(['format', '--data-dir', str(controller.library.root),
                   '--workspace', controller.workspace, '--reference', row.id,
                   '--output', str(destination), '--overwrite'])
    assert result == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)['result'] == 'SAVED' and not captured.err
    assert row.title in destination.read_text()
    assert controller.get(row.id) == row


def test_reopened_library_literal_braces_survive_actual_workbench_formatting(tmp_path):
    app = LibraryApp(tmp_path / 'literal-library')
    workspace = app.create_workspace('Literal author boundary')
    controller = AcademicController(app, workspace)
    source = '@book{x,title={Retained literal author},author={\\{Research Group\\}},year=2026}'
    preview = controller.preview(source.encode('utf-8'), 'bibtex')
    imported = controller.import_selected(preview, (0,))
    identity, = imported.imported_ids
    reopened = AcademicController(LibraryApp(app.root), workspace)
    original = reopened.get(identity)
    assert original.authors == ('{Research Group}',)
    workbench = BibliographyWorkbench(reopened)
    formatted = workbench.preview(identity=identity, name_mode='literal')
    assert '{Research Group}' in formatted.bibliography
    assert '{Research Group}' in workbench.copy_text(formatted)
    html = workbench.export(formatted, tmp_path / 'literal-author.html', format='html')
    assert '{Research Group}' in html
    assert reopened.get(identity) == original
