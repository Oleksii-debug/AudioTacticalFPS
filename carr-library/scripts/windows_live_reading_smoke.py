"""Native live-reader controls and real SAPI synthesis acceptance.

The player below is an explicitly controlled PCM fixture. It establishes native
UI, exact source selection, synthesis and lifecycle behavior without claiming a
physical device, audible output, NVDA acceptance or actual MCI pause timing.
Run scripts/windows_live_player_probe.py separately on an audio-capable machine.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import sys
from tempfile import TemporaryDirectory
import time
import traceback
import wave


class ControlledPCMPlayer:
    """A disclosed test device; retains the actual generated WAV handle."""
    def __init__(self):
        self.state, self.position, self.duration = 'closed', 0., 0.
        self.stream, self.path, self.resume_count = None, None, 0
        self.opened = []

    def available(self):
        return True

    def status(self):
        return dict(state=self.state, position_seconds=self.position,
                    duration_seconds=self.duration, backend='CONTROLLED_PCM_TEST_FIXTURE')

    def open(self, path):
        assert self.stream is None
        with wave.open(str(path)) as audio:
            assert audio.getsampwidth() == 2 and audio.getnchannels() in (1, 2)
            self.duration = audio.getnframes() / audio.getframerate()
            assert self.duration > 0
        self.path = Path(path)
        self.stream = self.path.open('rb')
        self.opened.append(self.path)
        self.state, self.position = 'stopped', 0.
        return self.status()

    def play(self):
        self.state = 'playing'
        return self.status()

    def pause(self):
        if self.state == 'playing':
            self.state = 'paused'
        return self.status()

    def resume(self):
        self.resume_count += 1
        return self.play()

    def advance(self, seconds):
        if self.state == 'playing':
            self.position = min(self.duration, self.position + seconds)
            if self.position == self.duration:
                self.state = 'ended'

    def seek(self, seconds):
        self.position = seconds
        return self.status()

    def stop(self):
        self.state, self.position = 'stopped', 0.
        return self.status()

    def close(self):
        if self.stream is not None:
            assert self.path.is_file(), 'The playback WAV was removed before closing the device.'
            self.stream.close()
            self.stream = None
        self.state = 'closed'


def canonical_copy_probe(wx, library, desk, discover, render):
    """Exercise the real canonical dialog, draft format and shared controller."""
    from carr_library.reader.live_reading import LiveReader
    from carr_library.speech.session import ReaderDraftStore
    from carr_library.speech.source import read_source_range
    from carr_library.ui.live_reading import native_cursor_position
    from carr_library.ui.speech_dialog import show_speech_reader
    original = desk.page(1).raw_text
    fixture = ControlledPCMPlayer()
    frame = wx.Frame(None, title='Canonical reading-copy acceptance host')
    frame.library = library
    frame.Show()
    context = {'stage': 0, 'failure': None, 'ui': None, 'dialog': None, 'timer': None}
    started = time.monotonic()
    slot = 'native-shared-live-reader-acceptance'

    def close_without_prompt():
        # Failure teardown must not open a human save-confirmation dialog.
        if context['timer'] is not None:
            context['timer'].Stop()
        ui = context['ui']
        if ui is not None:
            document = ui['document']()
            document._saved_signature = document._signature()
            ui['close']()

    def tick(event):
        try:
            assert time.monotonic() - started < 180, 'Canonical reading-copy probe timed out.'
            ui = context['ui']
            live, document = ui['live'], ui['document']()
            view = live.core.status()
            if view.state == 'error':
                raise AssertionError(view.message)
            if context['stage'] == 0:
                if not live.ready() or live.core.busy or not live._voices:
                    return
                assert context['dialog'].IsShown() and context['dialog'].IsModal()
                assert 'Reading copy' in ui['editor'].GetName()
                assert document.source_text == original and document.text == original and document.revision == 0
                assert live.voice.GetSelection() == wx.NOT_FOUND
                index = next(i for i, voice in enumerate(live._voices) if voice.id.startswith('sapi5-'))
                live.voice.SetSelection(index)
                ui['editor'].SetValue('Changed ' + original)
                # GetValue is the actual native edited text, not presumed UTF-16 offsets.
                edited = ui['editor'].GetValue()
                selected = edited.splitlines()[0]
                ui['editor'].SetSelection(0, native_cursor_position(ui['editor'], edited, len(selected)))
                context['selected'] = selected
                ui['editor'].SetFocus()
                live.start('selection')
                context['stage'] = 1
            elif context['stage'] == 1 and view.state == 'playing':
                assert view.section.derivative and not live.source.supports_resume
                assert view.section.text[view.chunk_start:view.chunk_end] == context['selected']
                assert wx.Window.FindFocus() is ui['editor']
                assert live.source.document.source_text == original
                fixture.advance(fixture.duration)
                context['stage'] = 2
            elif context['stage'] == 2 and view.state == 'complete' and not live.core.busy:
                button = ui['buttons']['save']
                command = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
                button.GetEventHandler().ProcessEvent(command)
                recovered = ReaderDraftStore(library.root, slot=slot).load()
                assert recovered is not None and recovered.source_locations == (desk.page(1).location,)
                assert recovered.document.source_text == original and recovered.document.text == document.text
                assert recovered.audio_seconds == 0 and recovered.document.undo()
                assert recovered.document.text == original
                assert all(not path.exists() for path in fixture.opened)
                context['stage'] = 3
                context['timer'].Stop()
                ui['close']()
        except Exception:
            context['failure'] = traceback.format_exc()
            close_without_prompt()

    def ready(dialog, ui):
        context.update(dialog=dialog, ui=ui)
        timer = wx.Timer(dialog)
        context['timer'] = timer
        dialog.Bind(wx.EVT_TIMER, tick, timer)
        timer.Start(50)

    try:
        show_speech_reader(frame, source_loader=lambda cancelled: read_source_range(
            desk, 1, 1, cancelled=cancelled), recovery_slot=slot, on_ready=ready,
            live_factory=lambda source, **kwargs: LiveReader(source, discover=discover,
                render=render, player_factory=lambda: fixture, **kwargs))
    finally:
        if context['ui'] is not None:
            completion = context['ui']['live'].core.close()
            if completion is not None:
                completion.result(timeout=65)
        frame.Destroy()
    if context['failure']:
        raise AssertionError(context['failure'])
    assert context['stage'] == 3 and fixture.stream is None
    return dict(canonical_reading_copy='PASS', shared_live_controller='PASS',
                unchanged_native_crlf_revision='PASS', edited_copy_source_binding='PASS',
                canonical_v2_draft_and_undo='PASS')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if sys.platform != 'win32':
        raise RuntimeError('This probe requires actual Windows; no native PASS is available here.')
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()  # Retain the DLL-directory lifetime.
    import wx
    from carr_library.application import LibraryApp
    from carr_library.reader.live_reading import LiveReader
    from carr_library.reader.live_state import LiveState
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.speech.live_adapter import discover_voices, render_speech
    from carr_library.ui.live_reading import reading_range
    from carr_library.ui.reading_desk import ReadingDialog

    wx_app = wx.App(False)
    with TemporaryDirectory(prefix='carr-native-live-reader-') as temporary:
        root = Path(temporary) / 'Українська бібліотека'
        library = LibraryApp(root)
        workspace = library.create_workspace('Native live-reader probe')
        raw = 'Є 🚂.\r\nRepeat source words.\r\nRepeat Ω only.\fNext source location.'
        book = library.add_text(workspace, 'Actual native source', raw, language='uk')
        desk = ReadingDesk(library, workspace, book['edition'])
        first = desk.page(1).raw_text
        start, end = first.rindex('Repeat'), len(first)
        expected = first[start:end]
        fixture = ControlledPCMPlayer()
        observed = {'discovery_roots': [], 'render_roots': [], 'requests': [], 'renders': []}

        def discover(**kwargs):
            assert Path(kwargs['configuration_root']) == root
            observed['discovery_roots'].append(str(kwargs['configuration_root']))
            return discover_voices(**kwargs)

        def render(request, target, **kwargs):
            assert Path(kwargs['configuration_root']) == root
            observed['render_roots'].append(str(kwargs['configuration_root']))
            observed['requests'].append(request.text)
            result = render_speech(request, target, **kwargs)
            assert request.voice_id.startswith('sapi5-') and result.duration_seconds > 0
            observed['renders'].append({'sha256': result.sha256,
                                        'duration_seconds': result.duration_seconds,
                                        'voice_id': result.voice_id})
            return result

        frame = wx.Frame(None, title='Native live-reader acceptance host', size=(1100, 950))
        frame.library = library
        frame.Show()
        reader = ReadingDialog(frame, desk, ReadingState(root), guard=lambda: None,
            live_factory=lambda source, **kwargs: LiveReader(source, discover=discover,
                render=render, player_factory=lambda: fixture, **kwargs))
        controls = reader.live
        timer = wx.Timer(reader.dialog)
        started = time.monotonic()
        state = {'stage': 0, 'failure': None, 'pause_at': None, 'pause_started': None,
                 'voice_id': None}

        def native_position(prefix):
            wanted = prefix.replace('\r\n', '\n').replace('\r', '\n')
            for position in range(reader.text.GetLastPosition() + 1):
                try:
                    actual = reader.text.GetRange(0, position)
                except UnicodeError:
                    continue
                if actual.replace('\r\n', '\n').replace('\r', '\n') == wanted:
                    return position
            raise AssertionError('Native UTF-16/CRLF position could not be mapped to the exact source.')

        def advance(event):
            try:
                assert time.monotonic() - started < 180, 'The native live-reader probe timed out.'
                view = controls.core.status()
                if view.state == 'error':
                    raise AssertionError(view.message)
                stage = state['stage']
                if stage == 0:
                    if reader.controller.current is None or not controls._voices or controls.core.busy:
                        return
                    assert reader.dialog.IsModal() and reader.dialog.IsShown()
                    assert 'source' in reader.text.GetName().lower()
                    assert 'chunk' in controls.status.GetName().lower()
                    assert controls.voice.GetSelection() == wx.NOT_FOUND, 'A voice was selected implicitly.'
                    choices = [(index, voice) for index, voice in enumerate(controls._voices)
                               if voice.id.startswith('sapi5-')]
                    assert choices, 'No actual installed SAPI5 voice; real synthesis acceptance is incomplete.'
                    index, selected = choices[0]
                    state['voice_id'] = selected.id
                    controls.voice.SetSelection(index)
                    reader.text.SetSelection(native_position(first[:start]), native_position(first[:end]))
                    assert reading_range(reader.text, first, 'selection') == (start, end)
                    reader.text.SetFocus()
                    controls.start('selection')
                    state['stage'] = 1
                elif stage == 1 and view.state == 'playing':
                    assert observed['requests'] == [expected]
                    assert wx.Window.FindFocus() is reader.text, 'Background reading stole keyboard focus.'
                    fixture.advance(min(.1, fixture.duration / 4))
                    controls.pause()
                    state['stage'] = 2
                elif stage == 2 and view.state == 'paused':
                    if state['pause_started'] is None:
                        state['pause_started'] = time.monotonic()
                        state['pause_at'] = view.audio_seconds
                        return
                    fixture.advance(.05)
                    assert fixture.position == state['pause_at']
                    if time.monotonic() - state['pause_started'] < .3:
                        return
                    controls.pause()
                    state['stage'] = 3
                elif stage == 3 and view.state == 'playing' and fixture.resume_count == 1:
                    assert fixture.position == state['pause_at']
                    fixture.advance(fixture.duration)
                    state['stage'] = 4
                elif stage == 4 and view.state == 'complete' and not controls.core.busy:
                    saved = LiveState(root).resume(workspace, book['edition'])
                    assert saved.offset == end == saved.end and saved.location == desk.page(1).location
                    reader.text.SetInsertionPoint(native_position(first[:start]))
                    assert reading_range(reader.text, first, 'cursor') == (start, end)
                    controls.start('cursor')
                    state['stage'] = 5
                elif stage == 5 and view.state == 'playing':
                    assert observed['requests'] == [expected, expected]
                    controls.stop()
                    state['stage'] = 6
                elif stage == 6 and not controls.core.busy:
                    assert view.state == 'stopped', view.message
                    assert all(not path.exists() for path in fixture.opened)
                    assert not any(path.is_dir() for path in root.glob('.live-reading-*'))
                    state['stage'] = 7
                    timer.Stop()
                    reader.close()
            except Exception:
                state['failure'] = traceback.format_exc()
                timer.Stop()
                reader.close()

        reader.dialog.Bind(wx.EVT_TIMER, advance, timer)
        timer.Start(50)
        try:
            reader.show()
        finally:
            completion = controls.core.close()
            if completion is not None:
                completion.result(timeout=65)
            frame.Destroy()
        if state['failure']:
            raise AssertionError(state['failure'])
        assert state['stage'] == 7
        assert len(observed['renders']) == 2 and observed['discovery_roots']
        assert fixture.stream is None
        copy_report = canonical_copy_probe(wx, library, desk, discover, render)
        assert len(observed['renders']) == 3
        report = dict(status='PASS', platform=platform.platform(),
                      native_modal_controls='PASS', native_unicode_crlf_selection='PASS',
                      explicit_voice_selection='PASS', actual_sapi_synthesis='PASS',
                      library_configuration_root='PASS', background_focus_preserved='PASS',
                      controlled_pause_resume_stop='PASS', exact_source_resume='PASS',
                      temporary_audio_cleanup='PASS', selected_voice_id=state['voice_id'],
                      renders=observed['renders'], player='CONTROLLED_PCM_TEST_FIXTURE',
                      physical_audio_device='NOT_RUN', actual_local_player='NOT_RUN',
                      audible_listening='NOT_RUN', nvda='NOT_RUN', **copy_report)
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded + '\n', encoding='utf-8')
        print(encoded)
    del native_handle
    del wx_app


if __name__ == '__main__':
    main()
