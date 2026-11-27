"""Actual Windows keys in the installed-model OCR chooser; no NVDA/quality claim."""
from hashlib import sha256
import argparse
import faulthandler
import importlib.metadata
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import traceback


def _installed_package_root(distributions, package_file, checkout):
    """Bind installed acceptance to the imported package's actual wheel record.

    A checkout egg-info entry may precede the wheel in metadata discovery.
    Its project-relative locate_file('carr_library') is not package evidence.
    Source acceptance never consults installed distribution metadata.
    """
    actual = Path(package_file).resolve(strict=True)
    checkout = Path(checkout).resolve(strict=True)
    if actual.is_relative_to(checkout):
        raise RuntimeError('Installed acceptance imported the checkout package.')
    matches = []
    for distribution in distributions:
        for member in distribution.files or ():
            if str(member).replace('\\', '/') != 'carr_library/__init__.py':
                continue
            try:
                declared = Path(distribution.locate_file(member)).resolve(strict=True)
            except FileNotFoundError:
                continue  # An unrelated stale metadata entry is not this package.
            if declared == actual:
                matches.append(declared.parent)
    if len(matches) != 1:
        raise RuntimeError('Exactly one installed wheel record must own the imported package.')
    return matches[0]


def _seed_ci_ocr_environment(environment):
    # A complete-bundle gate binds its own OCR directory explicitly. The CI
    # preparation path is only a default for source/ordinary installed lanes.
    if 'CARR_OCR_DIR' not in environment and environment.get('CARR_CI_OCR_DIR'):
        environment['CARR_OCR_DIR'] = environment['CARR_CI_OCR_DIR']


def _write_report(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n',
                    encoding='utf-8')


def _run_probe(args, report):
    import wx
    import carr_library
    from carr_library.extract.page_ocr import Config
    import carr_library.extract.page_ocr as page_module
    import carr_library.extract.language_detection as detector_module
    import carr_library.extract.ocr_regions as region_module
    import carr_library.extract.ocr_languages as inventory_module
    import carr_library.ui.ocr_options as options
    import carr_library.ui.ocr_language_dialog as dialog_module

    def check(name, okay):
        if not okay:
            raise AssertionError(name)
        if name not in report['checks']:
            report['checks'].append(name)
            print('PASS:', name, flush=True)

    checkout = Path(__file__).resolve(strict=True).parents[1]
    expected_root = checkout / 'src' / 'carr_library'
    if args.source is not None:
        source = args.source.resolve(strict=True)
        check('explicit source root matches this candidate', source == checkout / 'src')
        check('actual source package',
              Path(carr_library.__file__).resolve(strict=True) == expected_root / '__init__.py')
        actual_root = expected_root
    else:
        actual_root = _installed_package_root(
            importlib.metadata.distributions(), carr_library.__file__, checkout)
    for module in (page_module, detector_module, region_module, inventory_module,
                   options, dialog_module):
        relative = Path(*module.__name__.split('.')[1:]).with_suffix('.py')
        actual = Path(module.__file__).resolve(strict=True)
        expected = expected_root / relative
        check('exact module origin: ' + module.__name__, actual == actual_root / relative)
        digest = sha256(actual.read_bytes()).hexdigest()
        check('exact candidate module: ' + module.__name__,
              digest == sha256(expected.read_bytes()).hexdigest())
        report['source_sha256'][module.__name__] = digest
    for filename in ('ocr-language-detector.json', 'PY3LANGID_LICENSE'):
        actual = actual_root / 'extract' / filename
        check('packaged detector provenance: ' + filename,
              sha256(actual.read_bytes()).hexdigest()
              == sha256((expected_root / 'extract' / filename).read_bytes()).hexdigest())
    check('actual wxMSW controls', wx.Platform == '__WXMSW__')
    check('actual pinned offline detector', detector_module.Detector().identify(
        'The research library preserves the original document and its exact words. '
        'A reader reviews every proposed quotation before approving the printed page.'
    )['language'] == 'en')

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            abort(traceback.format_exc())
            return True

    app = ProbeApp(False)
    app.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    frame = wx.Frame(None, title='Automatic OCR keyboard acceptance', size=(620, 380))
    frame.Show()
    app.SetTopWindow(frame)
    simulator = wx.UIActionSimulator()
    current = [Config(languages='eng', dpi=150, timeout=120)]
    phase, opened, completed = ['open'], [None], [0]
    dialogs = []
    deadline = monotonic() + 90
    timer = wx.Timer(frame)
    stopped = [False]

    def finish():
        if stopped[0]:
            return
        stopped[0] = True
        timer.Stop()
        frame.Destroy()

    def abort(detail):
        report['errors'].append(detail)
        report['failed_stage'] = phase[0]
        print('FAIL:', detail, flush=True)
        _write_report(args.report, report)
        timer.Stop()
        dialog = opened[0]
        if dialog is not None:
            try:
                dialog.cancel_event.set()
                if dialog.IsModal():
                    dialog.EndModal(wx.ID_CANCEL)
            except RuntimeError:
                pass
        wx.CallAfter(finish)

    with TemporaryDirectory(prefix='carr-native-auto-ocr-') as temporary:
        preset_root = Path(temporary)

        def open_selection():
            try:
                phase[0] = 'inventory'
                before = current[0]
                result = options.choose_model_config(frame, before, preset_root=preset_root)
                opened[0] = None
                if report['errors']:
                    return
                if completed[0] == 0:
                    check('keyboard opt-in returns automatic config',
                          result is not None and result.automatic_languages)
                    check('opt-in preserves explicit selected model and limits',
                          result.languages == before.languages and result.dpi == before.dpi
                          and result.timeout == before.timeout and result.max_pixels == before.max_pixels
                          and result.model_policy == before.model_policy
                          and result.model_profile == before.model_profile)
                    current[0] = result
                elif completed[0] == 1:
                    check('keyboard manual override preserves exact model choice',
                          result is not None and not result.automatic_languages
                          and result.languages == before.languages)
                    current[0] = result
                else:
                    check('Escape discards an unaccepted automatic choice',
                          result is None and current[0] is before
                          and current[0].automatic_languages is False)
                check('choosing OCR settings does not silently persist a preset',
                      not (preset_root / inventory_module.FILENAME).exists())
                completed[0] += 1
                if completed[0] < 3:
                    phase[0] = 'open'
                    wx.CallAfter(open_selection)
                else:
                    wx.CallAfter(finish)
            except Exception:
                abort(traceback.format_exc())

        def tick(event):
            try:
                if monotonic() > deadline:
                    detail = ''
                    if opened[0] is not None:
                        detail = opened[0].status.GetValue()
                    raise TimeoutError('Native OCR selection did not complete: ' + detail)
                dialog = next((window for window in wx.GetTopLevelWindows()
                    if isinstance(window, wx.Dialog) and window.IsShown() and window.IsModal()
                    and not getattr(window, 'closed', True)
                    and isinstance(window, dialog_module.OcrOptionsActions)), None)
                if dialog is None:
                    return
                opened[0] = dialog
                if not any(known is dialog for known in dialogs):
                    dialogs.append(dialog)
                if dialog.busy or dialog.catalog is None:
                    return
                if phase[0] == 'inventory':
                    check('actual English installed OCR inventory', 'eng' in dialog.catalog.selectable)
                    check('full ordered language and script controls remain available',
                          isinstance(dialog.available, wx.ListBox)
                          and isinstance(dialog.chosen, wx.ListBox)
                          and dialog.ok.IsEnabled())
                    control = dialog.automatic
                    check('named native automatic-language checkbox',
                          isinstance(control, wx.CheckBox)
                          and control.GetName() ==
                          'Automatic OCR language identification and regional candidates')
                    check('explicit automatic option is restored into the next dialog',
                          control.GetValue() is current[0].automatic_languages)
                    phase[0] = 'toggle'
                control = dialog.automatic
                wanted = completed[0] != 1
                if phase[0] == 'toggle':
                    dialog.Raise()
                    control.SetFocus()
                    if wx.Window.FindFocus() is not control:
                        return
                    check('keyboard toggle changes the current setting', control.GetValue() != wanted)
                    if not simulator.Char(wx.WXK_SPACE):
                        raise AssertionError('Native checkbox key failed.')
                    phase[0] = 'accept'
                    return
                if phase[0] == 'accept' and control.GetValue() == wanted:
                    if completed[0] == 2:
                        if not simulator.Char(wx.WXK_ESCAPE):
                            raise AssertionError('Native Escape failed.')
                    else:
                        dialog.ok.SetFocus()
                        if wx.Window.FindFocus() is not dialog.ok:
                            return
                        if not simulator.Char(wx.WXK_RETURN):
                            raise AssertionError('Native Use key failed.')
                    phase[0] = 'waiting'
            except Exception:
                abort(traceback.format_exc())

        frame.Bind(wx.EVT_TIMER, tick, timer)
        timer.Start(70)
        wx.CallAfter(open_selection)
        try:
            app.MainLoop()
        finally:
            timer.Stop()
            # Executor cleanup uses retained Python ownership even after the
            # modal's native wrapper has already been deleted.
            for window in dialogs:
                window.cancel_event.set()
                window.executor.shutdown(wait=True, cancel_futures=True)
                if window.closed:
                    continue
                try:
                    if window.IsModal():
                        window.EndModal(wx.ID_CANCEL)
                    window.Destroy()
                except RuntimeError:
                    pass
            if not stopped[0]:
                finish()
        if completed[0] != 3:
            raise AssertionError('All three actual keyboard scenarios must complete.')
    if report['errors']:
        raise AssertionError('Native OCR keyboard acceptance reported a failure.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--source', type=Path)
    mode.add_argument('--expect-installed', '--installed', dest='installed', action='store_true')
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args(argv)
    report = {'result': 'FAIL', 'failed_stage': 'incomplete', 'checks': [], 'errors': [],
              'source_sha256': {}, 'manual_nvda': 'NOT_RUN',
              'ocr_accuracy_qualification': 'NOT_RUN'}
    _write_report(args.report, report)
    if os.name != 'nt':
        report.update(result='NOT_RUN', failed_stage='platform',
                      errors=['Actual Windows controls are required.'])
        _write_report(args.report, report)
        return 77
    faulthandler.enable()
    faulthandler.dump_traceback_later(120, exit=True)
    handle = None
    exit_code = 1
    try:
        if args.source is not None:
            sys.path.insert(0, str(args.source.resolve(strict=True)))
        _seed_ci_ocr_environment(os.environ)
        from carr_library.native_runtime import activate_bundled_runtime
        handle = activate_bundled_runtime()
        _run_probe(args, report)
        report.update(result='PASS', failed_stage=None)
        exit_code = 0
    except BaseException:
        detail = traceback.format_exc()
        report['errors'].append(detail)
        print(detail, file=sys.stderr, flush=True)
    finally:
        try:
            if handle is not None:
                handle.close()
        except BaseException:
            report.update(result='FAIL', failed_stage='runtime cleanup')
            report['errors'].append(traceback.format_exc())
            exit_code = 1
        finally:
            _write_report(args.report, report)
            print(json.dumps(report, ensure_ascii=False), flush=True)
            faulthandler.cancel_dump_traceback_later()
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
