"""Installed native selected-language view; no acoustic or NVDA acceptance claim."""
import json
import os
from pathlib import Path


def main():
    if os.name != 'nt':
        print(json.dumps(dict(status='NOT_RUN', reason='Native Windows is required.', nvda='NOT_RUN')))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    import wx
    from carr_library.ui import language_catalog_dialog as ui

    app, frame, dialog = wx.App(False), None, None
    checks = []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS:', label, flush=True)

    try:
        check('installed package is used', 'site-packages' in [p.casefold() for p in Path(ui.__file__).parts])
        check('native Windows controls are used', wx.Platform == '__WXMSW__')
        frame = wx.Frame(None, title='Synthetic language catalog acceptance')
        frame.Show()
        dialog = ui.create_language_catalog_dialog(frame)
        dialog.Show()
        app.Yield()
        check('all 135 selected entries are visible', len(dialog.rows) == dialog.languages.GetCount() == 135)
        check('filter and details have accessible names',
              dialog.filter.GetName() == 'Filter selected languages by name, country, region or identifier'
              and dialog.details.GetName() == 'Selected language geography, capabilities and evidence limits')
        check('capability details are read-only native text',
              bool(dialog.details.GetWindowStyleFlag() & wx.TE_READONLY)
              and bool(dialog.details.GetWindowStyleFlag() & wx.TE_MULTILINE))
        check('Escape uses native cancellation', dialog.GetEscapeId() == wx.ID_CANCEL)
        dialog.filter.SetFocus()
        app.Yield()
        check('filter receives keyboard focus', wx.Window.FindFocus() is dialog.filter)
        dialog.filter.Navigate()
        app.Yield()
        check('Tab reaches the selected-language list', wx.Window.FindFocus() is dialog.languages)
        dialog.filter.SetValue('kab')
        app.Yield()
        check('native filter exposes the partial group without assigning a voice',
              len(dialog.rows) == 1 and dialog.rows[0].id == 'tamazight'
              and 'partial-variant-resources-present-unverified' in dialog.languages.GetString(0)
              and 'No voice is selected or substituted' in dialog.details.GetValue())
        event = wx.CommandEvent(wx.EVT_TEXT_ENTER.typeId, dialog.filter.GetId())
        event.SetEventObject(dialog.filter)
        check('Enter in filter navigates to language results', dialog.filter.GetEventHandler().ProcessEvent(event))
        app.Yield()
        check('native result list has focus after Enter', wx.Window.FindFocus() is dialog.languages)
        check('acoustic and installation limits remain explicit',
              'not establish current local installation' in dialog.details.GetValue()
              and 'Windows/NVDA acceptance' in dialog.details.GetValue()
              and 'not assessed' in dialog.details.GetValue())
        dialog.filter.SetValue('no-selected-language-has-this-name')
        app.Yield()
        check('empty search clears the previous result',
              not dialog.rows and dialog.languages.GetSelection() == wx.NOT_FOUND
              and 'No selected languages match' in dialog.details.GetValue())
    finally:
        if dialog is not None:
            dialog.Destroy()
        if frame is not None:
            frame.Destroy()
        app.Yield()
        if native_handle is not None:
            native_handle.close()
    print(json.dumps(dict(status='PASS', checks=checks, count=len(checks),
                          acoustic='NOT_RUN', nvda='NOT_RUN')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
