"""Native installed translation controls with synthetic REST and a temporary OS-vault entry.

No Google call, billing or linguistic-quality claim. Actual native Space/focus,
shared gateway/ledger/job state, OAuth-vault reads and derivative bytes are checked.
Run: python -I -B scripts/windows_translation_v3_smoke.py --expect-installed --output result.json
"""
from argparse import ArgumentParser
from concurrent.futures import ThreadPoolExecutor
import faulthandler
from hashlib import sha256
from io import BytesIO
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from time import monotonic, sleep, time
import traceback
import xml.etree.ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED


def run_native(source, report, checkpoint):
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.assistant import profiles, translation, providers
    from carr_library.assistant import (translation_layout, translation_partition,
        translation_layout_worker, translation_epub_layout_worker)
    from carr_library.assistant.credentials import WindowsCredentialVault
    from carr_library.speech.reading import ReadingDocument
    from carr_library.ui import translation_dialog as ui

    checks, errors, calls = report['checks'], [], []
    check_lock = threading.RLock()
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        with check_lock:
            checks.append(label)
            checkpoint(label)

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            self.ExitMainLoop()
            return False
    app = App(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND

    def pump(predicate, label, timeout=20):
        deadline = monotonic() + timeout
        while not predicate():
            if errors:
                raise RuntimeError(errors[-1])
            if monotonic() > deadline:
                raise TimeoutError(label)
            app.Yield()
            sleep(.01)

    def press(control):
        name = control.GetName()
        top = wx.GetTopLevelParent(control)
        top.Raise(); user32.SetForegroundWindow(top.GetHandle()); control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control and user32.GetForegroundWindow() == top.GetHandle(), 'Native foreground focus')
        check('foreground focus: ' + name, wx.Window.FindFocus() is control)
        check('native Space down: ' + name, simulator.KeyDown(wx.WXK_SPACE))
        app.Yield(); sleep(.04)
        check('native Space up: ' + name, simulator.KeyUp(wx.WXK_SPACE))
        app.Yield()

    modules = (profiles, translation, providers, ui, translation_layout, translation_partition,
               translation_layout_worker, translation_epub_layout_worker)
    origins = {module.__name__: str(Path(module.__file__).resolve()) for module in modules}
    if source is None:
        from importlib.metadata import distribution
        package = Path(distribution('universal-research-library').locate_file('carr_library')).resolve(strict=True)
        for name, path in origins.items():
            check('installed module: ' + name, Path(path).is_relative_to(package))
    check('native Windows wx', wx.Platform == '__WXMSW__')
    parent = 'projects/123456789/locations/us-central1'
    address = 'https://translation.googleapis.com/v3/' + parent
    glossary = parent + '/glossaries/reviewed-terms'
    original = 'Unchanged synthetic source — Україна &amp; original.'

    class SyntheticHTTPS:
        def __init__(self, host, port, **options):
            check('fixed official authority', host == 'translation.googleapis.com' and port in (None, 443))
            self.position = 0
        def request(self, method, path, body, headers):
            calls.append((method, path))
            check('OAuth bearer, never API-key header', headers.get('Authorization', '').startswith('Bearer SYNTHETIC_') and 'X-goog-api-key' not in headers)
            if method == 'GET':
                result = {'languages': [{'languageCode': language, 'displayName': language, 'supportSource': True, 'supportTarget': True}
                                       for language in ('en', 'uk')]}
            else:
                request = json.loads(body)
                result = {'translations': [{'translatedText': 'Unused ordinary result', 'model': request['model']}],
                    'glossaryTranslations': [{'translatedText': 'Похідний переклад &amp; ' + request['contents'][0],
                        'model': request['model'], 'glossaryConfig': request['glossaryConfig']}]}
            self.raw = json.dumps(result, ensure_ascii=False).encode('utf-8')
        def getresponse(self):
            self.status = 200
            return self
        def getheader(self, name, default=None):
            return {'Content-Type': 'application/json', 'Content-Length': str(len(self.raw))}.get(name, default)
        def read(self, size):
            block = self.raw[self.position:self.position + size]; self.position += len(block)
            return block
        def close(self): pass

    def synthetic_transport(self, profile, route, body, headers, *, method='POST', **kwargs):
        # Test-only network replacement. The real gateway, vault, request
        # hashing, decoded ledger binding and response checks remain active.
        from urllib.parse import urlsplit
        parsed = urlsplit(profile.endpoint)
        connection = SyntheticHTTPS(parsed.hostname, parsed.port)
        try:
            connection.request(method, route, body, headers)
            response = connection.getresponse()
            return providers.HTTPResult(response.getheader('Content-Type'),
                response.read(kwargs.get('maximum_bytes', 1024 * 1024) + 1), response.status)
        finally:
            connection.close()

    actual_transport, actual_message, actual_directory = providers.HTTPTransport.request, wx.MessageBox, wx.DirDialog
    actual_file, actual_source_picker = wx.FileDialog, wx.SingleChoiceDialog
    vault, reference, frame = WindowsCredentialVault(), None, None
    executor = ThreadPoolExecutor(max_workers=1)
    with TemporaryDirectory(prefix='resonance-translation-native-') as temporary:
        root = Path(temporary)
        try:
            library = LibraryApp(root / 'private library')
            workspace = library.create_workspace('Native retained structure originals')
            from docx import Document
            document = Document()
            paragraph = document.add_paragraph(style='Heading 1')
            paragraph.add_run('Retained DOCX heading Ω.').bold = True
            table = document.add_table(rows=1, cols=2)
            table.style = 'Table Grid'
            table.cell(0, 0).text, table.cell(0, 1).text = 'Left original cell.', 'Right original cell.'
            document.sections[0].header.paragraphs[0].text = 'Untranslated original header.'
            original_docx = BytesIO()
            document.save(original_docx)
            package = (
                '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">'
                '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                '<dc:identifier id="book-id">urn:uuid:11111111-2222-4333-8444-555555555555</dc:identifier>'
                '<dc:title>Native retained EPUB</dc:title><dc:creator>Synthetic source</dc:creator>'
                '<dc:language>en</dc:language><meta property="dcterms:modified">2026-11-28T00:00:00Z</meta>'
                '</metadata><manifest><item id="text" href="text.xhtml" media-type="application/xhtml+xml"/>'
                '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                '<item id="style" href="style.css" media-type="text/css"/></manifest>'
                '<spine><itemref idref="text"/></spine></package>').encode('utf-8')
            members = {
                'META-INF/container.xml': b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
                'OEBPS/content.opf': package,
                'OEBPS/text.xhtml': ('<html xmlns="http://www.w3.org/1999/xhtml" lang="en" xml:lang="en">'
                    '<head><title>Original head title</title><link rel="stylesheet" type="text/css" href="style.css"/></head>'
                    '<body><h1 id="heading" class="kept">Retained EPUB heading Ω.</h1>'
                    '<p>Unchanged original paragraph.</p><table id="cells"><tbody><tr>'
                    '<td>Left EPUB cell.</td><td>Right EPUB cell.</td></tr></tbody></table></body></html>').encode('utf-8'),
                'OEBPS/nav.xhtml': b'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Original navigation</title></head><body><nav epub:type="toc" id="toc"><ol><li><a href="text.xhtml#heading">Original heading</a></li></ol></nav></body></html>',
                'OEBPS/style.css': b'.kept { color: #123456; } table { border: 1px solid black; }'}
            original_epub = BytesIO()
            with ZipFile(original_epub, 'w') as archive:
                archive.writestr('mimetype', b'application/epub+zip', compress_type=ZIP_STORED)
                for name, value in members.items():
                    archive.writestr(name, value, compress_type=ZIP_DEFLATED)
            structured = {}
            for kind, raw in (('docx', original_docx.getvalue()), ('epub', original_epub.getvalue())):
                path = root / ('original-' + kind + '.' + kind)
                path.write_bytes(raw)
                title, edition = 'Native structure ' + kind.upper(), 'native-structure-' + kind
                imported = library._import_file(workspace, path, title=title,
                    authors=('Synthetic original author',), work_id='work-' + edition, edition_id=edition)
                check('retained ' + kind.upper() + ' canonical import', imported['state'] == 'complete')
                structured[kind] = dict(path=path, raw=raw, title=title, edition=edition)
            profile = profiles.ProviderProfile('native_translation', 'Synthetic v3 account', 'google-translation-v3',
                address, 'general/nmt', capabilities=('translation.text', 'translation.languages'), enabled=True)
            store = profiles.ProfileStore(library.root / 'state' / 'providers')
            profile = profiles.save_google_token(store, vault, profile, 'SYNTHETIC_NATIVE_TOKEN', time() + 1800)
            reference = profile.credential_ref
            providers.HTTPTransport.request = synthetic_transport
            approvals = []
            def approve(message, *args, **kwargs):
                approvals.append(message)
                return wx.YES
            wx.MessageBox = approve
            class Directory(actual_directory):
                def ShowModal(self): return wx.ID_OK
                def GetPath(self): return str(root)
            wx.DirDialog = Directory
            export_path = [None]
            class NewFile(actual_file):
                # Only this destination answer is controlled. The real native
                # dialog identity/lifetime and all service export work remain.
                def ShowModal(self): return wx.ID_OK
                def GetPath(self): return str(export_path[0])
            wx.FileDialog = NewFile
            class SourceChoice(actual_source_picker):
                def ShowModal(self):
                    armed, failures, identity = [True], [], self.GetId()
                    def destroyed(event):
                        if event.GetId() == identity:
                            armed[0] = False
                        event.Skip()
                    self.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
                    def accept():
                        if not armed[0]:
                            return
                        try:
                            self.SetSelection(0)
                            check('native retained-source choice', self.GetSelection() == 0)
                            press(self.FindWindow(wx.ID_OK))
                        except BaseException:
                            failures.append(traceback.format_exc())
                            if armed[0] and self.IsModal():
                                self.EndModal(wx.ID_CANCEL)
                    later = wx.CallLater(80, accept)
                    try:
                        result = super().ShowModal()
                    finally:
                        armed[0] = False
                        later.Stop()
                    if failures:
                        raise RuntimeError(failures[-1])
                    return result
            wx.SingleChoiceDialog = SourceChoice
            frame = wx.Frame(None, title='Native Advanced v3 translation acceptance')
            frame.library = library
            frame.active = (workspace, None)
            frame.working = frame._closing = False
            frame.cancel_event = threading.Event()
            def run_job(title, callback, done, failed):
                frame.working = True; frame.cancel_event.clear()
                future = executor.submit(callback)
                def finish():
                    frame.working = False
                    try:
                        result = future.result()
                    except Exception as error:
                        failed(error)
                    else:
                        done(result)
                future.add_done_callback(lambda unused: wx.CallAfter(finish))
                return True
            frame.run_job = run_job
            frame.Show(); app.SetTopWindow(frame)

            def exercise():
                dialog = None
                try:
                    dialog = next(window for window in wx.GetTopLevelWindows() if window.GetName() == 'Translation workspace')
                    check('real modal translation workspace', dialog.IsModal())
                    def find(name):
                        result = wx.FindWindowByName(name, dialog)
                        check('named native control: ' + name, result is not None)
                        return result
                    status = find('Preview, supported languages and job status')
                    find('Source language code; auto detects').ChangeValue('en')
                    find('Target language code').ChangeValue('uk')
                    glossary_input = find('Advanced v3 glossary resource; blank for none')
                    glossary_input.ChangeValue(glossary)
                    selected = find('Selected text; originals remain unchanged'); selected.ChangeValue(original)
                    find('Maximum approved budget in USD; no default price').ChangeValue('1')
                    press(find('Fetch languages'))
                    pump(lambda: not frame.working and 'source: True' in status.GetValue(), 'Language discovery completion')
                    check('language completion retains readable native status focus', wx.Window.FindFocus() is status)
                    press(find('Preview'))
                    pump(lambda: not frame.working and 'No request sent' in status.GetValue(), 'Asynchronous preview completion')
                    check('preview contains exact endpoint and glossary', address in status.GetValue() and glossary in status.GetValue())
                    press(find('Translate / resume'))
                    pump(lambda: not frame.working and '"status": "complete"' in status.GetValue(), 'Translation completion')
                    check('translation completion status focus', wx.Window.FindFocus() is status)
                    check('confirmation binds exact glossary', glossary in approvals[-1] and address in approvals[-1])
                    press(find('Export'))
                    pump(lambda: not frame.working and status.GetValue().startswith('Saved new files in:'), 'Derivative export')
                    output, = root.glob('Translation-*')
                    reading = ReadingDocument.load(output / 'translation.reading.json')
                    receipt = json.loads((output / 'translation-provenance.json').read_text(encoding='utf-8'))
                    check('retained original and non-authoritative derivative', reading.source_text == original and not reading.source_span(0, len(reading.text)).exact)
                    check('actual exported hashes', all(sha256((output / name).read_bytes()).hexdigest() == checksum for name, checksum in receipt['files'].items()))
                    check('verified glossary provenance', receipt['chunks'][0]['glossary_config']['glossary'] == glossary)
                    check('independent decoded translation receipt is retained',
                          receipt['chunks'][0]['decoded_receipt_integrity'] == 'decoded-ledger-bound')
                    check('original selected text unchanged', selected.GetValue() == original)
                    before = len(calls)
                    press(find('Preview'))
                    pump(lambda: not frame.working and 'No request sent' in status.GetValue(), 'Second asynchronous preview')
                    glossary_input.ChangeValue(glossary + '-changed-after-review')
                    press(find('Translate / resume'))
                    pump(lambda: 'Preview this exact' in status.GetValue(), 'Changed glossary rejection')
                    check('changed glossary requires fresh review without transfer',
                          len(calls) == before and not frame.working)
                    glossary_input.ChangeValue(glossary)
                    layout_receipts = []
                    for mode, kind in ((4, 'docx'), (5, 'epub')):
                        fixture = structured[kind]
                        scope = find('Translation input scope')
                        scope.SetSelection(mode)
                        dialog.on_scope_changed()
                        find('Source title filter for library selection').ChangeValue(fixture['title'])
                        before = len(calls)
                        press(find('Choose library sources'))
                        pump(lambda: not frame.working and '"format": "' + kind + '"' in status.GetValue(),
                             'Retained ' + kind.upper() + ' structure capture')
                        check('native ' + kind + ' capture is read-only and sends nothing',
                              len(calls) == before and not selected.IsEditable()
                              and fixture['path'].read_bytes() == fixture['raw'])
                        press(find('Preview'))
                        pump(lambda: not frame.working and 'No request sent' in status.GetValue(),
                             kind.upper() + ' structure review')
                        check(kind + ' preview binds explicit structure profile without transfer',
                              len(calls) == before and ('carr-docx-body-layout/1' if kind == 'docx'
                              else 'carr-epub-xhtml-layout/1') in status.GetValue())
                        press(find('Translate / resume'))
                        pump(lambda: not frame.working and '"status": "complete"' in status.GetValue(),
                             kind.upper() + ' source-bound translation')
                        jobs = [job for job in dialog.translation_service.list()
                                if job.get('source_partition', {}).get('profile') ==
                                ('carr-docx-body-layout/1' if kind == 'docx' else 'carr-epub-xhtml-layout/1')]
                        check(kind + ' accepted chunks belong to one format-bound complete job',
                              len(jobs) == 1 and jobs[0]['status'] == 'complete'
                              and len(jobs[0]['receipts']) == len(jobs[0]['chunks']))
                        target = root / ('NEW translated native.' + kind)
                        export_path[0] = target
                        before = len(calls)
                        press(find('Export DOCX / EPUB'))
                        pump(lambda: not frame.working and status.GetValue().startswith(
                            'Saved and verified new ' + kind.upper()), kind.upper() + ' new-file export')
                        check(kind + ' new-file export is offline and original bytes stay exact',
                              len(calls) == before and target.is_file()
                              and fixture['path'].read_bytes() == fixture['raw'])
                        changed = {'word/document.xml'} if kind == 'docx' else {
                            'OEBPS/content.opf', 'OEBPS/text.xhtml'}
                        with ZipFile(BytesIO(fixture['raw'])) as original_package, ZipFile(target) as translated_package:
                            check(kind + ' unselected ZIP payloads remain exact',
                                  set(original_package.namelist()) == set(translated_package.namelist())
                                  and all(original_package.read(name) == translated_package.read(name)
                                          for name in original_package.namelist() if name not in changed))
                            if kind == 'docx':
                                xml = ET.fromstring(translated_package.read('word/document.xml'))
                                ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                                check('translated DOCX keeps actual table cells and paragraph style',
                                      len(xml.findall('.//w:tc', ns)) == 2
                                      and any(node.get('{' + ns['w'] + '}val') == 'Heading1'
                                              for node in xml.findall('.//w:pStyle', ns))
                                      and 'Похідний переклад' in ''.join(xml.itertext()))
                            else:
                                xml = ET.fromstring(translated_package.read('OEBPS/text.xhtml'))
                                ns = {'x': 'http://www.w3.org/1999/xhtml', 'dc': 'http://purl.org/dc/elements/1.1/'}
                                package_xml = ET.fromstring(translated_package.read('OEBPS/content.opf'))
                                check('translated EPUB keeps exact IDs cells and declared target language',
                                      xml.find('.//x:h1', ns).get('id') == 'heading'
                                      and len(xml.findall('.//x:td', ns)) == 2
                                      and 'uk' in [node.text for node in package_xml.findall('.//dc:language', ns)]
                                      and 'Похідний переклад' in ''.join(xml.itertext()))
                        layout_receipts.append(dict(format=kind, job_id=jobs[0]['id'],
                            source_sha256=sha256(fixture['raw']).hexdigest(),
                            source_partition=jobs[0]['source_partition'],
                            output_sha256=sha256(target.read_bytes()).hexdigest(),
                            output_bytes=target.stat().st_size, provider_calls_during_export=0,
                            quotation_authority=False))
                    report['structured_exports'] = layout_receipts
                    press(find('Close'))
                except Exception:
                    errors.append(traceback.format_exc())
                    if dialog is not None and dialog.IsModal():
                        dialog.EndModal(wx.ID_CANCEL)

            def opened():
                wx.CallLater(200, exercise)
                try:
                    ui.show_translation(frame)
                except Exception:
                    errors.append(traceback.format_exc())
                finally:
                    app.ExitMainLoop()
            wx.CallAfter(opened)
            app.MainLoop()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            providers.HTTPTransport.request, wx.MessageBox, wx.DirDialog = actual_transport, actual_message, actual_directory
            wx.FileDialog, wx.SingleChoiceDialog = actual_file, actual_source_picker
            if reference:
                vault.delete('native_translation', reference)
            if frame is not None:
                frame.Destroy()
    if errors:
        raise RuntimeError('\n'.join(errors))
    report.update(synthetic_rest_calls=len(calls), native_keyboard='PASS',
                  live_google_api='NOT_RUN', billing_and_quality='NOT_RUN',
                  native_approval_and_file_pickers='NOT_RUN', manual_nvda='NOT_RUN',
                  origins=origins, source_override=source is not None,
                  module_sha256={name: sha256(Path(path).read_bytes()).hexdigest()
                                 for name, path in origins.items()})


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--expect-installed', action='store_true')
    args = parser.parse_args()
    report = {'status': 'FAIL', 'phase': 'incomplete', 'checks': [], 'manual_nvda': 'NOT_RUN'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.RLock()
    def checkpoint(phase):
        with lock:
            report['phase'] = phase
            args.output.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n',
                                   encoding='utf-8')
            print('TRANSLATION_V3_PHASE ' + phase, flush=True)
    checkpoint('starting')
    if os.name != 'nt' or not sys.flags.isolated or not sys.dont_write_bytecode:
        raise RuntimeError('Run the native Advanced translation probe with Windows Python -I -B.')
    if args.expect_installed and args.source is not None:
        raise ValueError('Installed acceptance must not inject checkout source.')
    faulthandler.dump_traceback_later(180, repeat=False, exit=True)
    handle = None
    try:
        source = args.source.resolve(strict=True) if args.source is not None else None
        if source is not None:
            sys.path.insert(0, str(source))
            sys.path.insert(0, str(source / 'src'))
        from carr_library.native_runtime import activate_bundled_runtime
        handle = activate_bundled_runtime()
        run_native(source, report, checkpoint)
        report['status'] = 'PASS'
        checkpoint('complete')
        return 0
    except BaseException:
        report['failure'] = traceback.format_exc()[-32000:]
        checkpoint('failed')
        raise
    finally:
        try:
            if handle is not None:
                handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())
