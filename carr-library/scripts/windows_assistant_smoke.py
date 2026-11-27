"""Exercise installed primary assistant controls and durable jobs on Windows.

The documents, library and jobs are temporary synthetic fixtures. Native buttons
run the production planner, importer, search and checkpoint recovery. This does
not claim manual screen-reader acceptance or execute paid provider operations.
"""
from argparse import ArgumentParser
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from time import monotonic


SOURCE_FILES = (
    'assistant/__init__.py', 'assistant/models.py', 'assistant/planner.py',
    'assistant/service.py', 'assistant/state.py', 'assistant/backup_receipt.py',
    'ui/assistant_dialog.py',
)
EXPECTED_CHECKS = (
    'native preview is non-executing and shows exact operations',
    'native Run executes real import and scoped search',
    'native progress reports completion only after durable verification',
    'reopened native saved job resumes without duplicate imports',
    'unsupported native instructions execute nothing',
    'original Unicode document bytes remain unchanged',
)


def exercise_controls(root, wx):
    """Run actual native controls; also callable by a separate local GUI preflight."""
    from carr_library.application import LibraryApp
    from carr_library.assistant import AssistantService
    from carr_library.ui import assistant_dialog

    inputs, outputs = root / 'Українські джерела', root / 'Обраний результат'
    inputs.mkdir()
    outputs.mkdir()
    source = inputs / 'залізниця Ω.txt'
    raw = 'Railway transport connected cities.\r\nЗалізниця з’єднувала міста.'.encode('utf-8')
    source.write_bytes(raw)
    library = LibraryApp(root / 'library')
    other = library.create_workspace('Separate private scope')
    library.add_text(other, 'Excluded source', 'Railway transport belongs to another library.')
    baseline_ids = tuple(row.edition_id for row in library.browse(other))
    checks, progress, completed_state = [], [], {}

    def descendants(window):
        for child in window.GetChildren():
            yield child
            yield from descendants(child)

    def button(dialog, label):
        return next(control for control in descendants(dialog)
                    if isinstance(control, wx.Button) and control.GetLabel().replace('&', '') == label)

    def click(control):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, control.GetId())
        event.SetEventObject(control)
        control.GetEventHandler().ProcessEvent(event)

    def run_dialog(reopened):
        # A new application object and native dialog must recover the durable job.
        frame = wx.Frame(None, title='Synthetic native assistant acceptance')
        frame.library = LibraryApp(library.root) if reopened else library
        frame.active = None
        frame.Show()
        observer = AssistantService(frame.library)
        started, failures = monotonic(), []
        state = {'phase': 'open', 'dialog': None}

        def close():
            dialog = state['dialog']
            if dialog is not None and not state.get('closing'):
                state['closing'] = True
                click(button(dialog, 'Close'))

        def observe(review):
            change = review.ChangeValue
            def record(value):
                change(value)  # Preserve the real native control update.
                match = re.match(r'^(\d+)/(\d+):', str(value))
                final = str(value).startswith('complete:')
                if match or final:
                    try:
                        jobs = observer.jobs()
                        assert len(jobs) == 1
                        job = jobs[0]
                        done, total = map(int, match.groups()) if match else (job.completed, job.total)
                        assert total == job.total == 3 and 0 <= done <= job.completed
                        if done == total or final:
                            assert job.status == 'complete' and job.completed == job.total
                        progress.append({'reopened': reopened, 'displayed_completed': done,
                                         'total': total, 'durable_completed': job.completed,
                                         'durable_status': job.status, 'final_text': final})
                    except BaseException as exc:
                        failures.append(type(exc).__name__ + ': ' + str(exc))
                        wx.CallAfter(close)
            # Observation only: no service, result, thread or native event is mocked.
            review.ChangeValue = record

        def select_saved():
            try:
                if monotonic() - started > 60:
                    raise RuntimeError('The native saved-job selector did not open.')
                choices = [window for window in wx.GetTopLevelWindows()
                           if isinstance(window, wx.SingleChoiceDialog)
                           and window.GetParent() == state['dialog']
                           and window.IsShown() and window.IsModal()
                           and window.GetTitle() == 'Assistant jobs']
                if not choices:
                    wx.CallLater(25, select_saved)
                    return
                choice = choices[0]
                assert len(observer.jobs()) == 1
                choice.SetSelection(0)
                assert choice.GetSelection() == 0
                click(choice.FindWindowById(wx.ID_OK))
            except BaseException as exc:
                failures.append(type(exc).__name__ + ': ' + str(exc))
                for window in wx.GetTopLevelWindows():
                    if isinstance(window, wx.SingleChoiceDialog):
                        window.EndModal(wx.ID_CANCEL)
                wx.CallAfter(close)

        def poll():
            try:
                if monotonic() - started > 60:
                    raise RuntimeError('Native assistant controls exceeded the test deadline.')
                dialog = state['dialog']
                if dialog is None:
                    dialog = next((window for window in wx.GetTopLevelWindows()
                                   if isinstance(window, wx.Dialog)
                                   and window.GetParent() == frame
                                   and window.IsShown() and window.IsModal()
                                   and window.GetTitle().startswith('Local assistant')), None)
                    if dialog is None:
                        wx.CallLater(25, poll)
                        return
                    state['dialog'] = dialog
                controls = {control.GetName(): control for control in descendants(dialog)}
                review = controls['Plan preview and actual results']
                if state['phase'] == 'open':
                    observe(review)
                    if reopened:
                        wx.CallLater(25, select_saved)
                        click(button(dialog, 'Saved jobs'))
                        if failures:
                            close()
                            return
                        assert button(dialog, 'Run / resume').IsEnabled()
                        assert 'original reviewed scope' in review.GetValue()
                    else:
                        controls['Allowed input folder (optional; no other folders are readable)'].SetValue(str(inputs))
                        controls['Chosen output folder (required for audio, extraction or backup)'].SetValue(str(outputs))
                        controls['Goal — guided English/Ukrainian commands or typed JSON plan'].SetValue(
                            'Create library "Native assistant"\nImport "' + str(source) + '"\nSearch "Railway transport"')
                        assert not button(dialog, 'Run / resume').IsEnabled()
                        click(button(dialog, 'Preview plan'))
                        assert button(dialog, 'Run / resume').IsEnabled()
                        assert 'library.import' in review.GetValue() and 'library.search' in review.GetValue()
                        assert [row.id for row in frame.library.workspaces()] == [other]
                        assert not observer.jobs() and source.read_bytes() == raw
                        checks.append(EXPECTED_CHECKS[0])
                    click(button(dialog, 'Run / resume'))
                    state['phase'] = 'running'
                elif state['phase'] == 'running' and review.GetValue().startswith('complete:'):
                    assert '3 of 3 verified operations' in review.GetValue()
                    job, = observer.jobs()
                    assert job.status == 'complete' and job.completed == job.total == 3
                    workspace = job.results[0]['workspace']
                    assert workspace != other
                    imported = tuple(row.edition_id for row in frame.library.browse(workspace))
                    assert len(imported) == 1
                    hits = job.results[2]['results']
                    assert hits and all(hit['workspace'] == workspace and hit['verified'] for hit in hits)
                    assert [hit['quote'] for hit in hits] == ['Railway transport']
                    assert tuple(row.edition_id for row in frame.library.browse(other)) == baseline_ids
                    assert len(frame.library.workspaces()) == 2
                    if reopened:
                        assert job.id == completed_state['id'] and imported == completed_state['editions']
                        assert job.results == completed_state['results']
                        checks.append(EXPECTED_CHECKS[3])
                        controls['Goal — guided English/Ukrainian commands or typed JSON plan'].SetValue(
                            'Upload every file to a remote service')
                        click(button(dialog, 'Preview plan'))
                        assert not button(dialog, 'Run / resume').IsEnabled()
                        assert 'unsupported' in review.GetValue().lower()
                        assert len(observer.jobs()) == 1 and len(frame.library.browse(workspace)) == 1
                        checks.append(EXPECTED_CHECKS[4])
                        assert source.read_bytes() == raw
                        checks.append(EXPECTED_CHECKS[5])
                    else:
                        completed_state.update(id=job.id, editions=imported, results=job.results)
                        observed = [row for row in progress if not row['reopened']]
                        assert {row['displayed_completed'] for row in observed} == {0, 1, 2, 3}
                        assert any(row['final_text'] for row in observed)
                        checks.extend(EXPECTED_CHECKS[1:3])
                    close()
                    return
                if not state.get('closing'):
                    wx.CallLater(25, poll)
            except BaseException as exc:
                failures.append(type(exc).__name__ + ': ' + str(exc))
                close()

        wx.CallAfter(poll)
        try:
            assistant_dialog.show_assistant(frame)
        finally:
            frame.Destroy()
        if failures:
            raise RuntimeError('; '.join(failures))

    run_dialog(False)
    run_dialog(True)
    if tuple(checks) != EXPECTED_CHECKS:
        raise RuntimeError('Native assistant did not complete every required operation.')
    return {'checks': checks, 'check_count': len(checks), 'progress_observations': progress,
            'saved_job_reopened': True, 'unrelated_workspace_unchanged': True}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32':
        print(json.dumps({'status': 'NOT_RUN', 'windows_execution': 'NOT_RUN'}))
        return 77
    bundle = args.bundle.resolve()
    import carr_library
    installed = Path(carr_library.__file__).resolve().parent
    if not installed.is_relative_to(bundle / '.venv'):
        raise ValueError('The probe must use the actual installed application.')
    manifest = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8'))
    if any(not re.fullmatch('[0-9a-f]{40}', manifest.get(key, '')) for key in ('source_commit', 'source_tree')):
        raise ValueError('The installed bundle must identify the exact committed source.')
    from carr_library.native_runtime import activate_bundled_runtime
    from carr_library.audio_runtime import activate_bundled_audio_runtime
    native_handle = activate_bundled_runtime(bundle_root=bundle)
    assert native_handle is not None and activate_bundled_audio_runtime(bundle_root=bundle)
    import wx
    source = Path(__file__).resolve().parents[1] / 'src' / 'carr_library'
    hashes = {}
    for name in SOURCE_FILES:
        raw = (source / name).read_bytes()
        if (installed / name).read_bytes() != raw:
            raise ValueError('Installed assistant source differs from the acceptance checkout.')
        hashes[name] = sha256(raw).hexdigest()
    application = wx.App(False)
    with TemporaryDirectory(prefix='carr-native-assistant-Архів-') as temporary:
        result = exercise_controls(Path(temporary), wx)
    for name, expected in hashes.items():
        if sha256((source / name).read_bytes()).hexdigest() != expected:
            raise ValueError('Acceptance source changed during execution.')
    result.update(status='PASS', windows_execution='PASS', installed_modules='PASS',
                  source_commit=manifest['source_commit'], source_tree=manifest['source_tree'],
                  source_hashes=hashes, manual_nvda='NOT_RUN', paid_api='NOT_RUN')
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
