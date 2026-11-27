"""Actual shared progress owner and extracted Studio save/cleanup contracts."""
import ast
import atexit
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
from pathlib import Path
import runpy
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def studio_handlers(names, values, extra=None):
    path = Path(find_spec('carr_library.ui.studio_dialog').origin)
    tree = ast.parse(path.read_text(encoding='utf-8'))
    studio = next(row for row in tree.body if isinstance(row, ast.FunctionDef) and row.name == 'show_studio')
    nodes = [row for row in studio.body if isinstance(row, ast.FunctionDef) and row.name in names]
    assert {row.name for row in nodes} == set(names)
    output = ', '.join(f'{name}={name}' for name in names)
    factory = ast.parse('def make(' + ', '.join(values) + '):\n    return SimpleNamespace(' + output + ')')
    factory.body[0].body[:0] = nodes
    scope = dict(SimpleNamespace=SimpleNamespace, threading=threading, __package__='carr_library.ui')
    scope.update(extra or {})
    exec(compile(ast.fix_missing_locations(factory), str(path), 'exec'), scope)
    return scope['make'](**values)


@pytest.fixture
def native_panel(tmp_path, monkeypatch):
    module = runpy.run_path(str(Path(__file__).with_name('test_progress_media_integration.py')))
    generator = module['panel'].__wrapped__(tmp_path, monkeypatch)
    panel = next(generator)
    try:
        yield panel
    finally:
        try:
            next(generator)
        except StopIteration:
            pass


@pytest.mark.parametrize('failure', ['false', 'raise'])
def test_accepted_progress_owner_is_retained_until_executor_cleanup_retry(native_panel, monkeypatch, failure):
    panel = native_panel
    class VoiceOwner:
        library_root = panel.frame.library.root
        rejected = True
        attempts = []
        def speak_progress(self, *args, **kwargs): pass
        def cancel(self): return True
        def close(self):
            self.attempts.append(threading.get_ident())
            if self.rejected:
                if failure == 'raise':
                    raise OSError('Synthetic unconfirmed accepted progress worker')
                return False
            return True
    voice = VoiceOwner()
    panel.set_speech_provider(voice, root=voice.library_root)
    retained_callbacks = []
    monkeypatch.setattr(atexit, 'register', lambda callback: retained_callbacks.append(callback))
    monkeypatch.setattr(atexit, 'unregister', lambda callback: retained_callbacks.remove(callback))
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)
    service = SimpleNamespace(close=Mock(return_value=True))
    reports, posted = [], []
    values = dict(progress=panel.legacy, owned_progress_voices=set(),
                  cleanup_retry_registered=False, service=service,
                  frame=SimpleNamespace(_closing=True),
                  wx=SimpleNamespace(CallAfter=lambda callback: posted.append(callback)))
    callbacks = studio_handlers(('retire_progress_voice', 'release_service'), values)
    def work():
        entered.set()
        assert release.wait(5)
        completed.set()
    active = executor.submit(work)
    try:
        assert entered.wait(5)
        assert panel.close() is False
        assert panel.closed and panel._retained_providers == [voice]
        cleanup = executor.submit(callbacks.release_service)
        assert not cleanup.done() and not service.close.called
        release.set()
        cleanup.result(5)
        assert completed.is_set() and active.done()
        assert panel._retained_providers == [voice]
        assert retained_callbacks == [callbacks.release_service]
        assert voice.attempts[-1] != threading.get_ident()
        voice.rejected = False
        executor.submit(retained_callbacks[0]).result(5)
        assert panel._retained_providers == [] and retained_callbacks == []
        assert service.close.call_count == 2
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=False)
        voice.rejected = False
        panel.close()
    # No deleted control is needed for either close attempt; notices are gated.
    for callback in posted:
        callback()


def test_save_coordinator_uses_actual_saved_signature_and_blocks_dead_native_controls():
    from carr_library.ui.save_all import SaveResult
    saved, reports = [], []
    state = {'text': 'Exact source-derived production', 'title': 'Synthetic title', 'voices': ('voice-1',)}
    controller = SimpleNamespace(save=lambda *signature: saved.append(signature) or SimpleNamespace(
        revision=2, scenes=(object(),), roles=(object(),)))
    editor = SimpleNamespace(GetValue=lambda: state['text'], SetFocus=Mock())
    title = SimpleNamespace(GetValue=lambda: state['title'])
    values = dict(alive=True, busy=False, draft_dirty=True, saved_signature=None,
        controller=controller, editor=editor, title=title, selected_voices=lambda: state['voices'],
        report=lambda message, focus=False: reports.append(message), dialog=True)
    callbacks = studio_handlers(('save', 'unsaved', 'save_registered', 'focus_registered'), values,
                               {'SaveResult': SaveResult})
    assert callbacks.unsaved()
    results = []
    callbacks.save_registered(results.append)
    assert saved == [(state['text'], state['title'], state['voices'])]
    assert results[0].saved and not callbacks.unsaved()
    state['text'] += ' Changed.'
    assert callbacks.unsaved()
    dead = dict(values, alive=False, editor=SimpleNamespace(
        GetValue=Mock(side_effect=AssertionError('Dead editor queried')), SetFocus=Mock()))
    closed = studio_handlers(('save', 'unsaved', 'save_registered', 'focus_registered'), dead,
                            {'SaveResult': SaveResult})
    assert not closed.unsaved()
    failures = []
    closed.save_registered(failures.append)
    closed.focus_registered()
    assert not failures[0].saved and 'closed' in failures[0].message
    dead['editor'].GetValue.assert_not_called()
    dead['editor'].SetFocus.assert_not_called()
