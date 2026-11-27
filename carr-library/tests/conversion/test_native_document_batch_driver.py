"""Probe failure cleanup is testable without claiming native execution."""
import importlib.util
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest


_spec = importlib.util.spec_from_file_location('document_batch_native_probe',
    Path(__file__).resolve().parents[2] / 'scripts/windows_document_batch_smoke.py')
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


@pytest.mark.parametrize('failure', ['none', 'timer', 'key', 'dialog', 'enumeration'])
def test_abort_retains_outer_loop_and_cleans_only_owned_native_dialogs(failure):
    calls, errors = [], []
    frame = SimpleNamespace(cancel_event=Event())
    foreign = object()
    class Dialog:
        def __init__(self, owner): self.owner = owner
        def GetParent(self): return self.owner
        def IsModal(self): return True
        def EndModal(self, code):
            calls.append(('close', self.owner))
            if failure == 'dialog': raise RuntimeError('close failed')
    owned, unrelated = Dialog(frame), Dialog(foreign)
    def windows():
        if failure == 'enumeration': raise RuntimeError('enumeration failed')
        return [owned, unrelated]
    def stop():
        calls.append('timer')
        if failure == 'timer': raise RuntimeError('timer failed')
    def release():
        calls.append('key')
        if failure == 'key': raise RuntimeError('key failed')
    loop = SimpleNamespace(ScheduleExit=lambda code: calls.append(('outer_exit', code)))
    app = SimpleNamespace(GetMainLoop=lambda: pytest.fail('The active nested loop must not replace the retained owner.'))
    wx = SimpleNamespace(Dialog=Dialog, GetTopLevelWindows=windows, ID_CANCEL=9)
    if failure == 'enumeration':
        with pytest.raises(RuntimeError, match='enumeration'):
            probe._abort(wx, app, frame, loop, SimpleNamespace(Stop=stop), release, errors, 'original failure')
    else:
        probe._abort(wx, app, frame, loop, SimpleNamespace(Stop=stop), release, errors, 'original failure')
    assert frame.cancel_event.is_set() and 'timer' in calls and 'key' in calls
    assert ('close', foreign) not in calls
    assert calls[-1] == ('outer_exit', 1) and errors[0] == 'original failure'
    if failure not in ('none', 'enumeration'):
        assert len(errors) == 2
