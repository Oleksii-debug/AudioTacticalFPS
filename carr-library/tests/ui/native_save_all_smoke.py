"""Installed Windows Save All and close boundaries with real wx controls/stores.

Only synthetic source text is used. This is native event/storage evidence;
screen-reader speech, power failure and device playback are not qualified here.
"""
from argparse import ArgumentParser
from contextlib import contextmanager
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic
import traceback


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    if not args.native or os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Explicit native Windows run required.',
                          'nvda': 'NOT_RUN'}), flush=True)
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.reader.narration import NarrationStore, new_project
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.narration_editor import open_narration_editor
    from carr_library.ui import productivity, save_all as coordinator_module

    checks, failures = [], []
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False

    gui = ProbeApp(False)
    native_loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(native_loop)
    check('real Windows native controls', wx.Platform == '__WXMSW__')
    check('save implementation loaded from installed package', all(
        'site-packages' in [part.casefold() for part in Path(module.__file__).parts]
        for module in (productivity, coordinator_module)))

    def pump(predicate, seconds=12):
        deadline = monotonic() + seconds
        while not predicate():
            if failures:
                raise AssertionError('\n'.join(failures))
            if monotonic() >= deadline:
                raise TimeoutError('Native Save All boundary did not complete in time.')
            gui.Yield()
            wx.MilliSleep(10)

    def click(button):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        check('native button handler: ' + button.GetLabel(),
              button.GetEventHandler().ProcessEvent(event))

    with TemporaryDirectory(prefix='carr-save-all-native-') as temporary:
        library = LibraryApp(Path(temporary) / 'library')
        workspace = library.create_workspace('Save All synthetic library')
        topic = library.create_topic(workspace, 'Synthetic notes')
        imported = library.add_text(workspace, 'Immutable source', 'Exact source — Україна.')
        hit, = library.search(workspace, 'Exact')
        library.save_hit(workspace, topic, hit.ref, hit.revision, 'Baseline note')
        source_bytes = library.assets.resolve(imported['asset']).read_bytes()
        frame = DesktopFrame(library)
        gui.SetTopWindow(frame)
        frame.Show()
        releases, timers = [], []
        try:
            pump(lambda: not frame.working)
            frame._open_workspace(workspace, topic)
            pump(lambda: not frame.working and frame.evidence is not None
                 and frame.evidence.displayed is not None)
            panel, coordinator = frame.evidence, frame._save_coordinator
            real_transaction = panel.transaction
            store = NarrationStore(library.root)

            for fail_commit in (False, True):
                entered, release = Event(), Event()
                releases.append(release)
                previous_note = library.evidence(workspace, topic)[0].note
                wanted_note = 'Failed note remains editable' if fail_commit else 'Committed native note — 🚂'
                panel.note.SetValue(wanted_note)
                reading = new_project('Immutable reading original', title='Native Save All')
                state = {'alive': True, 'phase': 0}
                deadline = monotonic() + 12

                @contextmanager
                def transaction():
                    entered.set()
                    if not release.wait(8):
                        raise TimeoutError('Synthetic write gate was not released.')
                    with real_transaction() as service:
                        yield service
                        if fail_commit:
                            raise OSError('Synthetic transaction commit failure')
                panel.transaction = transaction

                def ready(dialog, controller, controls, *, state=state, fail_commit=fail_commit,
                          entered=entered, release=release, previous_note=previous_note,
                          wanted_note=wanted_note, reading=reading, deadline=deadline):
                    def fail():
                        release.set()
                        if state['alive'] and dialog.IsModal():
                            dialog.EndModal(wx.ID_CANCEL)

                    def poll():
                        if not state['alive']:
                            return
                        try:
                            if monotonic() >= deadline:
                                raise TimeoutError('Native modal save/close did not finish.')
                            if state['phase'] == 0 and entered.is_set():
                                check('Save All waits for the real note transaction', coordinator.busy)
                                check('note is uncommitted while the worker is held',
                                      library.evidence(workspace, topic)[0].note == previous_note)
                                click(controls['close'])
                                check('native Close keeps the editor open during save',
                                      dialog.IsModal() and getattr(dialog, '_save_close_pending', False))
                                release.set()
                                state['phase'] = 1
                            elif state['phase'] == 1 and not coordinator.busy and fail_commit:
                                check('failed note commit cancels the pending close',
                                      dialog.IsModal() and not getattr(dialog, '_save_close_pending', False))
                                check('failed note remains editable and baseline is preserved',
                                      panel.note.GetValue() == wanted_note and panel._dirty()
                                      and library.evidence(workspace, topic)[0].note == previous_note)
                                check('another valid reading draft still saves after the failure',
                                      store.load(reading.id)[0].current_text == 'Native reading derivative')
                                state['phase'] = 2
                                dialog.Close()
                                return
                            timers.append(wx.CallLater(25, poll))
                        except Exception:
                            failures.append(traceback.format_exc())
                            fail()

                    try:
                        check('reading editor shares the main Save All coordinator',
                              dialog._save_coordinator is coordinator)
                        controls['editor'].SetValue('Native reading derivative')
                        click(controls['save_all'])
                        timers.append(wx.CallLater(25, poll))
                    except Exception:
                        failures.append(traceback.format_exc())
                        fail()

                result = open_narration_editor(frame, store, reading, on_ready=ready)
                state['alive'] = False
                release.set()
                pump(lambda: not frame.working)
                if failures:
                    raise AssertionError('\n'.join(failures))
                check('reading draft survives the complete native event flow',
                      result.current_text == 'Native reading derivative'
                      and store.load(reading.id)[0].current_text == result.current_text)
                if not fail_commit:
                    check('successful pending close follows both durable writes',
                          library.evidence(workspace, topic)[0].note == wanted_note
                          and not coordinator.busy)
                check('closed reading editor unregisters its save participant',
                      set(coordinator.participants) == {'research.note'})
                panel.transaction = real_transaction

            # The main Exit/Close path must also wait, without blocking its UI thread.
            entered, release = Event(), Event()
            releases.append(release)
            @contextmanager
            def final_transaction():
                entered.set()
                if not release.wait(8):
                    raise TimeoutError('Synthetic main-close write was not released.')
                with real_transaction() as service:
                    yield service
            panel.transaction = final_transaction
            panel.note.SetValue('Final note before native main-window close')
            file_menu = frame.menu_bar.GetMenu(0)
            item = next(item for item in file_menu.GetMenuItems()
                        if 'Save &all changes' in item.GetItemLabel())
            event = wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId())
            check('existing File Save All command reaches the native handler',
                  frame.GetEventHandler().ProcessEvent(event))
            pump(entered.is_set)
            frame.Close()
            check('native main close is deferred during the write',
                  not frame._closing and getattr(frame, '_save_close_pending', False))
            release.set()
            pump(lambda: frame._closing)
            check('main close occurs after the note is committed',
                  library.evidence(workspace, topic)[0].note == 'Final note before native main-window close')
            check('all save operations preserve immutable retained source bytes',
                  library.assets.resolve(imported['asset']).read_bytes() == source_bytes)
            check('no native callback failed', not failures)
            print(json.dumps({'status': 'PASS', 'checks': len(checks), 'wx': wx.version(),
                              'native_events': 'PASS', 'durable_stores': 'PASS',
                              'deferred_close': 'PASS', 'nvda': 'NOT_RUN'}, sort_keys=True), flush=True)
        finally:
            for release in releases:
                release.set()
            for timer in timers:
                timer.Stop()
            frame._closing = True
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            if frame:
                frame.Destroy()
            gui.Yield()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
