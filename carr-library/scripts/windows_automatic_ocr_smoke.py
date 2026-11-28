"""Actual Windows keyboard selection of automatic OCR and explicit manual override."""
from hashlib import sha256
import argparse
import json
import os
from pathlib import Path
import sys
import sysconfig
from time import monotonic
import traceback


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed', action='store_true')
    args = parser.parse_args(argv)
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows required.', 'manual_nvda': 'NOT_RUN'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    runtime = activate_bundled_runtime()
    try:
        return _native(args.installed)
    finally:
        if runtime is not None: runtime.close()


def _native(installed):
    import wx
    from carr_library.extract.page_ocr import Config
    import carr_library.extract.page_ocr as page_module
    import carr_library.extract.language_detection as detector_module
    import carr_library.extract.ocr_regions as region_module
    import carr_library.ui.ocr_selection as selection

    checks, failures, identities = [], [], {}
    def check(name, okay):
        if not okay: raise AssertionError(name)
        if name not in checks:
            checks.append(name); print('PASS:', name, flush=True)
    check('actual wxMSW controls', wx.Platform == '__WXMSW__')
    checkout = Path(__file__).resolve().parents[1]
    installed_root = Path(os.environ.get('CARR_PROGRAM_DIR') or sysconfig.get_path('purelib')).resolve()
    for module in (page_module, detector_module, region_module, selection):
        path = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        digest = sha256(path.read_bytes()).hexdigest()
        check('exact source: ' + module.__name__, digest == sha256(expected.read_bytes()).hexdigest())
        if installed:
            check('installed origin: ' + module.__name__,
                  path.is_relative_to(installed_root) and not path.is_relative_to(checkout))
        identities[module.__name__] = digest
    check('actual pinned offline detector', detector_module.Detector().identify(
        'The research library preserves the original document and its exact words. '
        'A reader reviews every proposed quotation before approving the printed page.'
    )['language'] == 'en')
    available = selection.discover_installed_models(timeout=20)
    check('actual English OCR inventory', 'eng' in available)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return True
    app = ProbeApp(False); app.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    frame = wx.Frame(None, title='Automatic OCR keyboard acceptance', size=(620, 380))
    frame.Show(); app.SetTopWindow(frame)
    simulator = wx.UIActionSimulator()
    current, phase, opened, completed = [Config()], ['open'], [None], [0]
    deadline = monotonic() + 45
    timer = wx.Timer(frame)
    def abort(detail):
        failures.append(detail); print('FAIL:', detail, flush=True)
        timer.Stop()
        dialog = opened[0]
        if dialog is not None:
            try: dialog.EndModal(wx.ID_CANCEL)
            except RuntimeError: pass
        wx.CallAfter(frame.Destroy)

    def open_selection():
        try:
            phase[0] = 'toggle'
            result = selection._choose_languages(frame, 'installed', available, current[0])
            opened[0] = None
            if failures: return
            if completed[0] == 0:
                check('keyboard opt-in returns automatic config', result is not None and result.automatic_languages)
                current[0] = result
            elif completed[0] == 1:
                check('keyboard manual override preserves exact model choice',
                      result is not None and not result.automatic_languages and result.languages == current[0].languages)
                current[0] = result
            else:
                check('Escape discards an unaccepted automatic choice',
                      result is None and current[0].automatic_languages is False)
            completed[0] += 1
            if completed[0] < 3:
                phase[0] = 'open'; wx.CallAfter(open_selection)
            else:
                timer.Stop(); wx.CallAfter(frame.Destroy)
        except Exception:
            abort(traceback.format_exc())

    def tick(event):
        try:
            if monotonic() > deadline: raise TimeoutError('Native OCR selection did not complete.')
            dialog = next((window for window in wx.GetTopLevelWindows()
                           if isinstance(window, wx.Dialog) and window.IsShown()
                           and wx.FindWindowByName('Automatic OCR language detection; bounded selected model pool', window)), None)
            if dialog is None: return
            opened[0] = dialog
            control = wx.FindWindowByName('Automatic OCR language detection; bounded selected model pool', dialog)
            wanted = completed[0] != 1
            if phase[0] == 'toggle':
                dialog.Raise(); control.SetFocus()
                if wx.Window.FindFocus() is not control: return
                check('native automatic-language control is a checkbox', isinstance(control, wx.CheckBox))
                if control.GetValue() != wanted:
                    if not simulator.Char(wx.WXK_SPACE): raise AssertionError('Native checkbox key failed.')
                phase[0] = 'accept'
                return
            if phase[0] == 'accept' and control.GetValue() == wanted:
                if completed[0] == 2:
                    if not simulator.Char(wx.WXK_ESCAPE): raise AssertionError('Native Escape failed.')
                else:
                    button = wx.FindWindowById(wx.ID_OK, dialog)
                    button.SetFocus()
                    if wx.Window.FindFocus() is not button: return
                    if not simulator.Char(wx.WXK_RETURN): raise AssertionError('Native Use key failed.')
                phase[0] = 'waiting'
        except Exception:
            abort(traceback.format_exc())
    frame.Bind(wx.EVT_TIMER, tick, timer)
    timer.Start(70); wx.CallAfter(open_selection)
    app.MainLoop()
    report = {'result': 'PASS' if not failures and completed[0] == 3 else 'FAIL',
              'checks': checks, 'errors': failures, 'source_sha256': identities,
              'manual_nvda': 'NOT_RUN', 'ocr_accuracy_qualification': 'NOT_RUN'}
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['result'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
