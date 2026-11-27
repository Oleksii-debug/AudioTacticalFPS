"""Installed native book batch order, prepare, receipt reuse and close settlement.

All text and PCM are synthetic public fixtures. No voice quality, model
qualification or NVDA acceptance is claimed.
"""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic
import traceback
import wave


def run_native():
    from carr_library.native_runtime import activate_bundled_runtime
    runtime_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio.book_audio import BookAudioService
    from carr_library.audio.book_plan import SplitOptions, capture_plan
    from carr_library.audio.models import Role
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui import book_batch_dialog
    errors, checks = [], []
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc()[-2000:])
            self.ExitMainLoop()
            return False

    class SyntheticEngine:
        def __init__(self):
            self.calls = []
        def cache_identity(self, voice):
            return {'fixture': 'native-book-batch-tone-1', 'voice': voice}
        def synthesize(self, text, output, **options):
            self.calls.append(text)
            with wave.open(str(output), 'wb') as stream:
                stream.setparams((1, 2, 8000, 800, 'NONE', 'not compressed'))
                stream.writeframes(b'\x00\x00' * 800)
            return output

    gui = ProbeApp(False)
    check('actual Windows wx', wx.Platform == '__WXMSW__')
    check('installed product module', 'site-packages' in
          [part.casefold() for part in Path(book_batch_dialog.__file__).parts])
    timer, frame, view = None, None, None
    with TemporaryDirectory(prefix='resonance-book-batch-') as folder:
        root = Path(folder)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Книги ü')
        editions = [library.add_text(workspace, title, text, authors=('Автор',), language='uk')
                    for title, text in [('Перша — čítanie', '[sound not-a-command.wav] Перша.'),
                                        ('Друга — ü', 'Друга книжка.')]]
        plans = [capture_plan(library, workspace, item['edition'], options=SplitOptions(),
                    role=Role('Narrator', voice='fixture:uk', language='uk'), sample_rate=8000)
                 for item in editions]
        originals = {path.name: sha256(path.read_bytes()).hexdigest() for path in library.assets_dir.iterdir()}
        output = root / 'export'
        output.mkdir()
        engine = SyntheticEngine()
        books = BookAudioService(library, workspace, engine=engine)
        frame = DesktopFrame(library)
        gui.SetTopWindow(frame)
        frame.Show()
        actions = book_batch_dialog.install_book_batch_actions(frame)
        check('shared Audio command registration is idempotent',
              book_batch_dialog.install_book_batch_actions(frame) is actions)
        view = book_batch_dialog.BookBatchDialog(frame, service=books)
        for plan in plans:
            view.state.add(plan)
        view.state.output(output)
        view.render(plans[0].id)
        view.show()
        check('explicit voice starts unselected', view.voice.GetSelection() == wx.NOT_FOUND)
        check('review field is real named readonly text', isinstance(view.report_box, wx.TextCtrl)
              and not view.report_box.IsEditable() and bool(view.report_box.GetName()))
        check('Unicode retained titles in native list', 'Перша — čítanie' in view.selected.GetString(0))
        deadline = monotonic() + 90
        state = {'phase': 0, 'pressed': False, 'active': True}
        hold_started, hold_release = Event(), Event()
        original_status = ['']
        native_handle = view.dialog.GetHandle()

        def press(key):
            button = view.buttons[key]
            check('enabled owned button ' + key, button.IsEnabled() and button.GetParent() is view.body)
            check('focused native button ' + key, wx.Window.FindFocus() is button)
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            check('native event dispatched ' + key, button.GetEventHandler().ProcessEvent(event))

        def press_next(key):
            if not state['pressed']:
                view.buttons[key].SetFocus()
                state['pressed'] = True
                return False
            press(key)
            state['pressed'] = False
            return True

        def ready():
            return not view.busy and not frame.working

        def poll(event=None):
            if not state['active']:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Native multi-book acceptance did not settle within its finite deadline.')
                phase = state['phase']
                if phase == 0:
                    if not press_next('down'):
                        return
                    check('order is exact and selected identity retained',
                          [row['plan'].id for row in view.state.rows] == [plans[1].id, plans[0].id]
                          and view.selection() == plans[0].id)
                    state['phase'] = 1
                elif phase == 1:
                    if press_next('preview'):
                        state['phase'] = 2
                elif phase == 2 and ready():
                    check('native preview complete without synthesis', view.state.reviewed is not None
                          and not view.state.reviewed.prepared and not engine.calls)
                    if press_next('prepare'):
                        state['phase'] = 3
                elif phase == 3 and ready():
                    check('native prepare creates current managed projects only', view.state.reviewed.prepared
                          and len(list((library.root / 'audio-projects').glob('*/studio/project.json'))) == 2
                          and not engine.calls and not list(output.iterdir()))
                    if press_next('run'):
                        state['phase'] = 4
                elif phase == 4 and ready():
                    check('actual native Run returned accepted independent exports',
                          len(engine.calls) == 2 and len(list(output.glob('*/complete.json'))) == 2)
                    if press_next('run'):
                        state['phase'] = 5
                elif phase == 5 and ready():
                    check('native resume verifies existing production without new synthesis', len(engine.calls) == 2)
                    def held():
                        hold_started.set()
                        if not hold_release.wait(8):
                            raise TimeoutError('Probe cleanup hold expired.')
                        return 'held worker settled'
                    view.begin('Native close-settlement fixture', held, lambda value: None)
                    state['phase'] = 6
                elif phase == 6 and hold_started.is_set():
                    if not press_next('close'):
                        return
                    check('close waits for actual worker acknowledgement', view.alive and view.closing and view.busy)
                    hold_release.set()
                    state['phase'] = 7
                elif phase == 7 and not view.alive and not frame.working:
                    import ctypes
                    api = ctypes.WinDLL('user32', use_last_error=True)
                    api.IsWindow.argtypes = [ctypes.c_void_p]
                    api.IsWindow.restype = ctypes.c_int
                    if api.IsWindow(native_handle):
                        return
                    check('actual native book window destroyed', True)
                    check('immutable retained original bytes unchanged',
                          originals == {path.name: sha256(path.read_bytes()).hexdigest()
                                        for path in library.assets_dir.iterdir()})
                    state['active'] = False
                    timer.Stop()
                    gui.ExitMainLoop()
            except BaseException:
                errors.append(traceback.format_exc()[-2000:])
                hold_release.set()
                state['active'] = False
                timer.Stop()
                gui.ExitMainLoop()
        timer = wx.Timer(frame)
        frame.Bind(wx.EVT_TIMER, poll, timer)
        timer.Start(40)
        try:
            gui.MainLoop()
            if errors:
                raise AssertionError(errors[-1])
            check('complete native state sequence', state['phase'] == 7 and not state['active'])
            print(json.dumps({'status': 'PASS', 'checks': len(checks), 'native_order': 'PASS',
                'native_prepare': 'PASS', 'actual_ffmpeg_receipt_reuse': 'PASS',
                'native_close_settlement': 'PASS', 'voice_quality': 'NOT_QUALIFIED', 'nvda': 'NOT_RUN'}), flush=True)
            return 0
        finally:
            state['active'] = False
            hold_release.set()
            if timer is not None:
                timer.Stop()
            if view is not None:
                view.cancel.set()
            if frame is not None:
                frame.cancel_event.set()
                frame.executor.shutdown(wait=True, cancel_futures=True)
                if view is not None and view.alive:
                    view.dispose()
                frame._closing = True
                progress = getattr(frame, 'operation_progress', None)
                if progress is not None:
                    progress.close()
                frame.Destroy()
            gui.Yield()


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    if not args.native or os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Explicit installed Windows execution required.'}), flush=True)
        return 77
    finished = Event()
    def watchdog():
        if not finished.wait(120):
            print(json.dumps({'status': 'FAIL', 'reason': 'Native book probe exceeded 120 seconds.'}), flush=True)
            os._exit(1)
    Thread(target=watchdog, daemon=True, name='book-native-watchdog').start()
    try:
        return run_native()
    except BaseException as error:
        print(json.dumps({'status': 'FAIL', 'error': type(error).__name__ + ': ' + str(error)[:800]}), flush=True)
        return 1
    finally:
        finished.set()


if __name__ == '__main__':
    raise SystemExit(main())
