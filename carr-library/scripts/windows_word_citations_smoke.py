"""Optional real desktop Word qualification using only newly created synthetic files.

No existing user document is opened. This probe alone explicitly creates and
closes its own empty Word instance. Production attaches only to an already
running Word. --require-word fails an unavailable/NOT_RUN result.
"""
import argparse
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

SCHEMA = 'carr-native-word-citations/1'


def unavailable(reason):
    return {'schema': SCHEMA, 'status': 'NOT_RUN', 'reason': reason,
            'actual_office': 'NOT_RUN', 'actual_nvda': 'NOT_RUN', 'checks': 0, 'check_names': []}


def probe():
    if os.name != 'nt':
        return unavailable('Windows desktop Word is unavailable on this platform.')
    try:
        import pythoncom
        from win32com.client import DispatchEx
    except ImportError:
        return unavailable('The optional pinned pywin32 runtime is unavailable.')
    from carr_library.application import LibraryApp
    from carr_library.ui.academic_library_dialog import AcademicController
    from carr_library.citations.bibliography_workbench import BibliographyWorkbench, records_for_style
    from carr_library.citations.documents import CitationParagraph, citation_document, read_citation_docx
    from carr_library.citations.word_com import WordBridge, _WordApplication, _BEFORE

    native, owned, checks = None, False, []
    original = document = None
    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    try:
        try:
            native = DispatchEx('Word.Application')
        except pythoncom.com_error as exc:
            if (int(exc.hresult) & 0xffffffff) in (0x80040154, 0x800401f3):
                return unavailable('Desktop Microsoft Word is not installed/registered.')
            raise
        if int(native.Documents.Count) != 0:
            raise RuntimeError('The probe did not receive a new empty Word instance.')
        owned = True
        native.Visible = True

        @contextmanager
        def connector():
            session = _WordApplication(native)
            try:
                yield session
            finally:
                session._release()

        with tempfile.TemporaryDirectory(prefix='CARR Word synthetic Ω ') as temporary:
            root = Path(temporary).resolve(strict=True)
            app = LibraryApp(root / 'library')
            workspace = app.create_workspace('Synthetic Word qualification')
            controller = AcademicController(app, workspace)
            first = controller.create(dict(title='Synthetic source one 😀', authors=('Smith, Jane',), year='2024'))
            second = controller.create(dict(title='Synthetic source two', authors=('Reader, Ana',), year='2025'))
            workbench = BibliographyWorkbench(controller)
            preview = workbench.preview(name_mode='family-given')
            records, _ = records_for_style(workbench.check(preview), name_mode=preview.name_mode)
            bridge = WordBridge(workbench, connector=connector)
            for style in ('apa', 'chicago-note'):
                prefix = style + '.'
                cluster = dict(id='retained-citation-id', items=[{'id': first.id}],
                               note_index=1 if style == 'chicago-note' else None)
                source = root / (style + '-original.docx')
                payload = citation_document(records, style=style, citations=[cluster],
                    paragraphs=[CitationParagraph('Synthetic untouched body 😀 ', ('retained-citation-id',))])
                source.write_bytes(payload)
                original_hash = sha256(payload).hexdigest()
                original = native.Documents.Open(FileName=str(source), AddToRecentFiles=False, ReadOnly=False)
                original.Activate()
                copy = bridge.connect(root / (style + '-working.docx'))
                document = native.ActiveDocument
                assert Path(str(document.FullName)).resolve() == copy.path
                assert copy.path.read_bytes() == payload
                checks.append(prefix + 'exact_new_copy')
                initial = bridge.inspect(copy)
                assert initial.citation_ids == ('retained-citation-id',)
                assert int(document.ContentControls.Count) >= 1
                checks.append(prefix + 'native_bound_controls')
                position = int(document.Bookmarks.Item(_BEFORE).Range.End) + 1
                document.Range(position, position).Select()
                before = read_citation_docx(source)
                inserted = bridge.change(copy, preview=preview, items=[{'id': second.id}], insert=True)
                new_id, = set(inserted.citation_ids) - {'retained-citation-id'}
                assert inserted.status == 'complete' and inserted.quotation_verified is False
                checks.append(prefix + 'real_insert_stable_ids')
                assert document.Undo(1)
                assert bridge.inspect(copy).citation_ids == initial.citation_ids
                # This independently exercises actual Word custom-XML undo.
                from carr_library.citations.word_com import _ComDocument
                assert _ComDocument(native, document).snapshot() == before
                checks.append(prefix + 'single_word_undo_restores_snapshot')
                document.Range(position, position).Select()
                inserted = bridge.change(copy, preview=preview, items=[{'id': second.id}], insert=True)
                new_id, = set(inserted.citation_ids) - {'retained-citation-id'}
                edited = bridge.change(copy, preview=preview, occurrence_id=new_id,
                    items=[{'id': second.id, 'locator': '12', 'label': 'page'}])
                assert edited.citation_ids == inserted.citation_ids
                checks.append(prefix + 'real_edit_retains_ids')
                refreshed = bridge.change(copy, preview=preview)
                assert refreshed.citation_ids == edited.citation_ids and not refreshed.saved_to_disk
                checks.append(prefix + 'real_bibliography_refresh')
                if style == 'chicago-note':
                    assert int(document.Endnotes.Count) == 2
                else:
                    assert int(document.Endnotes.Count) == 0 and int(document.ContentControls.Count) == 3
                checks.append(prefix + 'native_story_structure')
                document.Save()  # Explicit synthetic probe save, never production auto-save.
                saved = read_citation_docx(copy.path)
                assert tuple(saved['citation_ids']) == refreshed.citation_ids
                assert saved['quotation_verified'] is saved['visible_edits_verified'] is False
                checks.append(prefix + 'actual_word_save_readback')
                original.Activate()
                try:
                    bridge.change(copy, preview=preview)
                except ValueError:
                    pass
                else:
                    raise AssertionError('An unrelated active document was accepted.')
                checks.append(prefix + 'foreign_active_document_refused')
                assert sha256(source.read_bytes()).hexdigest() == original_hash
                assert 'Synthetic untouched body' in str(document.Content.Text)
                checks.append(prefix + 'original_and_prose_preserved')
                document.Close(SaveChanges=0)
                original.Close(SaveChanges=0)
        assert len(checks) == len(set(checks)) == 20
        return {'schema': SCHEMA, 'status': 'PASS', 'actual_office': 'PASS',
                'actual_nvda': 'NOT_RUN', 'controlled_boundary': False,
                'checks': len(checks), 'check_names': checks}
    finally:
        # Detach the probe's remaining document proxies before leaving its
        # own COM apartment; production session wrappers do the same.
        document = original = None
        try:
            if native is not None and owned:
                native.Quit(SaveChanges=0)
        finally:
            native = None
            pythoncom.CoUninitialize()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-word', action='store_true',
        help='Require actual Office qualification; unavailable Word is a failing result.')
    args = parser.parse_args(argv)
    try:
        result = probe()
    except Exception as exc:
        result = {'schema': SCHEMA, 'status': 'FAIL', 'actual_office': 'FAIL',
                  'actual_nvda': 'NOT_RUN', 'error_type': type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result['status'] == 'FAIL':
        return 1
    return 2 if args.require_word and result['status'] != 'PASS' else 0


if __name__ == '__main__':
    raise SystemExit(main())
