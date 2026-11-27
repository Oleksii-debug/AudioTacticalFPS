"""Installed native ordered translation review with synthetic HTTPS only.

Actual DesktopFrame commands, one executor, source snapshots, provider ledger,
pause/resume, and new ZIP bytes are exercised. Native approval/file picker
answers are explicitly controlled fixture inputs; no paid request or quality
claim is made. The child owns its GUI/process lifetime under a finite watchdog.
"""
import argparse
import faulthandler
from hashlib import sha256
import http.client
import importlib.util
import json
import os
from pathlib import Path
import ssl
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, time
import traceback
from types import SimpleNamespace
from zipfile import ZipFile


def _bindings():
    path = Path(__file__).with_name('windows_publishing_smoke.py')
    spec = importlib.util.spec_from_file_location('_translation_batch_bindings', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _OwnedModalAbort:
    """Unwind one owned modal loop per GUI turn, never its suspended parent."""

    def __init__(self, wx, app, owners, report):
        self.wx, self.app, self.owners, self.report = wx, app, owners, report
        self.active = False
        self.timer = None
        self.reported = set()

    def request(self):
        if not self.active:
            self.active = True
            self.wx.CallAfter(self._advance)

    def stop(self):
        self.active = False
        if self.timer is not None:
            self.timer.Stop()
            self.timer = None

    def _advance(self):
        self.timer = None
        if not self.active:
            return
        try:
            # Owners are innermost first. EndModal() requests a return; its
            # IsModal() remains true until that ShowModal() actually unwinds.
            for owner in self.owners():
                if owner is None:
                    continue
                dialog = owner.dialog
                if dialog and dialog.IsModal():
                    if dialog.IsShown():
                        owner.close()
                    self.timer = self.wx.CallLater(10, self._advance)
                    return
            self.active = False
            self.app.ExitMainLoop()
        except (Exception, KeyboardInterrupt):
            detail = traceback.format_exc()
            if detail not in self.reported:
                self.reported.add(detail)
                self.report(detail)
            # Cleanup refusal remains a failure. Do not force-delete a native
            # owner or report silence; the outer owned process bound applies.
            self.timer = self.wx.CallLater(25, self._advance)


def _nested_modal_failure_acceptance(wx, app, check, select_failure_handler):
    """Real Windows negative control for the probe's former nested-loop hang."""
    events, returned, failures = [], [], []
    owners = []
    abort = _OwnedModalAbort(wx, app, lambda: tuple(reversed(owners)), failures.append)

    def fail(detail):
        failures.append(detail)
        abort.request()

    def owner(label, parent):
        dialog = wx.Dialog(parent, title='Owned EPUB failure control — ' + label)
        native_id = dialog.GetId()
        record = SimpleNamespace(dialog=dialog, loop=None, native_id=native_id)

        def destroyed(event):
            if event.GetId() == native_id:
                events.append(label)
            event.Skip()

        def close():
            if wx.EventLoopBase.GetActive() != record.loop:
                raise AssertionError('Attempted to close a suspended ' + label + ' loop.')
            dialog.EndModal(wx.ID_CANCEL)

        record.close = close
        dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
        owners.append(record)
        return record

    def provoke(inner):
        inner.loop = wx.EventLoopBase.GetActive()
        try:
            raise AssertionError('deliberate owned nested-modal failure')
        except AssertionError:
            fail(traceback.format_exc())

    def open_inner(outer):
        outer.loop = wx.EventLoopBase.GetActive()
        inner = owner('inner', outer.dialog)
        wx.CallAfter(provoke, inner)
        try:
            inner.dialog.ShowModal()
            returned.append('inner')
        finally:
            if inner.dialog:
                inner.dialog.Destroy()

    # wxPython MainLoop returns immediately with no top-level windows. Own the
    # actual outer dialog before starting the loop, then show it on that loop.
    outer = owner('outer', None)

    def open_outer():
        wx.CallAfter(open_inner, outer)
        try:
            outer.dialog.ShowModal()
            returned.append('outer')
        finally:
            if outer.dialog:
                outer.dialog.Destroy()
        app.ExitMainLoop()

    select_failure_handler(fail)
    try:
        wx.CallAfter(open_outer)
        app.MainLoop()
    finally:
        abort.stop()
        select_failure_handler(None)
    check('actual nested-modal failure returns child before parent',
          returned == ['inner', 'outer']
          and len(failures) == 1
          and 'deliberate owned nested-modal failure' in failures[0])
    check('actual failed modal owners receive one native destruction each',
          events.count('inner') == 1 and events.count('outer') == 1
          and all(not item.dialog for item in owners))


def _run_owned_native():
    from carr_library.audio.models import AudioBudget
    from carr_library.audio.process import run_process
    result = run_process(
        [sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--native-child'],
        budget=AudioBudget(timeout=210, max_log_bytes=1024 * 1024),
        owned_tree=True, allow_failure=True)
    sys.stdout.write(result.stdout.decode('utf-8', errors='replace'))
    sys.stdout.flush()
    sys.stderr.write(result.stderr.decode('utf-8', errors='replace'))
    sys.stderr.flush()
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-child', action='store_true', help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Actual Windows wx and Credential Manager are required.'}))
        return 77
    if not arguments.native_child:
        return _run_owned_native()
    faulthandler.dump_traceback_later(180, exit=True)
    from carr_library.native_runtime import activate_bundled_runtime
    runtime = activate_bundled_runtime()
    try:
        return _run()
    finally:
        try:
            if runtime is not None:
                runtime.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def _run():
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library import application, backup_translation_batches
    from carr_library.assistant import profiles, providers, translation, translation_batch, translation_source
    from carr_library.assistant.credentials import WindowsCredentialVault
    from carr_library.research import export_file
    from carr_library.speech import reading
    from carr_library.ui import desktop_app, translation_dialog, translation_batch_dialog

    modules = (application, profiles, providers, translation, translation_batch, translation_source,
               export_file, reading, desktop_app, translation_dialog, translation_batch_dialog,
               backup_translation_batches)
    bindings = _bindings()
    origins = bindings.verify_installed_sources(*modules)
    checks, errors, calls, approvals = [], [], [], []
    evidence, held, windows, releases = {}, [None], [], []
    deadline = monotonic() + 150
    frame = None
    abort = None
    failure_handler = None
    parent = 'projects/123456789/locations/us-central1'
    endpoint = 'https://translation.googleapis.com/v3/' + parent
    glossary = parent + '/glossaries/reviewed-terms'
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    def release_key():
        value, held[0] = held[0], None
        if value is not None and not simulator.KeyUp(*value):
            raise AssertionError('Native key release failed.')

    def report_failure(detail):
        errors.append(detail)
        print('FAIL: translation batch native\n' + detail, file=sys.stderr, flush=True)

    def failed(detail=None):
        report_failure(detail or traceback.format_exc())
        for event in releases:
            event.set()
        try:
            release_key()
        except Exception:
            errors.append(traceback.format_exc())
        abort.request()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            detail = traceback.format_exc()
            if failure_handler is not None:
                failure_handler(detail)
            else:
                failed(detail)
            return True

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    check('actual native Windows controls', wx.Platform == '__WXMSW__')

    def owners():
        found = []
        for window in wx.GetTopLevelWindows():
            if not window or not isinstance(window, wx.Dialog) or not window.IsModal():
                continue
            current, depth, owned = window, 0, False
            while current:
                if frame is not None and current == frame:
                    owned = True
                    break
                current = current.GetParent()
                depth += 1
            if owned:
                found.append((depth, SimpleNamespace(
                    dialog=window, close=lambda peer=window: peer.EndModal(wx.ID_CANCEL))))
        return tuple(row[1] for row in sorted(found, key=lambda row: row[0], reverse=True))

    abort = _OwnedModalAbort(wx, gui, owners, report_failure)
    def select_failure_handler(value):
        nonlocal failure_handler
        failure_handler = value
    _nested_modal_failure_acceptance(wx, gui, check, select_failure_handler)

    def later(callback, delay=25):
        def invoke():
            if errors:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Native translation batch acceptance exceeded its deadline.')
                callback()
            except (Exception, KeyboardInterrupt):
                failed()
        wx.CallLater(delay, invoke)

    def until(predicate, completed, *, timeout=20):
        expires = monotonic() + timeout
        def poll():
            if predicate():
                completed()
            elif monotonic() >= expires:
                raise TimeoutError('Native translation batch stage did not complete.')
            else:
                later(poll)
        later(poll)

    def foreground(control):
        owner = wx.GetTopLevelParent(control)
        handle = user32.GetForegroundWindow()
        process = wintypes.DWORD()
        if handle:
            user32.GetWindowThreadProcessId(handle, ctypes.byref(process))
        ready = (process.value == os.getpid() and int(handle or 0) == int(owner.GetHandle())
                 and wx.Window.FindFocus() is control)
        if not ready:
            owner.Raise()
            user32.SetForegroundWindow(owner.GetHandle())
            control.SetFocus()
        return ready

    def key(control, code, completed, modifiers=wx.MOD_NONE):
        def down():
            check('actual native key has foreground and focus', foreground(control))
            held[0] = code, modifiers
            check('native key down', simulator.KeyDown(code, modifiers))
            def up():
                release_key()
                later(completed)
            later(up, 40)
        until(lambda: held[0] is None and foreground(control), down, timeout=5)

    def press(control, completed):
        key(control, wx.WXK_SPACE, completed)

    def modal(name):
        return next((value for value in wx.GetTopLevelWindows() if value
            and isinstance(value, wx.Dialog) and value.GetName() == name and value.IsShown()), None)

    def find(view, name):
        value = wx.FindWindowByName(name, view)
        check('named native control: ' + name, value is not None)
        return value

    def data_files():
        directory = library.root / 'state' / 'translation'
        return {str(path.relative_to(directory)): path.read_bytes()
            for part in ('jobs', 'batches') for path in (directory / part).glob('*.json')}

    class SyntheticHTTPS:
        def __init__(self, host, port, **kwargs):
            check('synthetic transport retains the exact official authority',
                  host == 'translation.googleapis.com' and port in (None, 443)
                  and isinstance(kwargs.get('context'), ssl.SSLContext))
            self.position = 0
        def request(self, method, route, body, headers):
            check('synthetic OAuth travels only in the bearer header',
                  headers.get('Authorization') == 'Bearer SYNTHETIC_NATIVE_BATCH_TOKEN'
                  and 'X-goog-api-key' not in headers
                  and 'SYNTHETIC_NATIVE_BATCH_TOKEN' not in route)
            calls.append({'method': method, 'route': route, 'body': body})
            if method == 'GET':
                value = {'languages': [
                    {'languageCode': code, 'displayName': code,
                     'supportSource': True, 'supportTarget': True} for code in ('en', 'uk')]}
            else:
                payload = json.loads(body)
                row = {'translatedText': 'Похідний текст &amp; ' + payload['contents'][0],
                       'model': payload['model']}
                if 'sourceLanguageCode' not in payload:
                    row['detectedLanguageCode'] = 'en'
                value = {'translations': [row]}
                if 'glossaryConfig' in payload:
                    value['glossaryTranslations'] = [{**row, 'glossaryConfig': payload['glossaryConfig']}]
            self.raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
        def getresponse(self):
            self.status = 200
            return self
        def getheader(self, name, default=None):
            return {'Content-Type': 'application/json', 'Content-Length': str(len(self.raw))}.get(name, default)
        def read(self, maximum):
            part = self.raw[self.position:self.position + maximum]
            self.position += len(part)
            return part
        def close(self):
            pass

    actual_connection = http.client.HTTPSConnection
    actual_message, actual_file = wx.MessageDialog, wx.FileDialog
    actual_resume = translation.TranslationService.resume
    vault, credential_ref = WindowsCredentialVault(), None
    with TemporaryDirectory(prefix='resonance-translation-batch-native-') as temporary:
        root = Path(temporary)
        library = application.LibraryApp(root / 'private library')
        workspace = library.create_workspace('Native reviewed translation batch')
        originals = [
            library.add_text(workspace, 'First retained document', 'Original first source. Україна.\n'),
            library.add_text(workspace, 'Second retained document', 'Other retained source. Україна.\r\n')]
        original_bytes = {item['asset']: library.assets.resolve(item['asset']).read_bytes() for item in originals}
        profile = profiles.ProviderProfile('native_batch_translation', 'Synthetic native batch account',
            'google-translation-v3', endpoint, 'general/nmt',
            capabilities=('translation.text', 'translation.languages'), enabled=True)
        service = translation.TranslationService(library.root / 'state' / 'translation', library=library,
            gateway=providers.Gateway(library.root / 'state' / 'translation' / 'provider',
                                      vault=vault, mode='APPROVED_REMOTE'))
        observer = translation_batch.TranslationBatchService(service)
        output = root / 'explicit native batch.zip'
        pause_ready, pause_release = Event(), Event()
        releases.append(pause_release)
        pause_once = [False]
        selection = {'view': None}

        # These two OS choosers supply explicit synthetic answers only. Every
        # batch action, multi-book native selection and actual job is unchanged.
        class ExplicitApproval:
            def __init__(self, parent, message, caption, style):
                self.live = True
                approvals.append({'message': message, 'caption': caption})
            def __bool__(self):
                return self.live
            def ShowModal(self):
                return wx.ID_YES
            def Destroy(self):
                self.live = False
        class ExplicitDestination:
            def __init__(self, *args, **kwargs):
                self.live = True
            def __bool__(self):
                return self.live
            def ShowModal(self):
                return wx.ID_OK
            def GetPath(self):
                return str(output)
            def Destroy(self):
                self.live = False

        def hold_committed_child(self, identity, *args, **kwargs):
            result = actual_resume(self, identity, *args, **kwargs)
            if result['status'] == 'complete' and not pause_once[0]:
                pause_once[0] = True
                evidence['first_child'] = identity
                pause_ready.set()
                if not pause_release.wait(20):
                    raise TimeoutError('The actual completed translation was not released after native Pause.')
            return result

        def source_unchanged():
            return all(library.assets.resolve(identity).read_bytes() == raw
                       for identity, raw in original_bytes.items())

        def start():
            check('actual menu is in the sole current keyboard manager',
                  'tools.translation_batch' in frame.keyboard_commands.bindings
                  and frame.keyboard_settings.manager is frame.keyboard_commands)
            check('actual selected workspace is pinned', frame.active == (workspace, None))
            check('opening the host creates no translation job', observer.list() == ())
            frame.keyboard_commands.apply_overrides({
                **frame.keyboard_commands.overrides,
                'tools.translation_batch': 'Ctrl+Alt+Shift+F10'})
            frame.keyboard_commands.guard_native()
            key(frame.choice, wx.WXK_F10, lambda: until(
                lambda: modal('Translation document batch') is not None, opened),
                wx.MOD_CONTROL | wx.MOD_ALT | wx.MOD_SHIFT)

        def opened():
            view = selection['view'] = modal('Translation document batch')
            check('batch opens on the genuine native modal loop', view.IsModal())
            find(view, 'Source language code; auto detects').ChangeValue('en')
            find(view, 'Target language code').ChangeValue('uk')
            find(view, 'Advanced glossary resource; blank for none').ChangeValue(glossary)
            find(view, 'Per-document USD reservation; explicit, no default').ChangeValue('1')
            press(find(view, 'Add library books...'), lambda: until(
                lambda: any(window and isinstance(window, wx.MultiChoiceDialog)
                            and window.GetTitle() == 'Translation source books'
                            for window in wx.GetTopLevelWindows()), select_books))

        def select_books():
            chooser = next(window for window in wx.GetTopLevelWindows()
                           if window and isinstance(window, wx.MultiChoiceDialog)
                           and window.GetTitle() == 'Translation source books')
            check('source multi-selection is an actual owned modal', chooser.IsModal())
            chooser.SetSelections([0, 1])
            check('two explicit native source selections', tuple(chooser.GetSelections()) == (0, 1))
            press(chooser.FindWindow(wx.ID_OK), lambda: until(
                lambda: not frame.working
                and find(selection['view'], 'Ordered translation documents').GetCount() == 2, captured))

        def captured():
            view = selection['view']
            check('capturing native source rows sends no text', not any(row['method'] == 'POST' for row in calls))
            check('exact two source rows are displayed', find(view, 'Ordered translation documents').GetCount() == 2)
            press(find(view, 'Move document down'), lambda: press(
                find(view, 'Review entire translation batch'), lambda: until(
                    lambda: not frame.working
                    and 'REVIEW ONLY' in find(view, 'Translation batch review and existing checkpoints').GetValue(),
                    reviewed)))

        def reviewed():
            view = selection['view']
            status = find(view, 'Translation batch review and existing checkpoints')
            text = status.GetValue()
            check('aggregate native review binds source, glossary, budget and format',
                  glossary in text and endpoint in text and '"budget": "2"' in text
                  and 'REVIEW ONLY' in text and 'source_anchor' in text)
            check('native review focus is retained', wx.Window.FindFocus() is status)
            check('review creates no approval or queued child', observer.list() == ())
            press(find(view, 'Prepare approved batch'), lambda: until(
                lambda: not frame.working and len(observer.list()) == 1
                and 'PREPARED' in status.GetValue(), prepared))

        def prepared():
            view = selection['view']
            snapshot, = observer.list()
            identity = evidence['batch_id'] = snapshot['id']
            manifest = observer._load(identity)
            review = observer.get_review(identity)
            check('exact native review has two immutable child identities',
                  len(manifest['members']) == 2 and len({row['job']['id'] for row in manifest['members']}) == 2
                  and all(row['job']['kind'] == 'translation/2' for row in manifest['members']))
            check('actual preparation saved approvals but sent no text',
                  not any(row['method'] == 'POST' for row in calls) and snapshot['completed'] == 0)
            check('controlled approval binds the exact displayed immutable review',
                  approvals and review['review_sha256'] in approvals[-1]['message']
                  and 'Total reservation: 2 USD' in approvals[-1]['message'])
            evidence['manifest_sha256'] = sha256(observer.jobs.path(identity).read_bytes()).hexdigest()
            translation.TranslationService.resume = hold_committed_child
            press(find(view, 'Run or resume batch'), lambda: until(pause_ready.is_set, pause_now))

        def pause_now():
            view = selection['view']
            first = service._load(evidence['first_child'])
            check('native Pause is exercised after an actual accepted child receipt',
                  first['status'] == 'complete' and len(first['receipts']) == len(first['chunks']) == 1)
            evidence['first_receipt_bytes'] = service.jobs.path(first['id']).read_bytes()
            press(find(view, 'Pause batch'), release_paused)

        def release_paused():
            pause_release.set()
            until(lambda: not frame.working and '"status": "paused"' in
                find(selection['view'], 'Translation batch review and existing checkpoints').GetValue(), paused)

        def paused():
            view = selection['view']
            identity = evidence['batch_id']
            snapshot = observer.get(identity)
            check('native Pause leaves exactly one paid result and no next transfer',
                  snapshot['completed'] == 1 and len([row for row in calls if row['method'] == 'POST']) == 1)
            translation.TranslationService.resume = actual_resume
            press(find(view, 'Run or resume batch'), lambda: until(
                lambda: not frame.working and observer.get(identity)['status'] == 'complete', complete))

        def complete():
            view = selection['view']
            identity = evidence['batch_id']
            check('resume reuses the actual first accepted receipt',
                  service.jobs.path(evidence['first_child']).read_bytes() == evidence['first_receipt_bytes']
                  and len([row for row in calls if row['method'] == 'POST']) == 2)
            check('review and retained sources remain immutable',
                  sha256(observer.jobs.path(identity).read_bytes()).hexdigest() == evidence['manifest_sha256']
                  and source_unchanged())
            press(find(view, 'Export completed ZIP...'), lambda: until(
                lambda: not frame.working and output.is_file()
                and 'Saved translation package:' in
                    find(view, 'Translation batch review and existing checkpoints').GetValue(), exported))

        def exported():
            with ZipFile(output) as archive:
                check('actual new ZIP contains every independent derivative and receipt', len(archive.namelist()) == 11)
                report = json.loads(archive.read('batch-provenance.json'))
                check('ZIP provenance keeps derivative authority explicit',
                      report['batch_id'] == evidence['batch_id'] and report['quotation_authority'] is False
                      and report['source_is_immutable'] is True and len(report['items']) == 2)
                for row in report['items']:
                    for name, checksum in row['files'].items():
                        check('actual ZIP byte hash: ' + row['directory'] + '/' + name,
                              sha256(archive.read(row['directory'] + '/' + name)).hexdigest() == checksum)
                    document = reading.ReadingDocument.from_dict(json.loads(
                        archive.read(row['directory'] + '/translation.reading.json')))
                    check('retained source anchor remains derivative-only',
                          document.source_anchor is not None
                          and not document.source_span(0, len(document.text)).exact)
            evidence['output_sha256'] = sha256(output.read_bytes()).hexdigest()
            evidence.pop('first_receipt_bytes')
            press(find(selection['view'], 'Close batch'), lambda: until(
                lambda: modal('Translation document batch') is None,
                lambda: lifetime('direct', lambda: lifetime('parent', finish))))

        def lifetime(ending, continuation):
            """Hold the real review result on the actual Desktop executor."""
            ready, released = Event(), Event()
            releases.append(released)
            delivered, destroyed, forbidden = [], [], []
            original_records = data_files()
            call_count = len(calls)
            rows = []
            for item in originals:
                selected = translation_source.capture_source(library, workspace, item['edition'])
                rows.append({'profile': profile.to_dict(), 'source': 'en', 'target': 'uk',
                    'budget': '1', **{name: selected[name] for name in
                    ('text', 'title', 'source_anchor', 'source_metadata', 'source_topic')}})

            class Owner(wx.Frame):
                def __init__(self):
                    super().__init__(frame, title='Translation batch native lifetime ' + ending)
                    self.library, self.active, self._closing = library, frame.active, False
                @property
                def working(self):
                    return frame.working
                @property
                def cancel_event(self):
                    return frame.cancel_event
                def run_job(self, title, operation, done=None, failed=None, **kwargs):
                    def actual_result():
                        result = operation()
                        ready.set()
                        if not released.wait(20):
                            raise TimeoutError('Actual review result was not released after native deletion.')
                        return result
                    def accepted(value):
                        delivered.append(('done', value['review_sha256']))
                        if done is not None:
                            done(value)
                    def rejected(error):
                        delivered.append(('failed', str(error)))
                        if failed is not None:
                            failed(error)
                    return frame.run_job(title, actual_result, done=accepted, failed=rejected, **kwargs)

            owner = Owner()
            windows.append(owner)
            owner.Show()
            view, close, snapshot = translation_batch_dialog.create_translation_batch_dialog(owner, items=rows)
            windows.append(view)
            native_id = view.GetId()
            def observed(event):
                if event.GetId() == native_id:
                    destroyed.append(event.GetId())
                event.Skip()
            view.Bind(wx.EVT_WINDOW_DESTROY, observed)
            controls = [find(view, name) for name in (
                'Translation batch title', 'Account for added or selected documents',
                'Ordered translation documents', 'Translation batch review and existing checkpoints',
                'Review entire translation batch', 'Prepare approved batch')]
            view.Show()

            def destroy():
                check('actual immutable review is held before native result delivery',
                      ready.is_set() and frame.working and not delivered)
                def deny(label):
                    def rejected(*args, **kwargs):
                        forbidden.append(label)
                        raise AssertionError('Deleted translation batch control accessed: ' + label)
                    return rejected
                for control in controls:
                    name = control.GetName()
                    for method in ('ChangeValue', 'SetValue', 'SetSelection', 'SetItems', 'Enable', 'SetFocus'):
                        if hasattr(control, method):
                            setattr(control, method, deny(name + '.' + method))
                if ending == 'direct':
                    view.Destroy()
                else:
                    def unexpected_override():
                        raise AssertionError('Native parent deletion must bypass the Python Destroy override.')
                    view.Destroy = unexpected_override
                    owner.DestroyChildren()
                until(lambda: destroyed == [native_id] and not view, deleted)

            def deleted():
                check('owner deletion does not cancel unrelated host work', not frame.cancel_event.is_set())
                close()  # Actual bound cleanup after deletion must remain inert.
                released.set()
                until(lambda: bool(delivered) and not frame.working, settled)

            def settled():
                check('actual late review is delivered only after the native owner is gone',
                      len(delivered) == 1 and delivered[0][0] == 'done' and not forbidden)
                check('closed review sends no provider request or new approval',
                      len(calls) == call_count and data_files() == original_records and source_unchanged())
                evidence.setdefault('native_lifetime', []).append({
                    'path': ending, 'native_id': native_id, 'destroy_events': len(destroyed),
                    'actual_late_result': delivered[0][0], 'forbidden_control_accesses': len(forbidden)})
                if owner:
                    owner.Destroy()
                until(lambda: not owner, continuation)

            press(find(view, 'Review entire translation batch'), lambda: until(ready.is_set, destroy))

        def finish():
            check('selected installed product source remains exact after all native work',
                  bindings.verify_installed_sources(*modules) == origins)
            check('all retained source bytes survive the complete native scenario', source_unchanged())
            check('no extra request follows completion or native deletion',
                  len([row for row in calls if row['method'] == 'POST']) == 2)
            evidence['complete'] = True
            gui.ExitMainLoop()

        try:
            profile = profiles.save_google_token(
                profiles.ProfileStore(library.root / 'state' / 'providers'), vault, profile,
                'SYNTHETIC_NATIVE_BATCH_TOKEN', time() + 1800)
            credential_ref = profile.credential_ref
            http.client.HTTPSConnection = SyntheticHTTPS
            wx.MessageDialog, wx.FileDialog = ExplicitApproval, ExplicitDestination
            approval = providers.Authorization('batch_discovery', 'batch_discovery', ((profile.id, endpoint),),
                ('translation.languages',), ('user-instruction',), '0', 1, time() + 300)
            service.refresh_languages(profile, authorization=approval)
            frame = desktop_app.DesktopFrame(library)
            gui.SetTopWindow(frame)
            frame.Show()
            later(lambda: until(lambda: not frame.working, start))
            gui.MainLoop()
            if errors:
                raise AssertionError('\n'.join(errors))
            check('normal native MainLoop completed the entire ordered batch scenario',
                  evidence.get('complete') is True)
        finally:
            abort.stop()
            for event in releases:
                event.set()
            release_key()
            translation.TranslationService.resume = actual_resume
            http.client.HTTPSConnection = actual_connection
            wx.MessageDialog, wx.FileDialog = actual_message, actual_file
            for window in reversed(windows):
                if window:
                    if isinstance(window, wx.Dialog):
                        wx.Dialog.Destroy(window)
                    else:
                        window.Destroy()
            if frame is not None:
                frame._closing = True
                frame.cancel_event.set()
                frame.executor.shutdown(wait=True, cancel_futures=True)
                panel = getattr(frame, 'progress_panel', None)
                if panel is not None:
                    panel.close()
                if frame:
                    frame.Destroy()
                gui.Yield()
            if credential_ref:
                vault.delete('native_batch_translation', credential_ref)

    print(json.dumps({'status': 'PASS', 'checks': len(checks), 'source_identity': origins,
        'evidence': evidence, 'native_menu_order_review_pause_resume': 'PASS',
        'actual_gateway_ledger_and_zip': 'PASS', 'native_pending_owner_deletion': 'PASS',
        'synthetic_http_requests': len(calls), 'live_google_api': 'NOT_RUN',
        'paid_translation_and_linguistic_quality': 'NOT_RUN',
        'native_approval_and_file_pickers': 'NOT_RUN', 'physical_keyboard': 'NOT_RUN',
        'nvda': 'NOT_RUN'}, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
