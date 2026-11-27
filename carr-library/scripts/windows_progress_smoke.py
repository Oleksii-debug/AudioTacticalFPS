"""Installed native Windows progress controls; synthetic data, no NVDA claim."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import traceback


def main():
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows is required.',
                          'nvda': 'NOT_RUN', 'physical_tone_listening': 'NOT_RUN'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library import progress
    from carr_library.ui import progress_view as ui

    checks, errors, sent = [], [], []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            return False

    application = ProbeApp(False)

    def pump(predicate):
        deadline = monotonic() + 15
        while not predicate():
            if errors:
                raise RuntimeError('\n'.join(errors))
            if monotonic() > deadline:
                raise TimeoutError('Native progress probe exceeded its deadline.')
            application.Yield()
            sleep(.01)
        application.Yield()
        if errors:
            raise RuntimeError('\n'.join(errors))

    def descendants(window):
        for child in window.GetChildren():
            yield child
            yield from descendants(child)

    check('progress loads from the installed wheel',
          'site-packages' in [part.casefold() for part in Path(progress.__file__).parts]
          and 'site-packages' in [part.casefold() for part in Path(ui.__file__).parts])
    check('native Windows wx is active', wx.Platform == '__WXMSW__')
    with TemporaryDirectory(prefix='research-progress-native-') as directory:
        root = Path(directory)
        frame = wx.Frame(None, title='Synthetic native progress probe', size=(850, 370))
        application.SetTopWindow(frame)
        panel = wx.Panel(frame)
        layout = wx.BoxSizer(wx.VERTICAL)
        editor = wx.TextCtrl(panel, value='Focus must remain here while progress changes.')
        editor.SetName('Synthetic editing focus')
        layout.Add(editor, 0, wx.EXPAND | wx.ALL, 8)
        widget = ui.ProgressView(panel, settings_root=root, label='Synthetic operation progress',
                                 tone_sender=lambda *args: sent.append(args), wx_module=wx)
        layout.Add(widget.panel, 1, wx.EXPAND | wx.ALL, 8)
        panel.SetSizer(layout)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        frame.SetSizer(outer)
        identity = int(wx.NewIdRef())
        frame.Bind(wx.EVT_MENU, widget.query, id=identity)
        frame.SetAcceleratorTable(wx.AcceleratorTable([(wx.ACCEL_CTRL | wx.ACCEL_ALT, ord('P'), identity)]))
        try:
            frame.Show()
            frame.Raise()
            editor.SetFocus()
            pump(lambda: wx.Window.FindFocus() is editor)
            check('native gauge and status have accessible names',
                  isinstance(widget.gauge, wx.Gauge)
                  and widget.gauge.GetName() == 'Synthetic operation progress percentage'
                  and widget.status.GetName() == 'Synthetic operation progress phase and completed units')
            check('native status is read only', bool(widget.status.GetWindowStyleFlag() & wx.TE_READONLY))
            check('absent preferences default to silent', progress.ProgressSettingsStore(root).load().mode == 'off')
            tracker = widget.track(progress.ProgressTracker('Synthetic import', total=4, unit='files'))
            for done, expected in ((0, 0), (1, 25), (2, 50), (3, 75), (4, 99)):
                tracker.update(phase='Verify original bytes', completed=done, total=4)
                widget.refresh(force=True)
                application.Yield()
                check(f'actual native counter is {expected} before accepted completion', widget.gauge.GetValue() == expected)
                check(f'phase and units are readable at {expected}',
                      'Verify original bytes' in widget.status.GetValue()
                      and f'{done} of 4 files' in widget.status.GetValue())
                check(f'update at {expected} preserves editing focus', wx.Window.FindFocus() is editor)
            check('default silent output sends no tone', sent == [])
            tracker.finish(verified=True)
            widget.refresh(force=True)
            check('accepted operation reaches native 100', widget.gauge.GetValue() == 100
                  and '100 percent' in widget.status.GetValue())

            render = widget.track(progress.ProgressTracker('Synthetic render'))
            for phase in ('Synthesize chunks', 'Mix final tracks', 'Verify rendered output'):
                render.update(phase=phase)
                widget.refresh(force=True)
                check('unknown-duration phase stays indeterminate: ' + phase,
                      'indeterminate' in widget.status.GetValue() and render.snapshot().percent is None)
            render.finish('cancelled')
            widget.refresh(force=True)
            check('cancelled native operation never reaches 100', widget.gauge.GetValue() != 100
                  and 'Cancelled' in widget.status.GetValue())

            event = wx.CommandEvent(wx.EVT_MENU.typeId, identity)
            check('current-progress command dispatches the native handler', frame.GetEventHandler().ProcessEvent(event))
            pump(lambda: wx.Window.FindFocus() is widget.status)
            check('only explicit query moves focus to progress', wx.Window.FindFocus() is widget.status)

            original = progress.ProgressSettings('beep', 37, 9, True, True)
            progress.ProgressSettingsStore(root).save(original)

            def edit_preferences():
                try:
                    dialog, = [item for item in wx.GetTopLevelWindows()
                               if isinstance(item, wx.Dialog) and item.GetTitle() == 'Progress sound settings']
                    controls = {item.GetName(): item for item in descendants(dialog)}
                    mode = controls['Application progress output']
                    volume = controls['Progress sound volume']
                    interval = controls['Minimum progress sound interval in seconds']
                    check('saved settings load into the native dialog',
                          mode.GetSelection() == 1 and volume.GetValue() == 37 and interval.GetValue() == 9)
                    mode.SetSelection(0)
                    volume.SetValue(25)
                    save = dialog.FindWindow(wx.ID_OK)
                    click = wx.CommandEvent(wx.EVT_BUTTON.typeId, save.GetId())
                    click.SetEventObject(save)
                    check('native Save handler accepts settings', save.GetEventHandler().ProcessEvent(click))
                except Exception:
                    errors.append(traceback.format_exc())
                    for item in wx.GetTopLevelWindows():
                        if isinstance(item, wx.Dialog) and item.IsModal():
                            item.EndModal(wx.ID_CANCEL)

            def expire():
                errors.append('Progress preferences did not close before the deadline.')
                for item in wx.GetTopLevelWindows():
                    if isinstance(item, wx.Dialog) and item.IsModal():
                        item.EndModal(wx.ID_CANCEL)

            ready = wx.CallLater(100, edit_preferences)
            watchdog = wx.CallLater(15000, expire)
            try:
                widget.configure()
            finally:
                ready.Stop()
                watchdog.Stop()
            if errors:
                raise RuntimeError('\n'.join(errors))
            check('native preferences save survives a new store instance',
                  progress.ProgressSettingsStore(root).load() == progress.ProgressSettings('off', 25, 9, True, True))
        finally:
            widget.close()
            frame.Destroy()
            application.ProcessPendingEvents()
            application.Yield()
    if native_handle is not None:
        native_handle.close()
    print(json.dumps({'status': 'PASS', 'checks': checks, 'nvda': 'NOT_RUN',
                      'physical_tone_listening': 'NOT_RUN',
                      'scope': 'installed native controls and handler outcomes; synthetic work only'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
