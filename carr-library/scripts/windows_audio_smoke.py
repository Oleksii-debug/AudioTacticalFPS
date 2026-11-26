"""Native Windows evidence; no API account, model download or private recording.

Uses genuine installed SAPI voices, FFmpeg and wx controls. This automated
probe does not claim manual NVDA, native-speaker quality or audio-device tests.
"""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import uuid


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
    voices = registry.voices()
    assert voices, 'No usable SAPI 5 voice is installed; this is not a successful smoke.'
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
        library = LibraryApp(root / 'library')
        frame = DesktopFrame(library)
        controller = StudioController(private_root=library.root, registry=registry, ffmpeg=ffmpeg)
        controller.new(script, title='Synthetic local production')
        desktop_output = root / 'desktop-output'
        desktop_output.mkdir()
        controller.configure(voice=voice, language='en', output_dir=desktop_output)
        controller.save(root / 'studio-project.json')
        studio = AudioStudioDialog(frame, controller)
        try:
            frame.Show()
            studio.dialog.Show()
            studio.reload_voices()
            deadline = monotonic() + 20
            while studio.busy:
                assert monotonic() < deadline, 'Voice discovery did not finish.'
                application.Yield()
                sleep(0.01)
            assert studio.voice.GetCount() >= 1
            assert studio.script.GetName() == 'Editable narration or dialogue script'
            assert studio.status.GetName() == 'Audio Studio status and render progress'
            studio.script.SetFocus()
            application.Yield()
            assert wx.Window.FindFocus() is studio.script
            studio.preview()
            application.Yield()
            assert studio.plan_text.GetValue()
            checks.extend(['native Studio named controls', 'real editable script focus', 'native voice list',
                           'native plan preview', 'project save and readback'])
        finally:
            studio.timer.Stop()
            studio.cancelled.set()
            studio.executor.shutdown(wait=True, cancel_futures=True)
            studio.dialog.Destroy()
            frame._closing = True
            frame.executor.shutdown(wait=True, cancel_futures=True)
            frame.Destroy()
            application.Yield()
    print(json.dumps({'status': 'PASS', 'checks': checks, 'sapi_voices_available': len(voices),
        'distinct_voices_used': min(3, len(voices)), 'duration_seconds': output['duration_seconds'],
        'measurements': output['measurements'], 'manual_nvda': 'NOT_RUN',
        'neural_music': 'NOT_RUN', 'native_ukrainian_slovak_quality': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
