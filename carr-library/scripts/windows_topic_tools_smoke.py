"""Actual installed Windows topic actions through the existing assistant dialog."""
import argparse
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
import threading
from time import monotonic, sleep
import traceback

MODULES = (
    'carr_library.assistant.models', 'carr_library.assistant.service',
    'carr_library.assistant.validation', 'carr_library.assistant.topic_tools',
    'carr_library.assistant.templates', 'carr_library.assistant.snapshots',
    'carr_library.research.topic_tree', 'carr_library.research.evidence_export',
    'carr_library.research.export_file', 'carr_library.ui.assistant_dialog',
)


def module_identities():
    prefix = Path(sys.prefix).resolve(strict=True)
    found = {}
    for name in MODULES:
        path = Path(importlib.import_module(name).__file__).resolve(strict=True)
        if not path.is_relative_to(prefix) or 'site-packages' not in {part.casefold() for part in path.parts}:
            raise ValueError('The native topic probe imported a checkout module: ' + name)
        found[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
    return found


def run(work):
    if os.name != 'nt' or not sys.flags.isolated:
        raise ValueError('Actual isolated Windows execution is required.')
    import wx
    from carr_library.application import LibraryApp
    from carr_library.assistant import AssistantService, Plan, Scope
    from carr_library.library_backup import create_backup, verify_backup, restore_backup
    from carr_library.ui.assistant_dialog import show_assistant

    work = Path(work).absolute()
    work.mkdir(mode=0o700)
    gui = wx.App(False)
    library = LibraryApp(work / 'library')
    workspace = library.create_workspace('Selected topic workflow')
    other = library.create_workspace('Unselected private topics')
    library.add_text(workspace, 'Synthetic original. Незмінне джерело.',
                     'Exact railway evidence.\r\nОригінальний рядок.', authors=('Synthetic Author',))
    existing = library.create_topic(workspace, 'Verified saved evidence')
    foreign = library.create_topic(other, 'Unselected topic')
    hit, = library.search(workspace, 'railway')
    library.save_hit(workspace, existing, hit.ref, hit.revision,
                     note='Separate research commentary. Окрема нотатка.')
    original_catalog = library.browse(workspace)
    original_evidence = library.evidence(workspace, existing)
    exact_export = library.export_topic(workspace, existing).encode('utf-8')
    output = work / 'exports'
    output.mkdir()
    service = AssistantService(library)
    frame = wx.Frame(None, title='Actual native assistant topic qualification')
    frame.library, frame.active, frame._closing = library, (workspace, None), False
    frame.Show()
    gui.SetTopWindow(frame)
    checks, errors, timers, selected = [], [], [], {}
    deadline = monotonic() + 90
    live = True

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('CHECK ' + name, flush=True)

    def press(button):
        if not isinstance(button, wx.Button) or not button.IsEnabled():
            raise AssertionError('The required native action is unavailable.')
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        if not button.GetEventHandler().ProcessEvent(event):
            raise AssertionError('The current native control did not handle its action.')

    def close_owned():
        controls = selected.get('controls')
        if controls is not None:
            try:
                press(controls['close'])
            except RuntimeError:
                pass

    def failed(error):
        errors.append(''.join(traceback.format_exception(type(error), error, error.__traceback__)))
        print(errors[-1], file=sys.stderr, flush=True)
        close_owned()

    def later(action):
        def invoke():
            if not live:
                return
            try:
                if monotonic() >= deadline:
                    raise TimeoutError('Native topic controls exceeded their explicit deadline.')
                action()
            except Exception as error:
                failed(error)
        timers.append(wx.CallLater(25, invoke))

    def until(condition, action):
        if condition():
            action()
        else:
            later(lambda: until(condition, action))

    def executed():
        controls = selected['controls']
        reports = service.jobs()
        check('native_current_dialog_completes_five_topic_steps',
              len(reports) == 1 and reports[0].status == 'complete' and reports[0].completed == 5)
        record = service.store.load(reports[0].id)
        selected['job'] = reports[0].id
        selected['record'] = record
        first, child, listed, empty_export, evidence_export = [row['result'] for row in record['steps']]
        check('native_created_topics_use_deterministic_scoped_references',
              first['workspace'] == child['workspace'] == workspace
              and child['parent'] == first['topic_id'])
        check('native_topic_list_excludes_unselected_workspace',
              {row['id'] for row in listed['topics']} == {existing, first['topic_id'], child['topic_id']}
              and foreign not in {row['id'] for row in listed['topics']})
        check('native_new_child_export_uses_its_resolved_topic',
              empty_export['topic_id'] == child['topic_id'] and (output / 'child.md').is_file())
        check('native_exact_evidence_export_preserves_original_bytes',
              (output / 'evidence.md').read_bytes() == exact_export
              and evidence_export['sha256'] == sha256(exact_export).hexdigest())
        check('native_export_keeps_commentary_separate_and_origins_visible',
              b'railway' in exact_export and b'Separate research commentary.' in exact_export
              and b'Synthetic Author' in exact_export)
        check('native_created_outputs_leave_no_staging_files',
              sorted(path.name for path in output.iterdir()) == ['child.md', 'evidence.md'])
        check('native_run_preserves_original_catalog_and_evidence',
              library.browse(workspace) == original_catalog
              and library.evidence(workspace, existing) == original_evidence)
        check('native_result_is_visible_after_owned_worker_finishes',
              'complete' in controls['review'].GetValue().casefold()
              and controls['preview'].IsEnabled() and not controls['pause'].IsEnabled())
        close_owned()

    def reviewed():
        controls = selected['controls']
        check('native_topic_preview_has_no_jobs_or_new_topics',
              service.jobs() == () and len(library.topics(workspace)) == 1)
        check('native_review_exposes_all_three_topic_tools',
              all(name in controls['review'].GetValue() for name in ('topic.list', 'topic.create', 'topic.export')))
        check('native_explicit_run_available_only_after_preview', controls['run'].IsEnabled())
        press(controls['run'])
        later(lambda: until(lambda: controls['preview'].IsEnabled(), executed))

    def ready(dialog, controls):
        selected['controls'] = controls
        try:
            check('native_topic_scope_and_actions_are_named',
                  all(controls[key].GetName() for key in ('scope', 'instruction', 'output', 'preview', 'run', 'close')))
            controls['instruction'].SetFocus()
            check('native_topic_instruction_accepts_keyboard_focus',
                  wx.Window.FindFocus() is controls['instruction'])
            controls['output'].ChangeValue(str(output))
            controls['instruction'].ChangeValue(
                'Створи тему "Parent"\nСтвори тему "Child" у "@0"\nПокажи теми\n'
                'Експортуй тему "@1" як "child.md"\n'
                'Експортуй тему "' + existing + '" як "evidence.md"')
            check('native_unreviewed_topic_draft_cannot_run', not controls['run'].IsEnabled())
            press(controls['preview'])
            later(lambda: until(lambda: controls['preview'].IsEnabled(), reviewed))
        except Exception as error:
            failed(error)

    try:
        show_assistant(frame, on_ready=ready)
        if errors:
            raise AssertionError('\n'.join(errors))
        until_threads = monotonic() + 5
        while any(thread.name.startswith('local-assistant') and thread.is_alive()
                  for thread in threading.enumerate()) and monotonic() < until_threads:
            sleep(.01)
        check('native_assistant_workers_settle_before_backup',
              not any(thread.name.startswith('local-assistant') and thread.is_alive()
                      for thread in threading.enumerate()))
        saved = service.workflow_templates().save('Current topic workflow', Plan.from_dict(selected['record']['plan']))
        check('actual_named_topic_workflow_is_an_inert_draft', 'scope' not in saved.draft
              and len(service.jobs()) == 1 and saved.steps[1].arguments['parent'] == '@0')
        backup = work / 'backup'
        checksum = create_backup(library, backup)
        verify_backup(backup, checksum)
        restored_root = restore_backup(backup, work / 'restored', checksum)
        restored = LibraryApp(restored_root)
        recovered = AssistantService(restored)
        check('actual_backup_restore_preserves_all_scoped_topics',
              restored.topics(workspace) == library.topics(workspace)
              and restored.topics(other) == library.topics(other))
        check('actual_backup_restore_preserves_exact_evidence',
              restored.evidence(workspace, existing) == original_evidence)
        check('actual_restored_topic_job_requires_new_approval',
              recovered.get(selected['job']).status == 'attention')
        before_output = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in output.iterdir()}
        try:
            recovered.resume(selected['job'])
        except PermissionError:
            refused = True
        else:
            refused = False
        check('actual_restored_job_does_not_replay_external_exports', refused and
              before_output == {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in output.iterdir()})
        check('actual_restore_retains_inert_topic_template',
              recovered.workflow_templates().get(saved.id) == saved)
        fresh_output = work / 'reviewed-new-output'
        fresh_output.mkdir()
        preview = recovered.preview(saved.instruction(), Scope(workspace, (), fresh_output))
        check('actual_rebound_topic_template_is_preview_only',
              preview.scope.output_root == fresh_output and len(recovered.jobs()) == 1
              and not list(fresh_output.iterdir()))
        return {'status': 'PASS', 'actual_windows': True, 'isolated': True, 'native_controls': True,
                'checks': checks, 'module_origins': module_identities(),
                'metrics': {'completed_steps': 5, 'new_topics': 2, 'exported_files': 2,
                            'restored_scoped_topics': 3, 'retained_template_records': 1,
                            'restored_jobs_replayed': 0},
                'nvda': 'NOT_RUN', 'language_model_quality': 'NOT_QUALIFIED',
                'physical_audio': 'NOT_RUN'}
    finally:
        live = False
        for timer in timers:
            timer.Stop()
        close_owned()
        frame.Destroy()
        wx.YieldIfNeeded()
        gui.Destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True, type=Path)
    args = parser.parse_args()
    native = None
    report = None
    try:
        if os.name != 'nt' or not sys.flags.isolated:
            raise ValueError('Use an actual isolated installed Windows interpreter.')
        import faulthandler
        faulthandler.dump_traceback_later(120, repeat=False)
        from carr_library.native_runtime import activate_bundled_runtime
        native = activate_bundled_runtime()
        before = module_identities()
        report = run(args.work)
        if report['module_origins'] != before:
            raise ValueError('Installed topic implementation bytes changed during execution.')
    except Exception as error:
        traceback.print_exc()
        report = {'status': 'FAIL', 'reason': type(error).__name__ + ': ' + str(error),
                  'actual_windows': os.name == 'nt', 'isolated': bool(sys.flags.isolated),
                  'nvda': 'NOT_RUN', 'language_model_quality': 'NOT_QUALIFIED'}
    finally:
        try:
            import faulthandler
            faulthandler.cancel_dump_traceback_later()
        except RuntimeError:
            pass
        if native is not None:
            try:
                native.close()
            except Exception as error:
                report = {'status': 'FAIL', 'reason': 'Native runtime cleanup: ' + str(error)}
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
