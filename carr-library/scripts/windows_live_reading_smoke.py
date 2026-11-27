"""Native live-reader controls and real SAPI synthesis acceptance.

The player below is an explicitly controlled PCM fixture. It establishes native
UI, exact source selection, synthesis and lifecycle behavior without claiming a
physical device, audible output, NVDA acceptance or actual MCI pause timing.
Run tests/speech/probe_windows_player.py separately on an audio-capable machine.
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

    def stop(self):
        self.state, self.position = 'stopped', 0.
        return self.status()

    def close(self):
        if self.stream is not None:
            assert self.path.is_file(), 'The playback WAV was removed before closing the device.'
            self.stream.close()
            self.stream = None
        self.state = 'closed'


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
    from carr_library.speech.local import discover_voices, render_speech
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
            assert result.engine == 'sapi5' and result.duration_seconds > 0
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
                               if voice.engine == 'sapi5']
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
                    reader.close()
            except Exception:
                state['failure'] = traceback.format_exc()
                reader.close()

        reader.dialog.Bind(wx.EVT_TIMER, advance, timer)
        timer.Start(50)
        try:
            reader.show()
        finally:
            timer.Stop()
            completion = controls.core.close()
            if completion is not None:
                completion.result(timeout=65)
            frame.Destroy()
        if state['failure']:
            raise AssertionError(state['failure'])
        assert state['stage'] == 7
        assert len(observed['renders']) == 2 and observed['discovery_roots']
        assert fixture.stream is None
        report = dict(status='PASS', platform=platform.platform(),
                      native_modal_controls='PASS', native_unicode_crlf_selection='PASS',
                      explicit_voice_selection='PASS', actual_sapi_synthesis='PASS',
                      library_configuration_root='PASS', background_focus_preserved='PASS',
                      controlled_pause_resume_stop='PASS', exact_source_resume='PASS',
                      temporary_audio_cleanup='PASS', selected_voice_id=state['voice_id'],
                      renders=observed['renders'], player='CONTROLLED_PCM_TEST_FIXTURE',
                      physical_audio_device='NOT_RUN', actual_local_player='NOT_RUN',
                      audible_listening='NOT_RUN', nvda='NOT_RUN')
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded + '\n', encoding='utf-8')
        print(encoded)
    del native_handle
    del wx_app


if __name__ == '__main__':
    main()
