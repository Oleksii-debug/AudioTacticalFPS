"""Actual POSIX result-path substitutions against a held completed order."""
from hashlib import sha256
import multiprocessing
import os
from pathlib import Path

import pytest

import carr_library.application as application
import carr_library.search.paging as paging
import carr_library.search.ranked_scope as ranked_scope


def substitution_child(expected, root, kind, reply):
    app = None
    try:
        for module in (application, paging, ranked_scope):
            path = Path(module.__file__).resolve()
            assert (str(path), sha256(path.read_bytes()).hexdigest()) == tuple(expected[module.__name__])
        app = application.LibraryApp(Path(root) / "library")
        workspace = app.create_workspace("Exact owned source")
        saved = app.add_text(workspace, "Source", "Needle first.\fNeedle second.")
        original = app.assets.resolve(saved["asset"]).read_bytes()
        first = app.search_page(workspace, "needle", mode="thematic", ranking_scope="all", limit=1)
        order = next(iter(app._search_cursor_owner._ranked.values()))
        retained_connection = order._result_db
        before = dict(app._selections)
        actual_check = order._check_result_file
        swapped = []
        foreign = Path(root) / "foreign.sqlite"
        foreign.write_bytes(order.path.read_bytes())
        foreign_before = foreign.read_bytes()
        def checked_then_substitute():
            actual_check()
            if not swapped:
                swapped.append(True)
                order.path.unlink()
                if kind == "fifo":
                    os.mkfifo(order.path)
                else:
                    order.path.symlink_to(foreign)
        order._check_result_file = checked_then_substitute
        try:
            app.search_page(workspace, "needle", mode="thematic", ranking_scope="all",
                            cursor=first.cursor, offset=1)
        except (OSError, ValueError):
            assert swapped == [True] and order._result_db is retained_connection
        else:
            raise AssertionError("The substituted result pathname was admitted.")
        assert app._selections == before
        assert foreign.read_bytes() == foreign_before
        assert app.assets.resolve(saved["asset"]).read_bytes() == original
        assert app.close_search()
        assert not order.directory.exists()
        reply.send({"status": "PASS", "kind": kind, "modules": expected})
    except BaseException as error:
        reply.send({"status": "FAIL", "type": type(error).__name__, "error": str(error)[:1024]})
    finally:
        if app is not None:
            app.close_search()
        reply.close()


@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_actual_child_does_not_reopen_a_substituted_result_path(tmp_path, kind):
    assert os.name == "posix", "This actual boundary is selected only on POSIX."
    expected = {}
    for module in (application, paging, ranked_scope):
        path = Path(module.__file__).resolve()
        expected[module.__name__] = (str(path), sha256(path.read_bytes()).hexdigest())
    # Fork retains the exact selected source/installed modules. Child verifies
    # their paths and bytes before creating any library or owned result handle.
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=substitution_child, args=(expected, str(tmp_path), kind, child))
    process.start()
    child.close()
    try:
        process.join(8)
        assert not process.is_alive(), "The actual result-path substitution blocked a native read."
        assert process.exitcode == 0 and parent.poll()
        assert parent.recv() == {"status": "PASS", "kind": kind, "modules": expected}
    finally:
        if process.is_alive():
            process.terminate()
            process.join(3)
        if process.is_alive():
            process.kill()
            process.join(3)
        parent.close()
        assert not process.is_alive()
        process.close()
