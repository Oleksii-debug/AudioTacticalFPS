"""Actual installed Windows workflow controls, current-scope run and passive recovery."""
import argparse
import ctypes
from ctypes import wintypes
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
from time import monotonic, sleep
import traceback

MODULES = (
    'carr_library.assistant.templates', 'carr_library.assistant.template_cli',
    'carr_library.assistant.service', 'carr_library.assistant.snapshots',
    'carr_library.assistant.models', 'carr_library.assistant.validation',
    'carr_library.ui.assistant_templates', 'carr_library.ui.assistant_dialog',
    'carr_library.ui.save_all', 'carr_library.library_backup',
)


def module_identities():
    prefix = Path(sys.prefix).resolve(strict=True)
    found = {}
    for name in MODULES:
        path = Path(importlib.import_module(name).__file__).resolve(strict=True)
        if not path.is_relative_to(prefix) or 'site-packages' not in {p.casefold() for p in path.parts}:
            raise ValueError('The native probe imported a checkout module: ' + name)
        found[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    return found


def run(work):
    if os.name != 'nt' or not sys.flags.isolated:
        raise RuntimeError('Actual isolated Windows execution is required.')
    import wx
    from carr_library.application import LibraryApp
    from carr_library.assistant import AssistantService, Scope
    from carr_library.assistant.template_cli import run_templates
    from carr_library.library_backup import create_backup, verify_backup, restore_backup
    from carr_library.ui.assistant_dialog import show_assistant
    from carr_library.ui.save_all import SaveCoordinator

    work = Path(work).absolute()
    work.mkdir(mode=0o700)
    gui = wx.App(False)
    library = LibraryApp(work / 'library')
    first = library.create_workspace('A first selected scope')
    second = library.create_workspace('B newly reviewed scope')
    library.add_text(first, 'Перший текст', 'Shared immutable first workspace source.')
    library.add_text(second, 'Другий текст', 'Shared immutable second workspace source.')
    inputs, outputs = work / 'old-input', work / 'old-output'
    inputs.mkdir()
    outputs.mkdir()
    service = AssistantService(library)
    frame = wx.Frame(None, title='Native named-workflow acceptance')
    frame.library, frame.active, frame._closing = library, (first, None), False
    frame._save_coordinator = SaveCoordinator()
    frame.Show()
    gui.SetTopWindow(frame)
    checks, failures, timers = [], [], []
    metrics = {}
    deadline = monotonic() + 150
    chosen = {}
    source_before = {row.id: library.browse(row.id)
                     for row in library.workspaces()}

    def check(name, value):
        if not value:
            raise AssertionError(name)
        checks.append(name)

    def press(button):
        if not isinstance(button, wx.Button) or not button.IsEnabled():
            raise AssertionError('Required native button is disabled.')
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        if not button.GetEventHandler().ProcessEvent(event):
            raise AssertionError('The actual native button did not handle its command.')

    def stop_modals():
        for window in reversed(wx.GetTopLevelWindows()):
            try:
                if hasattr(window, 'template_handlers'):
                    window.template_handlers['close']()
                elif isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            except RuntimeError:
                pass

    def scheduled(callback):
        def invoke():
            try:
                callback()
            except Exception:
                failures.append(traceback.format_exc())
                stop_modals()
        timers.append(wx.CallLater(20, invoke))

    def wait(condition):
        while not condition():
            if failures:
                raise AssertionError(failures[-1])
            if monotonic() >= deadline:
                raise TimeoutError('Actual workflow controls exceeded their bounded deadline.')
            wx.YieldIfNeeded()
            sleep(.01)

    def answer_name(parent, value):
        def accept():
            candidates = [window for window in wx.GetTopLevelWindows()
                          if isinstance(window, wx.TextEntryDialog) and window.GetParent() is parent]
            if not candidates:
                if monotonic() >= deadline:
                    raise TimeoutError('The actual workflow name dialog did not appear.')
                scheduled(accept)
                return
            if len(candidates) != 1:
                raise AssertionError('Expected one owned native workflow name dialog.')
            candidate = candidates[0]
            candidate.SetValue(value)
            candidate.EndModal(wx.ID_OK)
        scheduled(accept)

    def answer_remove(parent):
        api = ctypes.WinDLL('user32', use_last_error=True)
        api.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
        api.FindWindowW.restype = wintypes.HWND
        api.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
        api.GetWindow.restype = wintypes.HWND
        api.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        api.SendMessageW.restype = ctypes.c_ssize_t
        owner = parent.GetHandle()
        def accept():
            handle = api.FindWindowW(None, 'Remove workflow template')
            if not handle:
                if monotonic() >= deadline:
                    raise TimeoutError('The actual removal confirmation did not appear.')
                scheduled(accept)
                return
            if api.GetWindow(handle, 4) != owner:  # GW_OWNER
                raise AssertionError('The native removal prompt belongs to another window.')
            api.SendMessageW(handle, 0x0111, 6, 0)  # WM_COMMAND / IDYES
        scheduled(accept)

    def template_window():
        result = [window for window in wx.GetTopLevelWindows()
                  if hasattr(window, 'template_controls') and window.GetTitle() == 'Saved workflow templates']
        if len(result) != 1:
            raise AssertionError('Expected exactly one owned template settings window.')
        return result[0]

    def edit_templates():
        child = template_window()
        controls = child.template_controls
        buttons, rows = controls['buttons'], controls['rows']
        wait(lambda: buttons['refresh'].IsEnabled())
        check('native_template_controls_named', all(
            isinstance(button, wx.Button) and button.GetName() for button in buttons.values())
            and rows.GetName() and controls['status'].GetName())
        check('native_empty_inventory_no_job', rows.GetCount() == 0 and service.jobs() == ())
        answer_name(child, 'Початковий пошук')
        press(buttons['save'])
        summaries = []
        check('save_all_observes_pending_settings', frame._save_coordinator.has_unsaved()
              and frame._save_coordinator.save_all(summaries.append) and not summaries)
        wait(lambda: buttons['refresh'].IsEnabled())
        values = service.workflow_templates().list()
        check('native_save_publishes_inert_draft', len(values) == 1
              and values[0].revision == 1 and 'scope' not in values[0].draft and service.jobs() == ())
        check('save_all_waits_for_durable_completion', len(summaries) == 1
              and summaries[0].complete and not frame._save_coordinator.has_unsaved())
        rows.SetSelection(0)
        answer_name(child, 'Current native workflow')
        press(buttons['rename'])
        wait(lambda: buttons['refresh'].IsEnabled())
        values = service.workflow_templates().list()
        check('native_rename_uses_same_identity_revision', len(values) == 1
              and values[0].name == 'Current native workflow' and values[0].revision == 2)
        rows.SetSelection(0)
        answer_name(child, 'Current native workflow')
        press(buttons['update'])
        wait(lambda: buttons['refresh'].IsEnabled())
        value = service.workflow_templates().list()[0]
        check('native_update_commits_reviewed_revision', value.revision == 3
              and value.steps[0].tool == 'library.search' and value.steps[0].arguments['query'] == 'Shared')
        metrics['saved_revision'] = value.revision
        chosen['saved'] = value
        rows.SetSelection(0)
        press(buttons['load'])

    def ready(dialog, controls):
        try:
            check('native_main_template_action_named', controls['templates'].GetName() == 'Templates...'
                  and controls['templates'].IsEnabled())
            workspaces = library.workspaces()
            controls['scope'].SetSelection(next(i + 1 for i, row in enumerate(workspaces) if row.id == first))
            controls['input'].ChangeValue(str(inputs))
            controls['output'].ChangeValue(str(outputs))
            controls['full_backup'].SetValue(True)
            controls['budget'].ChangeValue('64')
            controls['instruction'].ChangeValue('Search "Shared"')
            press(controls['preview'])
            wait(lambda: controls['preview'].IsEnabled())
            check('native_original_plan_preview_only', controls['run'].IsEnabled() and service.jobs() == ())
            scheduled(edit_templates)
            press(controls['templates'])
            if failures:
                raise AssertionError(failures[-1])
            check('native_load_clears_previous_permissions', controls['scope'].GetSelection() == 0
                  and controls['input'].GetValue() == controls['output'].GetValue() == ''
                  and controls['full_backup'].GetValue() is False
                  and controls['budget'].GetValue() == '1024')
            check('native_loaded_draft_cannot_run_before_preview', not controls['run'].IsEnabled()
                  and service.jobs() == () and 'scope' not in json.loads(controls['instruction'].GetValue()))
            controls['scope'].SetSelection(next(i + 1 for i, row in enumerate(workspaces) if row.id == second))
            press(controls['preview'])
            wait(lambda: controls['preview'].IsEnabled())
            check('native_fresh_scope_preview_creates_no_job', controls['run'].IsEnabled()
                  and service.jobs() == () and second in controls['review'].GetValue())
            press(controls['run'])
            wait(lambda: controls['preview'].IsEnabled())
            jobs = service.jobs()
            check('native_explicit_run_completes_current_scope', len(jobs) == 1
                  and jobs[0].status == 'complete' and jobs[0].completed == 1)
            record = service.store.load(jobs[0].id)
            hits = record['steps'][0]['result']['results']
            check('native_results_use_only_new_workspace', bool(hits)
                  and all(hit['workspace'] == second for hit in hits)
                  and record['plan']['scope']['workspace_id'] == second)
            metrics['fresh_run_completed_steps'] = jobs[0].completed
            chosen['job_id'] = jobs[0].id
        except Exception:
            failures.append(traceback.format_exc())
        finally:
            press(controls['close'])

    try:
        show_assistant(frame, on_ready=ready)
        if failures:
            raise AssertionError('\n'.join(failures))
        saved = chosen['saved']
        export = work / 'explicit-template.json'
        receipt = run_templates(['export', saved.id, str(export)], service)
        check('actual_selected_template_export', receipt['status'] == 'complete'
              and json.loads(export.read_bytes()) == saved.to_dict())
        imported = run_templates(['import', str(export), '--name', 'Imported native workflow'], service)
        check('actual_import_creates_new_inert_identity', imported['id'] != saved.id
              and imported['revision'] == 1 and imported['draft'] == saved.draft
              and len(service.jobs()) == 1)
        metrics['template_records_after_import'] = len(service.workflow_templates().list())
        backup = work / 'complete-backup'
        digest = create_backup(library, backup)
        manifest = verify_backup(backup, digest)
        template_paths = [row['path'] for row in manifest['sidecars'] if '/templates/' in row['path']]
        check('actual_complete_backup_includes_typed_templates', len(template_paths) == 2)
        restored = restore_backup(backup, work / 'restored-library', digest)
        recovered = AssistantService(LibraryApp(restored))
        check('actual_restore_preserves_exact_inert_templates',
              recovered.workflow_templates().get(saved.id) == saved
              and len(recovered.workflow_templates().list()) == 2)
        metrics['restored_template_records'] = len(recovered.workflow_templates().list())
        check('actual_restored_old_job_requires_reapproval',
              recovered.get(chosen['job_id']).status == 'attention')
        before = {row.id for row in recovered.jobs()}
        current = recovered.preview(saved.instruction(), Scope(first))
        check('actual_restored_template_rebind_is_preview_only', current.scope.workspace_id == first
              and {row.id for row in recovered.jobs()} == before)
        second_backup = work / 'second-complete-backup'
        second_digest = create_backup(recovered.app, second_backup)
        check('actual_rebackup_preserves_template_inventory',
              len([row for row in verify_backup(second_backup, second_digest)['sidecars']
                   if '/templates/' in row['path']]) == 2)

        def remove_current():
            child = template_window()
            controls = child.template_controls
            wait(lambda: controls['buttons']['refresh'].IsEnabled())
            values = service.workflow_templates().list()
            controls['rows'].SetSelection(next(i for i, value in enumerate(values) if value.id == saved.id))
            answer_remove(child)
            press(controls['buttons']['remove'])
            wait(lambda: controls['buttons']['refresh'].IsEnabled())
            remaining = service.workflow_templates().list()
            check('native_remove_keeps_other_settings_and_jobs', len(remaining) == 1
                  and remaining[0].id == imported['id'] and len(service.jobs()) == 1)
            press(controls['buttons']['close'])

        def remove_ready(dialog, controls):
            try:
                scheduled(remove_current)
                press(controls['templates'])
            except Exception:
                failures.append(traceback.format_exc())
            finally:
                press(controls['close'])
        show_assistant(frame, on_ready=remove_ready)
        if failures:
            raise AssertionError('\n'.join(failures))
        check('actual_original_catalog_unchanged',
              source_before == {row.id: library.browse(row.id)
                                for row in library.workspaces()})
        check('native_all_settings_writes_settled', not frame._save_coordinator.has_unsaved()
              and not frame._save_coordinator.participants)
        return {'status': 'PASS', 'actual_windows': True, 'isolated': True,
                'checks': checks, 'metrics': metrics, 'module_origins': module_identities(),
                'nvda': 'NOT_RUN', 'language_model_quality': 'NOT_QUALIFIED',
                'physical_audio': 'NOT_RUN'}
    finally:
        for timer in timers:
            timer.Stop()
        stop_modals()
        frame.Destroy()
        wx.YieldIfNeeded()
        gui.Destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True, type=Path)
    args = parser.parse_args()
    handle = None
    report = None
    try:
        if sys.platform != 'win32' or not sys.flags.isolated:
            raise ValueError('Use an actual isolated installed Windows interpreter.')
        from carr_library.native_runtime import activate_bundled_runtime
        handle = activate_bundled_runtime()
        before = module_identities()
        report = run(args.work)
        if report['module_origins'] != before:
            raise ValueError('Installed native module identities changed during execution.')
    except Exception as error:
        traceback.print_exc()
        report = {'status': 'FAIL', 'reason': type(error).__name__ + ': ' + str(error),
                  'actual_windows': sys.platform == 'win32', 'isolated': bool(sys.flags.isolated),
                  'nvda': 'NOT_RUN', 'language_model_quality': 'NOT_QUALIFIED'}
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception as error:
                report = {'status': 'FAIL', 'reason': 'Native-runtime cleanup: ' + str(error),
                          'nvda': 'NOT_RUN'}
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
