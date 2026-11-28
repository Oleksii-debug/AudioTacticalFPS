"""Real wx local voice registration controls; inert model files, no inference.

Run against the installed package with python -I -B -X utf8. This proves native
configuration events/persistence, not pronunciation, NVDA speech or model audio.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import traceback


def main():
    if sys.platform not in ('win32', 'darwin') and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'A native display is required.', 'nvda_speech': 'NOT_RUN'}))
        return 77
    try:
        import wx
    except (ImportError, OSError):
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'wxPython is unavailable.', 'nvda_speech': 'NOT_RUN'}))
        return 77
    from carr_library.ui.voice_settings import VoiceSettingsDialog
    from carr_library.audio.voices import VoiceRegistry

    failures, checks, active = [], [], [None]
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
    def guarded(callback):
        try:
            callback()
        except BaseException:
            abort(traceback.format_exc())
    def later(callback):
        return wx.CallLater(80, lambda: guarded(callback))
    def click(button):
        assert button.IsShown() and button.IsEnabled()
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        assert button.GetEventHandler().ProcessEvent(event)
    def after_idle(callback):
        later(lambda: after_idle(callback)) if active[0].busy else callback()

    with TemporaryDirectory(prefix='carr-native-voice-settings-') as temporary:
        folder = Path(temporary) / 'installed-fixture'
        folder.mkdir()
        assets = {'voice.onnx': b'INERT TEST FIXTURE, NOT A MODEL', 'voice.json': b'{}'}
        for name, data in assets.items():
            (folder / name).write_bytes(data)
        manifest = folder / 'manifest.json'
        manifest.write_text(json.dumps({'schema_version': 1, 'engine': 'piper', 'model_id': 'native-fixture',
            'revision': 'a' * 40, 'license': 'Synthetic test fixture only',
            'files': {name: sha256(data).hexdigest() for name, data in assets.items()},
            'settings': {'model': 'voice.onnx', 'config': 'voice.json', 'language': 'uk', 'name': 'Native fixture'},
            'license_accepted': True, 'trusted_weights': True, 'personal_noncommercial': False}), encoding='utf-8')
        root = Path(temporary) / 'library'
        frame = wx.Frame(None, title='Native voice registration probe', size=(950, 850))
        gui.SetTopWindow(frame)
        frame.Show()
        def run():
            controller = VoiceSettingsDialog(frame, root)
            active[0] = controller
            def create():
                assert controller.dialog.IsModal()
                assert wx.Window.FindFocus() is controller.sapi
                assert controller.status.GetName() and all(field.GetName() for field in controller.fields.values())
                for control in (controller.sapi, controller.flite, controller.espeak):
                    control.SetValue(False)
                controller.fields['piper_executable'].SetValue(str(Path(sys.executable).resolve()))
                controller.fields['piper_manifest'].SetValue(str(manifest))
                click(controller.save_button)
                assert controller.busy
                later(lambda: after_idle(saved))
            def saved():
                value = controller.model.store.read()
                assert value['revision'] == 1 and value['restored'] is False
                assert set(value['bindings']) == {'piper.executable', 'piper.manifest'}
                voice, = VoiceRegistry.for_library(root).list_voices()
                assert voice.languages == ('uk',)
                assert wx.Window.FindFocus() is controller.status
                assert 'Native fixture' in controller.status.GetValue()
                checks.append('named native controls save pinned installation metadata and select the declared profile language')
                click(controller.reset_button)
                assert controller.model.store.read()['config'] == {}
                assert controller.fields['piper_manifest'].GetValue() == ''
                assert manifest.is_file()
                checks.append('native reset detaches the registration without deleting external assets')
                controller.close()
            later(create)
            controller.dialog.ShowModal()
            controller.close()
            controller.dialog.Destroy()
            active[0] = None
            frame.Destroy()
            gui.ExitMainLoop()
        timeout = wx.CallLater(90000, lambda: abort('Native voice registration probe timed out.'))
        later(run)
        gui.MainLoop()
        timeout.Stop()
    print(json.dumps({'status': 'FAIL' if failures else 'PASS', 'checks': checks,
                      'failures': failures, 'model_inference': 'NOT_RUN', 'nvda_speech': 'NOT_RUN'}, ensure_ascii=True))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
