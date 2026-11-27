"""Actual wx owner-package controls and isolated synthetic API; no neural inference."""
import ast
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import traceback
import zipfile


def _synthetic_child_command(command, runtime):
    assert len(command) >= 4, f'Incomplete synthetic child command: {command!r}'
    selected = Path(command[0])
    expected = Path(runtime) / 'python/python.exe'
    try:
        same_file = selected.samefile(expected)
    except OSError as error:
        raise AssertionError(f'Cannot identify the synthetic interpreter: {selected}; expected {expected}') from error
    # Windows TEMP can use an 8.3 spelling while manifest.asset() resolves the
    # same file to its long name. Require file identity, not that spelling.
    assert same_file, f'Unexpected synthetic interpreter: {selected}; expected the same file as {expected}'
    assert command[1:4] == ['-I', '-B', '-u'], f'Unexpected synthetic child isolation flags: {command[1:4]!r}'
    return [sys.executable, *command[1:]]


def main():
    if sys.platform not in ('win32', 'darwin') and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'A native display is required.'}))
        return 77
    import wx
    from carr_library.audio import model_voices
    from carr_library.audio.voice_settings import VoiceSettingsStore
    from carr_library.audio.voices import VoiceRegistry
    from carr_library.ui.voice_packages import VoicePackageDialog

    # Test-owned source constants only. No private source or model is loaded.
    fixture_path = Path(__file__).resolve().parents[1] / 'audio' / 'test_voice_packages.py'
    constants = {}
    for node in ast.parse(fixture_path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {'POLICY', 'PACKAGES', 'ASSETS', 'WORKER'}:
                    constants[target.id] = ast.literal_eval(node.value)
    assert set(constants) == {'POLICY', 'PACKAGES', 'ASSETS', 'WORKER'}
    failures, checks, active, child_failures = [], [], [None], []
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return False
    gui = ProbeApp(False)
    def abort(message):
        failures.append(message)
        if active[0] is not None:
            active[0].close()
        gui.ExitMainLoop()
    def guarded(action):
        try:
            action()
        except BaseException:
            abort(traceback.format_exc())
    def later(action):
        return wx.CallLater(60, lambda: guarded(action))
    def idle(action):
        later(lambda: idle(action)) if active[0].busy else action()
    def click(button):
        assert button.IsShown() and button.IsEnabled()
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        assert button.GetEventHandler().ProcessEvent(event)
    def hard_timeout():
        print(json.dumps({'status': 'FAIL', 'reason': 'Native reference package probe exceeded90seconds.'}), flush=True)
        os._exit(3)
    timer = threading.Timer(90, hard_timeout)
    timer.daemon = True
    timer.start()
    original_run = model_voices._run
    try:
        with TemporaryDirectory(prefix='carr-native-owner-package-') as temporary:
            base = Path(temporary)
            runtime = base / 'runtime'
            runtime.mkdir()
            assets = {'python/python.exe': b'INERT synthetic interpreter marker',
                'nvda_worker.py': constants['WORKER'].encode(),
                'voice_policy.py': constants['POLICY'].encode(),
                'voice_packages.py': constants['PACKAGES'].encode(),
                'voice_assets.py': constants['ASSETS'].encode(),
                'models/Holos/model.bin': b'INERT model',
                'models/OmniVoice/model.bin': b'INERT model',
                'models/voices/catalog.json': b'{"voices":{},"languages":{"uk":"Ukrainian","sk":"Slovak"}}'}
            for name, data in assets.items():
                path = runtime / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            manifest = runtime / 'manifest.json'
            manifest.write_bytes(json.dumps({'schema_version': 1, 'engine': 'dordiai-voices',
                'model_id': 'native-synthetic-owner-api', 'revision': 'f' * 40,
                'license': 'Synthetic fixture only; no voice assets',
                'files': {name: sha256(data).hexdigest() for name, data in assets.items()},
                'settings': {'protocol': 'nvda-worker-jsonl-pcm24k-v1', 'profile_languages': {}},
                'license_accepted': True, 'trusted_weights': True, 'personal_noncommercial': True}).encode())
            library = base / 'library'
            VoiceSettingsStore(library).register({'include_sapi': False, 'ffmpeg': False,
                'espeak_ng': False, 'dordiai_manifest': str(manifest)}, expected_revision=0)
            def actual_synthetic_child(command, **kwargs):
                try:
                    return original_run(_synthetic_child_command(command, runtime), **kwargs)
                except BaseException:
                    detail = traceback.format_exc()
                    child_failures.append(detail)
                    print(json.dumps({'event': 'synthetic-owner-child-failure', 'traceback': detail}), flush=True)
                    raise
            model_voices._run = actual_synthetic_child
            reference_data = {'sample.wav': b'INERT reference fixture', 'sample.txt': b'INERT reference text'}
            document = {'format': 'dordiai-voice-package', 'format_version': 1,
                'id': 'fixture', 'version': '1.0.0', 'name': 'Native synthetic reference',
                'engine': {'id': 'omnivoice', 'compatibility_version': 1},
                'languages': ['uk', 'sk'], 'styles': ['neutral'],
                'provenance': {'source': 'fixture:local', 'license': 'Synthetic fixture only'},
                'files': {name: {'sha256': sha256(data).hexdigest(), 'size_bytes': len(data),
                    'role': 'reference_audio' if name.endswith('.wav') else 'reference_text'}
                    for name, data in reference_data.items()}}
            archive = base / 'reference.zip'
            with zipfile.ZipFile(archive, 'w') as writer:
                writer.writestr('voice-package.json', json.dumps(document).encode())
                for name, data in reference_data.items():
                    writer.writestr(name, data)
            frame = wx.Frame(None, title='Native owner reference package probe', size=(1000, 950))
            gui.SetTopWindow(frame)
            frame.Show()
            def run():
                control = VoicePackageDialog(frame, library)
                active[0] = control
                def loaded():
                    assert control.dialog.IsModal()
                    assert control.backend.GetName() and control.rights.GetName()
                    assert control.status.GetName() and control.configure_button.IsEnabled()
                    control.backend.SetStringSelection('omnivoice')
                    control.reference_model.SetValue('models/OmniVoice')
                    click(control.configure_button)
                    later(lambda: idle(configured))
                def configured():
                    assert control.revision == 2, f'Revision {control.revision!r}; {control.status.GetValue()}'
                    assert control.backend.GetStringSelection() == 'omnivoice'
                    checks.append('native backend controls save only existing pinned local model/reference folders')
                    # The native file picker is not claimed here: the explicit
                    # test-selected local file enters the same async review.
                    control.start(lambda: control.manager.inspect(archive, cancelled=control.cancelled.is_set),
                                  control.reviewed)
                    later(lambda: idle(reviewed))
                def reviewed():
                    assert control.review is not None, control.status.GetValue()
                    assert not control.rights.GetValue() and control.supported.GetValue() == ''
                    click(control.apply_button)
                    assert 'permission' in control.status.GetValue()
                    assert not (runtime / 'models/voices/packages').exists()
                    control.rights.SetValue(True)
                    control.supported.SetValue('uk')
                    click(control.apply_button)
                    later(lambda: idle(installed))
                def installed():
                    voice, = VoiceRegistry.for_library(library).list_voices()
                    assert voice.id == 'dordiai-voices:fixture' and voice.languages == ('uk',)
                    assert voice.candidate_languages == ('sk',)
                    assert not control.rights.GetValue()
                    checks.append('native rights gate and exact reviewed language selection install data through actual child protocol')
                    control.profile.SetValue('fixture')
                    control.version.SetValue('1.0.0')
                    click(control.versions_button)
                    later(lambda: idle(listed))
                def listed():
                    assert '1.0.0' in control.status.GetValue()
                    click(control.review_button)
                    later(lambda: idle(version_reviewed))
                def version_reviewed():
                    assert control.review is not None and control.review.operation == 'activate'
                    assert not control.rights.GetValue()
                    control.rights.SetValue(True)
                    control.supported.SetValue('sk')
                    click(control.apply_button)
                    later(lambda: idle(activated))
                def activated():
                    registry = VoiceRegistry.for_library(library)
                    assert registry.choose_voice('sk').id == 'dordiai-voices:fixture'
                    assert registry.voices_for_language('uk') == ()
                    assert wx.Window.FindFocus() is control.status
                    checks.append('native installed-version review resets consent and rebinds one existing profile identity')
                    click(control.close_button)
                later(lambda: idle(loaded))
                control.dialog.ShowModal()
                control.close()
                control.dialog.Destroy()
                active[0] = None
                frame.Destroy()
                gui.ExitMainLoop()
            later(run)
            gui.MainLoop()
    finally:
        model_voices._run = original_run
        timer.cancel()
    print(json.dumps({'status': 'FAIL' if failures else 'PASS', 'checks': checks, 'failures': failures,
        'child_failures': child_failures,
        'owner_api': 'SYNTHETIC API through actual isolated child',
        'native_file_picker': 'NOT_RUN', 'model_inference': 'NOT_RUN', 'reference_quality': 'NOT_RUN',
        'nvda_speech': 'NOT_RUN'}, ensure_ascii=True))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())

