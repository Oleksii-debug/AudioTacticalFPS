"""Actual installed speech-preparation controls on the existing reader and Studio.

The authored source, three enumeration-only voices, unsaved-close answer and a
hold after genuine preparation are disclosed fixtures. All selected text rules,
history maps, recovery, Studio revisions, controls, keys and host workers are
real. This qualifies neither speech acoustics, native file pickers nor NVDA.
"""
import argparse
from dataclasses import replace
import faulthandler
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import threading
from tempfile import mkdtemp
from time import monotonic, sleep
from types import SimpleNamespace
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
    from carr_library.audio.models import AudioBudget
    from carr_library.audio.process import run_process
    result = run_process(
        [sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--native-child'],
        budget=AudioBudget(timeout=360, max_log_bytes=2 * 1024 * 1024),
        owned_tree=True, allow_failure=True)
    sys.stdout.write(result.stdout.decode('utf-8', errors='replace')); sys.stdout.flush()
    sys.stderr.write(result.stderr.decode('utf-8', errors='replace')); sys.stderr.flush()
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Native Windows is required.'}), flush=True)
        return 77
    if not args.native_child:
        return _run_owned_native()
    faulthandler.dump_traceback_later(330, exit=True)
    from carr_library.native_runtime import activate_bundled_runtime
    runtime = activate_bundled_runtime()
    try:
        return _run_native()
    finally:
        try:
            if runtime is not None:
                runtime.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def _run_native():
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library import application, durable_state
    from carr_library.application import LibraryApp
    from carr_library.reader import live_reading, speech_normalization
    from carr_library.reader._vendor_num2words import lang_SK, base, utils, compat, currency
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech import reading, preparation, session, source, Voice, VoiceRegistry
    from carr_library import runtime_settings
    from carr_library.studio import project as model, pronunciation
    from carr_library.ui import desktop_app, speech_dialog, speech_preparation, studio_pronunciation

    selected = (application, durable_state, live_reading, speech_normalization,
                lang_SK, base, utils, compat, currency, reading, preparation,
                session, source, runtime_settings, model, pronunciation,
                desktop_app, speech_dialog, speech_preparation, studio_pronunciation)
    binding = _acceptance_bindings()
    identities = binding.verify_installed_sources(*selected)
    sibling = Path(__file__).with_name('windows_publishing_smoke.py')
    scripts = {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in (Path(__file__), sibling)}
    vendor = Path(lang_SK.__file__).parent
    licenses = {name: sha256((vendor / name).read_bytes()).hexdigest()
                for name in ('COPYING', 'UPSTREAM.json')}
    checks, errors, jobs, peers = [], [], [], []
    frame, timer, failure_handler = None, None, None
    hold = SimpleNamespace(armed=False, ready=threading.Event(), release=threading.Event(), result=None)
    hold.release.set()
    state = SimpleNamespace(reader=None, view=None, reader_returned=True, waiting=None,
                            description='', entered=False, finished=False, started=monotonic())
    original_factory = speech_preparation.create_speech_preparation
    original_prepare = speech_preparation.prepare_selection
    original_registry, original_message = runtime_settings.voice_registry, wx.MessageBox

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    def failed(detail):
        errors.append(detail)
        print('FAIL: speech preparation\n' + detail, file=sys.stderr, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            detail = traceback.format_exc()
            if failure_handler is not None:
                failure_handler(detail)
            else:
                failed(detail)
                hold.release.set()
                abort.request()
            return True

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND

    def inside(peer, ancestor):
        for _ in range(64):
            if peer is None or not peer:
                return False
            if peer is ancestor:
                return True
            peer = peer.GetParent()
        raise ValueError('Native owner chain exceeds its bound.')

    def owners():
        windows = tuple(wx.GetTopLevelWindows())
        if len(windows) > 256:
            raise ValueError('Native owner inventory exceeds its bound.')
        modals = [p for p in windows if p and isinstance(p, wx.Dialog) and p.IsModal()
                  and frame and inside(p, frame)]
        hidden = [p for p in modals if not p.IsShown()]
        if hidden:
            selected_modal = hidden[0]
        elif modals:
            focused = wx.Window.FindFocus()
            top = wx.GetTopLevelParent(focused) if focused else None
            active = [p for p in modals if p.IsEnabled()]
            selected_modal = next((p for p in active if p is top), None)
            if selected_modal is None:
                if len(active) != 1:
                    raise RuntimeError('The current owned modal is still returning.')
                selected_modal = active[0]
        else:
            return ()
        return (SimpleNamespace(dialog=selected_modal,
            close=lambda value=selected_modal: value.EndModal(wx.ID_CANCEL)),)

    abort = _OwnedModalAbort(wx, gui, owners, failed)
    def select_failure_handler(value):
        nonlocal failure_handler
        failure_handler = value
    _nested_modal_failure_acceptance(wx, gui, check, select_failure_handler)

    def pump(predicate, message, seconds=8):
        deadline = monotonic() + seconds
        while not predicate():
            if errors:
                raise RuntimeError('A native callback failed.')
            if monotonic() >= deadline:
                raise TimeoutError(message)
            gui.Yield()
            sleep(.01)

    def focus(control):
        check('named control is alive, visible and enabled',
              bool(control) and control.IsShownOnScreen() and control.IsEnabled())
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control and user32.GetForegroundWindow() == top.GetHandle()
             and control.GetParent().GetClientRect().Contains(control.GetRect()),
             'The complete named control did not receive visible foreground focus.')

    def raw_key(code, modifiers=()):
        pressed = []
        try:
            for modifier in modifiers:
                check('native modifier down', simulator.KeyDown(modifier))
                pressed.append(modifier)
            check('native key down', simulator.KeyDown(code))
            pressed.append(code)
            sleep(.015)
        finally:
            for code in reversed(pressed):
                check('native key released', simulator.KeyUp(code))

    def press(control):
        focus(control)
        raw_key(wx.WXK_SPACE)

    def choose(control, index):
        focus(control)
        raw_key(wx.WXK_HOME)
        for _ in range(index):
            raw_key(wx.WXK_DOWN)
        raw_key(wx.WXK_TAB)
        pump(lambda: control.GetSelection() == index, 'Native choice did not accept the selected index.')

    def waiting(predicate, message, seconds=20):
        return predicate, message, monotonic() + seconds

    def titled(title):
        rows = [p for p in wx.GetTopLevelWindows() if p and isinstance(p, wx.Dialog)
                and p.GetTitle() == title and p.IsShown() and p.IsModal()]
        if len(rows) > 1:
            raise ValueError('Native dialog title is ambiguous.')
        return rows[0] if rows else None

    def current_child():
        return titled('Prepare reading copy for speech')

    def observe_factory(parent, **kwargs):
        actual_run = kwargs['run']
        def run(operation, done, *, on_failed, **options):
            record = SimpleNamespace(thread=None, returned=False, delivered=False,
                                     result=None, done=done, failed=on_failed)
            jobs.append(record)
            def work():
                record.thread = threading.get_ident()
                try:
                    record.result = operation()
                    return record.result
                finally:
                    record.returned = True
            def completed(value):
                try:
                    done(value)
                finally:
                    record.delivered = True
            def rejected(error):
                try:
                    on_failed(error)
                finally:
                    record.delivered = True
            return actual_run(work, completed, on_failed=rejected, **options)
        kwargs['run'] = run
        dialog = original_factory(parent, **kwargs)
        identity, deaths = dialog.GetId(), []
        def destroyed(event):
            if event.GetId() == identity:
                deaths.append(event.GetId())
            event.Skip()
        dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
        peers.append(SimpleNamespace(dialog=dialog, deaths=deaths))
        return dialog

    def held_prepare(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        if hold.armed:
            hold.armed = False
            hold.result = result
            hold.ready.set()
            if not hold.release.wait(25):
                raise TimeoutError('The genuine preparation hold was not released.')
        return result

    class EnumerationOnly:
        name = 'Authored speech-preparation enumeration; no synthesis'
        def voices(self):
            return (Voice('fixture:preparation-en', 'English enumeration', 'fixture', 'en-US'),
                    Voice('fixture:preparation-uk', 'Ukrainian enumeration', 'fixture', 'uk-UA'),
                    Voice('fixture:preparation-sk', 'Slovak enumeration', 'fixture', 'sk-SK'))
        def synthesize(self, *args, **kwargs):
            raise AssertionError('This text-preparation probe must not synthesize or qualify a voice.')
        def close(self):
            pass

    raw_root = Path(mkdtemp(prefix='resonance-native-preparation-'))
    raw_identity = raw_root.lstat()
    root = durable_state.local_path(raw_root, exists=True)
    current_identity = root.lstat()
    check('the canonical authored root is the same ordinary directory',
          (raw_identity.st_dev, raw_identity.st_ino) == (current_identity.st_dev, current_identity.st_ino))
    library = LibraryApp(root / 'private-library')
    workspace = library.create_workspace('Synthetic speech preparation')
    original = '😀 retained\r\n12 middle 5 kg.\r\nUnchanged end.'
    imported = library.add_text(workspace, 'Synthetic source text', original)
    desk = ReadingDesk(library, workspace, imported['edition'])
    retained = source.read_source_range(desk, 1, 1)
    asset = library.assets.resolve(imported['asset'])
    asset_bytes = asset.read_bytes()
    check('the actual imported source retains every Unicode and CRLF byte', retained.text == original)
    studio = pronunciation.StudioPronunciations(library.root)
    project = model.Project('native-speech-preparation', 'Synthetic preparation project',
        (model.Role('narrator', 'Narrator', 'fixture:preparation-en', 'en-US'),),
        (model.Scene('scene', 'Scene', (model.Line('first', 'narrator', original, source_ref='retained-original'),
            model.Line('second', 'narrator', 'Untouched second line.'))),),
        original_script=original, mode='authored')
    studio.projects.save(project)
    frame = desktop_app.DesktopFrame(library)
    frame.SetSize((1180, 980))
    frame.Show()
    gui.SetTopWindow(frame)
    desktop_executor = frame.executor
    speech_preparation.create_speech_preparation = observe_factory
    speech_preparation.prepare_selection = held_prepare
    runtime_settings.voice_registry = lambda *args, **kwargs: VoiceRegistry((EnumerationOnly(),))
    wx.MessageBox = lambda *args, **kwargs: wx.NO
    menu_state = SimpleNamespace(opened=0, highlighted=None)
    def menu_opened(event):
        menu_state.opened += 1
        event.Skip()
    def highlighted(event):
        menu_state.highlighted = event.GetId()
        event.Skip()
    frame.Bind(wx.EVT_MENU_OPEN, menu_opened)
    frame.Bind(wx.EVT_MENU_HIGHLIGHT_ALL, highlighted)

    def open_reader():
        check('the previous actual reader has returned', state.reader_returned)
        state.reader, state.view, state.reader_returned = None, None, False
        def launch():
            try:
                speech_dialog.show_speech_reader(frame, title='Synthetic preparation source',
                    recovery_slot='speech-preparation-native',
                    source_loader=lambda cancelled: source.read_source_range(desk, 1, 1, cancelled=cancelled),
                    on_ready=lambda dialog, view: (setattr(state, 'reader', dialog), setattr(state, 'view', view)))
            finally:
                state.reader_returned = True
        wx.CallAfter(launch)

    def loaded():
        return (state.reader and state.view is not None
                and state.view['document']().source_text == original
                and len(state.view['live']._voices) == 3 and not state.view['live'].core.busy)

    def prepare_reader():
        press(state.view['buttons']['prepare_speech'])
        yield waiting(lambda: current_child() is not None, 'The actual reader preparation button did not open its dialog')

    def configure(dialog, locale):
        choose(dialog.mode, 1)
        choose(dialog.locale, locale)
        for name in ('numbers', 'units'):
            check('speech rule is initially unchecked', dialog.expansions[name].GetValue() is False)
            press(dialog.expansions[name])
            pump(lambda selected=name: dialog.expansions[selected].GetValue() is True,
                 'The native speech rule checkbox did not become checked.')

    def preview(dialog):
        press(dialog.buttons['preview'])
        yield waiting(lambda: not dialog.busy and dialog.proposal is not None,
                      'The real preparation worker did not publish a reviewed proposal')

    def close_child(dialog):
        press(dialog.buttons['close'])
        yield waiting(lambda: not dialog and current_child() is None,
                      'The active preparation modal did not return before its parent')

    def command(label):
        check('Studio command starts from the same idle desktop',
              not frame.working and not owners() and frame.executor is desktop_executor)
        bar = frame.GetMenuBar()
        matches = [(index, menu, item) for index in range(bar.GetMenuCount())
                   for menu in (bar.GetMenu(index),) for item in menu.GetMenuItems()
                   if not item.IsSeparator() and item.GetItemLabelText() == label]
        check('the actual Studio command exists once', len(matches) == 1)
        index, menu, selected_item = matches[0]
        caption = bar.GetMenuLabel(index)
        marker = caption.find('&')
        check('the actual menu has a native mnemonic', 0 <= marker < len(caption) - 1)
        items = [item for item in menu.GetMenuItems() if not item.IsSeparator() and item.IsEnabled()]
        target = [number for number, item in enumerate(items) if item.GetId() == selected_item.GetId()]
        check('the exact command is enabled', len(target) == 1)
        focus(frame.status)
        before = menu_state.opened
        raw_key(ord(caption[marker + 1].upper()), (wx.WXK_ALT,))
        yield waiting(lambda: menu_state.opened > before, 'The actual Studio menu did not open', 8)
        raw_key(wx.WXK_HOME)
        yield waiting(lambda: menu_state.highlighted == items[0].GetId(),
                      'The menu did not highlight its first actual command', 8)
        for item in items[1:target[0] + 1]:
            raw_key(wx.WXK_DOWN)
            yield waiting(lambda selected=item.GetId(): menu_state.highlighted == selected,
                          'The actual Studio command was not highlighted', 8)
        raw_key(wx.WXK_RETURN)

    def scenario():
        check('installed Windows controls are active', wx.Platform == '__WXMSW__' and bool(identities))
        open_reader()
        yield waiting(loaded, 'The actual source reader and enumeration did not load')
        current = state.view['document']
        check('opening preparation did not edit the source', current().text == original)
        cases = ((0, 2, 'twelve middle five kilograms.'),
                 (1, 1, "дванадцять middle п'ять кілограмів."),
                 (2, 3, 'dvanásť middle päť kilogramov.'))
        for number, (voice_index, locale_index, expected_passage) in enumerate(cases):
            choose(state.view['live'].voice, voice_index)
            state.view['editor'].SetInsertionPoint(0)
            yield from prepare_reader()
            dialog = current_child()
            check('preparation defaults to exact verbatim', dialog.mode.GetSelection() == 0)
            if number == 0:
                initial = current().to_dict()
                yield from preview(dialog)
                check('native verbatim preview creates no derivative history',
                      dialog.proposal.result.text == original and not dialog.buttons['apply'].IsEnabled()
                      and current().to_dict() == initial)
            configure(dialog, locale_index)
            yield from preview(dialog)
            expected = original.replace('12 middle 5 kg.', expected_passage)
            check('the actual explicit locale proposal is exact', dialog.proposal.result.text == expected)
            before_history = current().to_dict()['revision']
            press(dialog.buttons['apply'])
            yield waiting(lambda: current().text == expected, 'Apply did not change the actual reading document')
            document = current()
            middle = document.text.index('middle')
            span = document.source_span(middle, middle + len('middle'))
            check('one compound edit retains exact untouched interior positions',
                  document.to_dict()['revision'] == before_history + 1
                  and 'changes' in document.to_dict()['edits'][before_history] and span.exact
                  and (span.start, span.end) == (original.index('middle'), original.index('middle') + len('middle'))
                  and not document.source_span(document.text.index(expected_passage), document.text.index(expected_passage) + 2).exact)
            check('the original and retained file remain byte exact',
                  document.source_text == original and asset.read_bytes() == asset_bytes)
            check('the derivative records strict v3 and passive source lineage',
                  document.to_dict()['schema'] == 'carr.reading-copy.v3' and document.source_anchor['quotation_authority'] is False)
            if number < 2:
                press(dialog.buttons['undo'])
                yield waiting(lambda: current().text == original, 'The same preparation Undo did not restore exact text')
                check('Undo restored the original source map', current().source_span(0, len(original)).exact)
            yield from close_child(dialog)
        accepted = current().to_dict()
        saved = session.ReaderDraftStore(library.root, slot='speech-preparation-native').for_source(retained).load()
        check('actual recovery saves the exact compound history and current source anchor',
              saved is not None and saved.document.to_dict() == accepted)

        # A real completed proposal is held; the dialog cancellation and
        # subsequent direct native deletion use the existing reader worker.
        for action in ('cancel', 'direct_destroy'):
            yield from prepare_reader()
            dialog = current_child()
            configure(dialog, 2)
            hold.ready.clear(); hold.release.clear(); hold.result = None; hold.armed = True
            press(dialog.buttons['preview'])
            yield waiting(hold.ready.is_set, 'The genuine preparation did not reach its completion hold')
            record = jobs[-1]
            if action == 'cancel':
                press(dialog.buttons['cancel'])
                yield waiting(dialog._withdrawn.is_set, 'The native Cancel event did not reach its owner')
                check('native Cancel is acknowledged before releasing the genuine completed proposal',
                      dialog.busy and dialog.proposal is None and not record.returned)
            else:
                dialog.Destroy()
            hold.release.set()
            yield waiting(lambda: record.returned and record.delivered,
                          'The same reader worker did not deliver its genuine completion')
            if action == 'cancel':
                check('native cancellation does not grant a stale proposal',
                      dialog.proposal is None and not dialog.busy and current().to_dict() == accepted)
                yield from close_child(dialog)
            else:
                check('direct destruction withdraws the genuine worker result',
                      not dialog and current().to_dict() == accepted)
                record.done(hold.result)
                check('a replayed genuine late callback cannot edit a closed owner',
                      not dialog and current().to_dict() == accepted)
        press(state.view['buttons']['close'])
        yield waiting(lambda: state.reader_returned and not state.reader,
                      'The source reader did not close after its active child returned')
        open_reader()
        yield waiting(loaded, 'The actual source reader did not reopen its saved draft')
        check('native reopen restores exact compound history without applying a new proposal',
              state.view['document']().to_dict() == accepted)
        press(state.view['buttons']['undo'])
        yield waiting(lambda: state.view['document']().text == original,
                      'The ordinary reader Undo did not undo the recovered compound')
        press(state.view['buttons']['close'])
        yield waiting(lambda: state.reader_returned and not state.reader,
                      'The recovered reader did not finish its real owned cleanup')

        yield from command('Review spoken-line pronunciation...')
        yield waiting(lambda: titled('Studio spoken-line pronunciation') is not None,
                      'The actual Studio command did not open its line editor')
        line = titled('Studio spoken-line pronunciation')
        yield waiting(lambda: not line.pending and len(line.projects) == 1,
                      'The current Studio service did not list the authored project')
        press(line.buttons['open'])
        yield waiting(lambda: not line.pending and line.draft is not None,
                      'The real selected spoken line did not open')
        check('Studio preserves the selected role and source-bound line',
              line.draft.project == project and line.draft.role.voice_id == 'fixture:preparation-en')
        press(line.buttons['prepare'])
        yield waiting(lambda: current_child() is not None, 'The actual Studio preparation button was unreachable')
        dialog = current_child()
        configure(dialog, 2)
        yield from preview(dialog)
        expected_line = original.replace('12 middle 5 kg.', cases[0][2])
        check('Studio uses the same exact reviewed text rules', dialog.proposal.result.text == expected_line)
        press(dialog.buttons['apply'])
        yield waiting(lambda: line.draft.document.text == expected_line,
                      'Studio preparation did not apply to the opened local draft')
        check('draft application does not publish a project revision',
              studio.projects.load(project.id) == project and line.draft.dirty)
        yield from close_child(dialog)
        press(line.buttons['save'])
        yield waiting(lambda: not line.pending and line.last_saved is not None,
                      'The actual Studio Save did not return its canonical receipt')
        changed_line = replace(project.scenes[0].lines[0], text=expected_line)
        expected_project = replace(project, revision=2, scenes=(replace(project.scenes[0],
            lines=(changed_line, project.scenes[0].lines[1])),))
        check('explicit Studio Save changes only the one spoken line revision',
              studio.projects.load(project.id) == expected_project
              and line.last_saved.after == expected_project and frame.executor is desktop_executor)
        press(line.buttons['undo'])
        yield waiting(lambda: not line.pending and line.last_saved is None,
                      'Studio saved Undo did not finish on the same executor')
        check('saved Undo preserves the original script and untouched line',
              studio.projects.load(project.id) == replace(project, revision=3))
        press(line.buttons['close'])
        yield waiting(lambda: not line, 'The Studio line modal did not return')
        check('every actual preparation worker ran away from the GUI thread',
              jobs and all(job.thread != threading.get_ident() and job.returned and job.delivered for job in jobs))
        check('every preparation dialog received one actual native destruction',
              peers and all(not peer.dialog and len(peer.deaths) == 1 for peer in peers))
        check('all original bytes survive native preparation and Studio history',
              asset.read_bytes() == asset_bytes)
        state.finished = True

    sequence = scenario()
    timer = wx.Timer(gui)
    def drive(event=None):
        if state.entered or state.finished:
            return
        state.entered = True
        try:
            if monotonic() - state.started > 270:
                raise TimeoutError('The native preparation scenario exceeded its finite bound.')
            if state.waiting is not None:
                predicate, state.description, deadline = state.waiting
                if not predicate():
                    if monotonic() > deadline:
                        raise TimeoutError(state.description)
                    return
                state.waiting = None
            try:
                state.waiting = next(sequence)
            except StopIteration:
                if not state.finished:
                    raise AssertionError('Native preparation ended before its final oracles.')
                timer.Stop()
                gui.ExitMainLoop()
        except (Exception, KeyboardInterrupt):
            failed(traceback.format_exc() + '\nWaiting: ' + state.description)
            timer.Stop()
            hold.release.set()
            abort.request()
        finally:
            state.entered = False

    gui.Bind(wx.EVT_TIMER, drive, timer)
    successful = False
    try:
        timer.Start(30)
        gui.MainLoop()
        if errors or not state.finished:
            raise RuntimeError('Native speech preparation failed; see bounded diagnostic records.')
        check('all selected installed module bytes remain exact', binding.verify_installed_sources(*selected) == identities)
        check('the selected probe and sibling helper remain exact',
              {str(p.resolve()): sha256(p.read_bytes()).hexdigest() for p in (Path(__file__), sibling)} == scripts)
        check('the shipped exact license and upstream record remain intact',
              {name: sha256((vendor / name).read_bytes()).hexdigest() for name in licenses} == licenses)
        successful = True
    finally:
        timer.Stop()
        timer.SetOwner(None)
        abort.stop()
        hold.release.set()
        speech_preparation.create_speech_preparation = original_factory
        speech_preparation.prepare_selection = original_prepare
        runtime_settings.voice_registry, wx.MessageBox = original_registry, original_message
        if frame:
            frame._closing = True
            frame.cancel_event.set()
        desktop_executor.shutdown(wait=True, cancel_futures=False)
        if not library.close_search():
            raise RuntimeError('The actual search owner has not confirmed cleanup.')
        if frame:
            if getattr(frame, 'progress_panel', None) is not None:
                frame.progress_panel.close()
            frame.Destroy()
        gui.Yield()
        if successful:
            import shutil
            shutil.rmtree(root)
        else:
            print('RETAINED_FAILED_NATIVE_FIXTURE ' + str(root), file=sys.stderr, flush=True)
    print(json.dumps({'result': 'PASS', 'checks': len(checks), 'native_keyboard_automation': 'PASS',
        'three_explicit_text_locales': ['en-US', 'uk-UA', 'sk-SK'], 'compound_source_maps': 'PASS',
        'actual_reader_recovery': 'PASS', 'actual_studio_save_undo': 'PASS',
        'existing_host_workers': 'PASS', 'held_cancel_direct_deletion_late_callback': 'PASS',
        'source_identity': identities, 'probe_and_binding': scripts, 'license_identity': licenses,
        'speech_quality': 'NOT_RUN', 'physical_audio': 'NOT_RUN', 'native_file_picker': 'NOT_RUN',
        'NVDA': 'NOT_RUN', 'controlled_seams': ['enumeration-only voices', 'unsaved-close answer NO',
                                              'hold after genuine preparation']},
        ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
