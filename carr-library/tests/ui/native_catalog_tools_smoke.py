"""Bounded native-wx Library tools smoke on a synthetic local library.

Run from carr-library: python tests/ui/native_catalog_tools_smoke.py --native
Requires wxPython and an existing authorized desktop. Missing prerequisites
produce NOT_RUN (77). Real widgets, native events, the desktop asynchronous job
runner, SQLite application services and the checked file writer are exercised.
The file chooser's temporary destination and overwrite answers are supplied by
this probe. Destination inspection, approval binding and publication are real;
native chooser/confirmation interaction and NVDA are explicitly NOT_RUN.
No screenshots, external file launches, real books or network access are used.
"""
from argparse import ArgumentParser
import csv
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch


def not_run(reason):
    print(json.dumps({'result': 'NOT_RUN', 'reason': reason, 'nvda': 'NOT_RUN'}), flush=True)
    return 77


def run_native(source, timeout):
    if sys.platform not in ('win32', 'darwin') and not (
            os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        return not_run('No authorized native display is available.')
    try:
        import wx
    except (ImportError, OSError) as exc:
        return not_run('Native wxPython is unavailable: ' + str(exc))
    sys.path.insert(0, str(source / 'src'))
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame
    from carr_library.ui import catalog_tools_dialog as catalog_tools
    from carr_library.ui.catalog_tools_dialog import LibraryToolsDialog

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
            existing_export = b'An existing export needs explicit replacement approval.\n'
            changed_export = b'A different, longer destination now needs a new replacement approval.\n'
            destinations['bibliography'].write_bytes(existing_export)
            approvals, render_calls, write_calls = [], [], []
            confirm_destination = catalog_tools.NativeDialogs.approve_export
            render_catalog = library.export_catalog
            write_export = catalog_tools.write_export

            def confirm(*args, **kwargs):
                approval = confirm_destination(*args, **kwargs)
                approvals.append(approval)
                return approval

            def render(*args, **kwargs):
                render_calls.append(True)
                return render_catalog(*args, **kwargs)

            def write(path, value, **kwargs):
                approval = kwargs.get('approval')
                check('writer receives the exact captured destination approval',
                      approval is not None and approval is approvals[-1]
                      and approval.requested == Path(path).absolute())
                write_calls.append(approval)
                return write_export(path, value, **kwargs)

            def command(control, event_type):
                event = wx.CommandEvent(event_type.typeId, control.GetId())
                event.SetEventObject(control)
                if event_type == wx.EVT_CHOICE:
                    event.SetInt(control.GetSelection())
                check('native event handled: ' + control.GetName(),
                      control.GetEventHandler().ProcessEvent(event))

            def export(button, destination, *, answer=None, change_while_confirming=False):
                # Supply deterministic dialog answers while retaining the real
                # destination capture, native button event, worker and writer.
                before_render, before_write = len(render_calls), len(write_calls)
                before_bytes = destination.read_bytes() if destination.exists() else None
                questions = []

                class SelectedDestination:
                    def __init__(self, *args, **kwargs):
                        check('save chooser leaves replacement to explicit approval',
                              bool(kwargs['style'] & wx.FD_SAVE)
                              and not kwargs['style'] & wx.FD_OVERWRITE_PROMPT)
                    def ShowModal(self): return wx.ID_OK
                    def GetPath(self): return str(destination)
                    def Destroy(self): pass

                def replacement_answer(message, title, style, parent):
                    check('replacement question names the existing destination with default No',
                          answer is not None and parent is dialog and destination.is_file()
                          and destination.name in message and title.startswith('Replace ')
                          and style == wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
                    questions.append(True)
                    if change_while_confirming:
                        destination.write_bytes(changed_export)
                    return answer

                with patch.object(wx, 'FileDialog', SelectedDestination), \
                        patch.object(wx, 'MessageBox', replacement_answer):
                    command(button, wx.EVT_BUTTON)
                check('only an existing destination needs a replacement answer',
                      len(questions) == int(before_bytes is not None))
                if answer == wx.NO:
                    check('declined replacement starts no worker, rendering or write',
                          not dialog.pending and not frame.working
                          and len(render_calls) == before_render and len(write_calls) == before_write)
                    check('declined replacement preserves the existing file and readable cancellation',
                          destination.read_bytes() == before_bytes
                          and dialog.status.GetValue() == 'Export cancelled. No file was written.')
                    return
                check('export started on the desktop worker', dialog.pending and frame.working)

            def completed():
                if dialog.pending or frame.working:
                    return False
                if 'not completed' in dialog.status.GetValue() or 'could not be started' in dialog.status.GetValue():
                    raise AssertionError(dialog.status.GetValue())
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
                        export(dialog.bibliography_button, destinations['bibliography'], answer=wx.NO)
                        export(dialog.bibliography_button, destinations['bibliography'],
                               answer=wx.YES, change_while_confirming=True)
                        state['phase'] = 'changed-destination'
                    elif phase == 'changed-destination' and not dialog.pending and not frame.working:
                        check('replacement rejects a destination changed during confirmation',
                              'The export destination changed.' in dialog.status.GetValue()
                              and destinations['bibliography'].read_bytes() == changed_export)
                        check('rejected publication leaves no staged export',
                              not list(folder.glob('.research-export-*')))
                        export(dialog.bibliography_button, destinations['bibliography'], answer=wx.YES)
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
            with patch.object(catalog_tools.NativeDialogs, 'approve_export', confirm), \
                    patch.object(library, 'export_catalog', render), \
                    patch.object(catalog_tools, 'write_export', write):
                result = dialog.ShowModal()
            if failures:
                raise AssertionError('; '.join(failures))
            check('native modal closes with no outstanding job',
                  result == wx.ID_CANCEL and dialog._closed and not frame.working)
            print(json.dumps({'result': 'PASS', 'checks': len(checks), 'wx': wx.version(),
                'sqlite': sqlite3.sqlite_version, 'native_platform': platform.system(),
                'source_sha256': sha256((source / 'src/carr_library/ui/catalog_tools_dialog.py').read_bytes()).hexdigest(),
                'file_chooser_interaction': 'NOT_RUN; synthetic destination supplied',
                'overwrite_dialog_interaction': 'NOT_RUN; deterministic answers supplied',
                'nvda': 'NOT_RUN'}, sort_keys=True), flush=True)
        except Exception as exc:
            print(json.dumps({'result': 'FAIL', 'error': str(exc), 'checks_passed': len(checks),
                              'nvda': 'NOT_RUN'}, sort_keys=True), flush=True)
            return 1
        finally:
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
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--timeout', type=float, default=45.0, help='Native event-loop deadline, 5 to 90 seconds.')
    args = parser.parse_args()
    if not 5 <= args.timeout <= 90:
        parser.error('--timeout must be between 5 and 90 seconds')
    return run_native(args.source.resolve(), args.timeout)


if __name__ == '__main__':
    raise SystemExit(main())
