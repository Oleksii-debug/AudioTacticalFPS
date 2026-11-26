"""Native installed-model manager probe; synthetic inputs, no NVDA claim.

Source installation may have no OCR engine. --require-bundled requires the
verified portable inventory and actual English recognition before UI checks.
"""
import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--require-bundled', action='store_true')
    arguments = parser.parse_args(argv)
    if os.name != 'nt':
        print(json.dumps({'status': 'NOT_RUN', 'reason': 'Native Windows is required.', 'nvda': 'NOT_RUN'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.extract.ocr_languages import discover_languages, store_preset, load_presets
    from carr_library.extract.ocr_runtime import check_runtime
    from carr_library.extract.page_ocr import Config
    from carr_library.ui import ocr_language_dialog as ui

    checks, errors = [], []

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
    check('manager loads from the installed wheel',
          'site-packages' in tuple(part.casefold() for part in Path(ui.__file__).parts))
    check('controls use native Windows wx', wx.Platform == '__WXMSW__')
    inventory = discover_languages(timeout=20)
    inference = {'status': 'NOT_RUN'}
    if arguments.require_bundled:
        check('portable OCR inventory has verified checksums', inventory.integrity == 'portable_sha256')
        check('bundled English Ukrainian Slovak and OSD are present',
              {'eng', 'ukr', 'slk', 'osd'} <= set(inventory.codes))
        inference = check_runtime()
        check('real bundled English recognition succeeds', inference['synthetic_recognition'] == 'RESEARCH')

    def click(window, label):
        buttons = [child for child in window.GetChildren()
                   if isinstance(child, wx.Button) and child.GetLabel() == label]
        check('native button exists: ' + label, len(buttons) == 1)
        button, = buttons
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        check('native button dispatches: ' + label, button.GetEventHandler().ProcessEvent(event))

    def drive(dialog, action, expected):
        deadline = monotonic() + 25
        timers = []

        def advance():
            try:
                if errors:
                    raise RuntimeError('\n'.join(errors))
                if monotonic() > deadline:
                    raise TimeoutError('Native OCR model discovery did not complete: ' + dialog.status.GetValue())
                if dialog.catalog is None or dialog.busy:
                    if not dialog.busy and 'did not complete' in dialog.status.GetValue():
                        raise AssertionError(dialog.status.GetValue())
                    timers.append(wx.CallLater(40, advance))
                    return
                action(dialog)
            except Exception:
                errors.append(traceback.format_exc())
                if dialog.IsModal():
                    dialog.EndModal(wx.ID_CANCEL)

        wx.CallAfter(advance)
        try:
            result = dialog.ShowModal()
        finally:
            for timer in timers:
                timer.Stop()
        if errors:
            raise RuntimeError('\n'.join(errors))
        check('native modal returns expected result', result == expected)

    with TemporaryDirectory(prefix='research-ocr-language-native-') as directory:
        root = Path(directory)
        frame = wx.Frame(None, title='OCR model native verification')
        frame.Show()
        try:
            dialog = ui.create_ocr_dialog(frame, Config(), preset_root=root)

            def select(view):
                check('filter and list controls have readable names',
                      view.filter.GetName() == 'Filter installed languages and scripts'
                      and view.available.GetName() == 'Installed recognition models'
                      and view.chosen.GetName() == 'Selected models in recognition order')
                check('status is a native read-only multiline control',
                      bool(view.status.GetWindowStyleFlag() & wx.TE_READONLY)
                      and bool(view.status.GetWindowStyleFlag() & wx.TE_MULTILINE))
                view.filter.SetFocus()
                application.Yield()
                check('filter receives native keyboard focus', wx.Window.FindFocus() is view.filter)
                view.filter.Navigate()
                application.Yield()
                check('Tab navigation reaches the installed-model list', wx.Window.FindFocus() is view.available)
                if view.catalog.integrity != 'unavailable':
                    check('orientation model is never a recognition choice', 'osd' not in view.catalog.selectable)
                    for code in ('ukr', 'slk'):
                        if code in view.catalog.selectable:
                            view.filter.SetValue(code)
                            view.filter_models()
                            click(view, '&Add selected')
                    expected_codes = ['eng'] + [code for code in ('ukr', 'slk') if code in view.catalog.selectable]
                    check('native multiple selection retains priority order', view.selected_codes == expected_codes)
                    if len(expected_codes) > 1:
                        click(view, 'Move &up')
                        expected_codes[-2], expected_codes[-1] = expected_codes[-1], expected_codes[-2]
                        check('native move changes recognition order', view.selected_codes == expected_codes)
                    store_preset(root, 'Native saved selection', view.current_config())
                    view._presets()
                    index = next(i for i, item in enumerate(view.preset_items) if item[2] == 'Native saved selection')
                    view.presets.SetSelection(index)
                    click(view, 'Use &preset')
                    check('saved preset restores the same ordered choice', view.selected_codes == expected_codes)
                else:
                    check('missing engine is explicit without a false OCR result',
                          'No local OCR engine' in view.status.GetValue())
                view.status.SetFocus()
                application.Yield()
                check('status text can receive native keyboard focus', wx.Window.FindFocus() is view.status)
                click(view, view.ok.GetLabel())

            try:
                drive(dialog, select, wx.ID_OK)
                config = dialog.result
                check('accepted native choice returns canonical OCR Config', isinstance(config, Config))
                if inventory.integrity != 'unavailable':
                    check('custom preset survives reopening the file', load_presets(root)[0]['languages'] == config.languages)
            finally:
                dialog.Destroy()
            second = ui.create_ocr_dialog(frame, config, preset_root=root)
            try:
                def cancel(view):
                    check('reopened dialog preserves accepted order', view.selected_codes == config.languages.split('+'))
                    view.EndModal(wx.ID_CANCEL)
                drive(second, cancel, wx.ID_CANCEL)
                check('cancel does not return an accepted configuration', second.result is None)
            finally:
                second.Destroy()
        finally:
            frame.Destroy()
            application.ProcessPendingEvents()
    print(json.dumps({'status': 'PASS', 'checks': len(checks), 'installed_models': list(inventory.codes),
                      'runtime_integrity': inventory.integrity, 'english_inference': inference['status'],
                      'nvda': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
