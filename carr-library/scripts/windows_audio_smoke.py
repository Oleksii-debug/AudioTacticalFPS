"""Native Windows evidence; no API account, model download or private recording.

Uses genuine installed SAPI voices, FFmpeg and wx controls. This automated
probe does not claim manual NVDA, native-speaker quality or audio-device tests.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest.mock import patch
import uuid
import wave


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows is required.', 'nvda': 'NOT_RUN'}))
        return 77
    from carr_library.audio.engines import SAPI5Engine, VoiceRegistry
    from carr_library.audio.production import parse_script, render_project
    from carr_library.audio.ffmpeg import wav_info
    from carr_library.providers import WindowsCredentialVault
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui.audio_studio import StudioController, AudioStudioDialog
    import wx

    def offline(*args, **kwargs):
        raise OSError('Network blocked during local audio acceptance.')

    socket.create_connection = offline
    socket.socket.connect = offline
    socket.socket.connect_ex = offline
    ffmpeg = Path(os.environ['CARR_FFMPEG'])
    registry = VoiceRegistry((SAPI5Engine(),))
    voices = tuple(v for v in registry.voices() if any(lang.lower().split('-')[0] == 'en' for lang in v.languages))
    assert voices, 'No usable English SAPI 5 voice is installed; this is not a successful smoke.'
    voice = voices[0].id
    script = (f'[role Narrator voice="{voice}" pan=center language=en]\n'
              f'[role Left voice="{voices[min(1,len(voices)-1)].id}" pan=left language=en]\n'
              f'[role Right voice="{voices[min(2,len(voices)-1)].id}" pan=right language=en]\n'
              '# Synthetic scene one\n[music mood=calm tempo=90 seed=42 gain=0.1]\n'
              'Narrator: This is a locally generated synthetic test.\n'
              'Left: The voice on the left speaks now.\n[pause 0.5]\n'
              'Right: The voice on the right answers.\n'
              '# Synthetic scene two\nNarrator: The project has two chapters.\n')
    checks = []
    with TemporaryDirectory(prefix='carr-audio-native-') as temp:
        root = Path(temp)
        plan = parse_script(script, voice=voice)
        output = render_project(plan, root / 'render', engine=registry, ffmpeg=ffmpeg)
        info = wav_info(output['output'])
        assert info.channels == 2 and info.frames > 48000
        assert output['measurements']['clipped'] == 0
        assert len(output['chapters']) == 2 and len(output['stems']) >= 3
        assert plan.original_script == script
        checks.extend(['actual SAPI synthesis', 'actual FFmpeg music and stereo render',
                       'measured duration and clipping', 'chapter, subtitle and stem output', 'immutable source'])
        second = render_project(plan, root / 'render', engine=registry, ffmpeg=ffmpeg)
        assert second['run_key'] == output['run_key'] and second['files'] == output['files']
        checks.append('verified complete output reused without resynthesis')

        vault = WindowsCredentialVault()
        reference, binding = 'url-api-' + uuid.uuid4().hex, sha256(b'synthetic-vault-binding').hexdigest()
        synthetic_secret = 'carr-synthetic-not-an-api-key-' + uuid.uuid4().hex
        try:
            vault.put(reference, binding, synthetic_secret)
            assert vault.get(reference, binding) == synthetic_secret
            try:
                vault.get(reference, sha256(b'wrong-destination').hexdigest())
            except ValueError:
                pass
            else:
                raise AssertionError('Vault destination binding failed.')
        finally:
            vault.delete(reference, binding)
        checks.append('actual Windows Credential Manager round-trip and destination isolation')

        application = wx.App(False)

        def wait_for(predicate, message, *, timeout=20):
            deadline = monotonic() + timeout
            while True:
                application.Yield()
                if predicate():
                    return
                assert monotonic() < deadline, message
                sleep(0.01)

        library = LibraryApp(root / 'library')
        frame = DesktopFrame(library)
        controller = StudioController(private_root=library.root, registry=registry, ffmpeg=ffmpeg)
        controller.new(script, title='Synthetic local production', input_kind='script')
        desktop_output = root / 'desktop-output'
        desktop_output.mkdir()
        controller.configure(voice=voice, language='en', output_dir=desktop_output)
        saved_project = controller.save(root / 'studio-project.json')
        reopened = StudioController(private_root=library.root, registry=registry, ffmpeg=ffmpeg)
        reopened.open(saved_project)
        assert reopened.to_dict() == controller.to_dict(), 'Saved project did not survive readback.'
        studio = AudioStudioDialog(frame, controller)
        try:
            frame.Show()
            studio.dialog.Show()
            studio.dialog.Raise()
            studio.reload_voices()
            wait_for(lambda: not studio.busy, 'Voice discovery did not finish.')
            assert studio.voice.GetCount() >= 1
            assert studio.script.GetName() == 'Editable narration or dialogue script'
            assert studio.status.GetName() == 'Audio Studio status and render progress'
            studio.notebook.SetSelection(0)
            studio.script.SetFocus()
            wait_for(lambda: wx.Window.FindFocus() is studio.script,
                     'The editable script did not receive native focus.', timeout=5)
            studio.preview()
            application.Yield()
            assert studio.plan_text.GetValue() == controller.preview()
            assert controller.script == script, 'Native preview changed the original script.'
            checks.extend(['native Studio named controls', 'real editable script focus', 'native voice list',
                           'native plan preview', 'project save and readback'])

            # Exercise the actual native event -> worker -> completed status
            # path, using a fresh output folder and the real production renderer.
            assert controller.result is None and not list(desktop_output.iterdir())
            saved_bytes = saved_project.read_bytes()
            ui_errors = []
            report_error = studio._error

            def unexpected_dialog(*args, **kwargs):
                message = 'The saved native render fixture unexpectedly requested a modal dialog.'
                ui_errors.append(message)
                raise AssertionError(message)

            def capture_error(error):
                ui_errors.append(str(error))
                report_error(error)

            def render_completed():
                assert not ui_errors, 'Native Render failed: ' + '; '.join(ui_errors)
                return not studio.busy and controller.result is not None

            with (patch.multiple(wx, FileDialog=unexpected_dialog, DirDialog=unexpected_dialog,
                                 MessageDialog=unexpected_dialog, TextEntryDialog=unexpected_dialog,
                                 MessageBox=unexpected_dialog),
                  patch.object(studio, '_error', side_effect=capture_error)):
                assert studio.render_button.IsEnabled()
                event = wx.CommandEvent(wx.EVT_BUTTON.typeId, studio.render_button.GetId())
                event.SetEventObject(studio.render_button)
                assert studio.render_button.GetEventHandler().ProcessEvent(event), 'Render event was not handled.'
                assert studio.busy, 'The native Render button did not start its production worker.'
                wait_for(render_completed, 'The native Render worker did not complete.', timeout=90)
                assert not ui_errors
                native_output = controller.result
                native_path = Path(native_output['output'])
                assert native_path.is_file() and native_path.resolve().is_relative_to(desktop_output.resolve())
                native_info = wav_info(native_path)
                assert native_info.channels == 2 and native_info.frames > 48000
                assert native_output['status'] == 'complete' and not native_output['measurements']['clipped']
                with wave.open(str(native_path), 'rb') as generated:
                    pcm = generated.readframes(min(generated.getnframes(), generated.getframerate() * 10))
                    assert any(pcm), 'The native Render worker produced silent PCM.'
                status = studio.status.GetValue()
                assert status.startswith('Audio render completed.\nMaster: ')
                assert str(native_path) in status
                assert studio.render_button.IsEnabled() and not studio.cancel_button.IsEnabled()
                wait_for(lambda: wx.Window.FindFocus() is studio.status,
                         'Completed render status did not receive native focus.', timeout=5)
                assert controller.script == script, 'Native rendering changed the original script.'
                assert saved_project.read_bytes() == saved_bytes, 'Native rendering changed the saved source project.'
                native_gui_render = {'status': 'PASS', 'frames': native_info.frames,
                    'sample_rate': native_info.rate, 'channels': native_info.channels,
                    'duration_seconds': native_output['duration_seconds'], 'nonzero_pcm': True,
                    'completed_status': True, 'source_unchanged': True, 'dialog_or_worker_errors': len(ui_errors)}
                checks.extend(['native Render button dispatched to production worker',
                               'native render produced actual nonzero PCM',
                               'accessible completed render status and restored controls',
                               'saved source bytes unchanged after native render'])
        finally:
            studio.closed = True
            studio.timer.Stop()
            studio.cancelled.set()
            studio.playback.stop()
            studio.executor.shutdown(wait=True, cancel_futures=True)
            studio.dialog.Destroy()
            frame._closing = True
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            frame.Destroy()
            application.Yield()
    print(json.dumps({'status': 'PASS', 'checks': checks, 'sapi_voices_available': len(voices),
        'distinct_voices_used': min(3, len(voices)), 'duration_seconds': output['duration_seconds'],
        'measurements': output['measurements'], 'native_gui_render': native_gui_render,
        'manual_nvda': 'NOT_RUN',
        'neural_music': 'NOT_RUN', 'native_ukrainian_slovak_quality': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
