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
    from carr_library.assistant import AssistantService, Plan, Scope, Step
    from carr_library.assistant.production import LocalProduction
    from carr_library.ui.assistant_dialog import show_assistant
    import wx

    def offline(*args, **kwargs):
        raise OSError('Network blocked during local audio acceptance.')

    socket.create_connection = offline
    socket.socket.connect = offline
    socket.socket.connect_ex = offline
    from carr_library.audio.runtime import resolve_ffmpeg
    ffmpeg = resolve_ffmpeg()
    assert ffmpeg is not None, 'No verified local FFmpeg runtime is available.'
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
        assistant_output = root / 'assistant-output'
        assistant_output.mkdir()
        producer = LocalProduction(library.root, registry=registry, ffmpeg=ffmpeg)
        assistant = AssistantService(library, production=producer)
        literal = 'Narrator: This label remains spoken. [pause 1] is literal source text.'
        job = assistant.submit(Plan(Scope(output_root=assistant_output, max_bytes=128 * 1024 ** 2), (
            Step('audio.produce', {'text': literal, 'voice_ids': [voice], 'language': 'en', 'stems': False}),)))
        report = assistant.run(job)
        assert report.status == 'complete', report.message
        assert report.results[0]['input_kind'] == 'narration'
        assert report.results[0]['source_sha256'] == sha256(literal.encode('utf-8')).hexdigest()
        assert wav_info(report.results[0]['output']).frames > 0
        assert assistant.resume(job).status == 'complete'
        checks.append('actual local assistant to SAPI/FFmpeg production and verified resume')
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
            def script_has_focus():
                focused = wx.Window.FindFocus()
                while focused is not None and focused is not studio.script:
                    focused = focused.GetParent()
                return focused is studio.script
            wait_for(script_has_focus, 'The editable script did not receive native focus.', timeout=5)
            studio.preview()
            application.Yield()
            assert studio.plan_text.GetValue() == controller.preview()
            assert controller.script == script, 'Native preview changed the original script.'
            assert studio.playback_position.GetName() == 'Playback position and state'
            assert studio.seek_seconds.GetName() == 'Seek to seconds'
            studio.read_playback_position()
            assert 'Stopped' in studio.status.GetValue()
            checks.extend(['native Studio named controls', 'real editable script focus', 'native voice list',
                           'native plan preview', 'project save and readback', 'native playback time and seek controls'])
            studio.dialog.Hide()

            native_result = {'complete': False, 'errors': []}
            def ready(dialog, controls):
                deadline = monotonic() + 20
                def press(control):
                    event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
                    event.SetEventObject(control)
                    assert control.GetEventHandler().ProcessEvent(event)
                def fail(exc):
                    native_result['errors'].append(str(exc))
                    press(controls['close'])
                def completed():
                    try:
                        if controls['run'].IsEnabled():
                            assert controls['review'].GetValue().startswith('complete: 1 of 1'), controls['review'].GetValue()
                            native_result['complete'] = True
                            press(controls['close'])
                        else:
                            assert monotonic() < deadline, 'Native assistant did not finish.'
                            wx.CallLater(25, completed)
                    except Exception as exc:
                        fail(exc)
                try:
                    assert controls['budget'].GetName() == 'Per-operation output and audio working-disk budget (MiB)'
                    controls['instruction'].SetValue('List libraries')
                    press(controls['preview'])
                    assert controls['run'].IsEnabled()
                    assert 'LOCAL ONLY' in controls['review'].GetValue()
                    press(controls['run'])
                    wx.CallLater(25, completed)
                except Exception as exc:
                    fail(exc)
            show_assistant(frame, production=producer, on_ready=ready)
            assert native_result['complete'] and not native_result['errors'], native_result
            checks.append('native assistant plan preview, actual scoped execution and budget controls')
        finally:
            studio.closed = True
            studio.timer.Stop()
            studio.playback_timer.Stop()
            studio.cancelled.set()
            studio.playback.close()
            studio.executor.shutdown(wait=True, cancel_futures=True)
            studio.dialog.Destroy()
            frame._closing = True
            frame.cancel_event.set()
            frame.executor.shutdown(wait=True, cancel_futures=True)
            frame.Destroy()
            application.Yield()
    print(json.dumps({'status': 'PASS', 'checks': checks, 'sapi_voices_available': len(voices),
        'distinct_voices_used': min(3, len(voices)), 'duration_seconds': output['duration_seconds'],
        'measurements': output['measurements'], 'manual_nvda': 'NOT_RUN',
        'physical_playback_device': 'NOT_RUN', 'neural_music': 'NOT_RUN',
        'native_ukrainian_slovak_quality': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
