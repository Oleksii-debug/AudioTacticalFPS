"""Installed native spoken-line review/save/undo; synthetic source, no speech quality claim."""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import threading
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback


def _acceptance_bindings():
    import importlib.util
    path = Path(__file__).with_name('windows_publishing_smoke.py')
    spec = importlib.util.spec_from_file_location('_pronunciation_acceptance_bindings', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    if os.name != 'nt':
        raise RuntimeError('This acceptance probe requires actual Windows.')
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    try:
        return run_native()
    finally:
        if native_handle is not None:
            native_handle.close()


def run_native():
    import ctypes
    from ctypes import wintypes
    import wx
    import carr_library
    from carr_library.application import LibraryApp
    from carr_library.durable_state import atomic_json, read_json
    from carr_library.speech.reading import PronunciationRule
    from carr_library.studio.project import Line, Project, Role, Scene
    from carr_library.studio.pronunciation import StudioPronunciations
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.pronunciation_dialog import DictionaryStore
    import carr_library.speech.reading as reading_module
    import carr_library.studio.project as project_module
    import carr_library.studio.pronunciation as service_module
    import carr_library.ui.pronunciation_dialog as shared_ui
    import carr_library.ui.studio_dialog as studio_ui
    import carr_library.ui.studio_pronunciation as line_ui

    checks, errors, observed = [], [], {}
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label); print('PASS: ' + label, flush=True)

    origin = Path(carr_library.__file__).resolve()
    identities = _acceptance_bindings().verify_installed_sources(
        reading_module, project_module, service_module, shared_ui, studio_ui, line_ui)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc()); abort(); return False
    gui = ProbeApp(False)
    check('actual Windows wx controls', wx.Platform == '__WXMSW__')
    simulator, held = wx.UIActionSimulator(), [None]
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def release():
        value, held[0] = held[0], None
        if value is not None:
            check('native key and modifiers released', simulator.KeyUp(*value))

    def abort():
        try:
            release()
        except Exception:
            errors.append(traceback.format_exc())
        frame = observed.get('frame')
        if frame is not None:
            frame.cancel_event.set()
        for window in wx.GetTopLevelWindows():
            try:
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            except RuntimeError:
                pass
        gui.ExitMainLoop()

    def later(callback, delay=30):
        def invoke():
            if errors:
                return
            try:
                callback()
            except Exception:
                errors.append(traceback.format_exc()); abort()
        wx.CallLater(delay, invoke)

    def until(predicate, complete, *, timeout=30):
        deadline = monotonic() + timeout
        def poll():
            if predicate():
                complete()
            elif monotonic() >= deadline:
                raise TimeoutError('Native Studio spoken-line operation did not finish.')
            else:
                later(poll)
        later(poll)

    def foreground(control):
        top = wx.GetTopLevelParent(control)
        handle, owner = user32.GetForegroundWindow(), wintypes.DWORD()
        if handle:
            user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        ready = owner.value == os.getpid() and int(handle or 0) == int(top.GetHandle()) and wx.Window.FindFocus() is control
        if not ready:
            top.Raise(); user32.SetForegroundWindow(top.GetHandle()); control.SetFocus()
        return ready

    def press(control, complete):
        name = control.GetName()
        def down():
            check('owned foreground and focus: ' + name, foreground(control))
            held[0] = (wx.WXK_SPACE, wx.MOD_NONE)
            try:
                check('native key down: ' + name, simulator.KeyDown(*held[0]))
            except BaseException:
                release(); raise
            def up():
                release(); later(complete)
            later(up, 40)
        until(lambda: held[0] is None and foreground(control), down, timeout=5)

    def titled(title):
        return next((window for window in wx.GetTopLevelWindows()
                     if window and isinstance(window, wx.Dialog) and window.GetTitle().startswith(title)), None)

    with TemporaryDirectory(prefix='resonance-native-spoken-line-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Synthetic spoken-line source')
        original = '😀 Dr. First.\r\nDr. Second.'
        source = library.add_text(workspace, 'Retained original source', original)
        asset = library.assets.resolve(source['asset'])
        source_digest = sha256(asset.read_bytes()).hexdigest()
        service = StudioPronunciations(library.root)
        project = Project('native-spoken-line', 'Native pronunciation fixture',
            (Role('narrator', 'Narrator', 'fixture:no-synthesis', 'en'),),
            (Scene('archive', 'Archive', (Line('first', 'narrator', original, source_ref='retained-original'),
                                        Line('second', 'narrator', 'Untouched second line.'))),),
            original_script=original, mode='authored')
        service.projects.save(project)
        DictionaryStore(library.root).save((PronunciationRule('Dr.', 'Doctor', language='en',
                                            voice_id='fixture:no-synthesis'),), expected=None)
        frame = DesktopFrame(library)
        observed['frame'] = frame
        gui.SetTopWindow(frame)
        menu_items = [item for index in range(frame.menu_bar.GetMenuCount())
                      for item in frame.menu_bar.GetMenu(index).GetMenuItems()
                      if item.GetItemLabelText() == 'Review spoken-line pronunciation...']
        check('one production Studio menu entry', len(menu_items) == 1)

        def opened():
            dialog = titled('Studio spoken-line pronunciation')
            observed['dialog'] = dialog
            until(lambda: not dialog.pending and len(dialog.projects) == 1,
                  lambda: press(dialog.buttons['open'], lambda: until(lambda: not dialog.pending, line_ready)))

        def line_ready():
            dialog = observed['dialog']
            check('native project scene line and role identity', dialog.draft.project == project
                  and dialog.draft.scene_id == 'archive' and dialog.draft.line_id == 'first'
                  and dialog.draft.role.voice_id == 'fixture:no-synthesis')
            check('read-only selectable native text', bool(dialog.text.GetWindowStyleFlag() & wx.TE_READONLY))
            observed['pass'] = 'first'
            open_review()

        def open_review():
            dialog = observed['dialog']
            # This known suffix is ASCII, so subtracting its length from the
            # native end avoids probing a position inside an emoji surrogate.
            start = dialog.text.GetLastPosition() - len('Dr. Second.')
            check('native Unicode and line-ending selection', dialog.text.GetValue().count('Dr.') == 2
                  and dialog.text.GetRange(start, start + 3) == 'Dr.')
            dialog.text.SetSelection(start, start + 3)
            press(dialog.buttons['review'], lambda: until(
                lambda: titled('Pronunciation rules') is not None, review_ready))

        def review_ready():
            child = titled('Pronunciation rules')
            observed['child'] = child
            until(lambda: child.loaded and not child.busy, preview)

        def preview():
            child = observed['child']
            check('same saved dictionary and exact role scope', len(child.rules) == 1
                  and child.scope.GetSelection() == 1 and child.voice.GetValue() == 'fixture:no-synthesis')
            press(child.buttons['preview'], lambda: until(lambda: not child.busy, reviewed))

        def reviewed():
            child = observed['child']
            check('native selected line preview', child.before.GetValue() == 'Dr.' and child.after.GetValue() == 'Doctor')
            press(child.buttons['apply'], applied)

        def applied():
            dialog, child = observed['dialog'], observed['child']
            check('apply edits only the selected local line draft',
                  dialog.draft.document.text == '😀 Dr. First.\r\nDoctor Second.' and dialog.draft.dirty)
            current = service.projects.load(project.id)
            check('review apply has not published a project', current.revision == (1 if observed['pass'] == 'first' else 3))
            press(child.buttons['close'], lambda: until(lambda: titled('Pronunciation rules') is None, save_line))

        def save_line():
            dialog = observed['dialog']
            check('dirty draft requires explicit save or discard', dialog.buttons['save'].IsEnabled()
                  and not dialog.project_choice.IsEnabled())
            if observed['pass'] == 'stale':
                external = replace(service.projects.load(project.id), title='External same-revision change')
                atomic_json(service.projects.directory(project.id) / 'project.json', external.to_dict(), replace=True)
                observed['external'] = external
                press(dialog.buttons['save'], lambda: until(lambda: not dialog.pending, stale_rejected))
            else:
                press(dialog.buttons['save'], lambda: until(lambda: not dialog.pending, saved))

        def saved():
            dialog = observed['dialog']
            saved = service.projects.load(project.id)
            expected_line = replace(project.scenes[0].lines[0], text='😀 Dr. First.\r\nDoctor Second.')
            expected = replace(project, revision=2, scenes=(replace(project.scenes[0],
                lines=(expected_line, project.scenes[0].lines[1])),))
            check('native Save publishes only one exact line revision', saved == expected and dialog.last_saved.after == saved)
            check('original source asset and original script unchanged', sha256(asset.read_bytes()).hexdigest() == source_digest
                  and saved.original_script == original)
            press(dialog.buttons['undo'], lambda: until(lambda: not dialog.pending, undone))

        def undone():
            dialog = observed['dialog']
            check('native saved Undo creates a new preserved revision',
                  service.projects.load(project.id) == replace(project, revision=3) and dialog.last_saved is None)
            history = [Project.from_dict(read_json(path)) for path in
                       (service.projects.directory(project.id) / 'revisions').iterdir()]
            check('both earlier project revisions remain', {item.revision for item in history} == {1, 2})
            observed['pass'] = 'stale'
            open_review()

        def stale_rejected():
            dialog = observed['dialog']
            check('same-revision external edit rejected by native Save', 'changed' in dialog.status.GetValue()
                  and service.projects.load(project.id) == observed['external'] and dialog.draft.dirty)
            press(dialog.buttons['discard'], discarded)

        def discarded():
            dialog = observed['dialog']
            check('discard removes only unsaved pronunciation draft', not dialog.draft.dirty
                  and service.projects.load(project.id) == observed['external'])
            press(dialog.buttons['reload'], lambda: until(lambda: not dialog.pending, reloaded))

        def reloaded():
            dialog = observed['dialog']
            check('reload binds the current external project identity', dialog.projects == (observed['external'],))
            observed['complete'] = True
            press(dialog.buttons['close'], lambda: None)

        def check_destroyed_callbacks():
            for mode in ('direct', 'native-parent'):
                owner = wx.Frame(frame, title='Owned pronunciation teardown fixture')
                owner.library, owner._closing, owner.working = library, False, False
                owner.cancel_event = threading.Event()
                pending, delivered, destroyed = [], [], []
                def run_job(label, operation, done, failed):
                    owner.working = True
                    pending.append((operation, done, failed))
                owner.run_job = run_job
                peer = line_ui.create_studio_pronunciation_dialog(owner, wx_module=wx)
                native_id = peer._native_id
                def destruction(event):
                    destroyed.append(event.GetId()); event.Skip()
                peer.Bind(wx.EVT_WINDOW_DESTROY, destruction)
                try:
                    peer.Show(); peer.reload()
                    check(mode + ': actual owner queues a pending review operation', peer.pending and len(pending) == 1)
                    def deliver():
                        owner.working = False
                        pending[0][1](())
                        pending[0][2](InterruptedError('Synthetic late owner callback'))
                        peer.report('Late review result'); peer.enable(); peer.review(); peer.save()
                        delivered.append(True)
                    if mode == 'direct':
                        peer.Destroy()
                    else:
                        owner.DestroyChildren()
                    deadline = monotonic() + 5
                    while peer or destroyed.count(native_id) != 1:
                        if monotonic() >= deadline:
                            raise TimeoutError('Owned pronunciation native teardown did not finish.')
                        gui.Yield(); sleep(.005)
                    wx.CallAfter(deliver)
                    deadline = monotonic() + 5
                    while not delivered:
                        if monotonic() >= deadline:
                            raise TimeoutError('Held pronunciation completion was not delivered after native deletion.')
                        gui.Yield(); sleep(.005)
                    check(mode + ': native destruction is delivered exactly once', destroyed.count(native_id) == 1)
                    check(mode + ': actual late callbacks observe disposed owner',
                          not peer.alive and not peer.pending and owner.cancel_event.is_set() and delivered == [True])
                finally:
                    if owner:
                        owner.Destroy()
            observed['native_lifetime'] = True

        def launch():
            frame.Show()
            until(lambda: titled('Studio spoken-line pronunciation') is not None, opened)
            command = wx.CommandEvent(wx.wxEVT_MENU, menu_items[0].GetId())
            check('native menu command reaches production handler', frame.GetEventHandler().ProcessEvent(command))
            release()
            if not errors:
                check('complete Studio native workflow returned', observed.get('complete') is True)
                check('final retained source digest unchanged', sha256(asset.read_bytes()).hexdigest() == source_digest)
                check_destroyed_callbacks()
            frame.Close(force=True)
            gui.ExitMainLoop()

        try:
            later(launch)
            gui.MainLoop()
        finally:
            try:
                release()
            finally:
                frame.cancel_event.set()
                try:
                    if frame and not frame.IsBeingDeleted():
                        frame.Close(force=True)
                finally:
                    frame.executor.shutdown(wait=True, cancel_futures=True)
        if errors:
            raise RuntimeError('\n'.join(errors))
        check('normal native MainLoop completed every case', observed.get('complete') is True)
        print(json.dumps({'result': 'PASS', 'checks': checks, 'count': len(checks),
            'package_origin': str(origin), 'source_identity': identities, 'source_sha256': source_digest,
            'native_keyboard_actions': 'PASS', 'entry_dispatch': 'native wx.EVT_MENU',
            'native_direct_parent_destruction_and_late_callbacks': 'PASS' if observed.get('native_lifetime') else 'NOT_RUN',
            'voice_quality': 'NOT_RUN', 'nvda': 'NOT_RUN', 'manual_keyboard': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)

