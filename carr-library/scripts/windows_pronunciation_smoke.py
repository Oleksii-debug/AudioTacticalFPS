"""Installed Windows pronunciation review probe; synthetic source, no voice/NVDA claim."""
import argparse
import faulthandler
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import sys
import traceback


def _acceptance_bindings():
    # Use the checked-in gate helper by explicit sibling path, never product sys.path.
    import importlib.util
    path = Path(__file__).with_name('windows_publishing_smoke.py')
    spec = importlib.util.spec_from_file_location('_reader_acceptance_bindings', path)
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
    """Bound this probe with the application's existing native process owner."""
    from carr_library.audio.models import AudioBudget
    from carr_library.audio.process import run_process
    result = run_process(
        [sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--native-child'],
        budget=AudioBudget(timeout=210, max_log_bytes=1024 * 1024),
        owned_tree=True, allow_failure=True)
    sys.stdout.write(result.stdout.decode('utf-8', errors='replace')); sys.stdout.flush()
    sys.stderr.write(result.stderr.decode('utf-8', errors='replace')); sys.stderr.flush()
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-child', action='store_true', help=argparse.SUPPRESS)
    options = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.',
                          'nvda': 'NOT_RUN', 'voice_quality': 'NOT_RUN'}))
        return 77
    if not options.native_child:
        return _run_owned_native()
    faulthandler.dump_traceback_later(180, exit=True)
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    try:
        return _run_native()
    finally:
        try:
            if native_handle is not None:
                native_handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def _run_native():
    import ctypes
    from ctypes import wintypes
    import wx
    import carr_library
    from carr_library.application import LibraryApp
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech import Voice, VoiceRegistry
    from carr_library.speech.reading import import_dictionary
    from carr_library.speech.session import ReaderDraftStore
    from carr_library.speech.source import read_source_range
    from carr_library.ui.speech_dialog import show_speech_reader
    import carr_library.speech.pronunciation_formats as formats_module
    import carr_library.speech.reading as reading_module
    import carr_library.ui.pronunciation_dialog as pronunciation_module
    import carr_library.ui.speech_dialog as reader_module
    import carr_library.runtime_settings as runtime_settings

    checks, errors, observed = [], [], {}
    failure_handler = None
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    origin = Path(carr_library.__file__).resolve()
    source_identity = _acceptance_bindings().verify_installed_sources(
        reading_module, formats_module, pronunciation_module, reader_module, runtime_settings)
    check('current installed package and reader bytes', bool(source_identity))

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            detail = traceback.format_exc()
            if failure_handler is not None:
                failure_handler(detail)
            else:
                fail(detail)
            return True

    gui = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    held = [None]
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def release():
        key = held[0]
        held[0] = None
        if key is not None:
            check('native key and modifiers released', simulator.KeyUp(*key))

    def owned_modals():
        owners = []
        dialog = observed.get('dialog')
        if dialog is not None:
            owners.append(SimpleNamespace(dialog=dialog, close=dialog.finish_close))
        reader, view = observed.get('reader'), observed.get('reader_view')
        if reader is not None and view is not None:
            owners.append(SimpleNamespace(dialog=reader, close=view['close']))
        return tuple(owners)

    def report_failure(detail):
        errors.append(detail)
        print('FAIL: native pronunciation\n' + detail, file=sys.stderr, flush=True)

    abort = _OwnedModalAbort(wx, gui, owned_modals, report_failure)

    def fail(detail=None):
        report_failure(detail or traceback.format_exc())
        try:
            release()
        except (Exception, KeyboardInterrupt):
            report_failure(traceback.format_exc())
        abort.request()

    def select_failure_handler(value):
        nonlocal failure_handler
        failure_handler = value

    _nested_modal_failure_acceptance(wx, gui, check, select_failure_handler)

    def later(callback, delay=30):
        def invoke():
            if errors:
                return
            try:
                callback()
            except (Exception, KeyboardInterrupt):
                fail(traceback.format_exc())
        wx.CallLater(delay, invoke)

    def until(predicate, complete, *, timeout=30):
        deadline = monotonic() + timeout
        def poll():
            if predicate():
                complete()
            elif monotonic() >= deadline:
                raise TimeoutError('Native pronunciation operation did not finish.')
            else:
                later(poll)
        later(poll)

    def foreground(control):
        top = wx.GetTopLevelParent(control)
        handle = user32.GetForegroundWindow()
        owner = wintypes.DWORD()
        if handle:
            user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        ready = (owner.value == os.getpid() and int(handle or 0) == int(top.GetHandle())
                 and wx.Window.FindFocus() is control)
        if not ready:
            top.Raise()
            user32.SetForegroundWindow(top.GetHandle())
            control.SetFocus()
        return ready

    def key(control, code, complete, modifiers=wx.MOD_NONE):
        name = control.GetName()
        def down():
            check('target owns foreground and native focus: ' + name, foreground(control))
            held[0] = (code, modifiers)
            try:
                check('native key down: ' + name, simulator.KeyDown(code, modifiers))
            except BaseException:
                release()
                raise
            def up():
                release()
                later(complete)
            later(up, 40)
        until(lambda: held[0] is None and foreground(control), down, timeout=5)

    def press(control, complete):
        key(control, wx.WXK_SPACE, complete)

    def children(window):
        for child in window.GetChildren():
            yield child
            yield from children(child)

    def named(window, name):
        return next(child for child in children(window) if child.GetName() == name)

    def top_with_title(prefix):
        return next((window for window in wx.GetTopLevelWindows()
                     if isinstance(window, wx.Dialog) and window.GetTitle().startswith(prefix)), None)

    class EnumerationOnly:
        name = 'Synthetic pronunciation probe enumeration'
        def voices(self):
            return (Voice('fixture:pronunciation', 'Synthetic enumeration only', 'fixture', 'en'),)
        def synthesize(self, *args, **kwargs):
            raise AssertionError('This probe must not synthesize or qualify a voice.')
        def close(self):
            observed['provider_closed'] = True

    with TemporaryDirectory(prefix='resonance-native-pronunciation-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Synthetic pronunciation acceptance')
        original = '😀 Retained source.\r\nDr. First speaks. Dr. Second listens.'
        imported = library.add_text(workspace, 'Synthetic source book', original)
        desk = ReadingDesk(library, workspace, imported['edition'])
        source = read_source_range(desk, 1, 1)
        original_asset = library.assets.resolve(imported['asset'])
        asset_bytes = original_asset.read_bytes()
        output = root / 'reviewed-pronunciations.json'
        foreign_rows = (
            ('dic', b'Dr.=Doctor\n'),
            ('rex', br'Dr\.=Doctor' + b'\n'),
            ('bxd', ('\ufeffVersion=1\r\nDelimiter=\x03\r\n'
                     'Authored native fixture\x03Resonance\x03Synthetic literal rule\r\n'
                     + '\x03'.join(['A' * 32, '18', '1', '1', '0', '0', '0', 'Dr.', 'Doctor', ''])
                     + '\r\n').encode('utf-8')),
        )
        foreign = []
        for kind, raw in foreign_rows:
            path = root / ('authored.' + kind)
            path.write_bytes(raw)
            foreign.append((kind, path, raw))
        observed['foreign'] = 0
        frame = wx.Frame(None, title='Resonance pronunciation native acceptance', size=(600, 350))
        frame.library = library
        frame.report = lambda message: observed.setdefault('reader_notices', []).append(str(message))
        original_registry = runtime_settings.voice_registry
        original_picker, original_message = wx.FileDialog, wx.MessageBox
        picks = []
        class SelectedFixturePath:
            def __init__(self, *args, **kwargs): pass
            def ShowModal(self): return wx.ID_OK
            def GetPath(self): return str(picks.pop(0))
            def Destroy(self): pass
        runtime_settings.voice_registry = lambda *args, **kwargs: VoiceRegistry((EnumerationOnly(),))
        wx.FileDialog = SelectedFixturePath
        wx.MessageBox = lambda *args, **kwargs: wx.NO

        def loaded():
            reader = top_with_title('Voice reader and reading copy')
            if reader is None:
                return False
            button = named(reader, 'Pronunciation rules')
            view = observed.get('reader_view')
            return bool(view is not None and button.IsEnabled() and not view['live'].core.busy
                        and view['document']().source_text == source.text)

        def open_review():
            reader = top_with_title('Voice reader and reading copy')
            editor = named(reader, 'Reading copy (editable; Ctrl+S saves)')
            initial = editor.GetValue()
            matches = [position for position in range(editor.GetLastPosition() - 2)
                       if editor.GetRange(position, position + 3) == 'Dr.']
            check('native Unicode/line-ending selection has two matches', len(matches) == 2)
            start = matches[-1]
            editor.SetSelection(start, start + 3)
            observed.update(reader=reader, editor=editor, initial=initial)
            key(editor, ord('N'), lambda: until(
                lambda: top_with_title('Pronunciation rules') is not None, dictionary_ready), wx.MOD_ALT)

        def dictionary_ready():
            dialog = top_with_title('Pronunciation rules')
            observed['dialog'] = dialog
            until(lambda: dialog.loaded and not dialog.busy, add_rule)

        def add_rule():
            dialog = observed['dialog']
            check('native selected-text scope', dialog.scope.GetSelection() == 1)
            check('named preview and review controls', all(control.GetName() for control in
                  (dialog.list, dialog.pattern, dialog.replacement, dialog.before, dialog.after, dialog.status)))
            dialog.pattern.ChangeValue('Dr.')
            dialog.replacement.ChangeValue('Doctor')
            press(dialog.buttons['add'], added)

        def added():
            dialog = observed['dialog']
            check('native rule edit did not apply automatically', len(dialog.rules) == 1
                  and observed['editor'].GetValue() == observed['initial'])
            press(dialog.buttons['preview'], lambda: until(lambda: not dialog.busy, reviewed))

        def reviewed():
            dialog = observed['dialog']
            check('native exact before/after preview', dialog.before.GetValue() == 'Dr.'
                  and dialog.after.GetValue() == 'Doctor' and dialog.buttons['apply'].IsEnabled())
            check('preview did not change the reading copy', observed['editor'].GetValue() == observed['initial'])
            press(dialog.buttons['save'], lambda: until(lambda: not dialog.busy, saved))

        def saved():
            dialog = observed['dialog']
            rules, token = dialog.store.load()
            check('explicit library dictionary save', rules == dialog.rules and token == dialog.saved_token)
            picks.append(output)
            press(dialog.buttons['export'], lambda: until(lambda: not dialog.busy, exported))

        def exported():
            dialog = observed['dialog']
            check('new dictionary export uses the canonical format', import_dictionary(output) == dialog.rules)
            observed['exported'] = output.read_bytes()
            picks.append(output)
            press(dialog.buttons['export'], lambda: until(lambda: not dialog.busy, no_clobber))

        def no_clobber():
            dialog = observed['dialog']
            check('existing export is preserved', output.read_bytes() == observed['exported']
                  and 'exists' in dialog.status.GetValue())
            press(dialog.buttons['remove'], removed)

        def removed():
            dialog = observed['dialog']
            check('remove invalidates preview', not dialog.rules and dialog.preview is None)
            picks.append(output)
            press(dialog.buttons['import'], lambda: until(lambda: not dialog.busy, imported_rules))

        def imported_rules():
            dialog = observed['dialog']
            check('import is review-only', len(dialog.rules) == 1 and dialog.preview is None
                  and observed['editor'].GetValue() == observed['initial'])
            press(dialog.buttons['preview'], lambda: until(lambda: not dialog.busy,
                  lambda: press(dialog.buttons['apply'], applied)))

        def applied():
            dialog = observed['dialog']
            initial = observed['initial']
            start = initial.rindex('Dr.')
            expected = initial[:start] + 'Doctor' + initial[start + 3:]
            check('native apply changes only the selected reading range',
                  observed['editor'].GetValue() == expected)
            check('retained source bytes remain unchanged', original_asset.read_bytes() == asset_bytes)
            press(dialog.buttons['undo'], undone)

        def undone():
            dialog = observed['dialog']
            check('native undo restores the reading copy', observed['editor'].GetValue() == observed['initial'])
            check('undo leaves dictionary rules saved', dialog.store.load()[0] == dialog.rules)
            observed['saved_dictionary'] = dialog.store.path.read_bytes()
            import_foreign()

        def import_foreign():
            dialog = observed['dialog']
            if observed['foreign'] == len(foreign):
                press(dialog.buttons['close'], close_reader)
                return
            kind, path, raw = foreign[observed['foreign']]
            check('explicit named encoding control for ' + kind,
                  dialog.import_encoding.GetName() == 'Dictionary import encoding'
                  and dialog.import_encoding.GetSelection() == 0)
            picks.append(path)
            press(dialog.buttons['import'], lambda: until(lambda: not dialog.busy, foreign_imported))

        def foreign_imported():
            dialog = observed['dialog']
            kind, path, raw = foreign[observed['foreign']]
            check('foreign ' + kind + ' import remains review-only',
                  len(dialog.rules) == 1 and dialog.preview is None
                  and observed['editor'].GetValue() == observed['initial']
                  and dialog.store.path.read_bytes() == observed['saved_dictionary']
                  and path.read_bytes() == raw
                  and kind.upper() in dialog.status.GetValue())
            press(dialog.buttons['preview'], lambda: until(lambda: not dialog.busy, foreign_reviewed))

        def foreign_reviewed():
            dialog = observed['dialog']
            kind, _path, _raw = foreign[observed['foreign']]
            check('foreign ' + kind + ' uses actual current preview worker',
                  dialog.before.GetValue() == 'Dr.' and dialog.after.GetValue() == 'Doctor'
                  and dialog.buttons['apply'].IsEnabled())
            press(dialog.buttons['apply'], foreign_applied)

        def foreign_applied():
            dialog = observed['dialog']
            kind, _path, _raw = foreign[observed['foreign']]
            initial = observed['initial']
            start = initial.rindex('Dr.')
            check('foreign ' + kind + ' applies only to selected reading derivative',
                  observed['editor'].GetValue() == initial[:start] + 'Doctor' + initial[start + 3:]
                  and original_asset.read_bytes() == asset_bytes
                  and dialog.store.path.read_bytes() == observed['saved_dictionary'])
            press(dialog.buttons['undo'], foreign_undone)

        def foreign_undone():
            dialog = observed['dialog']
            kind, path, raw = foreign[observed['foreign']]
            check('foreign ' + kind + ' remains undoable without changing imported bytes',
                  observed['editor'].GetValue() == observed['initial']
                  and path.read_bytes() == raw and original_asset.read_bytes() == asset_bytes
                  and dialog.store.path.read_bytes() == observed['saved_dictionary'])
            observed['foreign'] += 1
            import_foreign()

        def close_reader():
            until(lambda: top_with_title('Pronunciation rules') is None, finish_reader)

        def finish_reader():
            reader = observed['reader']
            close = next(control for control in children(reader)
                         if isinstance(control, wx.Button) and control.GetLabel() == 'C&lose')
            press(close, lambda: None)

        def launch():
            frame.Show()
            until(loaded, open_review)
            show_speech_reader(frame, title='Synthetic source reading copy', recovery_slot='pronunciation-native',
                source_loader=lambda cancelled: read_source_range(desk, 1, 1, cancelled=cancelled),
                on_ready=lambda dialog, view: observed.update(reader=dialog, reader_view=view))
            release()
            if not errors:
                recovered = ReaderDraftStore(library.root, slot='pronunciation-native').for_source(source).load()
                check('reading recovery retains exact source lineage', recovered is not None
                      and recovered.document.source_anchor == source.reading_document().source_anchor
                      and recovered.document.source_text == source.text)
                check('source locator grants no quotation authority',
                      recovered.document.source_anchor['quotation_authority'] is False)
                check('final source asset digest unchanged', sha256(original_asset.read_bytes()).hexdigest()
                      == sha256(asset_bytes).hexdigest())
                check('owned enumeration provider closed', observed.get('provider_closed') is True)
                check('all foreign formats used the same existing review and undo workflow',
                      observed['foreign'] == 3)
                check('selected installed dictionary and reader bytes remain unchanged',
                      _acceptance_bindings().verify_installed_sources(
                          reading_module, formats_module, pronunciation_module,
                          reader_module, runtime_settings) == source_identity)
                observed['workflow_complete'] = True
            frame.Destroy()
            gui.ExitMainLoop()

        try:
            later(launch)
            gui.MainLoop()
        finally:
            abort.stop()
            release()
            runtime_settings.voice_registry = original_registry
            wx.FileDialog, wx.MessageBox = original_picker, original_message
            for window in tuple(wx.GetTopLevelWindows()):
                try:
                    window.Destroy()
                except RuntimeError:
                    pass
        if errors:
            raise RuntimeError('\n'.join(errors))
        check('normal native MainLoop completed the whole workflow', observed.get('workflow_complete') is True)
        print(json.dumps({'result': 'PASS', 'checks': len(checks), 'package_origin': str(origin),
                          'source_identity': source_identity,
                          'source_sha256': sha256(asset_bytes).hexdigest(),
                          'dictionary_sha256': sha256(observed['exported']).hexdigest(),
                          'native_keyboard_automation': 'PASS',
                          'foreign_formats': ['dic', 'rex', 'bxd'],
                          'foreign_engine_full_compatibility': 'NOT_RUN', 'native_file_picker': 'NOT_RUN',
                          'confirmation_dialog': 'NOT_RUN', 'nvda': 'NOT_RUN',
                          'voice_quality': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
