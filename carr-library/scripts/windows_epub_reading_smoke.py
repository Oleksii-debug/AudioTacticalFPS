"""Installed native EPUB recorded-overlay acceptance with actual local playback.

The source audio is an explicit synthetic tone, not a qualified voice/model.
This exercises owned native keys and WavePlayer, not NVDA or listening quality.
"""
import faulthandler
from hashlib import file_digest, sha256
import json
import math
import os
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import traceback
import wave


def _cleanup_call(errors, label, callback):
    """Keep every owned cleanup attempt reachable after an earlier failure."""
    try:
        callback()
    except Exception:
        detail = label + ':\n' + traceback.format_exc()
        errors.append(detail)
        print('FAIL:', detail, flush=True)


def _release_native_keys(simulator, pressed, errors):
    for key in tuple(pressed):
        def release(key=key):
            if not simulator.KeyUp(key):
                raise RuntimeError('The native simulator did not confirm key release.')
        try:
            _cleanup_call(errors, 'Releasing native key ' + str(key), release)
        finally:
            pressed.discard(key)


def _check_audio_progress(overlay, stage):
    """Expose the real dialog's terminal error instead of polling to deadline."""
    # Stage 9 deliberately archives the source and expects the normal guard to
    # stop playback. Earlier stages require a live transport timer after load.
    if overlay is None or not 1 <= stage <= 8 or overlay.busy or overlay.closed:
        return
    if not overlay.timer.IsRunning():
        status = overlay.status.GetValue()[:2048]
        state = None if overlay.player is None else overlay.player.state
        raise RuntimeError(f'Recorded EPUB preparation/playback failed at stage {stage}; '
                           f'player={state!r}; status={status!r}')


def _abort_native_probe(wx, application, owner_loop, frame, owners, release, errors, detail):
    """Close the active child and wait for its ShowModal to return first."""
    errors.append(detail)
    print('FAIL:', detail, flush=True)
    overlay, reader = (*owners, None, None)[:2]
    pending = None
    stopped = False
    deadline = monotonic() + 10

    def stop():
        nonlocal stopped
        stopped = True
        if pending is not None:
            _cleanup_call(errors, 'Stopping the owned modal-unwind timer', pending.Stop)

    def cancel(owner):
        if owner is None:
            return
        _cleanup_call(errors, 'Cancelling reader work', owner.cancelled.set)
        timer = getattr(owner, 'timer', None)
        if timer is not None:
            _cleanup_call(errors, 'Stopping recorded-audio timer', timer.Stop)

    def close(owner):
        if owner is None:
            return
        _cleanup_call(errors, 'Closing ' + type(owner).__name__, owner.close)
        def end_modal():
            if owner.dialog and owner.dialog.IsModal():
                owner.dialog.EndModal(wx.ID_CANCEL)
        _cleanup_call(errors, 'Unwinding owned reader dialog', end_modal)

    def close_reader():
        nonlocal pending
        if stopped:
            return
        try:
            # EndModal only requests exit. wx can dispatch queued callbacks
            # before the inner loop returns, and closing an inactive outer
            # dialog then asserts in wxMSW. This field is cleared by the real
            # ReadingDialog.open_audio finally, after overlay.show() returns.
            if reader is not None and reader._audio_dialog is not None:
                if monotonic() >= deadline:
                    raise TimeoutError('The owned recorded-audio modal loop did not return during abort.')
                pending = wx.CallLater(25, close_reader)
                return
            close(reader)
            stop()
        except Exception:
            errors.append('Unwinding the owned recorded reader:\n' + traceback.format_exc())
            print('FAIL:', errors[-1], flush=True)
            stop()

    try:
        if frame is not None:
            _cleanup_call(errors, 'Cancelling desktop work', frame.cancel_event.set)
        _cleanup_call(errors, 'Releasing held native keys', release)
        # The probe may fail before its next drive callback observes the new
        # overlay. Use the reader's actual owned child, never an unrelated
        # top-level window or the child's early `closed` flag.
        inner = reader._audio_dialog if reader is not None else None
        if inner is None:
            inner = overlay
        cancel(inner)
        cancel(reader)
        close(inner)
        close_reader()
    finally:
        # App.ExitMainLoop exits the currently active ShowModal loop. The
        # retained owner must also exit after those nested loops unwind.
        loop = owner_loop if owner_loop is not None else application.GetMainLoop()
        if loop is None:
            raise RuntimeError('The recorded-reading main event loop is unavailable during abort.')
        loop.ScheduleExit(1)
    return stop


def _run_native():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.', 'nvda': 'NOT_RUN'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    print('PHASE: activating bundled native runtime', flush=True)
    native_handle = activate_bundled_runtime()
    print('PHASE: importing native desktop and recorded-reading modules', flush=True)
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.reader import ebook_audio
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.studio.render import ProductionService
    from carr_library.studio.publishing import PublicationService
    from carr_library.ui import ebook_audio_dialog, reading_desk
    from carr_library.ui.desktop_app import DesktopFrame

    errors, checks, pressed, audio_proof = [], [], set(), {}
    deadline = monotonic() + 120
    stage, key_pending, finished = 0, False, False
    in_drive, aborting, owner_loop = False, False, None
    abort_cleanup = None
    frame = reader = overlay = service = None

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return True

    gui = ProbeApp(False)
    gui.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND

    def release_all():
        nonlocal key_pending
        try:
            _release_native_keys(simulator, pressed, errors)
        finally:
            key_pending = False

    def abort(detail):
        nonlocal aborting, abort_cleanup
        if aborting:
            errors.append(detail)
            print('FAIL:', detail, flush=True)
            return
        aborting = True
        abort_cleanup = _abort_native_probe(wx, gui, owner_loop, frame, (overlay, reader), release_all,
                                           errors, 'Stage ' + str(stage) + ': ' + detail)

    def guarded(callback):
        def invoke():
            nonlocal owner_loop
            if finished or errors:
                return
            try:
                if owner_loop is None:
                    owner_loop = gui.GetMainLoop()
                callback()
            except Exception:
                abort(traceback.format_exc())
        return invoke

    def press(control, key=wx.WXK_SPACE):
        nonlocal key_pending
        key_pending = True
        top = wx.GetTopLevelParent(control)
        focus_deadline = min(deadline, monotonic() + 5)
        name = control.GetName() or control.GetLabel()

        def release():
            nonlocal key_pending
            try:
                if key in pressed:
                    check('native key release: ' + name, simulator.KeyUp(key))
                    pressed.discard(key)
            finally:
                key_pending = False

        def dispatch():
            top.Raise()
            user32.SetForegroundWindow(top.GetHandle())
            control.SetFocus()
            if wx.Window.FindFocus() != control or user32.GetForegroundWindow() != top.GetHandle():
                if monotonic() >= focus_deadline:
                    raise TimeoutError('Native focus did not reach ' + name)
                wx.CallLater(25, guarded(dispatch))
                return
            check('owned native focus: ' + name, wx.Window.FindFocus() == control)
            try:
                check('native key down: ' + name, simulator.KeyDown(key))
                pressed.add(key)
            except BaseException:
                simulator.KeyUp(key)
                raise
            wx.CallLater(40, guarded(release))

        wx.CallAfter(guarded(dispatch))

    def named(dialog, name):
        def descendants(window):
            for child in window.GetChildren():
                yield child
                yield from descendants(child)
        matches = [item for item in descendants(dialog) if item.GetName() == name]
        if len(matches) != 1:
            raise AssertionError('Unique accessible control missing: ' + name)
        return matches[0]

    def verify_audio(runtime):
        bundle = Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR') else None
        manifest = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8')) if bundle else None
        for name in ('ffmpeg', 'ffprobe'):
            binary = Path(getattr(runtime, name)).resolve(strict=True)
            with binary.open('rb') as stream:
                digest = file_digest(stream, 'sha256').hexdigest()
            audio_proof[name] = {'path': str(binary), 'sha256': digest}
            if bundle:
                entry = 'audio/bin/' + name + '.exe'
                check('exact bundled executable: ' + name, binary == (bundle / entry).resolve(strict=True))
                check('sealed executable bytes: ' + name, digest == manifest['files'][entry])

    class SyntheticTones:
        def voices(self):
            return (SimpleNamespace(id='synthetic-tone', name='Synthetic tone fixture', language='und', engine='test'),)

        def synthesize(self, text, voice_id, destination, *, settings, cancelled):
            if cancelled():
                raise InterruptedError('Synthetic production cancelled.')
            with wave.open(str(destination), 'wb') as stream:
                stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                stream.writeframes(b''.join(struct.pack('<h', int(3000 * math.sin(2 * math.pi * 440 * n / 24000)))
                                           for n in range(96000)))

    with TemporaryDirectory(prefix='resonance-native-epub-reading-', ignore_cleanup_errors=True) as temporary:
        root = Path(temporary)
        try:
            checkout = Path(__file__).resolve().parents[1]
            for module in (ebook_audio, ebook_audio_dialog, reading_desk):
                actual = Path(module.__file__).resolve()
                source = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
                check('exact installed source: ' + module.__name__, not actual.is_relative_to(checkout)
                      and sha256(actual.read_bytes()).digest() == sha256(source.read_bytes()).digest())
            check('real native Windows controls', wx.Platform == '__WXMSW__')
            print('PHASE: creating the synthetic library and production service', flush=True)
            library = LibraryApp(root / 'library')
            workspace = library.create_workspace('Recorded EPUB acceptance')
            service = ProductionService(library.root / 'state' / 'studio', voices=SyntheticTones())
            print('PHASE: verifying the actual local audio executables', flush=True)
            verify_audio(service._runtime())
            script = '[Scene: Native source]\nNarrator: Exact α 😀 source & words.\n[Pause: 0.2]\nReader: Second recorded line.'
            project = service.create(script, title='Recorded EPUB native fixture', voice_ids=('synthetic-tone',))
            job = service.submit(project, root, stems=False)
            print('PHASE: rendering actual synthetic PCM before the native event loop', flush=True)
            check('actual local production completes', service.run(job)['status'] == 'complete')
            publication = root / 'recorded.epub'
            print('PHASE: exporting the recorded EPUB before the native event loop', flush=True)
            receipt = PublicationService(service).export_epub(job, publication, language='en')
            original = publication.read_bytes()
            print('PHASE: importing the recorded EPUB before the native event loop', flush=True)
            book = library.import_file(workspace, publication)
            check('actual publication imports as a retained EPUB', book['state'] == 'complete')
            desk = ReadingDesk(library, workspace, book['edition'])
            expected_page = desk.page(1)
            print('PHASE: constructing the actual desktop frame', flush=True)
            frame = DesktopFrame(library)
            frame.Show(); gui.SetTopWindow(frame)

            def guard():
                if frame.library is not library or frame._closing:
                    raise ValueError('The library changed.')
                with library.read(workspace):
                    pass

            def drive():
                nonlocal stage, overlay, in_drive
                if errors or finished or in_drive:
                    return
                in_drive = True
                try:
                    if monotonic() >= deadline:
                        raise TimeoutError('Native recorded-EPUB acceptance exceeded its deadline at stage ' + str(stage))
                    _check_audio_progress(overlay, stage)
                    if key_pending:
                        return
                    if stage == 0 and reader.controller.current is not None and not reader.busy:
                        check('existing Reading Desk presents the exact source', reader.controller.current == expected_page)
                        stage = 1
                        press(named(reader.dialog, 'EPUB synchronized recorded audio...'))
                    elif stage == 1 and reader._audio_dialog is not None:
                        overlay = reader._audio_dialog
                        _check_audio_progress(overlay, stage)
                        if overlay.busy or overlay.transport is None:
                            return
                        check('opening recorded audio does not start playback', overlay.player.state == 'stopped'
                              and not overlay.transport.active)
                        print('PHASE: reading the native Unicode source selection', flush=True)
                        native_selection = overlay.text.GetStringSelection()
                        print('PHASE: native Unicode source selection returned: ' + ascii(native_selection), flush=True)
                        check('native source highlight is an exact Unicode fragment', native_selection
                              == expected_page.raw_text[overlay.session.chapter.cues[0].start:overlay.session.chapter.cues[0].end])
                        check('existing progress observes the owned transport', overlay.transport.observed
                              in frame.operation_progress._players)
                        stage = 2
                        press(named(overlay.dialog, 'Play from selected segment'))
                    elif stage == 2 and overlay.player.state == 'playing':
                        check('real WavePlayer opens the exact measured PCM interval', overlay.transport.prepared.frames == 96000)
                        stage = 3
                        press(named(overlay.dialog, 'Pause recorded audio'))
                    elif stage == 3 and overlay.player.state == 'paused':
                        check('native pause retains the selected segment', overlay.transport.index == 0)
                        stage = 4
                        press(named(overlay.dialog, 'Resume recorded audio'))
                    elif stage == 4 and overlay.player.state == 'playing' and not overlay.busy:
                        stage = 5
                        press(named(overlay.dialog, 'Stop recorded audio'))
                    elif stage == 5 and overlay.player.state == 'stopped' and not overlay.transport.active:
                        check('native Stop releases prepared playback', overlay.transport.prepared is None)
                        stage = 6
                        press(named(overlay.dialog, 'Next recorded segment'))
                    elif stage == 6 and overlay.transport.index == 1:
                        check('native selection never starts audio', overlay.player.state == 'stopped' and not overlay.transport.active)
                        stage = 7
                        press(named(overlay.dialog, 'Previous recorded segment'))
                    elif stage == 7 and overlay.transport.index == 0:
                        stage = 8
                        press(named(overlay.dialog, 'Play from selected segment'))
                    elif stage == 8 and overlay.player.state == 'playing':
                        library.archive_workspace(workspace)
                        stage = 9
                    elif stage == 9 and overlay.player.state == 'stopped' and not overlay.transport.active:
                        check('fresh source access stops archived playback visibly', 'archived' in overlay.status.GetValue()
                              and wx.Window.FindFocus() == overlay.status)
                        stage = 10
                        press(wx.Window.FindWindowById(wx.ID_CANCEL, overlay.dialog), wx.WXK_ESCAPE)
                    elif stage == 10 and reader._audio_dialog is None:
                        check('closing recorded audio confirms owned cleanup', overlay.closed and overlay.player.state == 'stopped'
                              and overlay.transport.observed not in frame.operation_progress._players)
                        stage = 11
                        press(wx.Window.FindWindowById(wx.ID_CANCEL, reader.dialog), wx.WXK_ESCAPE)
                finally:
                    in_drive = False
                    if not errors and not finished:
                        wx.CallLater(25, guarded(drive))

            def open_reader():
                nonlocal reader, finished
                if frame.working:
                    if monotonic() >= deadline:
                        raise TimeoutError('The real desktop did not become ready.')
                    wx.CallLater(25, guarded(open_reader))
                    return
                reader = reading_desk.ReadingDialog(frame, desk, ReadingState(library.root), guard)
                wx.CallLater(25, guarded(drive))
                reader.show()
                release_all()
                finished = True
                if not errors:
                    check('native Escape closes the existing reader after playback cleanup', stage == 11 and reader.closed)
                    check('original publication and retained bytes remain unchanged', publication.read_bytes() == original
                          and library.assets.resolve(desk.identity.asset_sha256).read_bytes() == original)
                    check('reading does not create quotation approval', not expected_page.verified and library._selections == {})
                    check('completed publication receipt still verifies', PublicationService(service).verify(receipt['id']) == receipt)
                owner_loop.ScheduleExit(1 if errors else 0)

            wx.CallAfter(guarded(open_reader))
            print('PHASE: entering the native recorded-reading event loop', flush=True)
            gui.MainLoop()
            print('PHASE: native recorded-reading event loop returned', flush=True)
        finally:
            if abort_cleanup is not None:
                _cleanup_call(errors, 'Stopping the owned modal-unwind timer', abort_cleanup)
            print('PHASE: releasing held native keys', flush=True)
            _cleanup_call(errors, 'Releasing held native keys', release_all)
            for owner in (overlay, reader):
                if owner is not None:
                    print('PHASE: closing ' + type(owner).__name__, flush=True)
                    _cleanup_call(errors, 'Closing ' + type(owner).__name__, owner.close)
                    print('PHASE: waiting for ' + type(owner).__name__ + ' executor cleanup', flush=True)
                    _cleanup_call(errors, 'Waiting for reader executor cleanup',
                                  lambda owner=owner: owner.executor.shutdown(wait=True, cancel_futures=True))
            if service is not None:
                print('PHASE: closing the owned production service', flush=True)
                _cleanup_call(errors, 'Closing the owned production service', service.close)
            if frame is not None:
                frame.cancel_event.set(); frame._closing = True
                print('PHASE: waiting for desktop executor cleanup', flush=True)
                _cleanup_call(errors, 'Waiting for desktop executor cleanup',
                              lambda: frame.executor.shutdown(wait=True, cancel_futures=True))
                print('PHASE: closing desktop progress and destroying the frame', flush=True)
                _cleanup_call(errors, 'Closing desktop progress', frame.operation_progress.close)
                _cleanup_call(errors, 'Destroying the desktop frame', frame.Destroy)
            print('PHASE: yielding deferred native destruction', flush=True)
            _cleanup_call(errors, 'Yielding deferred native destruction', gui.Yield)
            if native_handle is not None:
                print('PHASE: closing the bundled native runtime handle', flush=True)
                _cleanup_call(errors, 'Closing the bundled native runtime handle', native_handle.close)
            print('PHASE: recorded-reading cleanup returned', flush=True)
        if errors:
            raise RuntimeError('\n'.join(errors))
    print(json.dumps({'result': 'PASS', 'checks': checks, 'audio_runtime': audio_proof,
        'audio': 'Actual local FFmpeg and Windows playback of synthetic PCM tones',
        'native_keyboard': 'PASS', 'epubcheck': 'NOT_RUN', 'third_party_reader': 'NOT_RUN',
        'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN'}, indent=2))
    return 0


def main():
    if os.name != 'nt':
        return _run_native()
    # Diagnostics only: workflow and native stage deadlines remain mandatory.
    # This also records a native call or teardown wait blocking the wx timer.
    faulthandler.enable(file=sys.stderr, all_threads=True)
    faulthandler.dump_traceback_later(90, repeat=True, file=sys.stderr)
    try:
        return _run_native()
    finally:
        faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())
