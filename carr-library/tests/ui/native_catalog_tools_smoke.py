"""Bounded native-wx Library tools smoke on a synthetic local library.

Installed proof: python -I -B tests/ui/native_catalog_tools_smoke.py --native
Source proof: python -I -B tests/ui/native_catalog_tools_smoke.py --native --source .
The default imports the installed package and verifies exact module origins and
bytes against the checkout when present; only --source changes the import path.
Requires wxPython and an existing authorized desktop. Missing prerequisites
produce NOT_RUN (77). Real widgets, native events, the desktop asynchronous job
runner, SQLite application services and the checked file writer are exercised.
Only the file chooser's selected temporary destination is supplied by this
probe; native file-chooser interaction and NVDA are explicitly NOT_RUN.
No screenshots, external file launches, real books or network access are used.
"""
from argparse import ArgumentParser
from contextlib import ExitStack
import csv
from hashlib import sha256
from importlib import import_module
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
import sysconfig
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch


def not_run(reason):
    print(json.dumps({'result': 'NOT_RUN', 'reason': reason, 'nvda': 'NOT_RUN'}), flush=True)
    return 77


def _product_binding(source):
    """Bind this proof to installed bytes unless source use is explicit."""
    checkout_src = Path(__file__).resolve().parents[2] / 'src'
    override = source.resolve(strict=True) if source is not None else None
    purelib = Path(sysconfig.get_path('purelib')).resolve(strict=True)
    if override is not None:
        selected_src = (override / 'src').resolve(strict=True)
        if not selected_src.is_dir():
            raise AssertionError('--source must contain a src directory')
        sys.path.insert(0, str(selected_src))
        expected_root = selected_src
        reference_root = selected_src
    else:
        expected_root = purelib
        reference_root = checkout_src.resolve()

    # The four export-path modules must have the same provenance as the
    # package and the actual SQLite service used by this native proof.
    names = (
        'carr_library',
        'carr_library.application',
        'carr_library.ui.desktop_app',
        'carr_library.ui.catalog_tools_dialog',
        'carr_library.ui.export_destination',
        'carr_library.research.export_file',
    )
    modules, origins = {}, {}
    for name in names:
        module = import_module(name)
        relative = (Path('carr_library/__init__.py') if name == 'carr_library'
                    else Path(*name.split('.')).with_suffix('.py'))
        origin = Path(module.__file__).resolve(strict=True)
        expected = (expected_root / relative).resolve(strict=True)
        if origin != expected or not origin.is_relative_to(expected_root):
            raise AssertionError(
                f'{name} is not from the selected product root: {origin}; expected {expected}')
        if override is None and origin.is_relative_to(checkout_src.resolve()):
            raise AssertionError(f'Installed proof imported checkout source: {name}: {origin}')
        actual = origin.read_bytes()
        entry = {'path': str(origin), 'sha256': sha256(actual).hexdigest()}
        if reference_root.is_dir():
            reference = (reference_root / relative).resolve(strict=True)
            if not reference.is_relative_to(reference_root):
                raise AssertionError(f'Checkout reference escapes its source root: {name}')
            reference_bytes = reference.read_bytes()
            entry['checkout_sha256'] = sha256(reference_bytes).hexdigest()
            if actual != reference_bytes:
                raise AssertionError(
                    f'Imported module bytes differ from the selected checkout: {name}: {origin}')
            entry['matches_checkout'] = True
        else:
            entry['matches_checkout'] = None
        modules[name], origins[name] = module, entry
    return modules, {
        'source_override': str(override) if override is not None else None,
        'purelib': str(purelib),
        'module_origins': origins,
    }


def run_native(source, timeout):
    if sys.platform not in ('win32', 'darwin') and not (
            os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        return not_run('No authorized native display is available.')
    try:
        import wx
    except (ImportError, OSError) as exc:
        return not_run('Native wxPython is unavailable: ' + str(exc))
    try:
        modules, binding = _product_binding(source)
    except Exception as exc:
        print(json.dumps({'result': 'FAIL', 'stage': 'product-binding', 'error': str(exc),
                          'source_override': str(source) if source is not None else None,
                          'nvda': 'NOT_RUN'}, sort_keys=True), flush=True)
        return 1
    LibraryApp = modules['carr_library.application'].LibraryApp
    DesktopFrame = modules['carr_library.ui.desktop_app'].DesktopFrame
    LibraryToolsDialog = modules['carr_library.ui.catalog_tools_dialog'].LibraryToolsDialog
    print(json.dumps({'stage': 'product-binding', **binding}, sort_keys=True), flush=True)

    try:
        app = wx.App(False)
    except (Exception, SystemExit) as exc:
        return not_run('Native wx could not open its display: ' + str(exc))

    checks, failures = [], []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)

    with TemporaryDirectory(prefix='carr-native-library-tools-', ignore_cleanup_errors=True) as temporary:
        folder = Path(temporary)
        library = LibraryApp(folder / 'private-library')
        workspace = library.create_workspace('Synthetic tools library')
        other = library.create_workspace('Other synthetic library')
        collection = library.create_collection(workspace, 'Selected sources')
        topic = library.create_topic(workspace, 'Literature review')
        first = library.add_text(workspace, 'Synthetic source one',
            'Synthetic quotation — зразок.', authors=('Synthetic author', 'Žan'),
            collections=(collection,))
        library.add_text(workspace, 'Synthetic source two', 'Another synthetic source.',
                         authors=('Another synthetic author',))
        library.add_text(other, 'Outside this library', 'Unrelated synthetic source.')
        hit, = library.search(workspace, 'quotation', collection=collection)
        library.save_hit(workspace, topic, hit.ref, hit.revision, 'Separate synthetic research note')
        expected_scoped = library.catalog_report(workspace, collection=collection)
        expected_all = library.catalog_report(workspace)
        original_bytes = library.assets.resolve(first['asset']).read_bytes()
        frame = dialog = None
        active_timer = None
        export_guard = None
        export_observation = None
        try:
            frame = DesktopFrame(library)
            frame._open_workspace(workspace)
            app.SetTopWindow(frame)
            frame.Show()
            frame.Raise()
            dialog = LibraryToolsDialog(frame, workspace, library.collections(workspace), collection=collection)
            state = {'phase': 'start', 'deadline': monotonic() + timeout}
            destinations = {
                'bibliography': folder / 'bibliography.json',
                'inventory': folder / 'source-inventory.csv',
                'matrix': folder / 'review-matrix.csv',
            }

            def command(control, event_type):
                event = wx.CommandEvent(event_type.typeId, control.GetId())
                event.SetEventObject(control)
                if event_type == wx.EVT_CHOICE:
                    event.SetInt(control.GetSelection())
                check('native event handled: ' + control.GetName(),
                      control.GetEventHandler().ProcessEvent(event))

            def export(button, destination):
                nonlocal export_guard, export_observation
                if export_guard is not None:
                    raise AssertionError('The preceding export has not completed its native confirmation.')
                # No native chooser automation or external file launch is
                # claimed. Destination and confirmation are controlled; all
                # other widgets and handlers are real wx.
                previous = b'Previous export selected for replacement.'
                destination.write_bytes(previous)
                confirmations = []

                def confirm(message, title, style, parent):
                    check('confirmation names the final destination', destination.name in message)
                    check('replacement defaults to No', bool(style & wx.NO_DEFAULT))
                    check('confirmation belongs to this dialog', parent is dialog)
                    check('confirmation preserves previous bytes', destination.read_bytes() == previous)
                    confirmations.append(destination)
                    return wx.YES

                class SelectedDestination:
                    def __init__(self, *args, **kwargs):
                        check('export uses a save picker before bound replacement approval',
                              bool(kwargs['style'] & wx.FD_SAVE)
                              and not bool(kwargs['style'] & wx.FD_OVERWRITE_PROMPT))
                        check('save picker preserves the selected existing export',
                              destination.is_file() and destination.read_bytes() == previous)
                    def ShowModal(self): return wx.ID_OK
                    def GetPath(self): return str(destination)
                    def Destroy(self): pass
                # Keep both controlled native-dialog replacements alive until
                # the background capture, UI confirmation and publication have
                # completed. A short with-block would release them before the
                # queued completion callback and could open a real unattended modal.
                export_guard = ExitStack()
                export_guard.enter_context(patch.object(wx, 'FileDialog', SelectedDestination))
                export_guard.enter_context(patch.object(wx, 'MessageBox', confirm))
                export_observation = (destination, confirmations)
                command(button, wx.EVT_BUTTON)
                check('destination capture started on the desktop worker', dialog.pending and frame.working)
                check('confirmation waits for background capture', confirmations == [])

            def completed():
                nonlocal export_guard, export_observation
                if dialog.pending or frame.working:
                    return False
                if 'not completed' in dialog.status.GetValue() or 'could not be started' in dialog.status.GetValue():
                    raise AssertionError(dialog.status.GetValue())
                if export_observation is not None:
                    destination, confirmations = export_observation
                    check('one captured-destination confirmation', confirmations == [destination])
                    check('capture and export completed with no outstanding worker', not dialog.pending and not frame.working)
                    export_guard.close()
                    export_guard = None
                    export_observation = None
                return True

            def stop(error):
                failures.append(str(error))
                frame.cancel_event.set()
                dialog.dispose()
                if dialog.IsModal():
                    dialog.EndModal(wx.ID_CANCEL)

            def tick():
                nonlocal active_timer
                if failures or dialog._closed:
                    return
                try:
                    if monotonic() >= state['deadline']:
                        raise TimeoutError('Native Library tools exceeded its bounded event-loop timeout.')
                    phase = state['phase']
                    if phase == 'start':
                        check('native scope and readable report controls',
                              isinstance(dialog.scope, wx.Choice) and isinstance(dialog.report, wx.TextCtrl))
                        check('selected collection has explicit scope label',
                              dialog.scope.GetStringSelection() == 'Collection: Selected sources')
                        check('report has an accessible name',
                              dialog.report.GetName() == 'Library overview and quality report')
                        command(dialog.refresh_button, wx.EVT_BUTTON)
                        check('report starts asynchronously with controls guarded',
                              dialog.pending and frame.working and not dialog.scope.IsEnabled()
                              and dialog.cancel_button.IsEnabled())
                        state['phase'] = 'scoped-report'
                    elif phase == 'scoped-report' and completed():
                        if wx.Window.FindFocus() != dialog.report:
                            active_timer = wx.CallLater(10, tick)
                            return
                        check('complete scoped report rendered', dialog.report.GetValue() == expected_scoped)
                        check('native completion focuses the report', wx.Window.FindFocus() == dialog.report)
                        dialog.scope.SetSelection(0)
                        command(dialog.scope, wx.EVT_CHOICE)
                        check('changing scope clears the old report', dialog.report.GetValue() == '')
                        command(dialog.refresh_button, wx.EVT_BUTTON)
                        state['phase'] = 'all-report'
                    elif phase == 'all-report' and completed():
                        check('complete library report rendered', dialog.report.GetValue() == expected_all)
                        dialog.format.SetSelection(2)
                        export(dialog.bibliography_button, destinations['bibliography'])
                        state['phase'] = 'bibliography'
                    elif phase == 'bibliography' and completed():
                        rows = json.loads(destinations['bibliography'].read_text(encoding='utf-8'))
                        check('actual bibliography export contains exactly this library',
                              {row['title'] for row in rows} == {'Synthetic source one', 'Synthetic source two'})
                        export(dialog.inventory_button, destinations['inventory'])
                        state['phase'] = 'inventory'
                    elif phase == 'inventory' and completed():
                        with destinations['inventory'].open(encoding='utf-8', newline='') as stream:
                            rows = tuple(csv.DictReader(stream))
                        check('actual inventory CSV preserves separate source identities',
                              len(rows) == 2 and len({row['edition_id_json'] for row in rows}) == 2)
                        dialog.scope.SetSelection(dialog.scope_ids.index(collection))
                        command(dialog.scope, wx.EVT_CHOICE)
                        export(dialog.matrix_button, destinations['matrix'])
                        state['phase'] = 'matrix'
                    elif phase == 'matrix' and completed():
                        with destinations['matrix'].open(encoding='utf-8', newline='') as stream:
                            rows = tuple(csv.DictReader(stream))
                        check('actual matrix keeps verified quotation and note separate',
                              len(rows) == 1 and json.loads(rows[0]['quotation_json']) == 'quotation'
                              and json.loads(rows[0]['research_note_json']) == 'Separate synthetic research note')
                        check('exports preserve retained original bytes',
                              library.assets.resolve(first['asset']).read_bytes() == original_bytes)
                        check('close is enabled after asynchronous completion', dialog.close_button.IsEnabled())
                        command(dialog.close_button, wx.EVT_BUTTON)
                        state['phase'] = 'closed'
                        return
                except Exception as exc:
                    stop(exc)
                    return
                active_timer = wx.CallLater(10, tick)

            wx.CallAfter(tick)
            result = dialog.ShowModal()
            if failures:
                raise AssertionError('; '.join(failures))
            check('native modal closes with no outstanding job',
                  result == wx.ID_CANCEL and dialog._closed and not frame.working)
            print(json.dumps({'result': 'PASS', 'checks': len(checks), 'wx': wx.version(),
                'sqlite': sqlite3.sqlite_version, 'native_platform': platform.system(),
                'source_sha256': binding['module_origins']['carr_library.ui.catalog_tools_dialog']['sha256'],
                **binding,
                'file_chooser_interaction': 'NOT_RUN; synthetic destination supplied',
                'nvda': 'NOT_RUN'}, sort_keys=True), flush=True)
        except Exception as exc:
            print(json.dumps({'result': 'FAIL', 'error': str(exc), 'checks_passed': len(checks),
                              **binding, 'nvda': 'NOT_RUN'}, sort_keys=True), flush=True)
            return 1
        finally:
            # Suppress late completions before releasing a remaining native
            # dialog patch, including exceptions during either export phase.
            if frame is not None:
                frame._closing = True
                frame.cancel_event.set()
            if export_guard is not None:
                export_guard.close()
            if active_timer is not None:
                active_timer.Stop()
            if dialog is not None:
                dialog.dispose()
                dialog.Destroy()
            if frame is not None:
                frame.cancel_event.set()
                # Cooperative service cancellation is bounded independently by
                # the application. Never touch closed native controls afterward.
                frame._closing = True
                frame.executor.shutdown(wait=False, cancel_futures=True)
                frame.Destroy()
            app.Yield()
    return 0


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true', help='Run on the existing authorized native desktop.')
    parser.add_argument('--source', type=Path, default=None,
                        help='Explicit source checkout override; omitted verifies the installed package.')
    parser.add_argument('--timeout', type=float, default=45.0, help='Native event-loop deadline, 5 to 90 seconds.')
    args = parser.parse_args()
    if not 5 <= args.timeout <= 90:
        parser.error('--timeout must be between 5 and 90 seconds')
    return run_native(args.source.resolve() if args.source is not None else None, args.timeout)


if __name__ == '__main__':
    raise SystemExit(main())
