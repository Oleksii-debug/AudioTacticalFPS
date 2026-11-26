"""Installed Windows SAPI + native live-reader controls, without external calls.

Physical sound-device playback and manual NVDA remain separate acceptance gates.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from time import monotonic


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Windows is required.'}))
        return 77
    import wx
    from carr_library.application import LibraryApp
    from carr_library.speech import SpeechSettings, VoiceRegistry
    from carr_library.speech.session import ReaderDraftStore, ReaderResume
    from carr_library.speech import ReadingDocument
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.voice_reader import show_speech_reader

    def offline(*args, **kwargs):
        raise OSError('Network disabled during local voice-reader acceptance.')
    socket.create_connection = offline
    socket.socket.connect = offline
    socket.socket.connect_ex = offline
    checks = []
    with TemporaryDirectory(prefix='carr-live-reader-native-') as folder:
        root = Path(folder)
        library = LibraryApp(root / 'library')
        registry = VoiceRegistry.discover(library_root=library.root)
        voices = [voice for voice in registry.voices() if voice.engine.startswith('sapi5')]
        assert voices, 'No genuine installed SAPI voice was discovered.'
        literal = 'This synthetic source is read locally. [Pause is literal text.]'
        result = registry.synthesize(literal, voices[0].id, root / 'spoken.wav', settings=SpeechSettings())
        assert result.frames > 0 and result.duration_seconds > 0
        assert result.sha256 == sha256(result.path.read_bytes()).hexdigest()
        assert result.text_sha256 == sha256(literal.encode('utf-8')).hexdigest()
        document = ReadingDocument(literal, private_root=library.root)
        store = ReaderDraftStore(library.root)
        audio_key = sha256(json.dumps([literal, voices[0].id, 0, 80], ensure_ascii=False).encode('utf-8')).hexdigest()
        store.save(ReaderResume(document, voices[0].id, cursor=5,
                               audio_seconds=0.1, audio_key=audio_key, audio_sha256=result.sha256))
        assert ReaderDraftStore(library.root).load().audio_sha256 == result.sha256
        checks.extend(['actual shared-registry SAPI output', 'exact literal source and WAV digest',
                       'typed reading/voice/cursor recovery'])
        application = wx.App(False)
        frame = DesktopFrame(library)
        frame.Show()
        deadline = monotonic() + 45
        errors, inspected = [], []

        def inspect():
            dialogs = [window for window in wx.GetTopLevelWindows()
                       if hasattr(window, 'voice_reader_controls')]
            if not dialogs:
                if monotonic() < deadline:
                    wx.CallLater(100, inspect)
                    return
                errors.append('Native live reader did not create its controls.')
                return
            dialog = dialogs[0]
            controls = dialog.voice_reader_controls
            if controls['voices'].GetCount() == 0 and monotonic() < deadline:
                wx.CallLater(100, inspect)
                return
            try:
                assert controls['voices'].GetCount() > 0
                assert controls['read'].IsEnabled()
                assert controls['editor'].GetName() == 'Reading copy (editable)'
                controls['editor'].SetFocus()
                application.Yield()
                assert wx.Window.FindFocus() is controls['editor']
                assert all(controls[name].GetLabel() for name in ('read', 'pause', 'stop', 'seek'))
                inspected.append(True)
            except Exception as exc:
                errors.append(str(exc))
            finally:
                dialog.EndModal(wx.ID_CANCEL)

        try:
            wx.CallLater(100, inspect)
            show_speech_reader(frame, text=literal)
            assert inspected and not errors, errors
            checks.extend(['native named live-reader controls', 'actual installed voice choices',
                           'native editor keyboard focus', 'closed worker and playback resources'])
        finally:
            frame._closing = True
            frame.executor.shutdown(wait=True, cancel_futures=True)
            frame.Destroy()
            application.Yield()
    print(json.dumps({'status': 'PASS', 'checks': checks, 'manual_nvda': 'NOT_RUN',
                      'physical_playback_device': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
