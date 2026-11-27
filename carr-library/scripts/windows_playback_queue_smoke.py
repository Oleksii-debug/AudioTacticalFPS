"""Actual installed native queue controls and passive backup; no device claim."""
from argparse import ArgumentParser
from contextlib import ExitStack
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
import traceback
import wave

MODULES = (
    'carr_library.audio.playlist', 'carr_library.audio.playlist_session',
    'carr_library.audio.player', 'carr_library.audio.playback',
    'carr_library.audio.jobs', 'carr_library.audio.ffmpeg',
    'carr_library.ui.playback_queue', 'carr_library.ui.save_all',
    'carr_library.library_backup', 'carr_library.durable_state',
    'carr_library.operations.broker', 'carr_library.backup_files',
)


def identity():
    if not sys.flags.isolated:
        raise ValueError('Use the installed isolated interpreter.')
    prefix, result = Path(sys.prefix).resolve(strict=True), {}
    for name in MODULES:
        path = Path(importlib.import_module(name).__file__).resolve(strict=True)
        if not path.is_relative_to(prefix) or 'site-packages' not in {p.casefold() for p in path.parts}:
            raise ValueError('A retained queue implementation escaped the installed wheel.')
        result[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    return result


def pcm(path, value, *, rf64=False):
    raw = struct.pack('<h', value) * 8000
    if rf64:
        path.write_bytes(b'RF64' + struct.pack('<I', 0xffffffff) + b'WAVE' +
            b'ds64' + struct.pack('<IQQQI', 28, len(raw) + 72, len(raw), 8000, 0) +
            b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, 8000, 16000, 2, 16) +
            b'data' + struct.pack('<I', 0xffffffff) + raw)
    else:
        with wave.open(str(path), 'wb') as stream:
            stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
            stream.writeframes(raw)
    return path


def run_native():
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio.playlist import PlaylistStore, SavedPlaylist
    from carr_library.audio.playlist_session import PlaylistSession
    from carr_library.audio.player import WavePlayer
    from carr_library.library_backup import create_backup, verify_backup, restore_backup
    from carr_library.ui.playback_queue import PlaybackQueueDialog
    from carr_library.ui.save_all import SaveCoordinator

    checks, failures, timers = [], [], []
    state = {'window': None, 'phase': 'capture', 'active': True, 'done': False}
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)
    def failed():
        failures.append(traceback.format_exc())
        state['active'] = False
        window = state['window']
        if window is not None and not window.closed:
            # Synthetic probe cleanup only: no unsaved user document exists.
            window.session.baseline = window.session.value
            window.session.confirmation_pending = False
            window.close()

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            failed()
            return False

    application = App(False)
    loop = wx.GUIEventLoop()
    activation = wx.EventLoopActivator(loop)
    check('actual Windows native controls', wx.Platform == '__WXMSW__')
    check('installed retained queue modules', 'site-packages' in {
        part.casefold() for part in Path(importlib.import_module('carr_library.audio.playlist').__file__).parts})
    with TemporaryDirectory(prefix='carr-native-retained-queue-') as directory:
        root = Path(directory).resolve()
        library = LibraryApp(root / 'library')
        library.create_workspace('Public synthetic retained queue')
        sources = (pcm(root / 'first.wav', 123), pcm(root / 'second.rf64', 456, rf64=True))
        original = tuple(path.read_bytes() for path in sources)
        original_hashes = [sha256(raw).hexdigest() for raw in original]
        frame = wx.Frame(None, title='Retained playback queue qualification')
        frame.library, frame._closing, frame.working = library, False, False
        frame._save_coordinator = SaveCoordinator()
        frame.Show()

        def press(button):
            check('native button enabled and visible', button.IsEnabled() and button.IsShownOnScreen())
            event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
            event.SetEventObject(button)
            check('native button event handled', button.GetEventHandler().ProcessEvent(event))

        def schedule():
            if state['active']:
                timers.append(wx.CallLater(30, tick))

        def tick():
            if not state['active']:
                return
            window, phase = state['window'], state['phase']
            try:
                if window is None or window.future is not None:
                    schedule()
                    return
                if phase == 'capture':
                    check('initial queue opens without media or device',
                          not window.session.value.items and window.player.path is None
                          and window.session.admission is None and window.player.native is not None)
                    check('native queue has a readable accessible name',
                          window.queue.GetName() == 'Saved playback queue order and selection')
                    state['phase'] = 'captured'
                    press(window.controls['studio'])
                elif phase == 'captured':
                    rows = window.session.items
                    check('selected originals retained independently', len(rows) == 2
                          and all(row.path != source and row.path.stat().st_nlink == 1
                                  and (row.path.stat().st_dev, row.path.stat().st_ino)
                                      != (source.stat().st_dev, source.stat().st_ino)
                                  for row, source in zip(rows, sources)))
                    check('retained PCM matches exact selected bytes',
                          [row.path.read_bytes() for row in rows] == list(original)
                          and [row.sha256 for row in rows] == original_hashes)
                    check('captured queue remains an unsaved passive draft',
                          window.session.dirty and window.player.path is None)
                    check('native queue displays both retained labels',
                          window.queue.GetCount() == 2 and 'second.rf64' in window.queue.GetString(1))
                    window.queue.SetSelection(1)
                    event = wx.CommandEvent(wx.EVT_LISTBOX.typeId, window.queue.GetId())
                    event.SetEventObject(window.queue)
                    check('native selection event handled', window.queue.GetEventHandler().ProcessEvent(event))
                    check('native selection chooses exact queue member', window.session.value.index == 1)
                    window.seconds.ChangeValue('.375')
                    press(window.controls['seek'])
                    check('manual cursor uses exact original frames',
                          window.session.value.frame == 3000 and window.session.value.accuracy == 'manual')
                    state['phase'] = 'saved'
                    press(window.controls['save_all'])
                    check('Save All waits for actual confirmed queue publication', window.coordinator.busy)
                elif phase == 'saved':
                    if window.coordinator.busy:
                        schedule()
                        return
                    saved, token = PlaylistStore(library.root).load()
                    check('Save All preserves order selection and cursor',
                          saved == window.session.value and not window.session.dirty
                          and saved.index == 1 and saved.frame == 3000 and len(saved.items) == 2)
                    state['saved'], state['token'] = saved, token
                    state['phase'] = 'closed-first'
                    press(window.controls['close'])
                    return
                elif phase == 'reopen':
                    check('reopen restores passive exact frame and selection',
                          window.session.value == state['saved'] and window.session.token == state['token'])
                    check('reopen does not admit retained media automatically',
                          not window.session.reviewed and not window.session.items
                          and window.player.path is None and window.session.admission is None)
                    check('restored queue requires review before Play', not window.controls['play'].IsEnabled())
                    state['phase'] = 'reviewed'
                    press(window.controls['review'])
                elif phase == 'reviewed':
                    check('explicit review restores selected retained bytes without playback',
                          window.session.reviewed and len(window.session.items) == 2
                          and window.session.value.frame == 3000 and window.player.path is None)
                    pending, cancelled = [], []
                    def defer(action, **kwargs):
                        pending.append(action)
                        class Request:
                            def close(self):
                                cancelled.append(True)
                        return Request()
                    from types import SimpleNamespace
                    window.progress = SimpleNamespace(media_action=defer)
                    state['pending'], state['cancelled'] = pending, cancelled
                    state['phase'] = 'prepared'
                    press(window.controls['play'])
                elif phase == 'prepared':
                    prepared = window.prepared
                    check('deferred Play holds all verified input readers',
                          prepared is not None and prepared.readers is not None
                          and len(prepared.items) == 2 and len(state['pending']) == 1)
                    check('deferred Play does not open an output device',
                          window.player.path is None and window.session.admission is None)
                    state['prepared'] = prepared
                    state['phase'] = 'stopped'
                    press(window.controls['stop'])
                elif phase == 'stopped':
                    if window.stopping:
                        schedule()
                        return
                    check('Stop cancels delayed playback and releases readers',
                          state['prepared'].readers is None and state['cancelled']
                          and window.prepared is None and window.player.path is None)
                    check('Stop preserves manually saved cursor',
                          window.session.value == state['saved'] and not window.session.dirty)
                    state['phase'], state['done'] = 'closed-second', True
                    press(window.controls['close'])
                    return
                schedule()
            except BaseException:
                failed()

        def ready(window):
            state['window'] = window
            schedule()

        first = PlaybackQueueDialog(frame, initial_paths=sources, on_ready=ready)
        first.show()
        check('first native queue closes after owner settles',
              first.closed and first.future is None and first.session.closed
              and first.session.admission is None)
        if not failures:
            state.update(window=None, phase='reopen', active=True)
            second = PlaybackQueueDialog(frame, on_ready=ready)
            second.show()
            check('reopened native queue closes after owner settles',
                  second.closed and second.future is None and second.session.closed
                  and second.session.admission is None)
        state['active'] = False
        for timer in timers:
            timer.Stop()
        if failures:
            print('\n'.join(failures), flush=True)
            frame.Destroy()
            return {'status': 'FAIL', 'checks': checks, 'native_controls': 'EXECUTED',
                    'physical_playback': 'NOT_RUN', 'nvda': 'NOT_RUN'}
        snapshot, restored = root / 'snapshot', root / 'restored'
        proof = create_backup(library, snapshot)
        verify_backup(snapshot, proof)
        restore_backup(snapshot, restored, proof)
        reopened, token = PlaylistStore(restored).load()
        check('complete backup restores every retained PCM and exact queue',
              reopened == state['saved'] and token == state['token']
              and [sha256((restored / row.filename).read_bytes()).hexdigest()
                   for row in reopened.items] == original_hashes)
        player = WavePlayer()
        passive = PlaylistSession(restored, player)
        try:
            check('restored queue remains passive without a device',
                  not passive.reviewed and passive.admission is None and player.path is None)
        finally:
            passive.close()
        again = create_backup(LibraryApp(restored), root / 'rebackup')
        verify_backup(root / 'rebackup', again)
        check('restored queue supports verified rebackup', bool(again))
        check('both selected original files remain byte-identical',
              tuple(path.read_bytes() for path in sources) == original)
        frame.Destroy()
        return {'status': 'PASS' if state['done'] else 'FAIL', 'checks': checks,
            'native_controls': 'EXECUTED', 'physical_playback': 'NOT_RUN', 'nvda': 'NOT_RUN',
            'queue_state_sha256': state['token'], 'restored_state_sha256': token,
            'retained_sha256': original_hashes, 'original_sha256': original_hashes,
            'selected_index': reopened.index, 'cursor_frame': reopened.frame,
            'sample_rate': reopened.items[reopened.index].sample_rate,
            'saved_items': len(reopened.items)}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true')
    args = parser.parse_args()
    if os.name != 'nt' or not args.native:
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Explicit native Windows execution is required.'}))
        return 77
    settled = Event()
    def watchdog():
        if not settled.wait(180):
            print(json.dumps({'status': 'FAIL', 'reason': 'Retained queue probe exceeded 180 seconds.'}), flush=True)
            os._exit(124)
    Thread(target=watchdog, daemon=True).start()
    try:
        with ExitStack() as resources:
            from carr_library.native_runtime import activate_bundled_runtime
            handle = activate_bundled_runtime()
            if handle is not None:
                resources.callback(handle.close)
            before = identity()
            result = run_native()
            if identity() != before:
                raise ValueError('Installed retained queue implementations changed during native execution.')
            result.update(installed_mode=True, isolated=True, module_origins=before,
                          source_commit=os.environ['GITHUB_SHA'])
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        return 0 if result['status'] == 'PASS' else 1
    except BaseException as error:
        print(''.join(traceback.format_exception(type(error), error, error.__traceback__))[-16000:], flush=True)
        print(json.dumps({'status': 'FAIL', 'reason': type(error).__name__ + ': ' + str(error)[:4000],
                          'physical_playback': 'NOT_RUN', 'nvda': 'NOT_RUN'}), flush=True)
        return 1
    finally:
        settled.set()


if __name__ == '__main__':
    raise SystemExit(main())
