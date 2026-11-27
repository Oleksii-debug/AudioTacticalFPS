"""Real wx controls, file-backed state and synthetic renderer/player contracts.

No installed voice, hardware output, listening quality or NVDA result is inferred.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import traceback
from types import SimpleNamespace
import wave


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows is required.'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime()
    import wx
    from carr_library.audio.voice_settings import VoiceProfileStore, session_for, snapshot_voice_settings, validate_voice_settings_payload
    from carr_library.reader.narration import NarrationStore, new_project, revise
    from carr_library.ui.narration_editor import open_narration_editor
    from carr_library.ui.local_voice_settings import open_local_voice_settings
    from carr_library.ui import reading_playback, local_voice_settings
    from carr_library.audio import reading, voice_settings
    from carr_library.operations import ResourceBroker
    from carr_library.progress import progress_audio_suppressed
    checks, failures = [], []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            failures.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False
    gui = App(False)

    def pump(predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate():
            if failures:
                raise RuntimeError('\n'.join(failures))
            if time.monotonic() > deadline:
                raise TimeoutError('Native reading control operation timed out.')
            gui.Yield()
            time.sleep(.01)

    def click(button):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        check('native button event: ' + button.GetLabel(), button.GetEventHandler().ProcessEvent(event))

    def safe(dialog, operation):
        try:
            operation()
        except Exception:
            failures.append(traceback.format_exc())
            if dialog.IsModal():
                dialog.EndModal(wx.ID_CANCEL)

    def expire():
        failures.append('Native reading/profile controls exceeded their watchdog.')
        for dialog in wx.GetTopLevelWindows():
            if isinstance(dialog, wx.Dialog) and dialog.IsModal():
                dialog.EndModal(wx.ID_CANCEL)

    check('real Windows wx controls', wx.Platform == '__WXMSW__')
    check('new adapters load from the installed package', all('site-packages' in
        [part.casefold() for part in Path(module.__file__).parts]
        for module in (reading, voice_settings, reading_playback, local_voice_settings)))

    class SilentPlayer:
        def __init__(self):
            self.native = None
            self.state = 'stopped'
            self.started = []
        def status(self):
            return SimpleNamespace(state=self.state, error=None)
        def play(self, path):
            self.started.append(Path(path))
            self.state = 'playing'
        def pause(self):
            self.state = 'paused'
            return True
        def resume(self):
            self.state = 'playing'
            return True
        def stop(self):
            self.state = 'stopped'
        def close(self):
            self.state = 'closed'
        def poll(self):
            return None
        def seek(self, value):
            if not 0 <= value <= .1:
                raise ValueError('Invalid synthetic seek')
    class Registry:
        def voices(self):
            return ({'id': 'synthetic:one', 'name': 'Synthetic control fixture',
                     'languages': ['en'], 'available': True},)

    with tempfile.TemporaryDirectory(prefix='resonance-reading-native-') as temporary:
        base = Path(temporary)
        root = base / 'library'
        root.mkdir()
        store = NarrationStore(root)
        original = 'Original source remains exact.'
        edited = 'Edited 😀 reading copy tail'
        project = revise(new_project(original, title='Native reading controls'), edited)
        saved_digest = store.save(project)
        requests, blocking = [], threading.Event()
        player = SilentPlayer()

        def renderer(plan, directory, *, cancelled, **kwargs):
            requests.append((plan.original_script, plan.roles[0].rate, plan.roles[0].volume))
            if blocking.is_set():
                from carr_library.audio.jobs import RenderCancelled
                while not cancelled():
                    time.sleep(.01)
                raise RenderCancelled('Synthetic producer observed cancellation.')
            output = directory / 'master.wav'
            with wave.open(str(output), 'wb') as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(24000)
                stream.writeframes(b'\x01\0' * 2400)
            return {'output': str(output)}

        def ready(dialog, controller, controls):
            def exercise():
                pane, editor = controls['reading'], controls['editor']
                pane.player.close()
                pane.player = player
                pane.session = SimpleNamespace(registry=lambda: Registry())
                pane.renderer = renderer
                check('original and editable copy are distinct native controls',
                      controls['original'].GetValue() == original and editor.GetValue() == edited
                      and bool(controls['original'].GetWindowStyleFlag() & wx.TE_READONLY))
                check('reading controls remain keyboard reachable',
                      isinstance(pane.voice, wx.Choice) and isinstance(pane.rate, wx.SpinCtrl)
                      and dialog.GetAcceleratorTable().IsOk())
                click(pane.buttons['reload'])
                pump(lambda: not pane.busy)
                check('discovery does not silently select a voice', pane.voice.GetSelection() == wx.NOT_FOUND)
                pane.voice.SetSelection(0)
                pane.rate.SetValue(2)
                pane.volume.SetValue(45)
                end = editor.GetLastPosition()
                editor.SetSelection(end - 4, end)
                check('native selection is exact after supplementary Unicode', editor.GetStringSelection() == 'tail')
                click(pane.buttons['read'])
                pump(lambda: not pane.busy and len(player.started) == 1)
                check('selection reaches literal renderer with requested controls', requests[-1] == ('tail', 2, .45))
                click(pane.buttons['pause'])
                check('pause control delegates to current player', player.state == 'paused')
                click(pane.buttons['pause'])
                check('resume control delegates to current player', player.state == 'playing')
                click(pane.buttons['stop'])
                check('stop releases progress feedback suppression', player.state == 'stopped' and not progress_audio_suppressed())
                editor.SetInsertionPoint(end - 4)
                click(pane.buttons['cursor'])
                pump(lambda: not pane.busy and len(player.started) == 2)
                check('from-cursor uses native text range', requests[-1][0] == 'tail')
                click(pane.buttons['all'])
                pump(lambda: not pane.busy and len(player.started) == 3)
                check('Read all uses edited copy and leaves source unchanged',
                      requests[-1][0] == edited and controls['original'].GetValue() == original)
                destination = base / 'reading.wav'
                pane.audio.save_wav(destination)
                check('WAV export writes actual independent PCM bytes',
                      destination.is_file() and destination.stat().st_nlink == 1)
                blocking.set()
                click(pane.buttons['all'])
                pump(lambda: pane.busy and len(requests) == 4)
                editor.SetValue(edited + ' newer')
                pump(lambda: not pane.busy)
                check('editing cancels producer and prevents obsolete playback', len(player.started) == 3)
                check('shared heavy-media admission is released after cancellation',
                      not ResourceBroker.for_app(root).snapshot()['leases'])
                click(controls['save'])
                check('native save keeps original text unchanged',
                      store.load(project.id)[0].original_text == original)
                click(controls['close'])
            safe(dialog, exercise)

        watchdog = wx.CallLater(90000, expire)
        try:
            open_narration_editor(None, store, project, expected_digest=saved_digest, on_ready=ready)
        finally:
            watchdog.Stop()
        if failures:
            raise RuntimeError('\n'.join(failures))

        models = base / 'synthetic-model'
        models.mkdir()
        binary = base / 'synthetic-piper.exe'
        binary.write_bytes(b'NONEXECUTABLE CONTRACT FIXTURE')
        assets = {'voice.onnx': b'NOT MODEL WEIGHTS', 'voice.json': b'{}'}
        for name, raw in assets.items():
            (models / name).write_bytes(raw)
        manifest = models / 'manifest.json'
        manifest.write_text(json.dumps({'schema_version': 1, 'engine': 'piper', 'model_id': 'synthetic',
            'revision': 'a' * 40, 'license': 'synthetic-only', 'license_accepted': True,
            'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in assets.items()},
            'settings': {'model': 'voice.onnx', 'config': 'voice.json', 'language': 'en'}}), encoding='utf-8')
        session = session_for(root)
        def profile_ready(dialog, active, controls):
            def exercise():
                controls['name'].ChangeValue('Native profile fixture')
                controls['manifest'].ChangeValue(str(manifest))
                controls['executable'].ChangeValue(str(binary))
                click(controls['save'])
                pump(lambda: controls['save'].IsEnabled())
                rows = VoiceProfileStore(root).load()
                check('native profile save verifies and persists selected file identities',
                      len(rows) == 1 and rows[0].manifest_sha256 == hashlib.sha256(manifest.read_bytes()).hexdigest())
                check('saving an installed profile does not authorize execution', not session.active_ids())
                snapshot = snapshot_voice_settings(root)
                restored, = validate_voice_settings_payload(snapshot['voice-settings.json'])
                check('backup representation removes executable and model paths',
                      not restored.connected and restored.executable is None)
                click(controls['close'])
            safe(dialog, exercise)
        watchdog = wx.CallLater(60000, expire)
        try:
            open_local_voice_settings(None, root, session=session, on_ready=profile_ready)
        finally:
            watchdog.Stop()
    if handle is not None:
        handle.close()
    if failures:
        raise RuntimeError('\n'.join(failures))
    print(json.dumps({'status': 'PASS', 'checks': checks, 'count': len(checks),
        'renderer': 'synthetic PCM contract fixture', 'player': 'synthetic control fixture',
        'actual_optional_voice': 'NOT_RUN', 'physical_audio': 'NOT_RUN', 'nvda': 'NOT_RUN'},
        ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
