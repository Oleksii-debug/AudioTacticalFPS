"""Installed native EPUB recorded-overlay acceptance with actual local playback.

The source audio is an explicit synthetic tone, not a qualified voice/model.
This exercises owned native keys and WavePlayer, not NVDA or listening quality.
"""
import argparse
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
import threading
import wave

_cleanup_complete = threading.Event()


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.', 'nvda': 'NOT_RUN'}))
        return 77
    parser = argparse.ArgumentParser()
    parser.add_argument('--audio-directory', required=True, type=Path)
    parser.add_argument('--audio-lock', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'packaging' / 'windows-audio.lock.json')
    arguments = parser.parse_args()
    selected_audio = arguments.audio_directory.resolve(strict=True)
    audio_pin = json.loads(arguments.audio_lock.read_text(encoding='utf-8'))
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio import process as process_module
    from carr_library.audio import cleanup as cleanup_module
    from carr_library import runtime_settings
    from carr_library.speech import player as player_module
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
    modal_loops = []
    unwind_pending = False

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            detail = traceback.format_exc()
            errors.append(detail)
            print('FAIL: native recorded EPUB event loop\n' + detail, flush=True)
            abort()
            return True

    gui = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.IsWindow.argtypes, user32.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND

    def own_modal(owner):
        window = owner.dialog
        loop = wx.EventLoopBase.GetActive()
        if (loop is None or loop.IsMain() or not loop.IsRunning()
                or not window.IsModal() or not user32.IsWindow(window.GetHandle())):
            raise AssertionError('The recorded EPUB owner has no running native modal loop.')
        handle = window.GetHandle()
        if not any(previous is owner and native == handle
                   for previous, _, native, _ in modal_loops):
            modal_loops.append((owner, window, handle, loop))

    def abort():
        nonlocal unwind_pending
        if unwind_pending:
            return
        unwind_pending = True

        def close_active():
            nonlocal unwind_pending
            unwind_pending = False
            try:
                active = wx.EventLoopBase.GetActive()
                for owner, window, handle, loop in reversed(modal_loops):
                    if (not user32.IsWindow(handle) or not window.IsModal()
                            or not loop.IsRunning()):
                        continue
                    if active != loop:
                        # Let an already-closing inner loop return before its
                        # known parent is asked to close. Never end another loop.
                        wx.CallLater(25, abort)
                        return
                    owner.close()
                    # Product closure keeps ownership until silence and worker
                    # cleanup are confirmed. Its close veto is never bypassed.
                    wx.CallLater(25, abort)
                    return
                if active is not None and active.IsMain() and active.IsRunning():
                    active.ScheduleExit(1)
            except Exception:
                detail = traceback.format_exc()
                errors.append(detail)
                print('FAIL: native recorded EPUB owned cleanup\n' + detail, flush=True)

        wx.CallAfter(close_active)

    def guarded(callback, *, release=False):
        def invoke():
            if (finished or errors) and not release:
                return
            try:
                callback()
            except Exception:
                detail = traceback.format_exc()
                errors.append(detail)
                print('FAIL: native recorded EPUB callback\n' + detail, flush=True)
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
            wx.CallLater(40, guarded(release, release=True))

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
        for name in ('ffmpeg', 'ffprobe'):
            binary = Path(getattr(runtime, name)).resolve(strict=True)
            with binary.open('rb') as stream:
                digest = file_digest(stream, 'sha256').hexdigest()
            expected = audio_pin['output_files']['bin/' + name + '.exe']
            selected = selected_audio / 'bin' / (name + '.exe')
            check('explicit selected audio executable: ' + name, binary.samefile(selected))
            check('pinned audio executable bytes: ' + name,
                  binary.stat().st_size == expected['size'] and digest == expected['sha256'])
            audio_proof[name] = {'path': str(binary), 'sha256': digest,
                'binding': 'explicit selected directory and pinned lock'}

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
            for module in (ebook_audio, ebook_audio_dialog, reading_desk, runtime_settings, player_module,
                           process_module, cleanup_module):
                actual = Path(module.__file__).resolve()
                source = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
                check('exact installed source: ' + module.__name__, actual.is_relative_to(Path(sys.prefix).resolve()) and not actual.is_relative_to(checkout)
                      and sha256(actual.read_bytes()).digest() == sha256(source.read_bytes()).digest())
            check('real native Windows controls', wx.Platform == '__WXMSW__')
            library = LibraryApp(root / 'library')
            workspace = library.create_workspace('Recorded EPUB acceptance')
            settings = runtime_settings.load_settings(library.root / 'state')
            settings.update(ffmpeg=str(selected_audio / 'bin' / 'ffmpeg.exe'),
                            ffprobe=str(selected_audio / 'bin' / 'ffprobe.exe'))
            runtime_settings.save_settings(library.root / 'state', settings)
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
                        own_modal(reader)
                        check('existing Reading Desk presents the exact source', reader.controller.current == expected_page)
                        stage = 1
                        press(named(reader.dialog, 'EPUB synchronized recorded audio...'))
                    elif stage == 1 and reader._audio_dialog is not None:
                        overlay = reader._audio_dialog
                        own_modal(overlay)
                        if overlay.busy or overlay.transport is None:
                            return
                        check('recorded preparation borrows the current host worker', overlay.host is frame and not hasattr(overlay, 'executor'))
                        check('opening recorded audio does not start playback', overlay.player.state == 'stopped'
                              and not overlay.transport.active)
                        cue = overlay.session.chapter.cues[0]
                        selected_fragment = overlay.text.GetStringSelection()
                        expected_fragment = expected_page.raw_text[cue.start:cue.end]
                        if selected_fragment != expected_fragment:
                            print('DIAGNOSTIC: recorded EPUB source highlight ' + json.dumps({
                                'selected': selected_fragment[:4096],
                                'expected': expected_fragment[:4096],
                                'native_selection': overlay.text.GetSelection(),
                                'cue_offsets': [cue.start, cue.end],
                                'same_source_page': overlay.session.page.raw_text == expected_page.raw_text,
                                'native_value_exact': overlay.text.GetValue() == expected_page.raw_text,
                                'status': overlay.status.GetValue()[:4096]},
                                ensure_ascii=True), flush=True)
                        check('native source highlight is an exact Unicode fragment',
                              selected_fragment == expected_fragment)
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
                reader.show()
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
            gui.MainLoop()
        finally:
            for key in tuple(pressed):
                simulator.KeyUp(key)
                pressed.discard(key)
            for owner in (overlay, reader):
                if owner is not None:
                    owner.close()
                    if owner is reader:
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
    print(json.dumps({'status': 'PASS', 'result': 'PASS', 'checks': checks, 'audio_runtime': audio_proof,
        'audio': 'Actual local FFmpeg and Windows playback of synthetic PCM tones',
        'native_keyboard': 'PASS', 'epubcheck': 'NOT_RUN', 'third_party_reader': 'NOT_RUN',
        'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN'}, indent=2))
    return 0


if __name__ == '__main__':
    def watchdog():
        if not _cleanup_complete.wait(180):
            os.write(2, b'FAIL: native recorded EPUB work or owned cleanup exceeded 180 seconds.\n')
            os._exit(86)
    threading.Thread(target=watchdog, daemon=True, name='recorded-epub-native-watchdog').start()
    try:
        raise SystemExit(main())
    finally:
        _cleanup_complete.set()
