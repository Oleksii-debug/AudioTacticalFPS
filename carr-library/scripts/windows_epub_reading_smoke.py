"""Installed native EPUB recorded-overlay acceptance with actual local playback.

The source audio is an explicit synthetic tone, not a qualified voice/model.
This exercises owned native keys and WavePlayer, not NVDA or listening quality.
"""
from hashlib import file_digest, sha256
import json
import math
import os
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import traceback
import wave


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.', 'nvda': 'NOT_RUN'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
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
    frame = reader = overlay = service = None
    shutdown = {'requested': False, 'reader_returned': False, 'attempted': set()}

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    def remember_failure(context):
        message = context + '\n' + traceback.format_exc()
        errors.append(message)
        # Report the original failure before any nested modal cleanup can fail.
        print('FAIL:', message, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            remember_failure('Native EPUB event callback failed.')
            abort()
            return True

    gui = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND

    def abort():
        if shutdown['requested']:
            return
        shutdown['requested'] = True
        for key in tuple(pressed):
            try:
                if not simulator.KeyUp(key):
                    raise RuntimeError('The failed EPUB probe could not release its native key.')
            except Exception:
                remember_failure('Native EPUB key cleanup failed.')
            finally:
                pressed.discard(key)

        def advance():
            # End the innermost modal first. The Reading Desk clears its owned
            # child only after that child's ShowModal/finally has returned.
            if reader is None or shutdown['reader_returned']:
                gui.ExitMainLoop()
                return
            owner = reader._audio_dialog or reader
            if id(owner) not in shutdown['attempted']:
                shutdown['attempted'].add(id(owner))
                try:
                    owner.close()
                    if not owner.closed:
                        raise RuntimeError('The owned EPUB reader did not confirm closure.')
                except Exception:
                    remember_failure('Native EPUB owner cleanup failed.')
                try:
                    # Failure-only modal unwind does not change the owner's
                    # closure flag or claim that its retained resources closed.
                    if owner.dialog and owner.dialog.IsModal():
                        owner.dialog.EndModal(wx.ID_CANCEL)
                except Exception:
                    remember_failure('Native EPUB modal unwind failed.')
            # This callback must remain runnable after errors suppress normal
            # driver actions. Never close the parent inside its child loop.
            wx.CallLater(25, advance)

        wx.CallAfter(advance)

    def guarded(callback):
        def invoke():
            if finished or errors:
                return
            try:
                callback()
            except Exception:
                trace_failure_state()
                remember_failure('Native EPUB driver callback failed at stage ' + str(stage))
                abort()
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
            finally:
                pressed.discard(key)
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

    def trace_failure_state():
        try:
            trace_highlight()
        except Exception as error:
            # Observation failure must not suppress the original error or cleanup.
            print('EPUB failure-state observation unavailable: ' + type(error).__name__
                  + ': ' + str(error)[:256], flush=True)

    def expire():
        if finished:
            return
        trace_failure_state()
        try:
            raise TimeoutError('Native recorded-EPUB acceptance exceeded its deadline at stage ' + str(stage))
        except Exception:
            remember_failure('Native EPUB deadline expired.')
            abort()

    def trace_highlight():
        # Bounded observation only: do not normalize, select or repair text.
        print('EPUB diagnostic: inspecting the unchanged native highlight at stage ' + str(stage), flush=True)
        cue = overlay.session.chapter.cues[0]
        def text_record(value):
            return {'characters': len(value), 'sha256': sha256(value.encode('utf-8')).hexdigest(),
                    'prefix': value[:2048]}
        def observe(read):
            try:
                return read()
            except Exception as error:
                return {'unavailable': type(error).__name__, 'reason': str(error)[:256]}
        playback = {
            'cancelled': observe(lambda: overlay.cancelled.is_set()),
            'key_pending': key_pending,
            'player_state': observe(lambda: overlay.player.state),
            'transport_ticket': observe(lambda: list(overlay.transport.ticket)),
            'transport_active': observe(lambda: overlay.transport.active),
            'transport_closed': observe(lambda: overlay.transport.closed),
            'transport_interrupted': observe(lambda: overlay.transport.interrupted),
            'prepared_present': observe(lambda: overlay.transport.prepared is not None),
            'pending_present': getattr(overlay, '_pending', None) is not None,
        }
        print(json.dumps({'epub_highlight_diagnostic': {
            'stage': stage, 'busy': overlay.busy, 'closed': overlay.closed,
            'playback': playback,
            'native_selection': list(overlay.text.GetSelection()),
            'selected': text_record(overlay.text.GetStringSelection()),
            'control': text_record(overlay.text.GetValue()),
            'expected_fragment': text_record(expected_page.raw_text[cue.start:cue.end]),
            'expected_source': text_record(expected_page.raw_text),
            'session_source_matches': overlay.session.page.raw_text == expected_page.raw_text,
            'status': text_record(overlay.status.GetValue()),
        }}, ensure_ascii=True, sort_keys=True), flush=True)

    def verify_audio(runtime):
        bundle = Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR') else None
        manifest = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8')) if bundle else None
        for name in ('ffmpeg', 'ffprobe'):
            binary = Path(getattr(runtime, name)).resolve(strict=True)
            with binary.open('rb') as stream:
                digest = file_digest(stream, 'sha256').hexdigest()
            audio_proof[name] = {'path': str(binary), 'sha256': digest}
            if bundle:
                entry = 'audio-runtime/bin/' + name + '.exe'
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
            library = LibraryApp(root / 'library')
            workspace = library.create_workspace('Recorded EPUB acceptance')
            service = ProductionService(library.root / 'state' / 'studio', voices=SyntheticTones())
            verify_audio(service._runtime())
            script = '[Scene: Native source]\nNarrator: Exact α 😀 source & words.\n[Pause: 0.2]\nReader: Second recorded line.'
            project = service.create(script, title='Recorded EPUB native fixture', voice_ids=('synthetic-tone',))
            job = service.submit(project, root, stems=False)
            check('actual local production completes', service.run(job)['status'] == 'complete')
            publication = root / 'recorded.epub'
            receipt = PublicationService(service).export_epub(job, publication, language='en')
            original = publication.read_bytes()
            book = library.import_file(workspace, publication)
            check('actual publication imports as a retained EPUB', book['state'] == 'complete')
            desk = ReadingDesk(library, workspace, book['edition'])
            expected_page = desk.page(1)
            frame = DesktopFrame(library)
            frame.Show(); gui.SetTopWindow(frame)

            def guard():
                if frame.library is not library or frame._closing:
                    raise ValueError('The library changed.')
                with library.read(workspace):
                    pass

            def drive():
                nonlocal stage, overlay
                if errors or finished:
                    return
                if monotonic() >= deadline:
                    raise TimeoutError('Native recorded-EPUB acceptance exceeded its deadline at stage ' + str(stage))
                try:
                    if key_pending:
                        return
                    if stage == 0 and reader.controller.current is not None and not reader.busy:
                        check('existing Reading Desk presents the exact source', reader.controller.current == expected_page)
                        stage = 1
                        press(named(reader.dialog, 'EPUB synchronized recorded audio...'))
                    elif stage == 1 and reader._audio_dialog is not None:
                        overlay = reader._audio_dialog
                        if overlay.busy or overlay.transport is None:
                            return
                        check('opening recorded audio does not start playback', overlay.player.state == 'stopped'
                              and not overlay.transport.active)
                        trace_highlight()
                        check('native source highlight is an exact Unicode fragment', overlay.text.GetStringSelection()
                              == expected_page.raw_text[overlay.session.chapter.cues[0].start:overlay.session.chapter.cues[0].end])
                        check('existing progress observes the owned transport', overlay.transport.observed
                              in frame.progress_panel.media.players)
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
                              and overlay.transport.observed not in frame.progress_panel.media.players)
                        stage = 11
                        press(wx.Window.FindWindowById(wx.ID_CANCEL, reader.dialog), wx.WXK_ESCAPE)
                finally:
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
                try:
                    reader.show()
                finally:
                    shutdown['reader_returned'] = True
                for key in tuple(pressed):
                    check('native key released after modal completion', simulator.KeyUp(key))
                    pressed.discard(key)
                finished = True
                if not errors:
                    check('native Escape closes the existing reader after playback cleanup', stage == 11 and reader.closed)
                    check('original publication and retained bytes remain unchanged', publication.read_bytes() == original
                          and library.assets.resolve(desk.identity.asset_sha256).read_bytes() == original)
                    check('reading does not create quotation approval', not expected_page.verified and library._selections == {})
                    check('completed publication receipt still verifies', PublicationService(service).verify(receipt['id']) == receipt)
                gui.ExitMainLoop()

            wx.CallAfter(guarded(open_reader))
            # Unlike normal guarded actions, the deadline also reports an
            # already failing modal unwind instead of silently disappearing.
            wx.CallLater(max(1, int((deadline - monotonic()) * 1000)), expire)
            gui.MainLoop()
        finally:
            for key in tuple(pressed):
                simulator.KeyUp(key)
                pressed.discard(key)
            for owner in (overlay, reader):
                if owner is not None:
                    owner.close()
                    owner.executor.shutdown(wait=True, cancel_futures=True)
            if service is not None:
                service.close()
            if frame is not None:
                frame.cancel_event.set(); frame._closing = True
                frame.executor.shutdown(wait=True, cancel_futures=True)
                frame.progress_panel.close(); frame.Destroy()
            gui.Yield()
            if native_handle is not None:
                native_handle.close()
        if errors:
            raise RuntimeError('\n'.join(errors))
    print(json.dumps({'result': 'PASS', 'checks': checks, 'audio_runtime': audio_proof,
        'audio': 'Actual local FFmpeg and Windows playback of synthetic PCM tones',
        'native_keyboard': 'PASS', 'epubcheck': 'NOT_RUN', 'third_party_reader': 'NOT_RUN',
        'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN'}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
