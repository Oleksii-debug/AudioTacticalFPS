"""Queued native controller behavior, with real native controls tested separately."""
import ast
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
    wx = SimpleNamespace(Dialog=object, ID_CANCEL=2, NOT_FOUND=-1)
    env = dict(wx=wx, Path=Path, __name__='carr_library.ui._clips_probe', __package__='carr_library.ui')
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), env)
    value = object.__new__(env['ClipDialog'])
    value.closed = value.closing = value.pending = False
    value.library_root = tmp_path
    value._destroy_when_closed = False
    value._native_destroyed, value._window_id = False, 451
    value._destroy_cleanup_future = None
    value._editor_closed = False
    value._editor_close_lock = threading.RLock()
    value._unregister_cleanup = Mock()
    value.editor = SimpleNamespace(close=Mock())
    value.player = None
    value.playback_generation = 0
    value.previews = []
    value.recovery_notices = {}
    value.status = Mock()
    value._enable = Mock()
    value.Hide = Mock()
    value.Destroy = Mock()
    value.GetId = Mock(return_value=451)
    value.IsModal = Mock(return_value=False)
    value._accept = Mock()
    value.frame = SimpleNamespace(working=False, cancel_event=threading.Event(),
                                  library=SimpleNamespace(root=tmp_path))
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
    path.write_bytes(b'Explicit synthetic output')
    dialog._run('Preview', lambda: path, Mock())
    operation, done, failed = dialog.calls.pop(0)
    result = operation()  # The real worker completed before its delayed UI callback.
    dialog.close()
    dialog.frame.working = False
    done(result)
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
    released = []
    # Ordinary Close stops sound immediately; the retained editor finalizer
    # independently confirms closure before releasing its owned preview.
    dialog.player.close.side_effect = lambda: released.append(('player', path.exists()))
    dialog.editor.close.side_effect = lambda: released.append(('editor', path.exists()))
    generation = dialog.playback_generation
    dialog.close()
    assert dialog.playback_generation > generation
    assert released == [('player', True), ('player', True), ('editor', True)]
    dialog.editor.close.assert_called_once_with()
    assert not path.exists()
    dialog.close()
    assert released == [('player', True), ('player', True), ('editor', True)]


def test_copy_binds_the_original_selection_before_background_work(dialog):
    cue = Cue('copy-clip', '/retained.audio')
    scene = Scene('copy-scene', 'Scene', (), cues=(cue,))
    project = Project('copy-project', 'Project', (), (scene,))
    dialog.project = project
    dialog._scene, dialog._clip = lambda: scene, lambda: cue
    copied = Mock(return_value=cue)
    dialog.editor = SimpleNamespace(copy=copied, close=Mock())
    dialog.copy_clip()
    assert not copied.called and len(dialog.calls) == 1
    dialog.project = None
    dialog._scene = lambda: (_ for _ in ()).throw(AssertionError('Worker read a live scene selector'))
    dialog.finish()
    assert copied.call_args.args == (project, scene.id, cue.id)
    assert 'copied' in dialog.status.ChangeValue.call_args.args[0]


def test_closing_waits_for_the_owned_operation_before_disposing_editor(dialog):
    dialog._run('Operation', lambda: None, Mock())
    dialog.close()
    dialog.editor.close.assert_not_called()
    dialog.finish()
    dialog.editor.close.assert_called_once()
    dialog.close()
    dialog.editor.close.assert_called_once()


def test_device_close_failure_keeps_editor_and_preview_available(dialog, tmp_path):
    preview = tmp_path / 'clip-preview-held.wav'
    preview.write_bytes(b'fixture')
    dialog.previews = [preview]
    dialog.player = Mock()
    dialog.player.close.side_effect = OSError('device still owns audio')
    event = SimpleNamespace(CanVeto=lambda: True, Veto=Mock())
    dialog.close(event)
    assert not dialog.closed and preview.exists()
    dialog.editor.close.assert_not_called()
    event.Veto.assert_called_once()
    dialog.player.close.side_effect = None
    dialog.close()
    assert dialog.closed and not preview.exists()


def test_changed_library_refuses_new_clip_work_and_cancels_queued_work(dialog, tmp_path):
    operation = Mock()
    dialog.frame.library = SimpleNamespace(root=tmp_path / 'another')
    assert dialog._run('Edit', operation, Mock()) is False
    assert not dialog.calls
    dialog.frame.library = SimpleNamespace(root=dialog.library_root)
    assert dialog._run('Edit', operation, Mock())
    dialog.frame.library = SimpleNamespace(root=tmp_path / 'another')
    dialog.finish()
    operation.assert_not_called()
    assert 'cancelled' in dialog.status.ChangeValue.call_args.args[0]


def test_late_committed_result_never_rebinds_the_new_library_controls(dialog, tmp_path):
    done = Mock()
    dialog._run('Edit', lambda: 'already committed in original root', done)
    operation, finish, failed = dialog.calls.pop(0)
    result = operation()
    dialog.frame.library = SimpleNamespace(root=tmp_path / 'new library')
    dialog.frame.working = False
    finish(result)
    done.assert_not_called()
    assert 'original library' in dialog.status.ChangeValue.call_args.args[0]


def test_failed_editor_cleanup_keeps_the_dialog_and_preview_for_retry(dialog, tmp_path):
    preview = tmp_path / 'clip-preview-cleanup.wav'
    preview.write_bytes(b'fixture')
    dialog.previews = [preview]
    dialog.editor.close.side_effect = OSError('resource still owns an operation')
    event = SimpleNamespace(CanVeto=lambda: True, Veto=Mock())
    dialog.close(event)
    assert not dialog.closed and preview.exists()
    dialog.Hide.assert_not_called()
    event.Veto.assert_called_once()
    dialog.editor.close.side_effect = None
    dialog.close()
    assert dialog.closed and not preview.exists()


def test_late_preview_cleanup_error_does_not_trap_a_closing_dialog(dialog, tmp_path, monkeypatch):
    preview = tmp_path / 'clip-preview-delayed.wav'
    preview.write_bytes(b'fixture')
    dialog._run('Preview', lambda: preview, Mock())
    operation, finish, failed = dialog.calls.pop(0)
    result = operation()
    dialog.close()
    unlink = Path.unlink
    def held(path, *args, **kwargs):
        if path == preview:
            raise PermissionError('another process temporarily holds the cache')
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', held)
    dialog.frame.working = False
    finish(result)
    assert dialog.closed and not dialog.pending and preview.exists()
    dialog.Hide.assert_called_once()

def test_forced_host_close_cleans_editor_in_worker_when_ui_callback_is_discarded(dialog):
    started, finish_work, cleaned = threading.Event(), threading.Event(), threading.Event()
    threads = []
    def operation():
        started.set()
        assert finish_work.wait(5)
        return 'durable result'
    def cleanup():
        threads.append(threading.get_ident())
        assert finish_work.is_set()
        cleaned.set()
    dialog.editor.close = Mock(side_effect=cleanup)
    assert dialog._run('Owned work', operation, Mock())
    work, done, failed = dialog.calls.pop(0)
    errors = []
    def run():
        try:
            work()
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(5)
    dialog.frame._closing = True
    finish_work.set()
    worker.join(5)
    assert not worker.is_alive() and not errors and cleaned.is_set()
    assert threads == [worker.ident] and worker.ident != threading.get_ident()
    dialog.editor.close.assert_called_once()
    # Actual DesktopFrame discards done/failed in this state.
    dialog._close_editor()
    dialog.editor.close.assert_called_once()


def test_effect_review_and_save_bind_original_project_before_both_queued_steps(dialog, monkeypatch):
    from carr_library.audio import Effect
    from carr_library.ui import effects_dialog
    cue = Cue('clip', '/retained.audio')
    scene = Scene('scene', 'Scene', (), cues=(cue,))
    project = Project('project', 'Project', (), (scene,))
    dialog.project = project
    dialog._scene, dialog._clip = lambda: scene, lambda: cue
    returned = (Effect('gain', gain_db=-6),)
    review = Mock(return_value=returned)
    monkeypatch.setattr(effects_dialog, 'show_effect_chain', review)
    saved = Mock(return_value=project)
    runtime = SimpleNamespace(capabilities=lambda: {'effects': ['gain', 'tempo']})
    dialog.editor = SimpleNamespace(_current=Mock(return_value=project),
        service=SimpleNamespace(_runtime=lambda: runtime), set_effects=saved, close=Mock())
    dialog.clip_effects()
    assert not review.called and len(dialog.calls) == 1
    dialog.project = None
    dialog._scene = lambda: (_ for _ in ()).throw(AssertionError('Worker read a changed scene selector'))
    dialog.finish()
    assert review.call_args.args == (dialog, dialog.library_root, ())
    assert review.call_args.kwargs == {'sample_rate': 24000, 'available': ('gain',)}
    assert not saved.called and len(dialog.calls) == 1
    dialog.finish()
    assert saved.call_args.args == (project, 'clip', returned)
    assert saved.call_args.kwargs['scene_id'] == scene.id
    assert saved.call_args.kwargs['clip_id'] == cue.id
    dialog._accept.assert_called_once_with(project)


def test_closed_effect_review_never_opens_a_late_modal(dialog, monkeypatch):
    from carr_library.ui import effects_dialog
    scene = Scene('scene', 'Scene', (), minimum_duration=1)
    project = Project('project', 'Project', (), (scene,))
    dialog.project, dialog._scene = project, lambda: scene
    review = Mock()
    monkeypatch.setattr(effects_dialog, 'show_effect_chain', review)
    dialog.editor = SimpleNamespace(_current=Mock(),
        service=SimpleNamespace(_runtime=Mock()), close=Mock())
    dialog.master_effects()
    dialog.close()
    dialog.finish()
    review.assert_not_called()
    dialog.editor.service._runtime.assert_not_called()
    assert dialog.closed


@pytest.mark.parametrize('active', [False, True])
def test_registered_clip_owner_survives_forced_executor_close_without_ui_callback(dialog, active):
    from carr_library.ui.worker_lifetime import DesktopExecutor
    executor = DesktopExecutor()
    entered, release = threading.Event(), threading.Event()
    order, ordinary = [], []
    owner_thread = threading.get_ident()

    def operation():
        entered.set()
        assert release.wait(5)
        order.append('operation-complete')

    def cleanup():
        assert order == ['operation-complete']
        assert threading.get_ident() != owner_thread
        order.append('editor-closed')

    dialog.editor.close = Mock(side_effect=cleanup)
    dialog._unregister_cleanup = executor.register_cleanup(dialog._close_editor)
    if active:
        def run_job(title, work, done, failed):
            dialog.frame.working = True
            return executor.submit(work)
        dialog.frame.run_job = run_job
        dialog._run('Active clip operation', operation, Mock())
    else:
        executor.submit(operation)
    try:
        assert entered.wait(5)
        queued = executor.submit(lambda: ordinary.append('unwanted save'))
        dialog.frame._closing = True
        dialog.frame.cancel_event.set()
        executor.shutdown(wait=False, cancel_futures=True)
        assert queued.cancelled() and order == []
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)
        assert order == ['operation-complete', 'editor-closed']
        assert dialog._editor_closed and not ordinary
        dialog.editor.close.assert_called_once()
        # A callback captured before unregister is harmless.
        dialog._close_editor()
        dialog.editor.close.assert_called_once()
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


def test_failed_clip_release_keeps_registered_retry_authority(dialog):
    dialog.editor.close.side_effect = OSError('native owner remains active')
    with pytest.raises(OSError):
        dialog._close_editor()
    assert not dialog._editor_closed
    dialog._unregister_cleanup.assert_not_called()
    dialog.editor.close.side_effect = None
    dialog._close_editor()
    dialog._close_editor()
    assert dialog._editor_closed
    dialog._unregister_cleanup.assert_called_once()
    assert dialog.editor.close.call_count == 2


def _invalidate_native_wrapper(dialog):
    def invalid(*_args, **_kwargs):
        raise AssertionError('The deleted native wrapper was accessed.')
    for name in ('GetId', 'Hide', 'IsModal', 'Destroy', 'EndModal', '_enable'):
        setattr(dialog, name, Mock(side_effect=invalid))
    dialog.status.ChangeValue.reset_mock()
    dialog.status.SetFocus.reset_mock()
    dialog.status.ChangeValue.side_effect = invalid
    dialog.status.SetFocus.side_effect = invalid


@pytest.mark.parametrize('unrelated_cancelled', [False, True])
def test_child_destruction_does_not_revoke_clip_owner_or_other_operation(dialog, unrelated_cancelled):
    dialog.frame.executor = SimpleNamespace(submit=Mock())
    if unrelated_cancelled:
        dialog.frame.cancel_event.set()
    event = SimpleNamespace(GetId=lambda: dialog._window_id + 1, Skip=Mock())
    dialog._owner_destroyed(event)
    event.Skip.assert_called_once()
    assert not dialog.closed and not dialog.closing and not dialog._native_destroyed
    assert dialog.frame.cancel_event.is_set() is unrelated_cancelled
    dialog.frame.executor.submit.assert_not_called()
    dialog.editor.close.assert_not_called()


def test_destroyed_owner_discards_queued_result_after_completed_preview_without_widget_access(dialog, tmp_path):
    from carr_library.ui.worker_lifetime import DesktopExecutor
    executor = DesktopExecutor()
    entered, release = threading.Event(), threading.Event()
    path = tmp_path / 'clip-preview-complete-before-destroy.wav'
    path.write_bytes(b'Explicit private preview fixture')
    accepted = Mock()
    dialog._run('Preview', lambda: path, accepted)
    work, done, _failed = dialog.calls.pop(0)
    result = work()
    assert path in dialog.previews  # Claimed before the GUI callback can run.
    dialog.frame.executor = executor
    dialog._unregister_cleanup = executor.register_cleanup(dialog._close_editor)
    threads = []
    def blocker():
        entered.set()
        assert release.wait(5)
    def closed():
        assert release.is_set()
        threads.append(threading.get_ident())
    dialog.editor.close.side_effect = closed
    blocked = executor.submit(blocker)
    try:
        assert entered.wait(5)
        _invalidate_native_wrapper(dialog)
        event = SimpleNamespace(GetId=lambda: dialog._window_id, Skip=Mock())
        dialog._owner_destroyed(event)
        first_cleanup = dialog._destroy_cleanup_future
        dialog._owner_destroyed(event)
        assert dialog._destroy_cleanup_future is first_cleanup
        assert dialog.closed and dialog._native_destroyed and dialog.frame.cancel_event.is_set()
        dialog.editor.close.assert_not_called()
        done(result)
        accepted.assert_not_called()
        dialog.close()
        dialog._destroy_closed_window()
        release.set()
        blocked.result(5)
        first_cleanup.result(5)
        executor.shutdown(wait=True, cancel_futures=True)
        assert not path.exists() and dialog._editor_closed
        assert len(dialog.previews) == 1
        dialog.editor.close.assert_called_once()
        assert threads and all(value != threading.get_ident() for value in threads)
        dialog.Destroy.assert_not_called()
        dialog.status.ChangeValue.assert_not_called()
        event.Skip.assert_called()
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


def test_destroyed_owner_retains_cleanup_when_shutdown_cancels_its_queued_release(dialog, tmp_path):
    from carr_library.ui.worker_lifetime import DesktopExecutor
    executor = DesktopExecutor()
    entered, release = threading.Event(), threading.Event()
    path = tmp_path / 'clip-preview-returned-after-destroy.wav'
    dialog.frame.executor = executor
    dialog._unregister_cleanup = executor.register_cleanup(dialog._close_editor)
    workers = []
    def operation():
        entered.set()
        assert release.wait(5)
        path.write_bytes(b'Completed synthetic preview')
        return path
    def run_job(_title, work, _done, _failed):
        dialog.frame.working = True
        future = executor.submit(work)
        workers.append(future)
        return future
    dialog.frame.run_job = run_job
    try:
        dialog._run('Active preview', operation, Mock())
        assert entered.wait(5)
        _invalidate_native_wrapper(dialog)
        dialog._owner_destroyed(SimpleNamespace(GetId=lambda: dialog._window_id, Skip=Mock()))
        pending_cleanup = dialog._destroy_cleanup_future
        dialog.frame._closing = True
        executor.shutdown(wait=False, cancel_futures=True)
        assert pending_cleanup.cancelled()
        dialog.editor.close.assert_not_called()
        release.set()
        assert workers[0].result(5) == path
        executor.shutdown(wait=True, cancel_futures=True)
        assert dialog._editor_closed and not path.exists()
        dialog.editor.close.assert_called_once()
        dialog.Destroy.assert_not_called()
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


def test_destruction_after_shutdown_keeps_captured_registered_finalizer(dialog):
    from carr_library.ui.worker_lifetime import DesktopExecutor
    executor = DesktopExecutor()
    entered, release = threading.Event(), threading.Event()
    dialog.frame.executor = executor
    dialog._unregister_cleanup = executor.register_cleanup(dialog._close_editor)
    def active():
        entered.set()
        assert release.wait(5)
    running = executor.submit(active)
    try:
        assert entered.wait(5)
        executor.shutdown(wait=False, cancel_futures=True)
        _invalidate_native_wrapper(dialog)
        dialog._owner_destroyed(SimpleNamespace(GetId=lambda: dialog._window_id, Skip=Mock()))
        assert dialog._native_destroyed and dialog._destroy_cleanup_future is None
        # This clip was idle. Its destruction cannot cancel an unrelated task.
        assert not dialog.frame.cancel_event.is_set()
        dialog.editor.close.assert_not_called()
        release.set()
        running.result(5)
        executor.shutdown(wait=True, cancel_futures=True)
        dialog.editor.close.assert_called_once()
        dialog.Destroy.assert_not_called()
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)


def test_failed_destroy_release_keeps_the_exact_owner_registered_for_shutdown_retry(dialog):
    from carr_library.ui.worker_lifetime import DesktopExecutor
    executor = DesktopExecutor()
    dialog.frame.executor = executor
    unregister = executor.register_cleanup(dialog._close_editor)
    dialog._unregister_cleanup = Mock(side_effect=unregister)
    dialog.editor.close.side_effect = OSError('The owned service is not yet quiescent.')
    try:
        _invalidate_native_wrapper(dialog)
        dialog._owner_destroyed(SimpleNamespace(GetId=lambda: dialog._window_id, Skip=Mock()))
        with pytest.raises(OSError, match='not yet quiescent'):
            dialog._destroy_cleanup_future.result(5)
        assert not dialog._editor_closed
        dialog._unregister_cleanup.assert_not_called()
        dialog.editor.close.side_effect = None
        executor.shutdown(wait=True, cancel_futures=True)
        assert dialog._editor_closed and dialog.editor.close.call_count == 2
        dialog._unregister_cleanup.assert_called_once()
        dialog.Destroy.assert_not_called()
    finally:
        dialog.editor.close.side_effect = None
        executor.shutdown(wait=True, cancel_futures=True)


def test_modal_wrapper_does_not_close_or_destroy_an_already_deleted_window(dialog):
    import carr_library
    from carr_library.ui.worker_lifetime import DesktopExecutor
    executor = DesktopExecutor()
    dialog.frame.executor = executor
    dialog._unregister_cleanup = executor.register_cleanup(dialog._close_editor)
    source = Path(carr_library.__file__).parent / 'ui/studio_clips.py'
    module = ast.parse(source.read_text(encoding='utf-8'))
    wrapper = next(node for node in module.body
                   if isinstance(node, ast.FunctionDef) and node.name == 'show_clip_editor')
    queued = []
    def destroyed_modal():
        _invalidate_native_wrapper(dialog)
        dialog._owner_destroyed(SimpleNamespace(GetId=lambda: dialog._window_id, Skip=Mock()))
    dialog.ShowModal = Mock(side_effect=destroyed_modal)
    dialog.close = Mock(side_effect=AssertionError('The wrapper closed an already deleted window.'))
    namespace = dict(ClipDialog=lambda _frame: dialog,
                     wx=SimpleNamespace(CallAfter=queued.append))
    exec(compile(ast.Module(body=[wrapper], type_ignores=[]), str(source), 'exec'), namespace)
    try:
        namespace['show_clip_editor'](dialog.frame)
        dialog._destroy_cleanup_future.result(5)
        dialog.ShowModal.assert_called_once()
        dialog.close.assert_not_called()
        dialog.Destroy.assert_not_called()
        assert len(queued) == 1  # The existing initial reload was not replaced.
        assert dialog._native_destroyed and dialog._editor_closed
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def _notice_controls(dialog):
    class Choice:
        def __init__(self):
            self.items, self.selection = [], -1

        def Set(self, values):
            self.items = list(values)

        def SetSelection(self, value):
            self.selection = value

        def GetSelection(self):
            return self.selection

    dialog.projects, dialog.project = (), None
    dialog.project_choice, dialog.scene_choice = Choice(), Choice()
    dialog.clip_choice, dialog.role = Choice(), Choice()
    dialog.master_rate, dialog.minimum, dialog.clip_title = Mock(), Mock(), Mock()
    dialog.fields = {}


def _notice_worker_result(dialog):
    from concurrent.futures import ThreadPoolExecutor
    work, done, failed = dialog.calls.pop(0)
    # The production dispatcher owns its one executor. This test proves the
    # captured work uses that boundary rather than doing metadata I/O in done.
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(work).result(timeout=5)
    dialog.frame.working = False
    return result, done


def test_restored_input_notices_are_worker_read_and_fingerprint_bound_without_gui_io(
        dialog, tmp_path, monkeypatch):
    from dataclasses import replace
    from carr_library.durable_state import atomic_json
    from carr_library.studio import backup_inputs
    from carr_library.studio.project import ProjectStore

    studio_root = tmp_path / 'state' / 'studio'
    store = ProjectStore(studio_root / 'projects')
    cue = Cue('unavailable-clip', str(tmp_path / 'never-selected-owner-original.wav'))
    scene = Scene('restored-scene', 'Restored scene', (), cues=(cue,), minimum_duration=1)
    project = Project('restored-project', 'Restored project', (), (scene,))
    store.save(project)
    atomic_json(studio_root / 'input-recovery.json', {
        'schema': backup_inputs.RECOVERY_SCHEMA, 'notice': backup_inputs.NOTICE,
        'projects': [{
            'id': project.id, 'source_revision': project.revision,
            'source_fingerprint': project.fingerprint, 'revision': project.revision,
            'fingerprint': project.fingerprint, 'retained_inputs': [],
            'unresolved': [{'scene': scene.id, 'cue': cue.id, 'path': cue.path}],
        }],
    })
    _notice_controls(dialog)
    # There is deliberately no service.root: the captured library owns state.
    dialog.editor.service = SimpleNamespace(projects=store)
    gui_thread = threading.get_ident()
    calls = []
    actual_notice = backup_inputs.recovery_notice

    def worker_notice(root, current):
        assert threading.get_ident() != gui_thread
        assert root == studio_root
        calls.append((root, current.fingerprint))
        return actual_notice(root, current)

    monkeypatch.setattr(backup_inputs, 'recovery_notice', worker_notice)
    assert dialog.reload()
    loaded, finish = _notice_worker_result(dialog)
    assert loaded[0] == (project,)
    assert '1 unavailable clip reference(s)' in loaded[1][project.fingerprint]
    forbidden = Mock(side_effect=AssertionError('The GUI tried to read recovery metadata.'))
    monkeypatch.setattr(backup_inputs, 'recovery_notice', forbidden)
    finish(loaded)
    assert dialog.project == project
    assert '1 unavailable clip reference(s)' in dialog.status.ChangeValue.call_args.args[0]
    dialog.select_project()
    forbidden.assert_not_called()
    assert not Path(cue.path).exists()

    # The next committed revision removes the unavailable cue. A notice from
    # the same project ID's old fingerprint must never describe the new one.
    updated = replace(project, title='Reviewed project', revision=project.revision + 1,
                      scenes=(replace(scene, cues=()),))
    monkeypatch.setattr(backup_inputs, 'recovery_notice', worker_notice)
    assert dialog._run('Saving reviewed project',
        lambda: store.save(updated, expected_revision=project.revision,
                           expected_fingerprint=project.fingerprint),
        lambda current: type(dialog)._accept(dialog, current))
    saved, finish = _notice_worker_result(dialog)
    assert saved == updated
    assert '0 unavailable clip reference(s)' in dialog.recovery_notices[updated.fingerprint]
    assert '1 unavailable clip reference(s)' in dialog.recovery_notices[project.fingerprint]
    monkeypatch.setattr(backup_inputs, 'recovery_notice', forbidden)
    finish(saved)
    shown = dialog.status.ChangeValue.call_args.args[0]
    assert 'Saved Reviewed project' in shown and 'Original files were not modified.' in shown
    assert '0 unavailable clip reference(s)' in shown and '1 unavailable clip reference(s)' not in shown
    forbidden.assert_not_called()
    assert calls == [(studio_root, project.fingerprint), (studio_root, updated.fingerprint)]
    assert store.load(project.id) == updated


def test_unreadable_recovery_notice_does_not_relabel_a_committed_project(dialog, tmp_path):
    from carr_library.studio.project import ProjectStore
    studio_root = tmp_path / 'state' / 'studio'
    store = ProjectStore(studio_root / 'projects')
    (studio_root / 'input-recovery.json').write_text('{invalid metadata', encoding='utf-8')
    project = Project('committed-project', 'Committed project', (),
                      (Scene('scene', 'Scene', (), minimum_duration=1),))
    accepted = Mock()
    assert dialog._run('Saving project', lambda: store.save(project), accepted)
    saved, finish = _notice_worker_result(dialog)
    assert saved == project and store.load(project.id) == project
    finish(saved)
    accepted.assert_called_once_with(project)
    assert not dialog.pending and not dialog.closed
    assert 'Input recovery review is unavailable' in dialog.recovery_notices[project.fingerprint]
