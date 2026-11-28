"""Installed native EPUB recorded-overlay acceptance with an explicit output mode.

Default: actual WavePlayer and Windows output. --controlled-output explicitly
uses the existing disclosed PCM test device when qualifying native workflows
without an audio endpoint. Both modes use real production, import and transport.
Synthetic tones never qualify NVDA, listening quality or a voice/model.
"""
import argparse
import faulthandler
from hashlib import sha256
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


def _acceptance_bindings():
    # Use the checked-in gate helper by explicit sibling path, never product sys.path.
    import importlib.util
    path = Path(__file__).with_name('windows_publishing_smoke.py')
    spec = importlib.util.spec_from_file_location('_reader_acceptance_bindings', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



def _controlled_output_fixture():
    """Adapt the existing disclosed PCM fixture to recorded-overlay transport."""
    import importlib.util
    path = Path(__file__).with_name('windows_live_reading_smoke.py')
    expected = sha256(path.read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location('_epub_controlled_pcm_fixture', path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    created, openings = [], []

    class ControlledRecordedOutput:
        def __init__(self):
            self.device = helper.ControlledPCMPlayer()
            created.append(self)

        @property
        def state(self):
            state = self.device.state
            return 'stopped' if state in ('closed', 'ended') else state

        @property
        def position_seconds(self):
            return self.device.position

        @property
        def position_is_approximate(self):
            return False  # A controlled fixture coordinate, never an audible clock.

        def play(self, source):
            self.device.open(source)
            with wave.open(str(source)) as audio:
                openings.append({'sha256': sha256(Path(source).read_bytes()).hexdigest(),
                                 'frames': audio.getnframes(),
                                 'rate': audio.getframerate(),
                                 'channels': audio.getnchannels()})
            self.device.play()

        def pause(self):
            return self.device.pause()

        def resume(self):
            return self.device.resume()

        def close(self):
            # The existing fixture proves the WAV still exists when its owned
            # handle closes; the real transport releases the file afterwards.
            self.device.close()
            return True

    def proof():
        assert sha256(path.read_bytes()).hexdigest() == expected
        assert len(created) == 1 and len(openings) == 2, (len(created), openings)
        assert all(item.device.stream is None for item in created)
        assert all(item['frames'] == 96000 for item in openings), openings
        return {'backend': 'CONTROLLED_PCM_TEST_FIXTURE', 'fixture_sha256': expected,
                'openings': openings, 'owned_handles_closed': True,
                'physical_audio_device': 'NOT_RUN', 'actual_WavePlayer': 'NOT_RUN'}
    return ControlledRecordedOutput, proof


class _OwnedModalAbort:
    """Unwind one owned modal loop per GUI turn, never its suspended parent."""

    def __init__(self, wx, app, owners, report):
        self.wx, self.app, self.owners, self.report = wx, app, owners, report
        self.active = False
        self.timer = None
        self.reported = set()

    def request(self):
        if not self.active:
            self.active = True
            self.wx.CallAfter(self._advance)

    def stop(self):
        self.active = False
        if self.timer is not None:
            self.timer.Stop()
            self.timer = None

    def _advance(self):
        self.timer = None
        if not self.active:
            return
        try:
            # Owners are innermost first. EndModal() requests a return; its
            # IsModal() remains true until that ShowModal() actually unwinds.
            for owner in self.owners():
                if owner is None:
                    continue
                dialog = owner.dialog
                if dialog and dialog.IsModal():
                    if dialog.IsShown():
                        owner.close()
                    self.timer = self.wx.CallLater(10, self._advance)
                    return
            self.active = False
            self.app.ExitMainLoop()
        except (Exception, KeyboardInterrupt):
            detail = traceback.format_exc()
            if detail not in self.reported:
                self.reported.add(detail)
                self.report(detail)
            # Cleanup refusal remains a failure. Do not force-delete a native
            # owner or report silence; the outer owned process bound applies.
            self.timer = self.wx.CallLater(25, self._advance)


def _nested_modal_failure_acceptance(wx, app, check, select_failure_handler):
    """Real Windows negative control for the probe's former nested-loop hang."""
    events, returned, failures = [], [], []
    owners = []
    abort = _OwnedModalAbort(wx, app, lambda: tuple(reversed(owners)), failures.append)

    def fail(detail):
        failures.append(detail)
        abort.request()

    def owner(label, parent):
        dialog = wx.Dialog(parent, title='Owned EPUB failure control — ' + label)
        native_id = dialog.GetId()
        record = SimpleNamespace(dialog=dialog, loop=None, native_id=native_id)

        def destroyed(event):
            if event.GetId() == native_id:
                events.append(label)
            event.Skip()

        def close():
            if wx.EventLoopBase.GetActive() != record.loop:
                raise AssertionError('Attempted to close a suspended ' + label + ' loop.')
            dialog.EndModal(wx.ID_CANCEL)

        record.close = close
        dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
        owners.append(record)
        return record

    def provoke(inner):
        inner.loop = wx.EventLoopBase.GetActive()
        try:
            raise AssertionError('deliberate owned nested-modal failure')
        except AssertionError:
            fail(traceback.format_exc())

    def open_inner(outer):
        outer.loop = wx.EventLoopBase.GetActive()
        inner = owner('inner', outer.dialog)
        wx.CallAfter(provoke, inner)
        try:
            inner.dialog.ShowModal()
            returned.append('inner')
        finally:
            if inner.dialog:
                inner.dialog.Destroy()

    # wxPython MainLoop returns immediately with no top-level windows. Own the
    # actual outer dialog before starting the loop, then show it on that loop.
    outer = owner('outer', None)

    def open_outer():
        wx.CallAfter(open_inner, outer)
        try:
            outer.dialog.ShowModal()
            returned.append('outer')
        finally:
            if outer.dialog:
                outer.dialog.Destroy()
        app.ExitMainLoop()

    select_failure_handler(fail)
    try:
        wx.CallAfter(open_outer)
        app.MainLoop()
    finally:
        abort.stop()
        select_failure_handler(None)
    check('actual nested-modal failure returns child before parent',
          returned == ['inner', 'outer']
          and len(failures) == 1
          and 'deliberate owned nested-modal failure' in failures[0])
    check('actual failed modal owners receive one native destruction each',
          events.count('inner') == 1 and events.count('outer') == 1
          and all(not item.dialog for item in owners))


def _run_owned_native(options):
    """Reuse the application's real Windows Job owner for this bounded probe."""
    from carr_library.audio.models import AudioBudget
    from carr_library.audio.process import run_process

    result = run_process(
        [sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()),
         '--audio-evidence', str(options.audio_evidence.resolve()), '--native-child']
        + (['--controlled-output'] if options.controlled_output else []),
        budget=AudioBudget(timeout=210, max_log_bytes=1024 * 1024),
        owned_tree=True, allow_failure=True)
    # The child must execute every assertion. A zero process status alone does
    # not substitute for its source, keyboard, playback or cleanup checks.
    sys.stdout.write(result.stdout.decode('utf-8', errors='replace'))
    sys.stdout.flush()
    sys.stderr.write(result.stderr.decode('utf-8', errors='replace'))
    sys.stderr.flush()
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio-evidence', type=Path, required=True)
    parser.add_argument('--controlled-output', action='store_true',
                        help='Explicit disclosed PCM device for native UI/source checks; physical output remains NOT_RUN.')
    parser.add_argument('--native-child', action='store_true', help=argparse.SUPPRESS)
    options = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.', 'nvda': 'NOT_RUN'}))
        return 77
    if not options.native_child:
        return _run_owned_native(options)
    # This C-level watchdog remains effective if a native modal assertion or
    # cleanup blocks the GUI thread. It emits stacks and fails this child; the
    # parent still owns and confirms termination of its entire native job.
    faulthandler.dump_traceback_later(180, exit=True)
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    try:
        return _run_native(options)
    finally:
        try:
            if native_handle is not None:
                native_handle.close()
        finally:
            faulthandler.cancel_dump_traceback_later()


def _run_native(options):
    import ctypes
    from ctypes import wintypes
    import wx
    from carr_library.application import LibraryApp
    from carr_library.reader import ebook_audio, live_reading
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.studio.render import ProductionService
    from carr_library.studio.publishing import PublicationService
    from carr_library.ui import ebook_audio_dialog, ebook_model, reading_desk, progress_panel, progress_media
    from carr_library.ui.desktop_app import DesktopFrame

    errors, checks, pressed, audio_proof = [], [], set(), {}
    deadline = monotonic() + 120
    stage, key_pending, finished = 0, False, False
    frame = reader = overlay = service = None
    failure_handler = None
    playback_errors = []
    last_playback = [None, 0.0]
    actual_audio_dialog = ebook_audio_dialog.EbookAudioDialog
    output_proof = {'backend': 'WavePlayer', 'physical_audio_device': 'PENDING',
                    'actual_WavePlayer': 'PENDING'}
    controlled_factory = controlled_proof = None
    if options.controlled_output:
        controlled_factory, controlled_proof = _controlled_output_fixture()

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            detail = traceback.format_exc()
            if failure_handler is not None:
                failure_handler(detail)
            else:
                failed(detail)
            return True

    gui = ProbeApp(False)
    simulator = wx.UIActionSimulator()
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND

    def report_failure(detail):
        errors.append(detail)
        print('FAIL: native recorded EPUB at stage ' + str(stage) + '\n' + detail,
              file=sys.stderr, flush=True)

    def playback_snapshot():
        if overlay is None or overlay.closed:
            return {'overlay': 'absent' if overlay is None else 'closed'}
        transport = overlay.transport
        return {
            'stage': stage, 'busy': overlay.busy,
            'cancelled': overlay.cancelled.is_set(),
            'status': overlay.status.GetValue()[:2000],
            'player': None if overlay.player is None else overlay.player.state,
            'ticket': None if transport is None else transport.ticket,
            'active': None if transport is None else transport.active,
            'interrupted': None if transport is None else transport.interrupted,
            'prepared': transport is not None and transport.prepared is not None,
            'pending': overlay._pending is not None,
            'reported_errors': list(playback_errors),
        }

    def current_owners():
        child = getattr(reader, '_audio_dialog', None) if reader is not None else None
        return child or overlay, reader

    abort = _OwnedModalAbort(wx, gui, current_owners, report_failure)

    def failed(detail):
        report_failure(detail)
        abort.request()

    def select_failure_handler(value):
        nonlocal failure_handler
        failure_handler = value

    _nested_modal_failure_acceptance(wx, gui, check, select_failure_handler)

    def guarded(callback):
        def invoke():
            if finished or errors:
                return
            try:
                callback()
            except (Exception, KeyboardInterrupt):
                failed(traceback.format_exc())
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

    binding = _acceptance_bindings()
    def verify_audio(runtime):
        checked = binding.verify_audio_evidence(runtime, options.audio_evidence)
        if audio_proof and checked != audio_proof:
            raise AssertionError('External audio executable identity changed during native EPUB reading.')
        audio_proof.update(checked)

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
            if controlled_factory is not None:
                def controlled_dialog(*args, **kwargs):
                    return actual_audio_dialog(*args, player_factory=controlled_factory, **kwargs)
                ebook_audio_dialog.EbookAudioDialog = controlled_dialog
            source_identity = binding.verify_installed_sources(ebook_audio, ebook_audio_dialog, ebook_model,
                reading_desk, live_reading, progress_panel, progress_media)
            check('exact installed source and native reader modules', bool(source_identity))
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
                    raise TimeoutError('Native recorded-EPUB acceptance exceeded its deadline: '
                                       + json.dumps(playback_snapshot(), ensure_ascii=False))
                try:
                    if 2 <= stage <= 9:
                        observed = playback_snapshot()
                        now = monotonic()
                        if observed != last_playback[0] or now - last_playback[1] >= 5:
                            print('OBSERVED: actual playback ' + json.dumps(observed, ensure_ascii=False), flush=True)
                            last_playback[:] = [observed, now]
                        if playback_errors and stage < 9:
                            raise AssertionError('The real recorded-audio owner reported: '
                                                 + json.dumps(observed, ensure_ascii=False))
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
                        expected_highlight = expected_page.raw_text[
                            overlay.session.chapter.cues[0].start:overlay.session.chapter.cues[0].end]
                        print('CHECK: exact native Unicode selection; status='
                              + repr(overlay.status.GetValue())
                              + '; expected=' + repr(expected_highlight), flush=True)
                        actual_highlight = overlay.text.GetStringSelection()
                        print('OBSERVED: native selection=' + repr(actual_highlight)
                              + '; positions=' + repr(overlay.text.GetSelection()), flush=True)
                        check('native source highlight is an exact Unicode fragment',
                              actual_highlight == expected_highlight)
                        check('existing progress observes the owned transport', overlay.transport.observed
                              in frame.progress_panel.media.players
                              and overlay.progress is frame.operation_progress
                              and frame.operation_progress.panel is frame.progress_panel)
                        actual_error = overlay._error
                        def observed_error(error):
                            # Observe the existing error path; its real cleanup and
                            # user-visible report still run without substituted results.
                            playback_errors.append(type(error).__name__ + ': ' + str(error))
                            actual_error(error)
                        overlay._error = observed_error
                        stage = 2
                        press(named(overlay.dialog, 'Play from selected segment'))
                    elif stage == 2 and overlay.player.state == 'playing':
                        check(('controlled PCM device' if options.controlled_output else 'real WavePlayer')
                              + ' opens the exact measured PCM interval', overlay.transport.prepared.frames == 96000)
                        if options.controlled_output:
                            check('controlled device retains the real prepared WAV handle',
                                  overlay.player.device.stream is not None
                                  and overlay.player.device.path == overlay.transport.prepared.path)
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
                # This probe qualifies the retained recording, not local voice
                # discovery. Keep the real shared controller without loading a
                # separate voice runtime beside the recorded-audio operation.
                reader = reading_desk.ReadingDialog(frame, desk, ReadingState(library.root), guard,
                    live_factory=lambda source, **options: live_reading.LiveReader(source,
                        discover=lambda **unused: (), **options))
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
                    verify_audio(service._runtime())
                    check('installed source mapping remains unchanged',
                          binding.verify_installed_sources(ebook_audio, ebook_audio_dialog,
                              ebook_model, reading_desk, live_reading, progress_panel, progress_media) == source_identity)
                gui.ExitMainLoop()

            wx.CallAfter(guarded(open_reader))
            gui.MainLoop()
        finally:
            ebook_audio_dialog.EbookAudioDialog = actual_audio_dialog
            abort.stop()
            for key in tuple(pressed):
                simulator.KeyUp(key)
                pressed.discard(key)
            for owner in (overlay, reader):
                if owner is not None:
                    owner.close()
                    owner.executor.shutdown(wait=True, cancel_futures=True)
                    if owner is reader:
                        future = reader.live.core.close()
                        if future is not None:
                            future.result(10)
            if service is not None:
                service.close()
            if frame is not None:
                frame.cancel_event.set(); frame._closing = True
                frame.executor.shutdown(wait=True, cancel_futures=True)
                frame.operation_progress.close(); frame.Destroy()
            gui.Yield()
        if errors:
            raise RuntimeError('\n'.join(errors))
        if controlled_proof is not None:
            output_proof = controlled_proof()
        else:
            output_proof.update(physical_audio_device='PASS', actual_WavePlayer='PASS')
    print(json.dumps({'result': 'PASS', 'checks': checks, 'audio_runtime': audio_proof,
        'source_identity': source_identity,
        'audio': ('Actual local FFmpeg with disclosed controlled PCM output'
                  if options.controlled_output else 'Actual local FFmpeg and Windows playback of synthetic PCM tones'),
        'output_mode': output_proof, 'listening_quality': 'NOT_RUN',
        'native_keyboard': 'PASS', 'epubcheck': 'NOT_RUN', 'third_party_reader': 'NOT_RUN',
        'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN'}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
