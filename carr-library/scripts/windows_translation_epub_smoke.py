"""Installed native EPUB translation route with an actual format worker.

Only picker/consent answers and the selected synthetic HTTP transport are
controlled. wx buttons, DesktopExecutor/ProgressHub, retained source capture,
format children, ordinary jobs/ledger and protected EPUB publication are real.
No Word layout, live translator, quality, accessibility-reader or NVDA claim.
"""
import argparse
from hashlib import sha256
import http.client
from io import BytesIO
import importlib.util
import json
import xml.etree.ElementTree as ET
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic, sleep
import traceback
from zipfile import ZipFile


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows wx is required.'}))
        return 77
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'src')
    args = parser.parse_args()
    import ctypes
    from ctypes import wintypes
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library import progress, durable_state
    from carr_library.assistant import (
        profiles, providers, translation, translation_documents, translation_layout, translation_layout_worker,
        translation_partition, translation_epub_layout, translation_epub_layout_worker)
    from carr_library.ui import translation_documents as document_ui
    from carr_library.ui import translation_layout as layout_ui
    from carr_library.ui import desktop_app, worker_lifetime
    from carr_library.research import export_file
    from carr_library.reader import reading_desk
    from carr_library.audio import process
    from carr_library.operations import broker

    modules = (translation_layout, translation_layout_worker, translation_documents,
        translation, providers, profiles, document_ui, layout_ui, export_file,
        reading_desk, durable_state, progress, desktop_app, worker_lifetime, process, broker,
        translation_partition, translation_epub_layout, translation_epub_layout_worker)
    checks, errors, calls, children, origins = [], [], [], [], {}
    ui_thread = threading.get_ident()
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)
    for module in modules:
        actual = Path(module.__file__).resolve()
        expected = args.source_root / (module.__name__.replace('.', '/') + '.py')
        check('installed exact source: ' + module.__name__,
            actual.is_relative_to(Path(sys.prefix).resolve())
            and not actual.is_relative_to(args.source_root.resolve())
            and 'site-packages' in actual.parts and actual.read_bytes() == expected.read_bytes())
        origins[module.__name__] = {'path': str(actual), 'sha256': sha256(actual.read_bytes()).hexdigest()}

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            self.ExitMainLoop()
            return False
    app = App(False)
    simulator = wx.UIActionSimulator()
    check('native Windows wx', wx.Platform == '__WXMSW__')
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.IsWindow.argtypes, user32.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.SetForegroundWindow.argtypes, user32.SetForegroundWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.GetForegroundWindow.argtypes, user32.GetForegroundWindow.restype = [], wintypes.HWND
    def pump(predicate, label, timeout=35, *, cleanup=False):
        until = monotonic() + timeout
        while not predicate():
            if errors and not cleanup:
                raise RuntimeError(errors[-1])
            if monotonic() > until:
                raise TimeoutError(label)
            app.Yield()
            sleep(.01)
    def press(control):
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control and user32.GetForegroundWindow() == top.GetHandle(),
             'Native keyboard focus')
        check('native Space down: ' + control.GetName(), simulator.KeyDown(wx.WXK_SPACE))
        app.Yield()
        sleep(.04)
        check('native Space up: ' + control.GetName(), simulator.KeyUp(wx.WXK_SPACE))
        app.Yield()
    def find(dialog, name):
        result = wx.FindWindowByName(name, dialog)
        if result is None:
            raise AssertionError('Missing native control: ' + name)
        return result

    class SyntheticHTTP:
        def __init__(self, host, port, **options):
            if (host, port) != ('127.0.0.1', 55123):
                raise AssertionError('Only the explicitly selected synthetic transport is allowed.')
            self.position = 0
        def request(self, method, route, body=None, headers=None):
            task = progress.current_progress()
            if threading.get_ident() == ui_thread or task is None:
                raise AssertionError('Translation must run in the actual shared host worker/progress context.')
            if any(key.lower() in ('authorization', 'x-goog-api-key') for key in (headers or {})):
                raise AssertionError('The synthetic local transport must receive no credentials.')
            calls.append((method, route))
            if method == 'GET':
                result = [{'code': 'en', 'name': 'English', 'targets': ['uk']},
                          {'code': 'uk', 'name': 'Ukrainian', 'targets': []}]
            else:
                payload = json.loads(body)
                if route != '/translate' or payload['format'] != 'text':
                    raise AssertionError('Only approved paragraph text may leave the format mapping.')
                result = {'translatedText': 'Похідний контроль: ' + payload['q']}
            self.raw = json.dumps(result, ensure_ascii=False).encode('utf-8')
        def getresponse(self):
            self.status = 200
            return self
        def getheader(self, name, default=None):
            return {'Content-Type': 'application/json', 'Content-Length': str(len(self.raw))}.get(name, default)
        def read(self, size):
            value = self.raw[self.position:self.position + size]
            self.position += len(value)
            return value
        def close(self):
            pass

    finished = threading.Event()
    def watchdog():
        if not finished.wait(180):
            print('FAIL: native EPUB translation cleanup-inclusive watchdog', flush=True)
            os._exit(2)
    threading.Thread(target=watchdog, name='epub-native-watchdog', daemon=True).start()
    actual = (http.client.HTTPConnection, wx.MessageBox, wx.SingleChoiceDialog, wx.FileDialog,
              translation_layout.run_process, translation_epub_layout.write_binary_export)
    frame = None
    with TemporaryDirectory(prefix='resonance-epub-translation-') as temporary:
        root = Path(temporary)
        try:
            library = LibraryApp(root / 'private-library')
            workspace = library.create_workspace('Native EPUB translation')
            fixture_path = Path(__file__).with_name('translation_layout_fixtures.py').resolve(strict=True)
            fixture_sha = sha256(fixture_path.read_bytes()).hexdigest()
            spec = importlib.util.spec_from_file_location('native_epub_original_source', fixture_path)
            fixture = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fixture)
            raw = fixture.epub_bytes(chapters=1)
            source = root / 'Selected original Ω.epub'
            source.write_bytes(raw)
            imported = library._import_file(workspace, source, title='Native EPUB Ω',
                authors=('Synthetic source',), work_id='native-epub-work', edition_id='native-epub-edition')
            check('actual canonical EPUB import', imported['state'] == 'complete' and imported['verified'] is False)
            profile = profiles.ProviderProfile('native_epub_local', 'Synthetic local EPUB API',
                'libretranslate-local', 'http://127.0.0.1:55123', 'synthetic-protocol-only',
                ('translation.text', 'translation.languages'), local=True, enabled=True)
            profiles.ProfileStore(library.root / 'state/providers').save(profile)
            http.client.HTTPConnection = SyntheticHTTP
            answers = [wx.YES, wx.NO, wx.YES]
            def approval(message, title, flags, parent):
                check('explicit default-No consent: ' + title, bool(flags & wx.NO_DEFAULT))
                return answers.pop(0)
            wx.MessageBox = approval
            class Choice:
                def __init__(self, *args, **kwargs):
                    pass
                def ShowModal(self):
                    return wx.ID_OK
                def GetSelection(self):
                    return 0
                def Destroy(self):
                    pass
            output = root / 'Native translated Ω.epub'
            class Destination(Choice):
                def GetPath(self):
                    return str(output)
            wx.SingleChoiceDialog, wx.FileDialog = Choice, Destination
            original_run = translation_layout.run_process
            def owned_format(args, **kwargs):
                if threading.get_ident() == ui_thread or progress.current_progress() is None:
                    raise AssertionError('Format parsing must use the actual host worker and ProgressHub.')
                if kwargs.get('owned_windows_job') is not True:
                    raise AssertionError('The native format worker must use the existing Windows Job owner.')
                if Path(args[-2]).resolve() != Path(translation_epub_layout_worker.__file__).resolve():
                    raise AssertionError('The format worker must execute the exact installed module.')
                result = original_run(args, **kwargs)
                children.append(result.returncode)
                return result
            translation_layout.run_process = owned_format
            frame = desktop_app.DesktopFrame(library)
            frame.Show()
            app.SetTopWindow(frame)
            check('actual shared desktop executor', isinstance(frame.executor, worker_lifetime.DesktopExecutor))
            check('actual unchanged host worker', frame.run_job.__func__ is desktop_app.DesktopFrame.run_job)
            check('actual shared ProgressHub', isinstance(frame.progress_panel.hub, progress.ProgressHub))
            panel = frame.book.GetPage(1)
            check('actual retained catalog scope', panel.ready and panel.workspace == workspace
                  and 'native-epub-edition' in panel.rows and frame.active == (workspace, None))
            commands = {item.command_id for item in frame.keyboard_commands.registry.commands('main')}
            check('existing document keyboard command retained', 'tools.translation_documents' in commands)

            def formatted_phase():
                dialog = None
                try:
                    dialog = next(item for item in wx.GetTopLevelWindows()
                                  if item.GetName() == 'Formatted EPUB translation workstation')
                    check('formatted workstation is a native modal', dialog.IsModal())
                    status = find(dialog, 'Formatted translation status and limitations')
                    selection = find(dialog, 'Selected retained EPUB and paragraph jobs')
                    check('limitations visible before approval', status.GetValue() == translation_epub_layout_worker.NOTICE)
                    check('source selection is readonly', not selection.IsEditable())
                    find(dialog, 'EPUB source language; auto detects').ChangeValue('en')
                    find(dialog, 'EPUB total approved USD budget; local service requires 0').ChangeValue('0')
                    press(find(dialog, 'Fetch EPUB languages'))
                    pump(lambda: not frame.working and '"languages"' in status.GetValue(), 'Declared language request')
                    before = len(calls)
                    press(find(dialog, 'Choose retained EPUB'))
                    pump(lambda: not frame.working and '"paragraphs": 3' in selection.GetValue(),
                         'Actual retained EPUB mapping')
                    check('format inspection sends no source text', len(calls) == before)
                    press(find(dialog, 'Preview EPUB translation'))
                    pump(lambda: not frame.working and 'review_sha256' in status.GetValue(), 'Exact EPUB review')
                    check('review binds mapping and source with no quotation authority',
                          'mapping_sha256' in status.GetValue() and '"quotation_authority": false' in status.GetValue())
                    press(find(dialog, 'Approve and save EPUB jobs'))
                    check('default-No decision saves no job and makes no request',
                          not frame.working and not translation.TranslationService(
                              library.root / 'state/translation', library=library).list() and len(calls) == before)
                    press(find(dialog, 'Approve and save EPUB jobs'))
                    pump(lambda: not frame.working and '"status": "prepared"' in status.GetValue(), 'EPUB job preparation')
                    check('preparation makes no provider call', len(calls) == before)
                    press(find(dialog, 'Run / resume EPUB jobs'))
                    pump(lambda: not frame.working and '"status": "complete"' in status.GetValue(), 'EPUB paragraph completion')
                    check('exact three reviewed paragraph requests', len(calls) == before + 3)
                    profiles.ProfileStore(library.root / 'state/providers').remove(profile.id)
                    original_export = translation_epub_layout.write_binary_export
                    def publish_then_stop(*args, **kwargs):
                        receipt = original_export(*args, **kwargs)
                        frame.cancel_event.set()
                        return receipt
                    translation_epub_layout.write_binary_export = publish_then_stop
                    press(find(dialog, 'Export new translated EPUB'))
                    pump(lambda: not frame.working and '"format": "epub"' in status.GetValue(), 'Completed offline EPUB export')
                    receipt = json.loads(status.GetValue())
                    check('late Stop retains actual committed completion',
                          receipt['status'] == 'complete' and output.is_file() and frame.cancel_event.is_set())
                    check('completed export needs no active profile',
                          not profiles.ProfileStore(library.root / 'state/providers').list()
                          and len(calls) == before + 3)
                    exported = output.read_bytes()
                    check('exact committed EPUB receipt',
                          receipt['output_sha256'] == sha256(exported).hexdigest()
                          and receipt['output_bytes'] == len(exported)
                          and receipt['quotation_authority'] is False)
                    with ZipFile(BytesIO(raw)) as original_zip, ZipFile(BytesIO(exported)) as translated_zip:
                        check('all unselected EPUB parts preserved exactly',
                              original_zip.namelist() == translated_zip.namelist()
                              and all(original_zip.read(name) == translated_zip.read(name)
                                      for name in original_zip.namelist() if name not in receipt['changed_members']))
                    with ZipFile(BytesIO(exported)) as archive:
                        chapter = ET.fromstring(archive.read('OEBPS/chapter-1.xhtml'))
                        ns = '{http://www.w3.org/1999/xhtml}'
                        heading = chapter.find('.//' + ns + 'h1')
                        cells = chapter.findall('.//' + ns + 'td')
                        check('actual XHTML blocks/table/styles/links translation',
                              ''.join(heading.itertext()).startswith('Похідний контроль:')
                              and len(cells) == 2 and all(''.join(cell.itertext()).startswith('Похідний контроль:') for cell in cells)
                              and heading.get('class') == 'chapter' and heading.get('id') == 'heading'
                              and heading.find(ns + 'a').get('href') == '#table'
                              and heading.find('.//' + ns + 'strong') is not None
                              and heading.find('.//' + ns + 'em') is not None)
                    check('original source bytes remain unchanged', source.read_bytes() == raw)
                    check('completed export retains readable native status focus', wx.Window.FindFocus() is status)
                    translation_epub_layout.write_binary_export = original_export
                    press(find(dialog, 'Export new translated EPUB'))
                    pump(lambda: not frame.working and 'existing files are not replaced' in status.GetValue(),
                         'No-clobber refusal')
                    check('existing EPUB export never overwritten', output.read_bytes() == exported)
                    check('real installed formatting children completed', len(children) >= 4 and all(code == 0 for code in children))
                    check('format resource leases quiescent', broker.ResourceBroker.for_app(library.root).snapshot()['leases'] == [])
                    press(find(dialog, 'Close formatted translation'))
                except Exception:
                    errors.append(traceback.format_exc())
                    if dialog is not None and dialog.IsModal():
                        dialog.EndModal(wx.ID_CANCEL)

            def document_phase():
                dialog = None
                try:
                    dialog = next(item for item in wx.GetTopLevelWindows()
                                  if item.GetName() == 'Document translation workstation')
                    check('existing document workstation route preserved', dialog.IsModal())
                    wx.CallLater(150, formatted_phase)
                    press(find(dialog, 'Translate EPUB with layout'))
                    if not errors:
                        press(find(dialog, 'Close'))
                    elif dialog.IsModal():
                        dialog.EndModal(wx.ID_CANCEL)
                except Exception:
                    errors.append(traceback.format_exc())
                    if dialog is not None and dialog.IsModal():
                        dialog.EndModal(wx.ID_CANCEL)

            def opened():
                try:
                    wx.CallLater(150, document_phase)
                    document_ui.show_translation_documents(frame)
                except Exception:
                    errors.append(traceback.format_exc())
                finally:
                    app.ExitMainLoop()
            wx.CallAfter(opened)
            app.MainLoop()
        finally:
            try:
                if frame is not None:
                    handle, executor = frame.GetHandle(), frame.executor
                    frame.cancel_event.set()
                    try:
                        # Exercise the real host owner order, including its
                        # schedules/tray hooks before executor finalization.
                        frame.Close(force=True)
                        pump(lambda: not user32.IsWindow(handle),
                             'Actual host Close did not release its native owners.',
                             cleanup=True)
                    finally:
                        executor.shutdown(wait=True, cancel_futures=True)
                    check('host shutdown retains no failed cleanup owner',
                          not user32.IsWindow(handle) and not executor.cleanup_failures)
                app.Yield()
            finally:
                (http.client.HTTPConnection, wx.MessageBox, wx.SingleChoiceDialog, wx.FileDialog,
                 translation_layout.run_process, translation_epub_layout.write_binary_export) = actual
                if native_handle is not None:
                    native_handle.close()
    if sha256(fixture_path.read_bytes()).hexdigest() != fixture_sha:
        raise RuntimeError('The original native EPUB fixture changed during qualification.')
    for module in modules:
        value = origins[module.__name__]
        check('post-cleanup installed hash: ' + module.__name__,
              sha256(Path(value['path']).read_bytes()).hexdigest() == value['sha256'])
    if errors:
        raise RuntimeError('\n'.join(errors))
    finished.set()
    print(json.dumps({'status': 'PASS', 'checks': checks, 'check_count': len(checks),
        'source_origins': origins, 'native_keyboard': 'PASS', 'format_children': 'PASS',
        'source_bound_epub': 'PASS', 'package_preservation': 'PASS',
        'completed_offline_export': 'PASS', 'late_stop_completion': 'PASS',
        'synthetic_protocol_calls': len(calls), 'controlled_pickers_and_consent': True,
        'real_translation_service': 'NOT_RUN', 'translation_quality': 'NOT_RUN',
        'visual_layout': 'NOT_RUN', 'epubcheck': 'NOT_RUN', 'fixture_sha256': fixture_sha, 'nvda': 'NOT_RUN'}, ensure_ascii=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
