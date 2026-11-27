"""Queued native controller behavior, with real native controls tested separately."""
import ast
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.audio import AudioCancelled
from carr_library.studio.project import Cue, Project, Scene


@pytest.fixture
def dialog(tmp_path):
    import carr_library
    source = Path(carr_library.__file__).parent / 'ui/studio_clips.py'
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    nodes = [node for node in parsed.body if isinstance(node, (ast.Assign, ast.ClassDef))]
    wx = SimpleNamespace(Dialog=object, ID_CANCEL=2)
    env = dict(wx=wx, Path=Path, __name__='carr_library.ui._clips_probe', __package__='carr_library.ui')
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), env)
    value = object.__new__(env['ClipDialog'])
    value.closed = value.closing = value.pending = value.destroyed = False
    value.cancelled = threading.Event()
    value.player = None
    value.playback_generation = 0
    value.previews = []
    value.status = Mock()
    value._enable = Mock()
    value.Hide = Mock()
    value.IsModal = Mock(return_value=False)
    value._accept = Mock()
    value.library = SimpleNamespace(root=tmp_path / 'library')
    value.library_root = value.library.root
    value.library_root.mkdir()
    value._library_identity = value._library_stamp()
    value.frame = SimpleNamespace(working=False, cancel_event=threading.Event(),
                                  library=value.library, _closing=False)
    value.test_environment = env
    value.wx = wx
    calls = []
    def run_job(title, operation, done, failed):
        value.frame.working = True
        value.frame.cancel_event.clear()
        calls.append((operation, done, failed))
    value.frame.run_job = run_job
    def finish():
        operation, done, failed = calls.pop(0)
        try:
            result = operation()
        except Exception as exc:
            value.frame.working = False
            failed(exc)
        else:
            value.frame.working = False
            done(result)
    value.finish, value.calls = finish, calls
    return value


def test_close_during_worker_waits_for_cleanup_and_suppresses_late_controls(dialog):
    done = Mock()
    assert dialog._run('Clip operation', lambda: 'durable result', done)
    dialog.close()
    assert dialog.pending and dialog.closing and not dialog.closed
    assert dialog.frame.cancel_event.is_set()
    dialog.Hide.assert_not_called()
    dialog.finish()
    assert dialog.closed and not dialog.pending
    done.assert_not_called()
    dialog.Hide.assert_called_once()
    before = dialog.status.ChangeValue.call_count
    dialog.message('Late callback')
    assert dialog.status.ChangeValue.call_count == before


def test_close_discards_unplayed_preview_after_worker_returns(dialog, tmp_path):
    path = tmp_path / 'clip-preview-example.wav'
    def render_then_close():
        path.write_bytes(b'Explicit synthetic output')
        dialog.close()
        return path
    dialog._run('Preview', render_then_close, Mock(), preview=True)
    dialog.finish()
    assert dialog.closed and not path.exists()


def test_audio_cancellation_keeps_dialog_usable_and_has_interruption_type(dialog):
    def operation():
        raise AudioCancelled('Clip cancelled before publication')
    dialog._run('Import', operation, Mock())
    dialog.finish()
    assert not dialog.closed and not dialog.pending
    error = dialog.status.ChangeValue.call_args.args[0]
    assert 'cancelled before publication' in error
    assert dialog._run('Next operation', lambda: 'done', Mock())


def test_refused_shared_job_never_leaves_clip_controls_pending(dialog):
    dialog.frame.run_job = lambda *args: None
    dialog._run('Clip', lambda: None, Mock())
    assert not dialog.pending
    assert 'could not start' in dialog.status.ChangeValue.call_args.args[0]


def test_another_operation_does_not_receive_clip_cancellation(dialog):
    dialog.frame.working = True
    assert not dialog._run('Clip', Mock(), Mock())
    dialog.cancel()
    assert not dialog.frame.cancel_event.is_set()
    assert not dialog.calls


def test_save_binds_reviewed_clip_and_settings_before_queue(dialog):
    cue = Cue('clip-1', '/retained.audio')
    scene = Scene('scene-1', 'Scene', (), cues=(cue,))
    project = Project('project-1', 'Project', (), (scene,))
    dialog.project = project
    dialog._scene, dialog._clip = lambda: scene, lambda: cue
    settings = {'start': 2, 'trim_start': .25, 'duration': 1, 'pan': -1}
    dialog._settings = lambda: settings
    dialog.editor = SimpleNamespace(edit=Mock(return_value=project))
    dialog.save()
    dialog.project = None
    dialog._settings = lambda: {'start': 99}
    dialog.finish()
    args, kwargs = dialog.editor.edit.call_args
    assert args == (project, scene.id, cue.id)
    assert kwargs['start'] == 2 and kwargs['trim_start'] == .25 and kwargs['pan'] == -1
    dialog._accept.assert_called_once_with(project)


def test_stop_invalidates_deferred_playback_and_releases_native_file(dialog, tmp_path):
    path = tmp_path / 'clip-preview-current.wav'
    path.write_bytes(b'fixture')
    dialog.previews = [path]
    dialog.player = Mock()
    generation = dialog.playback_generation
    dialog.close()
    assert dialog.playback_generation > generation
    dialog.player.close.assert_called_once()
    assert not path.exists()


def studio_handler(name, environment):
    import carr_library
    source = Path(carr_library.__file__).parent / 'ui/studio_dialog.py'
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    parent = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == 'show_studio')
    function = next(node for node in parent.body if isinstance(node, ast.FunctionDef) and node.name == name)
    function.body = [ast.copy_location(ast.Global(names=node.names), node)
                     if isinstance(node, ast.Nonlocal) else node for node in function.body]
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), environment)
    return environment[name]


@pytest.mark.parametrize('requires_output,choose_output', [(False, False), (True, False), (True, True)])
def test_native_recovery_uses_explicit_new_parent_only_inside_worker(requires_output, choose_output):
    folder = Mock()
    folder.ShowModal.return_value = 2 if choose_output else 0
    folder.GetPath.return_value = 'C:/Explicit/new-output'
    wx = SimpleNamespace(DirDialog=Mock(return_value=folder), ID_OK=2)
    service = SimpleNamespace(
        get=Mock(return_value={'recovery': {'requires_output_directory': requires_output}}),
        relocate_restored_job=Mock(), run=Mock(return_value={'status': 'complete'}))
    queued = []
    cancel = threading.Event()
    env = dict(wx=wx, Path=Path, busy=False, current_job='selected-job', dialog=object(),
               service=service, cancel=cancel, report=Mock(), scope_current=lambda: True,
               stopping=cancel.is_set, run=lambda work, done, **kw: queued.append(work))
    studio_handler('render', env)()
    service.relocate_restored_job.assert_not_called()
    service.run.assert_not_called()
    if requires_output and not choose_output:
        assert not queued
        folder.Destroy.assert_called_once()
        return
    assert len(queued) == 1
    queued[0](lambda text: None)
    if requires_output:
        service.relocate_restored_job.assert_called_once_with('selected-job', Path('C:/Explicit/new-output'))
        folder.Destroy.assert_called_once()
    else:
        wx.DirDialog.assert_not_called()
        service.relocate_restored_job.assert_not_called()
    assert service.run.call_args.args == ('selected-job',)
    cancelled = service.run.call_args.kwargs['cancelled']
    assert not cancelled()
    cancel.set()
    assert cancelled()


def test_unconfirmed_playback_stop_keeps_dialog_files_and_blocks_new_work(dialog, tmp_path):
    path = tmp_path / 'clip-preview-retained.wav'
    path.write_bytes(b'Owned preview bytes')
    dialog.previews = [path]
    dialog.player = Mock()
    dialog.player.close.side_effect = RuntimeError('Injected native close failure')
    event = Mock()
    event.CanVeto.return_value = True
    dialog.close(event)
    assert not dialog.closed and path.exists()
    event.Veto.assert_called_once()
    assert 'cleanup is still pending' in dialog.status.ChangeValue.call_args.args[0]
    assert dialog._run('Next clip operation', Mock(), Mock()) is False
    assert not dialog.pending and not dialog.calls
    dialog.player.close.side_effect = None
    dialog.player.close.return_value = None
    dialog.close()
    assert dialog.closed and not path.exists()


@pytest.mark.parametrize('change', ['same-path-new-library', 'root-changed', 'frame-closing'])
def test_queued_clip_edit_cannot_use_a_replaced_library(dialog, tmp_path, change):
    operation, done = Mock(return_value='saved'), Mock()
    assert dialog._run('Clip edit', operation, done)
    if change == 'same-path-new-library':
        dialog.frame.library = SimpleNamespace(root=dialog.library_root)
    elif change == 'root-changed':
        dialog.library.root = tmp_path / 'replacement'
    else:
        dialog.frame._closing = True
    dialog.finish()
    operation.assert_not_called()
    done.assert_not_called()
    assert not dialog.pending
    assert 'active library changed or is closing' in dialog.status.ChangeValue.call_args.args[0]


def test_late_committed_clip_result_is_not_applied_to_another_library(dialog):
    done = Mock()
    def operation():
        dialog.frame.library = SimpleNamespace(root=dialog.library_root)
        return 'Committed in the original library'
    dialog._run('Clip edit', operation, done)
    dialog.finish()
    done.assert_not_called()
    assert not dialog.pending
    assert 'saved edit remains in its original library' in dialog.status.ChangeValue.call_args.args[0]


def test_destroy_cancels_only_its_queued_operation_without_dead_controls(dialog):
    operation, done = Mock(), Mock()
    dialog._run('Clip edit', operation, done)
    status_calls = dialog.status.ChangeValue.call_count
    enable_calls = dialog._enable.call_count
    event = SimpleNamespace(GetEventObject=lambda: dialog, Skip=Mock())
    dialog._destroyed(event)
    assert dialog.closed and dialog.cancelled.is_set() and dialog.frame.cancel_event.is_set()
    dialog.finish()
    operation.assert_not_called()
    done.assert_not_called()
    assert dialog.status.ChangeValue.call_count == status_calls
    assert dialog._enable.call_count == enable_calls
    event.Skip.assert_called_once()


def test_destroy_discards_preview_when_parent_never_delivers_completion(dialog, tmp_path):
    path = tmp_path / 'clip-preview-undelivered.wav'
    path.write_bytes(b'Synthetic pending preview')
    done = Mock()
    dialog._run('Preview', lambda: path, done, preview=True)
    operation, callback, failed = dialog.calls.pop()
    assert operation() == path  # Real worker completion precedes the UI callback.
    event = SimpleNamespace(GetEventObject=lambda: dialog, Skip=Mock())
    dialog._destroyed(event)
    assert not path.exists()
    callback(path)
    done.assert_not_called()


def test_destroy_retains_uncertain_native_playback_owner_and_preview(dialog, tmp_path):
    path = tmp_path / 'clip-preview-native-owned.wav'
    path.write_bytes(b'Owned preview')
    dialog.previews = [path]
    dialog.player = Mock()
    dialog.player.close.return_value = False
    progress = Mock()
    dialog.frame.progress_panel = progress
    event = SimpleNamespace(GetEventObject=lambda: dialog, Skip=Mock())
    dialog._destroyed(event)
    assert dialog.closed and path.exists()
    progress.unregister_player.assert_called_once_with(dialog.player, silence_confirmed=False)


def test_deferred_clip_playback_rechecks_exact_library(dialog, tmp_path, monkeypatch):
    import carr_library.speech.player as player_module
    import carr_library.speech.progress_speech as progress_module
    path = tmp_path / 'clip-preview-deferred.wav'
    path.write_bytes(b'Synthetic preview')
    cue = Cue('clip-1', '/retained.audio')
    scene = Scene('scene-1', 'Scene', (), cues=(cue,))
    dialog.project = Project('project-1', 'Project', (), (scene,))
    dialog._scene, dialog._clip = lambda: scene, lambda: cue
    dialog.editor = SimpleNamespace(preview=Mock(return_value=path))
    player = Mock()
    player.close.return_value = None
    deferred = Mock()
    monkeypatch.setattr(player_module, 'WavePlayer', lambda: player)
    monkeypatch.setattr(progress_module, 'play_when_quiet', deferred)
    dialog.wx.CallLater = Mock()
    dialog.preview()
    dialog.finish()
    playing = deferred.call_args.args[1]
    alive = deferred.call_args.kwargs['alive']
    assert alive()
    dialog.frame.library = SimpleNamespace(root=dialog.library_root)
    assert not alive()
    playing()
    player.play.assert_not_called()
    dialog.close()
    assert not path.exists()


@pytest.mark.parametrize('recovery,status', [(True, 'complete'), (False, 'waiting-input')])
def test_waiting_input_preview_never_constructs_or_stats_old_destination(recovery, status):
    old_path = Mock(side_effect=AssertionError('Old destination must remain inert metadata'))
    report = Mock()
    env = dict(busy=False, current_job='restored-job', scope_current=lambda: True,
               service=SimpleNamespace(get=Mock(return_value={
                   'recovery': {'requires_output_directory': recovery},
                   'status': status, 'destination': 'C:/old-owner/output'})),
               Path=old_path, report=report)
    studio_handler('preview', env)()
    old_path.assert_not_called()
    assert 'new output folder' in report.call_args.args[0]


def test_restore_cancelled_while_queued_never_relocates_or_renders():
    folder = Mock()
    folder.ShowModal.return_value = 2
    folder.GetPath.return_value = 'C:/Explicit/new-output'
    cancel = threading.Event()
    service = SimpleNamespace(get=Mock(return_value={'recovery': {'requires_output_directory': True}}),
                              relocate_restored_job=Mock(), run=Mock())
    queued = []
    env = dict(wx=SimpleNamespace(DirDialog=Mock(return_value=folder), ID_OK=2), Path=Path,
               busy=False, current_job='restored-job', dialog=object(), service=service,
               report=Mock(), scope_current=lambda: True, stopping=cancel.is_set,
               run=lambda work, done, **kw: queued.append(work))
    studio_handler('render', env)()
    cancel.set()
    with pytest.raises(InterruptedError, match='cancelled'):
        queued[0](lambda text: None)
    service.relocate_restored_job.assert_not_called()
    service.run.assert_not_called()


def test_unchanged_studio_script_preserves_attached_cues_source_and_render_settings(tmp_path):
    from dataclasses import replace
    from carr_library.ui.studio_dialog import StudioController
    controller = StudioController(SimpleNamespace(root=tmp_path))
    original = controller.save('Narrator: An exact source line.', 'Original title', ('voice-a',))
    line = replace(original.scenes[0].lines[0], source_ref='retained-source:edition-1:line-1')
    scene = replace(original.scenes[0], lines=(line,),
                    cues=(Cue('attached', '/never-open-legacy-audio.wav'),))
    altered = replace(original, scenes=(scene,), mode='authored', sample_rate=44100, revision=2)
    controller.service.projects.save(altered, expected_revision=original.revision)
    controller.project = altered
    saved = controller.save(altered.original_script, altered.title, ('different-default',))
    assert saved == altered
    assert controller.service.projects.load(saved.id) == altered


def test_structural_script_edit_cannot_discard_an_attached_clip(tmp_path):
    from dataclasses import replace
    from carr_library.ui.studio_dialog import StudioController
    controller = StudioController(SimpleNamespace(root=tmp_path))
    original = controller.save('[Scene: Original]\nNarrator: Retained line.', 'Project', ('voice-a',))
    scene = replace(original.scenes[0], cues=(Cue('attached', '/never-open-legacy-audio.wav'),))
    altered = replace(original, scenes=(scene,), revision=2)
    controller.service.projects.save(altered, expected_revision=original.revision)
    controller.project = altered
    edited_script = '[Scene: Renamed]\nNarrator: Changed line.'
    with pytest.raises(ValueError, match='attached audio changed its identity'):
        controller.save(edited_script, altered.title, ('voice-a',))
    assert controller.service.projects.load(altered.id) == altered
    assert controller.project == altered
    assert json.loads(controller.draft.read_text(encoding='utf-8'))['text'] == edited_script


def test_studio_controller_cannot_replace_a_newer_clip_revision(tmp_path):
    from dataclasses import replace
    from carr_library.ui.studio_dialog import StudioController
    controller = StudioController(SimpleNamespace(root=tmp_path))
    original = controller.save('Narrator: Original.', 'Project', ('voice-a',))
    newer = replace(original, title='Saved by another editor', revision=2)
    controller.service.projects.save(newer, expected_revision=original.revision)
    with pytest.raises(ValueError, match='changed'):
        controller.save(original.original_script, 'Stale editor title', ('voice-a',))
    assert controller.service.projects.load(original.id) == newer


def studio_scope_runner(frame):
    import carr_library
    from concurrent.futures import Future
    from carr_library.progress import ProgressHub, ProgressState, bind_progress, operation_outcome
    from carr_library.ui.progress_panel import NativeProgressPanel
    source = Path(carr_library.__file__).parent / 'ui/studio_dialog.py'
    parsed = ast.parse(source.read_text(encoding='utf-8'))
    window = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == 'show_studio')
    nodes = [next(node for node in window.body if isinstance(node, ast.FunctionDef) and node.name == name)
             for name in ('scope_current', 'stopping', 'run')]
    factory = ast.parse("""
def make(frame, executor, wx, progress, report, service):
    library = frame.library
    library_root = Path(library.root)
    alive, busy = True, False
    current_job = 'saved-job'
    dialog = SimpleNamespace(working=False)
    cancel = threading.Event()
    controls = lambda: None
    return run
""")
    factory.body[0].body[-1:-1] = nodes
    environment = dict(Path=Path, threading=threading, SimpleNamespace=SimpleNamespace,
                       ProgressState=ProgressState, bind_progress=bind_progress, operation_outcome=operation_outcome)
    exec(compile(ast.fix_missing_locations(factory), str(source), 'exec'), environment)
    future, executor, report = Future(), Mock(), Mock()
    executor.submit.return_value = future
    callbacks = []
    wx = SimpleNamespace(CallAfter=lambda callback: callbacks.append(callback))
    progress = object.__new__(NativeProgressPanel)
    progress.hub, progress.active_task, progress.feedback = ProgressHub(), None, Mock()
    progress.refresh = Mock()
    run = environment['make'](frame, executor, wx, progress, report, Mock())
    return SimpleNamespace(run=run, future=future, executor=executor, report=report, callbacks=callbacks)


@pytest.mark.parametrize('change', ['same-path-new-library', 'root-changed', 'frame-closing'])
def test_studio_queued_operation_checks_exact_library_before_execution(tmp_path, change):
    library = SimpleNamespace(root=tmp_path)
    frame = SimpleNamespace(library=library, _closing=False)
    handler = studio_scope_runner(frame)
    operation, done = Mock(), Mock()
    handler.run(operation, done)
    if change == 'same-path-new-library':
        frame.library = SimpleNamespace(root=tmp_path)
    elif change == 'root-changed':
        library.root = tmp_path / 'replacement'
    else:
        frame._closing = True
    with pytest.raises(InterruptedError, match='cancelled before'):
        handler.executor.submit.call_args.args[0]()
    operation.assert_not_called()
    handler.future.set_exception(InterruptedError('Explicit scope cancellation'))
    while handler.callbacks:
        handler.callbacks.pop(0)()
    done.assert_not_called()


def test_studio_late_result_cannot_update_replacement_library(tmp_path):
    frame = SimpleNamespace(library=SimpleNamespace(root=tmp_path), _closing=False)
    handler = studio_scope_runner(frame)
    done = Mock()
    handler.run(lambda: {'status': 'complete'}, done)
    result = handler.executor.submit.call_args.args[0]()
    frame.library = SimpleNamespace(root=tmp_path)
    handler.future.set_result(result)
    while handler.callbacks:
        handler.callbacks.pop(0)()
    done.assert_not_called()
    assert 'saved result remains in its original library' in handler.report.call_args.args[0]


def render_selection(dialog, tmp_path, *, accepted=True, newer=False):
    from dataclasses import replace
    project = Project('project-1', 'Saved clips', (),
                      (Scene('scene-1', 'Scene', (), minimum_duration=1),))
    current = replace(project, title='Changed elsewhere', revision=2) if newer else project
    parent = tmp_path / 'explicit-output-parent'
    chooser = Mock()
    chooser.ShowModal.return_value = 1 if accepted else 0
    chooser.GetPath.return_value = str(parent)
    dialog.wx.ID_OK = 1
    dialog.wx.DirDialog = Mock(return_value=chooser)
    service = SimpleNamespace(projects=SimpleNamespace(load=Mock(return_value=current)),
        submit=Mock(return_value='saved-job'),
        run=Mock(return_value={'status': 'complete', 'destination': str(parent / 'new-production')}))
    dialog.editor = SimpleNamespace(service=service)
    dialog.project = project
    return SimpleNamespace(project=project, parent=parent, chooser=chooser, service=service)


def test_render_saved_project_queues_exact_reviewed_revision_and_does_not_play(dialog, tmp_path):
    selected = render_selection(dialog, tmp_path)
    dialog.render()
    selected.chooser.Destroy.assert_called_once()
    selected.service.projects.load.assert_not_called()
    selected.service.submit.assert_not_called()
    selected.service.run.assert_not_called()
    assert len(dialog.calls) == 1 and dialog.pending
    dialog.project = None  # The queued action keeps the project the user reviewed.
    dialog.finish()
    selected.service.projects.load.assert_called_once_with(selected.project.id)
    selected.service.submit.assert_called_once_with(selected.project, selected.parent)
    assert selected.service.run.call_args.args == ('saved-job',)
    cancelled = selected.service.run.call_args.kwargs['cancelled']
    assert not cancelled()
    dialog.cancelled.set()
    assert cancelled()
    assert str(selected.parent / 'new-production') in dialog.status.ChangeValue.call_args.args[0]
    dialog.status.SetFocus.assert_called()
    assert dialog.player is None and not dialog.previews and not dialog.pending


def test_render_saved_project_rejects_stale_revision_before_job_creation(dialog, tmp_path):
    selected = render_selection(dialog, tmp_path, newer=True)
    dialog.render()
    dialog.finish()
    selected.service.submit.assert_not_called()
    selected.service.run.assert_not_called()
    assert 'Reload before rendering' in dialog.status.ChangeValue.call_args.args[0]
    assert dialog.player is None and not dialog.pending


def test_render_saved_project_cancelled_parent_picker_starts_no_work(dialog, tmp_path):
    selected = render_selection(dialog, tmp_path, accepted=False)
    dialog.render()
    selected.chooser.GetPath.assert_not_called()
    selected.chooser.Destroy.assert_called_once()
    selected.service.projects.load.assert_not_called()
    selected.service.submit.assert_not_called()
    selected.service.run.assert_not_called()
    assert not dialog.calls and not dialog.pending and dialog.player is None


@pytest.mark.parametrize('ending', ['completed', 'cancelled-before-start'])
def test_owned_service_release_waits_for_its_future_without_stopping_host_executor(dialog, ending):
    from concurrent.futures import Future
    future = Future()
    dialog._owns_editor = True
    dialog._service_release_requested = False
    dialog._worker_future = future
    dialog.editor = SimpleNamespace(service=SimpleNamespace(close=Mock()))
    dialog.frame.executor = Mock()
    dialog._release_service()
    dialog.editor.service.close.assert_not_called()
    dialog.frame.executor.shutdown.assert_not_called()
    if ending == 'completed':
        future.set_result({'status': 'complete'})
    else:
        assert future.cancel()
    dialog.editor.service.close.assert_called_once_with()
    dialog._release_service()
    dialog.editor.service.close.assert_called_once_with()
    dialog.frame.executor.shutdown.assert_not_called()


def test_borrowed_clip_editor_service_is_not_closed_by_dialog(dialog):
    dialog._owns_editor = False
    dialog.editor = SimpleNamespace(service=SimpleNamespace(close=Mock()))
    dialog._release_service()
    dialog.editor.service.close.assert_not_called()


def effect_selection(dialog):
    from dataclasses import replace
    from carr_library.audio import Effect
    from carr_library.studio.project import Line, Role
    cue = Cue('clip-1', 'inputs/' + '0' * 64 + '.audio')
    scene = Scene('scene-1', 'Scene', (Line('line-1', 'role-1', 'Literal source words.'),),
                  cues=(cue,))
    project = Project('project-1', 'Effects', (Role('role-1', 'Narrator'),), (scene,), schema=2)
    chain = (Effect('gain', gain_db=-3), Effect('highpass', frequency=80, enabled=False))
    dialog.project = project
    dialog._scene, dialog._clip = lambda: scene, lambda: cue
    dialog.editor = SimpleNamespace(
        effect_capabilities=Mock(return_value=('gain', 'highpass', 'tempo')),
        set_effects=Mock(return_value=replace(project, schema=3, revision=2, effects=chain)))
    dialog.wx.ID_OK = 1
    picker = Mock()
    picker.ShowModal.return_value, picker.GetSelection.return_value = 1, 0
    dialog.wx.SingleChoiceDialog = Mock(return_value=picker)
    return SimpleNamespace(project=project, scene=scene, cue=cue, chain=chain, picker=picker)


@pytest.mark.parametrize('scope', ['clip', 'role', 'scene', 'master'])
def test_effect_editor_queues_inspection_then_exact_reviewed_save_without_playback(dialog, monkeypatch, scope):
    import sys
    selected = effect_selection(dialog)
    editor = Mock(return_value=selected.chain)
    monkeypatch.setitem(sys.modules, 'carr_library.ui.effects_dialog',
                        SimpleNamespace(show_effect_chain=editor))
    assert dialog._effects(scope)
    dialog.editor.effect_capabilities.assert_not_called()
    dialog.editor.set_effects.assert_not_called()
    editor.assert_not_called()
    assert len(dialog.calls) == 1
    dialog.finish()
    dialog.editor.effect_capabilities.assert_called_once()
    assert dialog.editor.effect_capabilities.call_args.args == (selected.project,)
    editor.assert_called_once()
    assert editor.call_args.args == (dialog, dialog.library_root, ())
    assert editor.call_args.kwargs['available'] == ('gain', 'highpass')
    assert editor.call_args.kwargs['sample_rate'] == selected.project.sample_rate
    editor.call_args.kwargs['guard']()
    dialog.editor.set_effects.assert_not_called()
    assert len(dialog.calls) == 1 and dialog.pending
    dialog.finish()
    call = dialog.editor.set_effects.call_args
    assert call.args == (selected.project, scope, selected.chain)
    expected = {'clip': {'scene_id': selected.scene.id, 'clip_id': selected.cue.id},
                'role': {'role_id': selected.project.roles[0].id},
                'scene': {'scene_id': selected.scene.id}, 'master': {}}[scope]
    assert {key: value for key, value in call.kwargs.items() if key != 'cancelled'} == expected
    assert not call.kwargs['cancelled']()
    assert dialog._accept.call_count == 1
    assert dialog.player is None and not dialog.previews
    if scope == 'role':
        selected.picker.Destroy.assert_called_once()


def test_cancelled_effect_review_changes_no_project_and_queues_no_save(dialog, monkeypatch):
    import sys
    effect_selection(dialog)
    editor = Mock(return_value=None)
    monkeypatch.setitem(sys.modules, 'carr_library.ui.effects_dialog',
                        SimpleNamespace(show_effect_chain=editor))
    dialog.master_effects()
    dialog.finish()
    assert editor.call_count == 1
    dialog.editor.set_effects.assert_not_called()
    dialog._accept.assert_not_called()
    assert not dialog.calls and not dialog.pending and dialog.player is None


@pytest.mark.parametrize('boundary', ['inspection-queued', 'modal-open', 'save-queued'])
def test_effect_work_rejects_recreated_same_path_library_at_each_boundary(dialog, tmp_path, monkeypatch, boundary):
    import sys
    selected = effect_selection(dialog)
    old_library = dialog.library
    old_path = dialog.library_root
    def replace_root():
        old_path.rename(tmp_path / 'former-library')
        old_path.mkdir()
        assert dialog.frame.library is old_library and dialog.library.root == old_path
    def review(*args, **kwargs):
        if boundary == 'modal-open':
            replace_root()
        return selected.chain
    shown = Mock(side_effect=review)
    monkeypatch.setitem(sys.modules, 'carr_library.ui.effects_dialog',
                        SimpleNamespace(show_effect_chain=shown))
    dialog.master_effects()
    if boundary == 'inspection-queued':
        replace_root()
    dialog.finish()
    if boundary == 'save-queued':
        assert len(dialog.calls) == 1
        replace_root()
        dialog.finish()
    dialog.editor.set_effects.assert_not_called()
    dialog._accept.assert_not_called()
    assert not dialog.calls and not dialog.pending and dialog.player is None
    if boundary == 'inspection-queued':
        dialog.editor.effect_capabilities.assert_not_called()
        shown.assert_not_called()


def test_project_changed_after_effect_review_is_refused_before_save(dialog, monkeypatch):
    import sys
    from dataclasses import replace
    selected = effect_selection(dialog)
    monkeypatch.setitem(sys.modules, 'carr_library.ui.effects_dialog',
                        SimpleNamespace(show_effect_chain=Mock(return_value=selected.chain)))
    dialog.master_effects()
    dialog.finish()
    assert len(dialog.calls) == 1
    dialog.project = replace(selected.project, revision=2, title='Another saved revision')
    dialog.finish()
    dialog.editor.set_effects.assert_not_called()
    dialog._accept.assert_not_called()
    assert 'changed' in dialog.status.ChangeValue.call_args.args[0]
    assert not dialog.pending and dialog.player is None


def test_role_picker_cancel_does_not_inspect_runtime_or_save(dialog):
    selected = effect_selection(dialog)
    selected.picker.ShowModal.return_value = 0
    assert dialog.role_effects() is False
    selected.picker.Destroy.assert_called_once()
    dialog.editor.effect_capabilities.assert_not_called()
    dialog.editor.set_effects.assert_not_called()
    assert not dialog.calls and dialog.player is None
