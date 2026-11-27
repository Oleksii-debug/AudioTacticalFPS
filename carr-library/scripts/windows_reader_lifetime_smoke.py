"""Installed wx lifetime acceptance for the seven native reader/review dialogs.

Real direct and parent-driven deletion, native IDs and queued callbacks are
required. Worker values are explicit synthetic lifetime fixtures. This does not
claim playback quality, physical keyboard access, NVDA or language coverage.
"""
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import traceback


def _acceptance_bindings():
    path = Path(__file__).with_name('windows_publishing_smoke.py')
    spec = importlib.util.spec_from_file_location('_reader_lifetime_bindings', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Actual Windows is required.'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime()
    try:
        return _run()
    finally:
        if handle is not None:
            handle.close()


def _run():
    import wx
    from carr_library.application import LibraryApp
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.speech.reading import ReadingDocument
    from carr_library.speech import VoiceRegistry, live_adapter
    from carr_library.reader import live_reading
    from carr_library import runtime_settings
    from carr_library.ui import command_palette, ebook_audio_dialog, find_replace_dialog, language_matrix
    from carr_library.ui import keyboard_settings, pronunciation_dialog, reading_desk, speech_dialog

    source_identity = _acceptance_bindings().verify_installed_sources(
        command_palette, ebook_audio_dialog, find_replace_dialog, language_matrix, keyboard_settings,
        pronunciation_dialog, reading_desk, speech_dialog, live_adapter, live_reading)
    checks, failures, reports, events = [], [], [], []
    owners, hosts, releases, restore_registry = [], [], [], []
    deadline = monotonic() + 75
    current = {'case': 'starting'}
    finished = False

    def check(name, condition):
        if not condition:
            raise AssertionError(name + ': ' + current['case'])
        checks.append(current['case'] + ': ' + name)

    def fail():
        failures.append(traceback.format_exc())
        for release in releases:
            release.set()
        for window in wx.GetTopLevelWindows():
            if window and isinstance(window, wx.Dialog) and window.IsModal():
                window.EndModal(wx.ID_CANCEL)
        gui.ExitMainLoop()

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            fail()
            return True

    gui = ProbeApp(False)
    gui.SetExitOnFrameDelete(False)
    anchor = wx.Frame(None, title='Reader lifecycle acceptance', size=(500, 200))
    gui.SetTopWindow(anchor)
    anchor.Show()
    check('real native Windows controls', wx.Platform == '__WXMSW__')

    def later(callback):
        def guarded():
            if failures:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Native reader lifecycle acceptance exceeded its bound.')
                callback()
            except Exception:
                fail()
        wx.CallLater(10, guarded)

    def until(predicate, complete, *, timeout=5):
        end = monotonic() + timeout
        def poll():
            if predicate():
                complete()
            elif monotonic() >= end:
                raise TimeoutError('Native deletion or queued completion was not observed.')
            else:
                later(poll)
        later(poll)

    def voice_case(host, termination, complete):
        entered, release, registry_closed = Event(), Event(), Event()
        releases.append(release)
        original_factory = runtime_settings.voice_registry
        observed = {'returned': False}
        destroyed = []
        class InventoryOwner:
            name = 'Synthetic held native reader discovery'
            def voices(self):
                return ()
            def close(self):
                registry_closed.set()
        def inventory(state_root, *, cancelled, **_options):
            observed['cancelled'] = cancelled
            entered.set()
            if not release.wait(10):
                raise TimeoutError('Voice-reader fixture did not release its discovery worker.')
            return VoiceRegistry((InventoryOwner(),))
        runtime_settings.voice_registry = inventory
        restore_registry.append(original_factory)

        def launch():
            speech_dialog.show_speech_reader(host, text='Explicit native lifetime reading fixture.',
                                             recovery_slot='native-lifetime-' + termination)
            observed['returned'] = True

        def locate():
            matches = [window for window in wx.GetTopLevelWindows()
                       if window and isinstance(window, wx.Dialog) and window.GetParent() is host
                       and window.GetTitle() == 'Voice reader and reading copy']
            if len(matches) == 1 and entered.is_set():
                observed['window'] = matches[0]
                return True
            return False

        def ready():
            window = observed['window']
            owner_id = window.GetId()
            def watch(event):
                if event.GetId() == owner_id:
                    destroyed.append({'id': event.GetId(), 'original_wrapper': event.GetEventObject() is window})
                event.Skip()
            window.Bind(wx.EVT_WINDOW_DESTROY, watch)
            child = wx.Panel(window)
            child.Destroy()
            def child_gone():
                check('child deletion leaves queued voice worker attached',
                      not observed['cancelled']() and not destroyed)
                later(remove_owner)
            def remove_owner():
                if termination == 'direct':
                    window.Destroy()
                    check('direct voice Destroy detaches synchronously', observed['cancelled']())
                else:
                    host.DestroyChildren()
                # Live reading closes asynchronously. Release its real worker
                # and require owned inventory cleanup, without a GUI callback.
                release.set()
                until(lambda: not window and len(destroyed) == 1
                      and observed['returned'] and registry_closed.is_set(), replay)
            def replay():
                check('native voice destruction cancelled the actual queued worker', observed['cancelled']())
                check('voice modal cleanup returned after the real deletion event', len(destroyed) == 1)
                check('repeat voice Destroy is harmless', window.Destroy() is False)
                check('live adapter closed its actual unaccepted registry', registry_closed.is_set())
                # Service another native turn after actual worker cleanup.
                def finished_callback():
                    check('late voice completion event turn executed', True)
                    events.append({'case': current['case'], 'native_events': destroyed})
                    runtime_settings.voice_registry = original_factory
                    restore_registry.pop()
                    host.Destroy()
                    until(lambda: not host, complete)
                wx.CallAfter(finished_callback)
            until(lambda: not child, child_gone)

        host.Show()
        later(launch)
        until(locate, ready)

    with TemporaryDirectory(prefix='resonance-native-reader-lifetime-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Synthetic native reader lifetime')
        book = library.add_text(workspace, 'Retained lifetime source', 'Exact retained source. No quotation approval.')
        asset = library.assets.resolve(book['asset'])
        original = asset.read_bytes()
        cases = iter((kind, termination) for kind in
                     ('reading', 'audio', 'pronunciation', 'find-replace', 'palette', 'languages', 'voice')
                     for termination in ('direct', 'native-parent'))

        def next_case():
            nonlocal finished
            item = next(cases, None)
            if item is None:
                check('all original source bytes remain unchanged', asset.read_bytes() == original)
                check('reading lifetime grants no quotation authority', library._selections == {})
                finished = True
                gui.ExitMainLoop()
                return
            kind, termination = item
            current['case'] = kind + '/' + termination
            check('real main event loop is running', gui.GetMainLoop() is not None)
            host = wx.Frame(anchor, title=current['case'], size=(600, 300))
            hosts.append(host)
            host.library, host.active = library, (workspace, None)
            host.cancel_event = Event()
            host._closing = host.working = host.busy = False
            host.report = lambda text: reports.append(str(text))
            if kind == 'voice':
                voice_case(host, termination, next_case)
                return
            pending, accepted, discarded = [], [], []
            host.run_job = lambda *args: pending.append(args)
            entered, release, completed = Event(), Event(), Event()
            releases.append(release)
            desk = ReadingDesk(library, workspace, book['edition'])
            if kind == 'reading':
                owner = reading_desk.ReadingDialog(host, desk, ReadingState(library.root), lambda: None)
                window = owner.dialog
            elif kind == 'audio':
                owner = ebook_audio_dialog.EbookAudioDialog(host, desk, 1, lambda: None)
                window = owner.dialog
            elif kind == 'pronunciation':
                document = ReadingDocument(desk.page(1).raw_text)
                snapshot = pronunciation_dialog.ReadingSnapshot.capture(document, (0, 0))
                def run(work, done, *, on_failed, committed=False):
                    assert committed is False  # This fixture queues a dictionary load.
                    pending.append((work, done, on_failed))
                    return True
                owner = pronunciation_dialog.create_pronunciation_dialog(host, library_root=library.root,
                    capture=lambda: snapshot, apply=lambda preview: (_ for _ in ()).throw(
                        AssertionError('A lifetime fixture cannot apply a reading edit.')),
                    undo=lambda value: '', guard=lambda: None, run=run, cancel=host.cancel_event)
                window = owner
                owner.on_load()
            elif kind == 'languages':
                owner = language_matrix._native_dialog_type()(host, language_matrix.LanguageMatrixController())
                window = owner
                owner.refresh()
            elif kind == 'find-replace':
                document = ReadingDocument('Exact retained source. No quotation approval.')
                snapshot = pronunciation_dialog.ReadingSnapshot.capture(document, (0, 0))
                def run(work, done, *, on_failed):
                    pending.append((work, done, on_failed))
                    return True
                owner = find_replace_dialog.create_find_replace_dialog(host, capture=lambda: snapshot,
                    select=lambda *args: accepted.append('unexpected selection'),
                    apply=lambda *args: accepted.append('unexpected edit'), undo=lambda value: '',
                    guard=lambda: None, run=run, cancel=host.cancel_event)
                window = owner
                owner.pattern.ChangeValue('source')
                owner.on_find()
                # Produce a real isolated-worker result, hold its actual UI
                # completion until after the native owner is deleted below.
                find_result = pending[0][0]()
                check('real isolated search found the retained passage', len(find_result.matches) == 1)
            else:
                bar, menu = wx.MenuBar(), wx.Menu()
                menu.Append(wx.ID_ANY, 'Keyboard guide\tF1')
                bar.Append(menu, '&Help')
                host.SetMenuBar(bar)
                host.menu_bar = bar
                keyboard = keyboard_settings.KeyboardSettings(host)
                host.keyboard_settings = keyboard
                owner = command_palette.create_command_palette(keyboard)
                window = owner
                host.Bind(wx.EVT_MENU, lambda event: accepted.append('unexpected command'))
            owners.append((kind, owner))
            cached_id = window.GetId()
            destroyed = []
            def observe(event):
                if event.GetId() == cached_id:
                    destroyed.append({'id': event.GetId(), 'original_wrapper': event.GetEventObject() is window})
                event.Skip()
            window.Bind(wx.EVT_WINDOW_DESTROY, observe)
            host.Show()
            window.Show()

            def detached():
                return (owner.closed if kind in ('reading', 'audio') else
                        not owner._alive if kind == 'languages' else not owner.alive)

            if kind in ('reading', 'audio'):
                def work():
                    entered.set()
                    try:
                        if not release.wait(10):
                            raise TimeoutError('Native lifetime fixture did not release its worker.')
                        return 'owned synthetic result'
                    finally:
                        completed.set()
                if kind == 'reading':
                    owner._run('Bounded lifetime fixture', work, accepted.append)
                else:
                    owner._submit(work, accepted.append, discarded.append)
            else:
                entered.set()

            def replay():
                if kind == 'reading':
                    owner._error(RuntimeError('Late native report'))
                    owner._message('Late native message')
                    owner.go_to()
                    owner.add_bookmark()
                    owner.help()
                elif kind == 'audio':
                    owner.report('Late native report', focus=True)
                    owner._error(RuntimeError('Late completion'))
                    owner.tick()
                    owner.select()
                    owner.pause()
                    owner.resume()
                elif kind == 'pronunciation':
                    _, done, failed = pending[0]
                    done(((), None))
                    failed(RuntimeError('Late review failure'))
                    owner.report('Late review report')
                    owner.controls()
                    owner.on_preview()
                    owner.on_apply()
                elif kind == 'languages':
                    _, _, done, failed = pending[0]
                    done(language_matrix.InstalledVoiceInventory((), (), 'Synthetic lifetime completion'))
                    failed(RuntimeError('Late inventory failure'))
                    owner.filter()
                    owner.select()
                    owner.refresh()
                    owner.cancel()
                    check('canonical entries remain exact', len(owner.controller.catalog.entries) == 135)
                elif kind == 'find-replace':
                    _, done, failed = pending[0]
                    done(find_result)
                    failed(RuntimeError('Late search failure'))
                    owner.report('Late search report')
                    owner.controls()
                    owner.on_find()
                    owner.on_apply()
                    check('late search cannot change its source', document.text == snapshot.text)
                else:
                    owner.refresh()
                    owner.selection_changed()
                    owner.accept()
                    owner.report('Late command report', focus=True)
                check('queued late callback really executed without native access failure', True)
                check('no result or command was accepted after close', not accepted)
                if kind == 'audio':
                    check('late owned result was discarded exactly once', discarded == ['owned synthetic result'])
                check('exactly one real owner destruction event', len(destroyed) == 1)
                check('native wrapper is actually deleted', not window)
                check('repeat Python Destroy is harmless', window.Destroy() is False)
                events.append({'case': current['case'], 'native_events': destroyed})
                host.Destroy()
                until(lambda: not host, next_case)

            def deleted():
                check('owner detached after actual native destruction', detached())
                if kind in ('reading', 'audio'):
                    check('owned worker was cancelled', owner.cancelled.is_set())
                    release.set()
                    def drained():
                        owner.executor.shutdown(wait=True, cancel_futures=True)
                        wx.CallAfter(replay)
                    until(completed.is_set, drained)
                else:
                    check('owned cancellation matches pending work',
                          host.cancel_event.is_set() == (kind in ('pronunciation', 'find-replace', 'languages')))
                    wx.CallAfter(replay)

            def destroy_owner():
                if termination == 'direct':
                    window.Destroy()
                    check('direct Destroy detaches synchronously', detached())
                else:
                    host.DestroyChildren()
                until(lambda: not window and len(destroyed) == 1, deleted)

            def destroy_child():
                child = wx.Panel(window)
                child.Destroy()
                def child_deleted():
                    check('child deletion keeps owner live', not detached() and not destroyed)
                    later(destroy_owner)
                until(lambda: not child, child_deleted)

            until(entered.is_set, destroy_child)

        try:
            later(next_case)
            gui.MainLoop()
            check('all fourteen direct and parent cases completed', finished and len(events) == 14 and not failures)
        finally:
            for release in releases:
                release.set()
            for kind, owner in owners:
                if kind in ('reading', 'audio'):
                    owner.dispose()
                    owner.executor.shutdown(wait=True, cancel_futures=True)
                    if kind == 'reading':
                        future = owner.live.core.close()
                        if future is not None:
                            future.result(5)
                elif kind == 'languages':
                    owner.shutdown()
                else:
                    owner.dispose()
            if restore_registry:
                runtime_settings.voice_registry = restore_registry[0]
            for host in hosts:
                if host:
                    host.Destroy()
            if anchor:
                anchor.Destroy()
            gui.Yield()
        if failures:
            raise RuntimeError('\n'.join(failures))
    print(json.dumps({'status': 'PASS', 'source_identity': source_identity, 'checks': checks,
        'native_destruction_events': events, 'late_callbacks': 'PASS',
        'native_keyboard': 'NOT_RUN', 'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN',
        'audio_quality': 'NOT_ASSESSED', 'language_coverage': 'NOT_ASSESSED'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
