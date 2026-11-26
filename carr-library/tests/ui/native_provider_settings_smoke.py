"""Real wx profile controls and disk CRUD, with no API calls or real credentials.

Installed-package command:
    python -I -B -X utf8 tests/ui/native_provider_settings_smoke.py
This verifies native events and focus, not NVDA speech or a live API account.
"""
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import traceback
from types import SimpleNamespace


def main():
    if sys.platform not in ('win32', 'darwin') and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'A native display is required.', 'windows_nvda': 'NOT_RUN'}))
        return 77
    try:
        import wx
    except (ImportError, OSError):
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'wxPython is unavailable.', 'windows_nvda': 'NOT_RUN'}))
        return 77
    from carr_library.ui.provider_settings import ProviderSettingsDialog

    failures, checks, active = [], [], [None]
    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return False
    gui = ProbeApp(False)
    def abort(message):
        failures.append(message)
        if active[0] is not None:
            active[0].cancelled.set()
            active[0].closed = True
            if active[0].dialog.IsModal():
                active[0].dialog.EndModal(wx.ID_CANCEL)
        gui.ExitMainLoop()
    def guarded(callback):
        try:
            callback()
        except BaseException:
            abort(traceback.format_exc())
    def later(callback, milliseconds=70):
        return wx.CallLater(milliseconds, lambda: guarded(callback))
    def click(button):
        assert button.IsShown() and button.IsEnabled()
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        assert button.GetEventHandler().ProcessEvent(event)
    def after_idle(callback):
        if active[0].busy:
            later(lambda: after_idle(callback))
        else:
            callback()

    with TemporaryDirectory(prefix='carr-native-provider-') as temporary:
        root = Path(temporary) / 'library'
        frame = wx.Frame(None, title='Provider settings native probe', size=(950, 950))
        frame.library = SimpleNamespace(root=root)
        gui.SetTopWindow(frame)
        frame.Show()
        saved = []
        def run():
            settings = ProviderSettingsDialog(frame)
            active[0] = settings
            def create():
                assert settings.dialog.IsModal()
                assert wx.Window.FindFocus() is settings.fields['name']
                assert settings.fields['secret'].GetWindowStyleFlag() & wx.TE_PASSWORD
                assert settings.fields['secret'].GetValue() == ''
                assert not settings.test_button.IsEnabled()
                assert settings.fields['adapter'].GetStringSelection().startswith('Local')
                settings.fields['name'].SetValue('Native loopback account')
                settings.fields['model'].SetValue('local-explicit-model')
                settings.fields['endpoint'].SetValue('http://127.0.0.1:8137/v1')
                click(settings.save_button)
                assert settings.busy
                later(lambda: after_idle(edit))
            def edit():
                rows = settings.model.store.list_profiles()
                assert len(rows) == 1 and rows[0].local and rows[0].credential_ref is None
                saved.append(rows[0])
                assert rows[0].id in settings.list.GetString(0)
                assert settings.current == rows[0] and settings.test_button.IsEnabled()
                assert wx.Window.FindFocus() is settings.status
                checks.append('native named controls create a loopback profile without any network request')
                settings.fields['name'].SetValue('Edited native loopback account')
                assert not settings.test_button.IsEnabled()
                click(settings.save_button)
                later(lambda: after_idle(second))
            def second():
                rows = settings.model.store.list_profiles()
                assert rows[0].id == saved[0].id and rows[0].name == 'Edited native loopback account'
                assert rows[0].credential_ref is None and settings.fields['secret'].GetValue() == ''
                checks.append('native edit preserves stable account identity and does not reveal keys')
                click(settings.new_button)
                settings.fields['name'].SetValue('Edited native loopback account')
                settings.fields['model'].SetValue('second-model')
                click(settings.save_button)
                later(lambda: after_idle(select_first))
            def select_first():
                assert len(settings.profiles) == 2
                assert settings.list.GetString(0) != settings.list.GetString(1)
                settings.list.SetSelection(0)
                event = wx.CommandEvent(wx.EVT_LISTBOX.typeId, settings.list.GetId())
                event.SetEventObject(settings.list)
                settings.list.GetEventHandler().ProcessEvent(event)
                assert settings.current.id == saved[0].id
                checks.append('native selection distinguishes accounts sharing a display name')
                later(confirm_delete)
                click(settings.remove_button)
                later(lambda: after_idle(finish))
            def confirm_delete():
                dialogs = [window for window in wx.GetTopLevelWindows()
                           if isinstance(window, wx.MessageDialog) and window.GetTitle() == 'Remove selected connection?']
                assert len(dialogs) == 1 and dialogs[0].IsModal()
                dialogs[0].EndModal(wx.ID_YES)
            def finish():
                rows = settings.model.store.list_profiles()
                assert len(rows) == 1 and rows[0].id != saved[0].id
                assert not settings.busy
                assert not (root / 'provider-jobs').exists(), 'Profile CRUD unexpectedly started a provider operation.'
                checks.append('native confirmation removes only the selected account; no API test started')
                click(settings.close_button)
            later(create)
            settings.show()
            assert settings.closed
            active[0] = None
            frame.Destroy()
            gui.ExitMainLoop()
        watchdog = wx.CallLater(20000, lambda: abort('Native provider settings exceeded its 20 second watchdog.'))
        wx.CallAfter(lambda: guarded(run))
        gui.MainLoop()
        watchdog.Stop()
    if failures:
        print(json.dumps({'status': 'FAIL', 'failures': failures, 'checks': checks}, ensure_ascii=False))
        return 1
    print(json.dumps({'status': 'PASS', 'checks': checks, 'external_requests': 0,
                      'real_api_credentials_used': False, 'windows_nvda': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
