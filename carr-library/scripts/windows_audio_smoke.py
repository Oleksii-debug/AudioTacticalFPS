"""Native Windows evidence; no API account, model download or private recording.

Uses genuine installed SAPI voices, FFmpeg and wx controls. This automated
probe does not claim manual NVDA, native-speaker quality or audio-device tests.
"""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import socket
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import uuid


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'windows_execution': 'NOT_RUN',
                          'reason': 'Native Windows is required.', 'nvda': 'NOT_RUN'}))
        return 77
    if sys.flags.optimize:
        raise RuntimeError('Native acceptance requires assertions to remain enabled.')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    args = parser.parse_args()
    import carr_library
    from carr_library.audio_runtime import activate_bundled_audio_runtime
    tools = activate_bundled_audio_runtime(args.bundle)
    if tools is None:
        raise RuntimeError('Native audio acceptance requires the verified bundled runtime.')
    installed = args.bundle / '.venv'
    package = Path(carr_library.__file__).resolve()
    if (Path(sys.prefix).resolve() != installed.resolve()
            or not package.is_relative_to((installed / 'Lib/site-packages/carr_library').resolve())):
        raise RuntimeError('Native audio acceptance must run the installed wheel from this bundle.')
    manifest = json.loads((args.bundle / 'bundle-manifest.json').read_text(encoding='utf-8'))
    source = {key: manifest.get(key) for key in ('source_commit', 'source_tree')}
    if any(not isinstance(value, str) or not re.fullmatch('[0-9a-f]{40}', value)
           for value in source.values()):
        raise RuntimeError('The tested bundle must identify its exact source commit and tree.')
    from carr_library.audio.engines import SAPI5Engine, VoiceRegistry
    from carr_library.audio.production import parse_script, plan_narration, render_project
    from carr_library.audio.ffmpeg import find_local_tool, wav_info
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
    ffmpeg = find_local_tool('ffmpeg')
    if ffmpeg != Path(tools['ffmpeg']):
        raise RuntimeError('Default discovery did not select the verified bundled FFmpeg.')
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
    checks = ['installed wheel and sealed bundle source identity', 'verified default FFmpeg discovery']
    cases = ['installed_bundle_identity', 'verified_default_ffmpeg_discovery']
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
        cases.extend(['actual_sapi_synthesis', 'actual_ffmpeg_music_stereo_render',
                      'measured_duration_clipping', 'chapters_subtitles_stems', 'original_script_unchanged'])
        second = render_project(plan, root / 'render', engine=registry, ffmpeg=ffmpeg)
        assert second['run_key'] == output['run_key'] and second['files'] == output['files']
        checks.append('verified complete output reused without resynthesis')
        cases.append('verified_render_resume')

        literal = '# Literal heading\nNarrator: This label remains text.\n[pause 0.5]\nText after the brackets.'
        narration = plan_narration(literal, voice=voice)
        narrated = render_project(narration, root / 'literal-render', engine=registry, ffmpeg=ffmpeg)
        assert narration.input_kind == 'narration' and narration.original_script == literal
        assert ''.join(event.text for scene in narration.scenes for event in scene.events) == literal
        assert wav_info(narrated['output']).frames > 0 and narrated['measurements']['clipped'] == 0
        checks.append('actual literal narration preserves source that resembles script directions')
        cases.append('literal_narration_render')

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
        cases.append('windows_credential_vault')

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
        labels = [frame.menu_bar.GetMenuLabel(index) for index in range(frame.menu_bar.GetMenuCount())]
        mnemonics = [match.group(1).casefold() for label in labels
                     if (match := re.search(r'(?<!&)&(?!&)(.)', label))]
        assert len(mnemonics) == len(set(mnemonics)), 'Top-level native menu mnemonics collide.'
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
            cases.extend(['native_studio_named_controls', 'native_studio_script_focus',
                          'native_studio_voice_list', 'native_studio_plan_preview', 'saved_project_round_trip'])
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
    print(json.dumps({'status': 'PASS', 'windows_execution': 'PASS', **source,
        'execution_context': 'installed-wheel', 'cases': cases, 'checks': checks,
        'check_count': len(cases), 'sapi_voices_available': len(voices),
        'distinct_voices_used': min(3, len(voices)), 'duration_seconds': output['duration_seconds'],
        'measurements': output['measurements'], 'manual_nvda': 'NOT_RUN',
        'neural_music': 'NOT_RUN', 'native_ukrainian_slovak_quality': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
