"""Actual search owners through the current desktop lifetime methods.

Only native controls/events are logical peers. The SQLite/spool work and its
existing ThreadPoolExecutor are real; this is not native wx/NVDA qualification.
"""
import ast
from concurrent.futures import ThreadPoolExecutor
import gc
import importlib.util
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import weakref

import pytest

from carr_library.application import LibraryApp
from carr_library.search import ranked_scope


class NativeFramePeer:
    def on_close(self, event):
        event.Skip()


def host(app, executor):
    path = Path(importlib.util.find_spec("carr_library.ui.desktop_app").origin).resolve()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == "DesktopFrame")
    namespace = {"__name__": "carr_library.ui._search_owner_fixture", "__package__": "carr_library.ui",
                 "WorkspaceFrame": NativeFramePeer, "Path": Path, "LibraryApp": LibraryApp}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    frame = object.__new__(namespace["DesktopFrame"])
    frame.library, frame.executor = app, executor
    frame.working = frame.busy = frame._closing = False
    frame.cancel_event = Event()
    frame.progress_panel = None
    frame.keyboard_commands = None
    frame._confirm = Mock(return_value=True)
    frame._drop, frame.refresh_scopes, frame.report = Mock(), Mock(), Mock()
    frame.scopes = []
    frame.remember_data_dir = Mock()
    return frame


def close_event(*, vetoable=False):
    event = SimpleNamespace(veto=False, skipped=False)
    event.CanVeto = lambda: vetoable
    event.GetVeto = lambda: event.veto
    event.Veto = lambda: setattr(event, "veto", True)
    event.Skip = lambda: setattr(event, "skipped", True)
    return event


@pytest.fixture
def source(tmp_path):
    app = LibraryApp(tmp_path / "original")
    workspace = app.create_workspace("Original library")
    saved = app.add_text(workspace, "Actual source", "Needle first.\fNeedle second.")
    before = app.assets.resolve(saved["asset"]).read_bytes()
    yield app, workspace
    assert app.close_search()
    assert app.assets.resolve(saved["asset"]).read_bytes() == before


def hold_rank(monkeypatch):
    entered, release = Event(), Event()
    actual = ranked_scope._lexical
    def held(*args, **kwargs):
        result = actual(*args, **kwargs)
        entered.set()
        assert release.wait(10)
        return result
    monkeypatch.setattr(ranked_scope, "_lexical", held)
    return entered, release


def test_data_root_replacement_waits_for_the_real_search_owner(source, tmp_path, monkeypatch):
    app, workspace = source
    candidate = LibraryApp(tmp_path / "candidate")
    candidate.create_workspace("Candidate library")
    assert candidate.close_search()
    entered, release = hold_rank(monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as executor:
        frame = host(app, executor)
        future = executor.submit(app.search_page, workspace, "needle", mode="thematic", ranking_scope="all")
        try:
            assert entered.wait(10)
            # An independently submitted current owner still controls admission
            # even when the native frame has no current run_job flag.
            assert not frame.working
            with pytest.raises(RuntimeError, match="releasing"):
                frame._open_data_root(candidate.root)
            assert frame.library is app and app._search_cursor_owner.closed
            frame._drop.assert_not_called()
            frame.remember_data_dir.assert_not_called()
        finally:
            release.set()
        with pytest.raises((TimeoutError, ValueError)):
            future.result(timeout=10)
        assert frame._open_data_root(candidate.root)
        assert frame.library is not app and frame.library.root == candidate.root
        frame.remember_data_dir.assert_called_once_with(candidate.root)
        frame._drop.assert_called_once()
        assert app._search_cursor_owner._db is None and not app._search_cursor_owner._ranked
        assert frame.library.close_search()


def test_accepted_shutdown_withdraws_then_retains_active_search_until_worker_return(source, monkeypatch):
    app, workspace = source
    entered, release = hold_rank(monkeypatch)
    executor = ThreadPoolExecutor(max_workers=1)
    frame = host(app, executor)
    future = executor.submit(app.search_page, workspace, "needle", mode="thematic", ranking_scope="all")
    try:
        assert entered.wait(10)
        frame.working = True
        event = close_event()
        frame.on_close(event)
        owner = app._search_cursor_owner
        cleanup = owner._cleanup_owner
        assert event.skipped and frame._closing and frame.cancel_event.is_set()
        assert cleanup is not None and not cleanup.quiescent and owner.closed
        with pytest.raises(RuntimeError):
            executor.submit(lambda: None)
    finally:
        release.set()
        executor.shutdown(wait=True, cancel_futures=True)
    with pytest.raises((TimeoutError, ValueError)):
        future.result()
    assert cleanup.quiescent and owner._db is None and not owner._ranked
    assert not list((app.root / "search-cache").glob(".active-*"))


def test_destroyed_host_cannot_drop_idle_failed_directory_cleanup(source, monkeypatch):
    app, workspace = source
    app.search_page(workspace, "needle", mode="thematic", ranking_scope="all")
    owner = app._search_cursor_owner
    order = next(iter(owner._ranked.values()))
    directory = order.directory
    actual_cleanup = order._temporary.cleanup
    allowed = Event()
    def held_cleanup():
        assert order._result_db is None
        if not allowed.is_set():
            raise OSError("The actual private directory is temporarily unavailable.")
        actual_cleanup()
    monkeypatch.setattr(order._temporary, "cleanup", held_cleanup)
    executor = ThreadPoolExecutor(max_workers=1)
    frame = host(app, executor)
    try:
        frame.on_close(close_event())
        cleanup = owner._cleanup_owner
        cleanup_reference = weakref.ref(cleanup)
        assert cleanup is not None and not cleanup.quiescent
        assert directory.exists() and owner._db is None
        frame_reference = weakref.ref(frame)
        del frame, cleanup
        gc.collect()
        assert frame_reference() is None and cleanup_reference() is not None
        assert app.close_search() is False
        allowed.set()
        assert app.close_search()
        assert cleanup_reference().quiescent and not directory.exists()
    finally:
        allowed.set()
        executor.shutdown(wait=True, cancel_futures=True)
        assert app.close_search()
