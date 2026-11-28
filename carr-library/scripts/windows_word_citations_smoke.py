"""Optional actual desktop Word qualification on new synthetic documents only.

The production bridge never starts, saves, closes or quits Word. This explicit
probe creates one new empty instance after refusing any already-running Word.
It closes only that verified instance and only its authored document paths.
Office absence is NOT_RUN with zero checks; --require-word makes it a failure.
COM calls and cleanup are cooperative: a blocked external call is not reported
as cancelled, rolled back or quiescent. Run this on an otherwise idle desktop.
"""
import argparse
from contextlib import contextmanager
import gc
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from time import monotonic

SCHEMA = 'carr-native-word-citations/2'


def unavailable(reason):
    return dict(schema=SCHEMA, status='NOT_RUN', reason=reason, actual_office='NOT_RUN',
        native_keyboard='NOT_RUN', actual_nvda='NOT_RUN', checks=0, check_names=[])


def _bindings():
    path = Path(__file__).with_name('windows_publishing_smoke.py')
    spec = importlib.util.spec_from_file_location('_word_native_bindings', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from carr_library import application, durable_state
    from carr_library.academic_integration import documents, formatting, regeneration, word_com
    from carr_library.citations import bibliography_workbench
    from carr_library.ui import academic_library_dialog, citation_formatting, word_citations_dialog
    selected = (application, durable_state, documents, formatting, regeneration, word_com,
                bibliography_workbench, academic_library_dialog, citation_formatting, word_citations_dialog)
    files = (Path(__file__), path)
    return module, selected, {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in files}


def _windows_api():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user = ctypes.WinDLL('user32', use_last_error=True)
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
            ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
            ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
            ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
            ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32FirstW.restype = kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowThreadProcessId.restype = wintypes.DWORD

    def processes():
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        result, count = set(), 0
        try:
            entry = Entry()
            entry.dwSize = ctypes.sizeof(entry)
            present = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            while present:
                count += 1
                if count > 65536:
                    raise RuntimeError('The native process census exceeds its finite bound.')
                if entry.szExeFile.casefold() == 'winword.exe':
                    result.add(int(entry.th32ProcessID))
                present = kernel.Process32NextW(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(snapshot)
        return result

    def process_for_window(handle):
        pid = wintypes.DWORD()
        if not user.GetWindowThreadProcessId(int(handle), ctypes.byref(pid)) or not pid.value:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(pid.value)

    def hold_process(pid):
        handle = kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    return kernel, processes, process_for_window, hold_process


def probe(*, seconds=240):
    if os.name != 'nt':
        return unavailable('Windows desktop Word is unavailable on this platform.')
    from carr_library.application import LibraryApp
    from carr_library.academic_integration.documents import (
        CitationParagraph, read_citation_docx, export_citation_docx)
    from carr_library.academic_integration.formatting import CitationCluster, CitationRequest
    from carr_library.academic_integration import word_com as word
    from carr_library.durable_state import local_path, regular
    from carr_library.ui.academic_library_dialog import AcademicController
    from carr_library.ui.citation_formatting import CitationFormattingController

    bindings, selected, scripts = _bindings()
    identities = bindings.verify_installed_sources(*selected)
    kernel, processes, window_pid, hold_process = _windows_api()
    if processes():
        return unavailable('Close all existing Word sessions before this synthetic qualification.')
    try:
        word.prepare_word_runtime()  # The first comtypes import belongs to main.
    except word.WordUnavailable as exc:
        return unavailable(str(exc))
    import comtypes
    from comtypes.client import CreateObject

    native = original = document = process_handle = app = None
    owned_pid = None
    root = None
    allowed = set()
    checks = []
    started = monotonic()
    def checkpoint():
        if monotonic() - started > seconds:
            raise TimeoutError('The cooperative synthetic Word budget expired between host calls.')
    def check(name, value):
        checkpoint()
        if not value:
            raise AssertionError(name)
        if name in checks:
            raise AssertionError('Duplicate native oracle.')
        checks.append(name)
        print('PASS: ' + name, flush=True)
    def owns_instance():
        if owned_pid is None or native is None or window_pid(native.Hwnd) != owned_pid:
            raise RuntimeError('The synthetic Word application identity changed.')
        if kernel.WaitForSingleObject(process_handle, 0) != 258:
            raise RuntimeError('The held synthetic Word process is no longer running.')
    def documents_owned():
        owns_instance()
        count = int(native.Documents.Count)
        if not 0 <= count <= 4:
            raise RuntimeError('The synthetic Word instance has an unexpected document inventory.')
        result = []
        for index in range(1, count + 1):
            item = native.Documents.Item(index)
            path = local_path(str(item.FullName), exists=True)
            if path not in allowed:
                raise RuntimeError('A foreign document appeared; automatic close/quit was refused.')
            result.append(item)
        return result

    successful = False
    try:
        try:
            native = CreateObject('Word.Application', dynamic=True,
                                  clsctx=comtypes.CLSCTX_LOCAL_SERVER)
        except (comtypes.COMError, OSError) as exc:
            code = int(getattr(exc, 'hresult', getattr(exc, 'winerror', 0))) & 0xffffffff
            if code in (0x80040154, 0x800401f3):
                return unavailable('Desktop Microsoft Word is not installed or registered.')
            raise
        pid = window_pid(native.Hwnd)
        if pid not in processes() or int(native.Documents.Count) != 0:
            raise RuntimeError('CreateObject did not return a new empty Word process.')
        process_handle = hold_process(pid)
        owned_pid = pid
        native.Visible = True
        root = local_path(Path(tempfile.mkdtemp(prefix='carr-word-synthetic-')), exists=True)
        regular(root, directory=True)
        app = LibraryApp(root / 'library')
        workspace = app.create_workspace('Synthetic actual Word qualification')
        retained = app.add_text(workspace, 'Synthetic retained source', 'Exact source wording 😀.')
        retained_path = app.assets.resolve(retained['asset'])
        retained_bytes = retained_path.read_bytes()
        academic = AcademicController(app, workspace)
        first = academic.create(dict(title='Synthetic source one 😀', authors=('Smith, Jane',),
            year='2024', edition_id=retained['edition']))
        second = academic.create(dict(title='Synthetic source two', authors=('Reader, Ana',), year='2025'))

        @contextmanager
        def connector():
            # This is the actual owned Word COM object, not a controlled host.
            # Production's default running-instance/worker-STA attachment is a
            # separately tested contract; no cross-thread proxy is passed here.
            checkpoint()
            owns_instance()
            session = word._WordApplication(native)
            try:
                yield session
            finally:
                session._release()

        for style in ('apa', 'chicago-notes'):
            prefix = style + '.'
            formatter = CitationFormattingController(app, workspace)
            snapshot = formatter.load()
            cluster = CitationCluster((CitationRequest(first.id),), 'retained-citation-id',
                                      1 if style == 'chicago-notes' else None)
            formatted = formatter.render(snapshot, style=style, citations=(cluster,))
            source = root / (style + '-original.docx')
            export_citation_docx(formatted, source, paragraphs=[
                CitationParagraph('Synthetic untouched body 😀 ', ('retained-citation-id',))])
            source = local_path(source, exists=True)
            payload = source.read_bytes()
            original_hash = sha256(payload).hexdigest()
            allowed.add(source)
            checkpoint()
            original = native.Documents.Open(FileName=str(source), ConfirmConversions=False,
                AddToRecentFiles=False, ReadOnly=False, Visible=True, OpenAndRepair=False,
                NoEncodingDialog=True)
            original.Activate()
            bridge = word.WordBridge(formatter, snapshot, formatted, connector=connector)
            destination = root / (style + '-working.docx')
            # This exact create-only destination belongs to this synthetic run.
            allowed.add(destination)
            copy = bridge.connect(destination, cancelled=lambda: monotonic() - started > seconds)
            document = native.ActiveDocument
            check(prefix + 'exact_new_copy',
                  local_path(str(document.FullName), exists=True) == copy.path
                  and copy.path.read_bytes() == payload)
            initial = bridge.inspect(copy)
            check(prefix + 'native_bound_controls',
                  initial.citation_ids == ('retained-citation-id',)
                  and int(document.ContentControls.Count) >= 1)
            position = int(document.Bookmarks.Item(word._BEFORE).Range.End) + 1
            document.Range(position, position).Select()
            before = read_citation_docx(source)
            inserted = bridge.change(copy, items=(CitationRequest(second.id),), insert=True)
            new_id, = set(inserted.citation_ids) - {'retained-citation-id'}
            check(prefix + 'real_insert_stable_ids',
                  inserted.status == 'complete' and inserted.quotation_verified is False)
            if not document.Undo(1):
                raise AssertionError('Actual Word Undo refused its single action.')
            observed = bridge.inspect(copy)
            native_document = word._ComDocument(native, document)
            try:
                actual_snapshot = native_document.snapshot()
            finally:
                native_document.doc = native_document.app = None
                native_document = None
            check(prefix + 'single_word_undo_restores_snapshot',
                  observed.citation_ids == initial.citation_ids and actual_snapshot == before)
            document.Range(position, position).Select()
            inserted = bridge.change(copy, items=(CitationRequest(second.id),), insert=True)
            new_id, = set(inserted.citation_ids) - {'retained-citation-id'}
            edited = bridge.change(copy, occurrence_id=new_id,
                items=(CitationRequest(second.id, '12', locator_verified=True),))
            check(prefix + 'real_edit_retains_ids', edited.citation_ids == inserted.citation_ids)
            refreshed = bridge.change(copy)
            check(prefix + 'real_bibliography_refresh',
                  refreshed.citation_ids == edited.citation_ids and not refreshed.saved_to_disk)
            check(prefix + 'native_story_structure',
                  int(document.Endnotes.Count) == 2 if style == 'chicago-notes' else
                  int(document.Endnotes.Count) == 0 and int(document.ContentControls.Count) == 3)
            checkpoint()
            owns_instance()
            document.Save()  # Explicit synthetic-probe action, never production.
            saved = read_citation_docx(copy.path)
            check(prefix + 'actual_word_save_readback',
                  tuple(item['id'] for item in saved['clusters']) == refreshed.citation_ids
                  and tuple(item['cluster_id'] for item in saved['citations']) == refreshed.citation_ids
                  and tuple(item['id'] for item in saved['references']) == refreshed.reference_ids
                  and bridge.completed_result(copy).quotation_verified is False)
            original.Activate()
            try:
                bridge.change(copy)
            except ValueError:
                pass
            else:
                raise AssertionError('An unrelated active document was accepted.')
            check(prefix + 'foreign_active_document_refused', True)
            check(prefix + 'original_and_prose_preserved',
                  sha256(source.read_bytes()).hexdigest() == original_hash
                  and 'Synthetic untouched body' in str(document.Content.Text)
                  and retained_path.read_bytes() == retained_bytes)
            documents_owned()
            document.Close(SaveChanges=0)
            document = None
            original.Close(SaveChanges=0)
            original = None
        if len(checks) != len(set(checks)) or len(checks) != 20:
            raise AssertionError('The actual Word oracle set is incomplete.')
        if bindings.verify_installed_sources(*selected) != identities:
            raise AssertionError('An installed source changed during native qualification.')
        if {path: sha256(Path(path).read_bytes()).hexdigest() for path in scripts} != scripts:
            raise AssertionError('The native script or identity helper changed.')
        successful = True
    finally:
        # The first-import COM lifetime remains owned by main until normal exit.
        # Every Python/handle owner is released even if Word refuses cleanup.
        # No process is killed and no unverified/foreign document is closed.
        primary_failure = sys.exc_info()[1]
        cleanup_errors = []
        try:
            if native is not None and owned_pid is not None:
                for item in documents_owned():
                    item.Close(SaveChanges=0)
                item = None
                owns_instance()
                if int(native.Documents.Count) != 0:
                    raise RuntimeError('A document appeared during cleanup; automatic Quit was refused.')
                native.Quit(SaveChanges=0)
        except BaseException as exc:
            cleanup_errors.append(type(exc).__name__ + ': ' + str(exc)[:800])
        finally:
            item = document = original = native = None
            gc.collect()
            if process_handle is not None:
                try:
                    if kernel.WaitForSingleObject(process_handle, 10000) != 0:
                        cleanup_errors.append('Synthetic Word process exit is unconfirmed.')
                finally:
                    if not kernel.CloseHandle(process_handle):
                        cleanup_errors.append('The held synthetic process handle did not close.')
                    process_handle = None
            try:
                if app is not None and not app.close_search():
                    cleanup_errors.append('The synthetic library still owns a search resource.')
            except BaseException as exc:
                cleanup_errors.append(type(exc).__name__ + ': ' + str(exc)[:800])
            app = None
        if cleanup_errors:
            successful = False
            detail = '; '.join(cleanup_errors)[:1800]
            print('UNCONFIRMED_SYNTHETIC_WORD_CLEANUP ' + detail, file=sys.stderr, flush=True)
            if primary_failure is not None:
                primary_failure.add_note(detail)
        if root is not None:
            if successful:
                shutil.rmtree(root)
            else:
                print('RETAINED_SYNTHETIC_WORD_FIXTURE ' + str(root), file=sys.stderr, flush=True)
        if cleanup_errors and primary_failure is None:
            raise RuntimeError('Synthetic Word cleanup was not confirmed; retained files were kept.')
    return dict(schema=SCHEMA, status='PASS', actual_office='PASS', actual_nvda='NOT_RUN',
        native_keyboard='NOT_RUN', controlled_boundary=False, checks=len(checks), check_names=checks,
        source_identity=identities, probe_and_binding=scripts,
        host_scope='New synthetic Word instance; no original user document was opened.',
        attachment_scope='Direct owned COM object; production running-instance worker attachment has separate contract tests.',
        cancellation_scope='Cooperative between COM calls; no forced external-call quiescence or rollback claim.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-word', action='store_true',
        help='Fail if actual Office qualification is unavailable or did not pass.')
    parser.add_argument('--seconds', type=int, default=240,
        help='Cooperative budget between external COM calls, from 30 to 600 seconds.')
    args = parser.parse_args(argv)
    if not 30 <= args.seconds <= 600:
        parser.error('--seconds must be from 30 to 600.')
    try:
        result = probe(seconds=args.seconds)
    except Exception as exc:
        result = dict(schema=SCHEMA, status='FAIL', actual_office='FAIL', actual_nvda='NOT_RUN',
            native_keyboard='NOT_RUN', error_type=type(exc).__name__, message=str(exc)[:1600])
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False), flush=True)
    if result['status'] == 'FAIL':
        return 1
    return 2 if args.require_word and result['status'] != 'PASS' else 0


if __name__ == '__main__':
    raise SystemExit(main())
