"""Execute native callback logic with boundary doubles, not Windows acceptance."""
import ast
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock


def test_close_signals_worker_without_competing_write_and_ignores_late_callbacks():
    source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/assistant_dialog.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    show = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == 'show_assistant')
    helpers = {node.name: node for node in show.body if isinstance(node, ast.FunctionDef)}
    finish = next(node for node in helpers['run'].body if isinstance(node, ast.FunctionDef)
                  and node.name == 'finish')
    factory = ast.parse('''
def callbacks(cancel, executor, dialog, wx, service, review):
    alive, busy, job_id = True, True, 'active-backup'
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
    close, report, finish = namespace['callbacks'](cancel, executor, dialog, wx, service, review)

    # A completion callback may already be queued before the user closes.
    completed = Mock()
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
    completed.result.assert_not_called()
    assert not review.mock_calls
