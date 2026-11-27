"""Installed Windows Save All acceptance with real wx, SQLite and project files.

An observation gate delays delivery of one real worker result after its commit;
it does not replace a service or fabricate a result. Native menu/button events
exercise the registered command and modal studio. No audio is synthesized.
Run: python -I -B -X utf8 tests/ui/native_save_all_smoke.py
Unavailable Windows/wx returns77. Native CI requires0; NVDA remains NOT_RUN.
"""
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from time import monotonic
import traceback


def main():
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Requires native Windows x64 wx.', 'nvda': 'NOT_RUN'}))
        return 77
    try:
        import wx
    except (ImportError, OSError) as error:
        print(json.dumps({'status': 'NOT_RUN', 'reason': str(error), 'nvda': 'NOT_RUN'}))
        return 77
    import carr_library.ui.desktop_app as desktop_module
    import carr_library.ui.audio_studio as studio_module
    import carr_library.ui.save_all as save_module
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.audio_studio import AudioStudioDialog, StudioController

    checks, failures, worker_threads, reports = [], [], [], []
    gui_thread = threading.get_ident()
    committed, release, armed = threading.Event(), threading.Event(), threading.Event()
    armed.set()
    state = {'frame': None, 'view': None}
    timers = []
    deadline = monotonic() + 45

    def hard_timeout():
        print(json.dumps({'status': 'FAIL', 'errors': ['Save All native probe exceeded 60 seconds.'],
                          'nvda': 'NOT_RUN'}), flush=True)
        os._exit(1)

    watchdog = threading.Timer(60, hard_timeout)
    watchdog.daemon = True
    watchdog.start()

    def abort(message):
        failures.append(message)
        release.set()
        view = state['view']
        if view is not None and view.dialog.IsModal():
            view.dialog.EndModal(wx.ID_CANCEL)

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return False

    app = App(False)

    def healthy():
        if failures:
            raise AssertionError('; '.join(failures))
        if monotonic() >= deadline:
            raise TimeoutError('Save All native probe exceeded 45 seconds.')

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)

    def pump(predicate):
        while True:
            app.Yield()
            healthy()
            if predicate():
                return
            wx.MilliSleep(10)

    def later(action):
        def guarded():
            try:
                healthy()
                action()
            except BaseException:
                abort(traceback.format_exc())
        timers.append(wx.CallLater(25, guarded))

    def descendants(window):
        for child in window.GetChildren():
            yield child
            yield from descendants(child)

    def click(button):
        check('native Save All button is enabled', isinstance(button, wx.Button) and button.IsEnabled())
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        check('native Save All button event handled', bool(button.GetEventHandler().ProcessEvent(event)))

    class ObservedFrame(DesktopFrame):
        def report(self, message):
            check('status stays on the GUI thread', threading.get_ident() == gui_thread)
            reports.append(str(message))
            return super().report(message)

        def run_job(self, title, operation, done=None, failed=None):
            def observed():
                if title == 'Saving research note':
                    worker_threads.append(threading.get_ident())
                result = operation()
                if title == 'Saving research note' and armed.is_set():
                    armed.clear()
                    committed.set()
                    if not release.wait(8):
                        raise TimeoutError('The GUI did not release the committed result.')
                return result
            return super().run_job(title, observed, done, failed)

    try:
        checkout = Path(__file__).resolve().parents[2]
        check('tested modules come from installed package', all(
            not Path(module.__file__).resolve().is_relative_to(checkout)
            for module in (desktop_module, studio_module, save_module)))
        with TemporaryDirectory(prefix='CARR Save All Україна ') as folder:
            root = Path(folder)
            library = LibraryApp(root / 'private-library')
            workspace = library.create_workspace('Save All native acceptance')
            topic = library.create_topic(workspace, 'Research')
            source = 'Original exact passage remains unchanged.'
            library.add_text(workspace, 'Synthetic source', source)
            hit, = library.search(workspace, 'exact')
            library.save_hit(workspace, topic, hit.ref, hit.revision, 'Original saved note')
            frame = ObservedFrame(library)
            state['frame'] = frame
            frame.Show()
            pump(lambda: not frame.working)
            frame._open_workspace(workspace, topic)
            pump(lambda: not frame.working and frame.evidence is not None and frame.evidence.displayed is not None)
            panel = frame.evidence
            panel.note.SetValue('First native Save All snapshot')
            frame.keyboard_commands.apply_overrides({'file.save_all': 'Ctrl+Alt+F12'})
            binding = frame.keyboard_commands.bindings['file.save_all']
            check('personalized Save All menu and F1 guide agree',
                  'Ctrl+Alt+F12' in binding.item.GetItemLabel()
                  and 'Ctrl+Alt+F12: Save all open drafts' in frame.keyboard_commands.help_text())
            check('File command dispatches actual native menu event', frame.keyboard_commands.invoke('file.save_all'))
            pump(committed.is_set)
            check('database commit precedes completion delivery',
                  library.evidence(workspace, topic)[0].note == 'First native Save All snapshot'
                  and panel.displayed.note == 'Original saved note')
            check('Save All does not announce early success', frame.save_coordinator.busy
                  and frame.last_save_all_summary is None
                  and not any('open drafts saved' in text for text in reports))
            release.set()
            pump(lambda: not frame.save_coordinator.busy)
            check('final note baseline and success follow completion',
                  frame.last_save_all_summary.complete and not panel._dirty()
                  and panel.displayed.note == 'First native Save All snapshot')
            check('note writes use the library worker', worker_threads and all(t != gui_thread for t in worker_threads))

            controller = StudioController(private_root=library.root)
            controller.new('Immutable audio original.')
            project = root / 'audio-project.json'
            controller.save(project)
            panel.note.SetValue('Note saved from modal Audio Studio')
            view = AudioStudioDialog(frame, controller)
            state['view'] = view
            view.script.SetValue('Explicit audio reading-copy edit.')
            button = next(child for child in descendants(view.dialog)
                          if isinstance(child, wx.Button) and child.GetName() == 'Save all open drafts')
            observed = {}

            def begin_modal_save():
                if view.busy:
                    later(begin_modal_save)
                    return
                check('modal Studio registers beside live research note', len(frame.save_coordinator.participants) == 2)
                click(button)
                later(check_modal_save)

            def check_modal_save():
                if frame.save_coordinator.busy:
                    later(check_modal_save)
                    return
                summary = frame.last_save_all_summary
                check('modal Save All confirms both durable participants', summary.complete and len(summary.saved) == 2)
                check('modal note is committed', library.evidence(workspace, topic)[0].note == 'Note saved from modal Audio Studio')
                check('studio JSON contains edited copy and immutable source',
                      not view.editing_dirty and not controller.dirty
                      and controller.script == 'Explicit audio reading-copy edit.'
                      and controller.draft.source_text == 'Immutable audio original.')
                reopened = StudioController(private_root=library.root)
                reopened.open(project)
                check('saved Studio project reopens with exact copy', reopened.script == controller.script)
                observed['good'] = project.read_bytes()
                observed['external'] = observed['good'] + b'\n'
                project.write_bytes(observed['external'])
                panel.note.SetValue('Other participant survives project conflict')
                view.script.SetValue('Unsaved Studio edit must survive conflict.')
                click(button)
                later(check_conflict)

            def check_conflict():
                if frame.save_coordinator.busy:
                    later(check_conflict)
                    return
                summary = frame.last_save_all_summary
                check('project conflict produces an incomplete final summary',
                      not summary.complete and any(label == 'Audio Studio project' for label, _ in summary.remaining))
                check('other note still saves after project conflict',
                      library.evidence(workspace, topic)[0].note == 'Other participant survives project conflict')
                check('conflicted project and unsaved edit are retained',
                      project.read_bytes() == observed['external'] and controller.dirty and view.editing_dirty
                      and controller.script == 'Unsaved Studio edit must survive conflict.')
                project.write_bytes(observed['good'])
                check('explicit project save recovers after external conflict is resolved', view.save() is True)
                view.close()

            later(begin_modal_save)
            view.show()
            state['view'] = None
            healthy()
            check('closing Studio unregisters its save participant', len(frame.save_coordinator.participants) == 1)
            check('original library quotation remains unchanged', library.search(workspace, 'exact')[0].quote == hit.quote)
            frame.Close()
            app.Yield()
            state['frame'] = None
    except BaseException:
        failures.append(traceback.format_exc())
    finally:
        release.set()
        watchdog.cancel()
        for timer in timers:
            timer.Stop()
        frame = state['frame']
        if frame is not None:
            frame._closing = True
            frame.cancel_event.set()
            frame.executor.shutdown(wait=False, cancel_futures=True)
            frame.Destroy()
    print(json.dumps({'status': 'FAIL' if failures else 'PASS', 'checks': len(checks),
                      'cases': list(dict.fromkeys(checks)), 'errors': failures,
                      'windows_native': 'FAIL' if failures else 'PASS', 'nvda': 'NOT_RUN'}, ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
