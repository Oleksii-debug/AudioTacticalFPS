"""Execute native callback logic with boundary doubles, not Windows acceptance."""
import ast
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock


def test_close_signals_worker_without_competing_write_and_ignores_late_callbacks():
    from carr_library.ui import assistant_dialog
    source = Path(assistant_dialog.__file__).resolve()
    tree = ast.parse(source.read_text(encoding='utf-8'))
    show = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == 'show_assistant')
    helpers = {node.name: node for node in show.body if isinstance(node, ast.FunctionDef)}
    finish = next(node for node in helpers['run'].body if isinstance(node, ast.FunctionDef)
                  and node.name == 'finish')
    factory = ast.parse('''
def callbacks(cancel, executor, dialog, wx, service, review, progress, frame):
    alive, busy, job_id = True, True, 'active-backup'
    token = progress_token = 'synthetic-progress-token'
    bound_library = frame.library
    return close, report, finish
''').body[0]
    factory.body[1:1] = [helpers['close'], helpers['report'], finish]
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[])),
                 str(source), 'exec'), namespace)
    queued = []
    cancel, executor, dialog, review = Event(), Mock(), Mock(), Mock()
    service = Mock()
    service.pause.side_effect = RuntimeError('Assistant snapshot holds the state write guard.')
    wx = SimpleNamespace(ID_CLOSE=42, CallAfter=queued.append)
    from carr_library.progress import ProgressHub, finish_result
    reporter = ProgressHub().start('Synthetic assistant backup')
    progress = SimpleNamespace(request_cancel=Mock(), fail=Mock(), cancel=Mock(),
        finish_result=lambda result, **kwargs: finish_result(reporter, result,
            cancelled=kwargs.get('cancelled', False)))
    frame = SimpleNamespace(library=object())
    close, report, finish = namespace['callbacks'](
        cancel, executor, dialog, wx, service, review, progress, frame)

    # A completion callback may already be queued before the user closes.
    completed = Mock()
    completed.result.return_value = {'status': 'paused'}
    finish(completed)
    close()
    close()
    assert cancel.is_set()
    service.pause.assert_not_called()
    executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
    dialog.EndModal.assert_called_once_with(wx.ID_CLOSE)
    report('Late worker progress')
    finish(completed)
    for apply in queued:
        apply()
    assert reporter.event.state == 'paused' and reporter.event.percent is None
    progress.request_cancel.assert_called_once()
    assert not review.mock_calls
