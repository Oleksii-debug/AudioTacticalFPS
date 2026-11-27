"""Installed native queue restore, Save All and durable close; no audio output."""
from argparse import ArgumentParser
from hashlib import sha256
import json
import os
from pathlib import Path
import sysconfig
import tempfile
from threading import Event
from time import monotonic
import traceback
import wave


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not args.native or os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Explicit native Windows run required.'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.application import LibraryApp, open_existing_library
    from carr_library.audio import playlist
    from carr_library.audio.playback_processing import PlaybackControls
    from carr_library.library_backup import create_backup, restore_backup, verify_backup
    from carr_library.ui import playback_queue
    from carr_library.ui.audio_studio import AudioStudioDialog, StudioController

    checks, failures = [], []
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            return False

    gui = ProbeApp(False)
    loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(loop)
    def pump(condition, seconds=15):
        deadline = monotonic() + seconds
        while not condition():
            if failures:
                raise AssertionError('\n'.join(failures))
            if monotonic() >= deadline:
                raise TimeoutError('Native queue operation exceeded its bounded acceptance deadline.')
            gui.Yield()
            wx.MilliSleep(10)

    def descendants(window):
        for child in window.GetChildren():
            yield child
            yield from descendants(child)

    def button(window, name):
        values = [item for item in descendants(window)
                  if isinstance(item, wx.Button) and item.GetName() == name]
        if len(values) != 1:
            raise AssertionError('Expected one named native action: ' + name)
        return values[0]

    def press(control):
        if not control.IsEnabled():
            raise AssertionError('Required native queue button is disabled.')
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        if not control.GetEventHandler().ProcessEvent(event):
            raise AssertionError('Native queue button was not handled.')

    installed = Path(sysconfig.get_paths()['purelib']).resolve()
    check('exact installed queue core and native handlers', all(
        installed in Path(module.__file__).resolve().parents for module in (playlist, playback_queue)))
    check('actual Windows wx controls', wx.Platform == '__WXMSW__')
    entered, release = Event(), Event()
    window = frame = None
    with tempfile.TemporaryDirectory(prefix='carr-native-managed-queue-') as temporary:
        root = Path(temporary)
        app = LibraryApp(root / 'library')
        controller = StudioController(private_root=app.root)
        controller.new('Original native queue narration — Україна.', title='Native queue fixture')
        controller.save_in_library()
        source = root / 'Україна — recording.wav'
        with wave.open(str(source), 'wb') as audio:
            audio.setparams((1, 2, 8000, 8000, 'NONE', 'PCM'))
            audio.writeframes(b'\x20\x00' * 8000)
        original = sha256(source.read_bytes()).hexdigest()
        store = playlist.PlaylistStore(app.root, controller.project_id)
        saved = store.append([source])
        # Explicit synthetic retained cursor fixture. This is not observed
        # native audio-position or physical-output acceptance.
        store.checkpoint(saved, index=0, frame=4000, accuracy='completed_buffers')
        frame = wx.Frame(None, title='Native retained queue acceptance')
        frame.library = app
        frame.Show()
        gui.SetTopWindow(frame)
        try:
            window = AudioStudioDialog(frame, controller)
            window.dialog.Show()
            pump(lambda: window.dialog.IsShown())
            check('three explicit native persistence actions', all(
                isinstance(button(window.dialog, label), wx.Button)
                for label in ('Save queue and position', 'Restore saved queue', 'Resume saved position')))
            press(button(window.dialog, 'Restore saved queue'))
            pump(lambda: not window.busy and window.queue_state.review is not None)
            queue = window.queue_state
            check('restore installs retained relative queue without autoplay',
                  window.playback.status().state == 'stopped'
                  and window.playback.playlist == queue.store.paths(queue.review)
                  and queue.cursor == (0, 4000, 'completed_buffers'))
            check('current output remains an explicit choice',
                  window.playback_device.GetSelection() == 0
                  and window.playback.sleep_remaining_seconds is None)
            queue.controls = PlaybackControls(1.25, 2, .6, -.3)
            press(button(window.dialog, 'Save queue and position'))
            pump(lambda: not window.busy and not queue.dirty())
            check('native save commits typed independent playback preferences',
                  queue.store.review().state.controls == queue.controls)
            queue.cursor = (0, 6000, 'completed_buffers')
            actual_checkpoint = queue.store.checkpoint
            before = queue.store.path.read_bytes()
            def delayed(*arguments, **kwargs):
                entered.set()
                if not release.wait(10):
                    raise TimeoutError('Synthetic durability gate was not released.')
                return actual_checkpoint(*arguments, **kwargs)
            queue.store.checkpoint = delayed
            press(button(window.dialog, 'Save all open drafts'))
            pump(entered.is_set)
            check('Save All waits for actual durable queue completion',
                  window._save_coordinator.busy and queue.store.path.read_bytes() == before)
            window.dialog.Close()
            check('native close waits for the current Save All operation',
                  not window.closed and window._save_coordinator.busy)
            release.set()
            pump(lambda: window.closed)
            check('close follows the actual committed cursor',
                  queue.store.review().state.frame == 6000)
            check('original selected WAV and narration remain unchanged',
                  sha256(source.read_bytes()).hexdigest() == original
                  and controller.draft.source_text == 'Original native queue narration — Україна.')
            digest = create_backup(app, root / 'backup')
            verify_backup(root / 'backup', digest)
            target = restore_backup(root / 'backup', root / 'restored', digest)
            restored = open_existing_library(target)
            retained = playlist.PlaylistStore(restored.root, controller.project_id).review()
            check('complete backup restores queue and cursor under a different root',
                  retained.state.frame == 6000 and retained.state.controls == queue.controls)
            check('all native callbacks settled without exception', not failures)
            report = {'status': 'PASS', 'checks': checks, 'wx': wx.version(),
                      'native_events': 'PASS', 'durable_queue': 'PASS',
                      'cursor_fixture': 'synthetic_saved_frames',
                      'audio_output': 'NOT_RUN', 'physical_audio': 'NOT_RUN', 'nvda': 'NOT_RUN'}
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            print(json.dumps(report), flush=True)
        finally:
            release.set()
            if window is not None:
                window.cancelled.set()
                window.executor.shutdown(wait=True, cancel_futures=True)
                window.playback.close()
                window.timer.Stop()
                window.playback_timer.Stop()
                if not window.closed:
                    window.close()
                window.dialog.Destroy()
            if frame:
                frame.Destroy()
            gui.Yield()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
