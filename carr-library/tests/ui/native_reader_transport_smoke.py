"""Installed Windows source-narration UI and optional mandatory device check.

Run with ``python -I -B tests/ui/native_reader_transport_smoke.py --ui-only``
for actual wx/source/SAPI/FFmpeg acceptance on a runner without an audio device.
Without --ui-only an actual WinMM PASS is required; unavailable exits77. Neither
mode claims human listening, voice-language quality or NVDA acceptance.
"""
import argparse
from hashlib import sha256
from importlib import metadata
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--ui-only', action='store_true', help='Do not request an audio output device; report this explicitly.')
    options = parser.parse_args(argv)
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Actual Windows wx/SAPI is required.',
                          'manual_nvda': 'NOT_RUN', 'manual_listening': 'NOT_RUN'}))
        return 77
    import wx
    from carr_library.application import LibraryApp
    from carr_library.audio.playback import native_playback_probe
    from carr_library.reader.live_reader import select_source
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.reading_desk import ReaderController
    from carr_library.ui.live_reader import LiveReaderDialog
    import carr_library.ui.playback_controls as controls_module
    import carr_library.reader.live_reader as narrator_module

    distribution = metadata.distribution('universal-research-library')
    package_root = Path(distribution.locate_file('carr_library')).resolve()
    for module in (controls_module, narrator_module):
        assert Path(module.__file__).resolve().is_relative_to(package_root), 'The UI did not load from the installed distribution.'
    device = {'status': 'NOT_RUN', 'reason': 'Explicit --ui-only mode; no output device requested.'}
    if not options.ui_only:
        device = native_playback_probe()
        if device['status'] != 'PASS':
            print(json.dumps({'status': device['status'], 'audio_device': device,
                              'manual_listening': 'NOT_RUN', 'manual_nvda': 'NOT_RUN'}))
            return 77 if device['status'] == 'UNAVAILABLE' else 1
    application = wx.App(False)
    def wait_for(predicate, message, timeout=45):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            application.Yield()
            if predicate():
                return
            time.sleep(.01)
        raise AssertionError(message)
    def click(button):
        assert button is not None and button.IsEnabled(), 'A required native button is unavailable.'
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        button.GetEventHandler().ProcessEvent(event)
    checks = []
    with TemporaryDirectory(prefix='carr-native-reader-transport-') as temporary:
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        workspace = library.create_workspace('Synthetic reader transport')
        raw = 'This is an exact synthetic source.\r\n[pause 2] remains literal text for narration.'
        book = library.add_text(workspace, 'Synthetic source', raw)
        source = library.assets.resolve(book['asset'])
        original = sha256(source.read_bytes()).hexdigest()
        frame = DesktopFrame(library)
        desk = ReadingDesk(library, workspace, book['edition'])
        def guard():
            if frame.library is not library or frame._closing:
                raise ValueError('The source library changed.')
        reader = ReaderController(desk, ReadingState(library.root), guard=guard)
        page = reader.start()
        dialog = LiveReaderDialog(frame, frame, reader, select_source(page))
        try:
            frame.Show()
            dialog.dialog.Show()
            dialog.dialog.Raise()
            dialog.reload_voices()
            wait_for(lambda: not dialog.busy, 'Installed local voice discovery did not complete.')
            index = next((i for i, voice in enumerate(dialog.voices)
                          if voice.engine == 'sapi5' and any(lang.lower().startswith('en') for lang in voice.languages)), None)
            assert index is not None, 'An actual installed English SAPI voice is required.'
            dialog.voice.SetSelection(index)
            dialog.voice_changed()
            dialog.autoplay.SetValue(False)
            dialog.volume.SetValue(25)
            click(dialog.render)
            wait_for(lambda: not dialog.busy and not dialog.playback.busy,
                     'Actual source narration or PCM verification did not finish.')
            prepared = dialog.playback.controller.prepared
            assert prepared is not None, dialog.status.GetValue() + '\n' + dialog.playback.status.GetValue()
            assert prepared.owner.source == page.location
            assert sha256(source.read_bytes()).hexdigest() == original
            assert dialog.playback.status.GetName() == 'Playback state, position, duration and timing accuracy'
            assert dialog.voice.GetName() == 'Installed local voice; choose explicitly'
            checks.extend(['installed module origin', 'native named source and playback controls',
                           'actual SAPI and FFmpeg literal source narration', 'verified PCM owner binding', 'unchanged original source'])
            dialog.language.SetFocus()
            wait_for(lambda: wx.Window.FindFocus() is dialog.language, 'Native language control did not receive focus.', 5)
            for _ in range(100):
                dialog.playback.poll()
                application.Yield()
            wait_for(lambda: not dialog.playback.busy, 'Playback status query did not finish.', 5)
            assert wx.Window.FindFocus() is dialog.language, 'Background playback status stole native focus.'
            checks.append('100 background update requests preserve native focus')
            if not options.ui_only:
                click(dialog.playback.play_button)
                wait_for(lambda: not dialog.playback.busy and dialog.playback.controller.player.state == 'playing',
                         'Actual native playback did not start.', 5)
                click(dialog.playback.pause_button)
                wait_for(lambda: not dialog.playback.busy and dialog.playback.controller.player.state == 'paused',
                         'The native pause event did not pause playback.', 5)
                dialog.playback.seek.SetValue('0.2')
                click(wx.FindWindowByName('Seek to entered position', dialog.playback.window))
                wait_for(lambda: not dialog.playback.busy and dialog.playback.controller.player.state == 'paused'
                         and abs(dialog.playback.controller.player.position_seconds - .2) < .05,
                         'Native seek did not retain the paused state.', 5)
                click(dialog.playback.pause_button)
                wait_for(lambda: not dialog.playback.busy and dialog.playback.controller.player.state == 'playing',
                         'The native resume event did not resume playback.', 5)
                dialog.playback.sleep_minutes.SetValue('0.01')
                click(wx.FindWindowByName('Apply sleep timer', dialog.playback.window))
                wait_for(lambda: not dialog.playback.busy and dialog.playback.controller.player.state == 'stopped',
                         'The sleep timer did not stop the device.', 5)
                checks.extend(['native Play/Pause/Seek/Resume events', 'sleep timer confirms native device stop'])
            # A changed main library withdraws this source without editing it.
            frame.library = LibraryApp(root / 'new-library')
            dialog.playback.poll()
            wait_for(lambda: not dialog.playback.busy and dialog.playback.controller.prepared is None,
                     'A changed data folder did not withdraw the old recording.', 5)
            checks.append('changed library withdraws old playback capability')
            assert sha256(source.read_bytes()).hexdigest() == original
        finally:
            dialog._dispose()
            wait_for(lambda: dialog.playback.controller.closed and dialog.narrator.directory is None,
                     'Source narration did not release its worker, player and temporary output.', 10)
            if dialog.window_alive:
                dialog.dialog.Destroy()
            frame._closing = True
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            frame.Destroy()
            application.Yield()
    print(json.dumps({'status': 'PASS', 'scope': 'native source narration UI' if options.ui_only else 'native source narration UI and WinMM device',
                      'checks': checks, 'audio_device': device, 'manual_nvda': 'NOT_RUN',
                      'manual_listening': 'NOT_RUN', 'voice_quality': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
