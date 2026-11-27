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
    wx = SimpleNamespace(Dialog=object, ID_CANCEL=2)
    env = dict(wx=wx, Path=Path, __name__='carr_library.ui._clips_probe', __package__='carr_library.ui')
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), env)
    value = object.__new__(env['ClipDialog'])
    value.closed = value.closing = value.pending = False
    value.library_root = tmp_path
    value._destroy_when_closed = False
    value.editor = SimpleNamespace(close=Mock())
    value.player = None
    value.playback_generation = 0
    value.previews = []
    value.status = Mock()
    value._enable = Mock()
    value.Hide = Mock()
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
    generation = dialog.playback_generation
    dialog.close()
    assert dialog.playback_generation > generation
    dialog.player.close.assert_called_once()
    assert not path.exists()


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
