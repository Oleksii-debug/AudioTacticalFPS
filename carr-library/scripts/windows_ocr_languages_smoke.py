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
    from carr_library.extract.ocr_runtime import check_runtime, discover_ocr_runtime
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
        def descendants(parent):
            for child in parent.GetChildren():
                yield child
                yield from descendants(child)
        buttons = [child for child in descendants(window)
                   if isinstance(child, wx.Button) and child.GetLabel() == label]
        check('native button exists: ' + label, len(buttons) == 1)
        button, = buttons
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        check('native button dispatches: ' + label, button.GetEventHandler().ProcessEvent(event))

    def drive(dialog, action, expected, *, timeout=25, ready=None):
        deadline = monotonic() + timeout
        timers = []

        def advance():
            try:
                if errors:
                    raise RuntimeError('\n'.join(errors))
                if monotonic() > deadline:
                    raise TimeoutError('Native OCR model discovery did not complete: ' + dialog.status.GetValue())
                available = ready(dialog) if ready else dialog.catalog is not None and not dialog.busy
                if not available:
                    if ready is None and not dialog.busy and 'did not complete' in dialog.status.GetValue():
                        raise AssertionError(dialog.status.GetValue())
                    timers.append(wx.CallLater(40, advance))
                    return
                action(dialog)
                if dialog.IsModal():
                    timers.append(wx.CallLater(40, advance))
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
                check('offline pack flow is in the existing native dialog',
                      isinstance(view.pages, wx.Notebook) and view.pages.GetPageCount() == 2
                      and view.pages.GetPageText(1) == 'Offline packs')
                check('shared manager and compatible pack bindings are available',
                      len(view.model_source.GetItems()) == 3
                      and any(control.GetName() == 'Manage official Fast and Best models'
                              for control in view.pack_controls)
                      and view.pack_digest.GetName() == 'Independently recorded manifest SHA256'
                      and view.pack_installed.GetName() == 'Existing reviewed pack folder')
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
            if arguments.require_bundled:
                runtime = discover_ocr_runtime()
                from carr_library.extract.ocr_model_store import OcrModelStore
                from carr_library.ui.ocr_model_manager import create_ocr_model_manager
                store = OcrModelStore(root / 'managed-models')
                manager = create_ocr_model_manager(frame, store)
                phase = 0

                def install_models(view):
                    nonlocal phase
                    if phase == 0:
                        # Synthetic folder/checkbox input, followed by native event
                        # dispatch to the actual background verifier and installer.
                        view.source.SetValue(str(runtime.tessdata))
                        for index, row in enumerate(view._rows):
                            view.entries.Check(index, row.identifier in {'eng', 'ukr', 'slk'})
                        click(view, '&Install selected')
                    else:
                        check('native shared manager installs all selected official model bytes',
                              view.result is not None and view.result.profile == 'fast'
                              and set(view.result.installed) == {'eng', 'ukr', 'slk'})
                        store.select('fast', 'eng+ukr+slk').assert_unchanged()
                        check('managed models pass independent post-install verification',
                              set(store.available('fast')) == {'eng', 'ukr', 'slk'})
                        click(view, '&Close')
                    phase += 1

                try:
                    drive(manager, install_models, wx.ID_CANCEL,
                          timeout=90, ready=lambda view: not view.pending)
                finally:
                    manager.closed = True
                    manager.cancelled.set()
                    manager.executor.shutdown(wait=True, cancel_futures=True)
                    manager.Destroy()

                previous_store = os.environ.get('CARR_OCR_MODELS_DIR')
                os.environ['CARR_OCR_MODELS_DIR'] = str(root / 'managed-models')
                managed_view = ui.create_ocr_dialog(frame, Config(), preset_root=root)
                phase = 0

                def use_managed(view):
                    nonlocal phase
                    if phase == 0:
                        view.model_source.SetSelection(1)
                        event = wx.CommandEvent(wx.EVT_CHOICE.typeId, view.model_source.GetId())
                        event.SetEventObject(view.model_source)
                        check('native model source dispatches profile selection',
                              view.model_source.GetEventHandler().ProcessEvent(event))
                    else:
                        check('managed inventory is reloaded from the installed shared store',
                              view.catalog.model_profile == 'fast'
                              and set(view.catalog.codes) == {'eng', 'ukr', 'slk'})
                        view.selected_codes = ['ukr', 'eng', 'slk']
                        view._show_selected()
                        store_preset(root, 'Native managed selection', view.current_config())
                        click(view, view.ok.GetLabel())
                    phase += 1

                try:
                    drive(managed_view, use_managed, wx.ID_OK)
                    check('accepted managed choice binds profile and recognition order',
                          managed_view.result.model_profile == 'fast'
                          and managed_view.result.languages == 'ukr+eng+slk'
                          and managed_view.result.model_pack_path is None)
                    check('managed preset preserves the profile alongside earlier presets',
                          any(row['name'] == 'Native managed selection'
                              and row['model_profile'] == 'fast' for row in load_presets(root)))
                finally:
                    managed_view.Destroy()
                    if previous_store is None:
                        os.environ.pop('CARR_OCR_MODELS_DIR', None)
                    else:
                        os.environ['CARR_OCR_MODELS_DIR'] = previous_store

                # Preserve the existing reviewed-pack contract without a second
                # native installer: prepare an official compatibility fixture via
                # the public service, then activate it through the native chooser.
                from carr_library.extract.ocr_model_catalog import prepare_offline_pack
                from carr_library.extract.language_manager import install_offline_pack
                prepared, installed = root / 'prepared-model-pack', root / 'installed-model-pack'
                report = prepare_offline_pack(runtime.tessdata, prepared, ('eng', 'ukr', 'slk'),
                    profile='fast', max_bytes=128 * 1024 * 1024)
                install_offline_pack(prepared, installed, expected_manifest_sha256=report.manifest_sha256)
                pack_view = ui.create_ocr_dialog(frame, Config(), preset_root=root)
                phase = 0

                def pack_flow(view):
                    nonlocal phase
                    if phase == 0:
                        view.pages.SetSelection(1)
                        view.pack_installed.SetValue(str(installed))
                        view.pack_digest.SetValue(report.manifest_sha256)
                        check('existing reviewed pack retains license and completed receipt',
                              (installed / 'LICENSE.txt').is_file() and (installed / 'PACK-INSTALLED.json').is_file())
                        check('reviewed pack requires a separate activation action', view.active_pack is None)
                        click(view, 'Use verified pack')
                    else:
                        check('native activation uses actual verified model inventory',
                              view.catalog.integrity == 'verified_model_pack'
                              and set(view.catalog.codes) == {'eng', 'ukr', 'slk'})
                        view.selected_codes = ['eng', 'ukr', 'slk']
                        view._show_selected()
                        click(view, view.ok.GetLabel())
                    phase += 1

                try:
                    drive(pack_view, pack_flow, wx.ID_OK, timeout=90)
                    selected = pack_view.result
                    check('accepted OCR job binds pack path and independent manifest hash',
                          selected.model_pack_path == str(installed)
                          and selected.model_pack_sha256 == pack_view.pack_digest.GetValue())
                    runtime.assert_unchanged()
                    check('portable runtime is unchanged after pack installation', True)
                finally:
                    pack_view.Destroy()
        finally:
            frame.Destroy()
            application.ProcessPendingEvents()
    print(json.dumps({'status': 'PASS', 'checks': len(checks), 'installed_models': list(inventory.codes),
                      'runtime_integrity': inventory.integrity, 'english_inference': inference['status'],
                      'nvda': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
