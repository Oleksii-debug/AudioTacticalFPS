"""Independent file/scope races at the final public search-page grant.

Every lookahead executes the real public search before the mutation. The only
stable-timestamp seam is explicitly confined to timestamp observations in the
full-hash test; bytes, descriptors, inode/link observations and hashes stay real.
"""
from hashlib import sha256
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from reportlab.pdfgen.canvas import Canvas

from carr_library.application import LibraryApp
from carr_library.ingest import retained_assets
from carr_library.search import originals


@pytest.fixture
def library(tmp_path):
    apps = []

    def create(kind="txt"):
        folder = tmp_path / ("case-%d" % len(apps))
        folder.mkdir()
        app = LibraryApp(folder / "library")
        apps.append(app)
        workspace = app.create_workspace("One current source-page authority")
        collection = app.create_collection(workspace, "Only the selected source")
        if kind == "txt":
            result = app.add_text(
                workspace, "Retained Unicode original",
                "\ufeffNeedle Prior first Ї cafe\u0301.\r\n\fNeedle Prior second exact source.\r\n",
                collections=(collection,))
            assert result["verified"] is True
        else:
            source = folder / "actual.pdf"
            canvas = Canvas(str(source), invariant=1, pageCompression=0)
            for number in range(2):
                canvas.drawString(40, 700, "Needle Prior actual unreviewed page %d." % number)
                canvas.showPage()
            canvas.save()
            result = app.import_file(
                workspace, source, title="Unreviewed PDF", collections=(collection,))
            assert result["verified"] is False
        assert result["state"] == "complete" and result["completed"] == 2
        path = app.assets.resolve(result["asset"])
        raw = path.read_bytes()
        assert sha256(raw).hexdigest() == result["asset"]
        prior = app.search_page(workspace, "Prior", collection=collection, limit=1)
        assert len(prior.results) == 1 and prior.has_more_results
        assert prior.results[0].verified is (kind == "txt")
        return SimpleNamespace(
            app=app, workspace=workspace, collection=collection, result=result,
            path=path, raw=raw, prior=prior, selections=dict(app._selections))

    yield create
    for app in reversed(apps):
        assert app.close_search()


def _release_observer(monkeypatch):
    closed = []
    real = originals.PageOriginals.close

    def close(self):
        closed.append(self)
        return real(self)

    monkeypatch.setattr(originals.PageOriginals, "close", close)
    return closed


def _after_real_lookahead(monkeypatch, action):
    real = LibraryApp.search
    observed = []

    def search(self, *args, **options):
        result = real(self, *args, **options)
        if (options.get("_page_info") is not None
                and options.get("limit") == 1 and options.get("offset") == 1):
            assert len(result) == 1 and not observed
            proof = self._page_originals
            assert not proof.closed and len(proof._originals) == 1
            observed.append((tuple(result), proof))
            action(self, result)
        return result

    monkeypatch.setattr(LibraryApp, "search", search)
    return observed


def _search(book, **options):
    return book.app.search_page(
        book.workspace, "Needle", collection=book.collection, limit=1, **options)


def _assert_refused(book, lookahead, closed):
    assert len(lookahead) == len(closed) == 1
    assert lookahead[0][1] is closed[0]
    assert closed[0].closed and closed[0].bytes == 0
    assert not closed[0]._originals and not closed[0]._wanted
    assert closed[0]._wanted_count == 0
    assert book.app._selections == book.selections


def _replace_one_byte(path, raw):
    before = path.stat()
    changed = raw.replace(b"Needle", b"Noodle", 1)
    assert changed != raw and len(changed) == len(raw)
    with path.open("r+b") as output:
        output.write(changed)
        output.flush()
        os.fsync(output.fileno())
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_dev, after.st_ino, after.st_size, after.st_nlink, after.st_mtime_ns) == (
        before.st_dev, before.st_ino, before.st_size, before.st_nlink, before.st_mtime_ns)
    return changed


@pytest.mark.parametrize("kind", ["txt", "pdf"])
def test_final_real_hash_rejects_same_size_rewrite_after_lookahead(library, monkeypatch, kind):
    book = library(kind)
    real_identity = originals._identity
    first = real_identity(book.path)
    real_resolve = book.app.assets.resolve
    changed = []
    rechecked = []

    def stable_timestamps(path):
        value = real_identity(path)
        if Path(path) == book.path:
            # Only the observed file mtime/ctime are held stable. Every real
            # file/parent inode, mode, size, link count and attribute remains.
            file, parent = value
            return ((*file[:5], *first[0][5:7], *file[7:]), parent)
        return value

    def resolve(*args, **options):
        if changed:
            rechecked.append(args[0])
        return real_resolve(*args, **options)

    def mutate(_app, result):
        assert result[0].verified is (kind == "txt")
        changed.append(_replace_one_byte(book.path, book.raw))

    monkeypatch.setattr(originals, "_identity", stable_timestamps)
    monkeypatch.setattr(book.app.assets, "resolve", resolve)
    closed = _release_observer(monkeypatch)
    lookahead = _after_real_lookahead(monkeypatch, mutate)
    with pytest.raises(ValueError, match="retained asset digest mismatch"):
        _search(book)
    assert rechecked == [book.result["asset"]]
    assert book.path.read_bytes() == changed[0]
    _assert_refused(book, lookahead, closed)
    # Even the previously completed source handle must recheck the changed
    # retained source; an unverified PDF is never downgraded into permission.
    with pytest.raises(ValueError, match="retained asset digest mismatch"):
        book.app.preview(book.workspace, book.prior.results[0].ref,
                         book.prior.results[0].revision)


def test_equal_byte_atomic_replacement_after_lookahead_is_not_the_captured_file(
        library, monkeypatch):
    book = library()
    initial = book.path.stat()
    replacement = book.path.with_name(book.path.name + ".replacement")

    def mutate(_app, result):
        assert result[0].verified
        replacement.write_bytes(book.raw)
        os.utime(replacement, ns=(initial.st_atime_ns, initial.st_mtime_ns))
        assert replacement.stat().st_ino != initial.st_ino
        os.replace(replacement, book.path)
        assert book.path.read_bytes() == book.raw
        assert book.path.stat().st_ino != initial.st_ino
        assert book.path.stat().st_mtime_ns == initial.st_mtime_ns

    closed = _release_observer(monkeypatch)
    lookahead = _after_real_lookahead(monkeypatch, mutate)
    with pytest.raises(ValueError, match="original.*changed|Original.*changed"):
        _search(book)
    _assert_refused(book, lookahead, closed)
    assert book.path.read_bytes() == book.raw and not replacement.exists()


def test_actual_extra_link_after_lookahead_refuses_page_and_releases_proof(
        library, monkeypatch, tmp_path):
    book = library()
    alias = tmp_path / "linked-original.txt"

    def mutate(_app, _result):
        os.link(book.path, alias)
        current, linked = book.path.stat(), alias.stat()
        assert current.st_ino == linked.st_ino and current.st_nlink == linked.st_nlink == 2
        assert alias.read_bytes() == book.raw

    closed = _release_observer(monkeypatch)
    lookahead = _after_real_lookahead(monkeypatch, mutate)
    try:
        with pytest.raises(ValueError, match="regular"):
            _search(book)
        _assert_refused(book, lookahead, closed)
        assert book.path.read_bytes() == book.raw
    finally:
        if alias.exists():
            alias.unlink()
    assert book.path.stat().st_nlink == 1


@pytest.mark.parametrize("change", ["selected_collection", "new_workspace"])
def test_actual_catalog_commit_after_lookahead_invalidates_the_whole_page(
        library, monkeypatch, change):
    book = library()
    commits = []

    def mutate(_app, _result):
        if change == "selected_collection":
            with book.app.catalog_transaction(book.workspace) as catalog:
                assert catalog.remove(book.collection, book.result["edition"])
            commits.append(book.result["edition"])
        else:
            commits.append(book.app.create_workspace("Actual concurrent catalog generation"))

    closed = _release_observer(monkeypatch)
    lookahead = _after_real_lookahead(monkeypatch, mutate)
    with pytest.raises(ValueError, match="library changed"):
        _search(book)
    assert len(commits) == 1
    _assert_refused(book, lookahead, closed)
    assert book.path.read_bytes() == book.raw
    if change == "selected_collection":
        with pytest.raises(ValueError, match="left the selected collection"):
            book.app.preview(book.workspace, book.prior.results[0].ref,
                             book.prior.results[0].revision)


def test_cancel_after_actual_lookahead_does_not_publish_a_partial_page(library, monkeypatch):
    book = library()
    stopped = False

    def cancel(_app, _result):
        nonlocal stopped
        stopped = True

    closed = _release_observer(monkeypatch)
    lookahead = _after_real_lookahead(monkeypatch, cancel)
    with pytest.raises(TimeoutError, match="Search cancelled or timed out"):
        _search(book, cancelled=lambda: stopped)
    _assert_refused(book, lookahead, closed)
    assert book.path.read_bytes() == book.raw


def test_cancel_during_actual_final_descriptor_read_closes_it_before_return(
        library, monkeypatch):
    book = library()
    after_lookahead, stopped = False, False
    real_hash = retained_assets._hash_stream
    streams, reads = [], []

    class ObservedRead:
        def __init__(self, stream):
            self.stream = stream

        def read(self, size):
            nonlocal stopped
            block = self.stream.read(size)
            reads.append((size, len(block)))
            if block:
                stopped = True
            return block

    def hashed(stream, **options):
        if not after_lookahead:
            return real_hash(stream, **options)
        streams.append(stream)
        return real_hash(ObservedRead(stream), **options)

    def ready(_app, _result):
        nonlocal after_lookahead
        after_lookahead = True

    monkeypatch.setattr(retained_assets, "_hash_stream", hashed)
    closed = _release_observer(monkeypatch)
    lookahead = _after_real_lookahead(monkeypatch, ready)
    with pytest.raises(TimeoutError, match="Search cancelled or timed out"):
        _search(book, cancelled=lambda: stopped)
    assert reads and reads[0][1] > 0 and len(streams) == 1
    assert all(stream.closed for stream in streams)
    _assert_refused(book, lookahead, closed)
    assert book.path.read_bytes() == book.raw


def test_close_current_owner_after_lookahead_withdraws_before_grant(library, monkeypatch):
    book = library()
    owner = book.app._search_cursor_owner
    results = []

    def withdraw(_app, _result):
        results.append(book.app.close_search())
        assert owner.closed

    closed = _release_observer(monkeypatch)
    lookahead = _after_real_lookahead(monkeypatch, withdraw)
    with pytest.raises(TimeoutError, match="Search cancelled or timed out"):
        _search(book)
    assert results == [False]  # The real in-flight operation still owns its guard.
    _assert_refused(book, lookahead, closed)
    assert owner._db is None and book.app.close_search()
    with pytest.raises(ValueError, match="cursor.*owner"):
        _search(book, cursor=book.prior.cursor)
    assert book.path.read_bytes() == book.raw


@pytest.mark.parametrize("change", ["bytes", "equal_byte_inode"])
def test_later_original_final_read_cannot_hide_earlier_original_drift(
        tmp_path, monkeypatch, change):
    """A genuine B read changes already-verified A before the page is granted."""
    app = LibraryApp(tmp_path / "pair-library")
    workspace = app.create_workspace("Two current originals in one result page")
    first = app.add_text(workspace, "First source", "Needle Alpha retained text.\r\n")
    second = app.add_text(workspace, "Second source", "Needle Bravo retained text.\r\n")
    assert first["state"] == second["state"] == "complete"
    originals_by_hash = {}
    for result in (first, second):
        path = app.assets.resolve(result["asset"])
        originals_by_hash[result["asset"]] = (path, path.read_bytes())
    assert len(originals_by_hash) == 2
    prior = app.search_page(workspace, "Needle", limit=1)
    assert len(prior.results) == 1 and prior.has_more_results
    before = dict(app._selections)
    completed, mutations = [], []
    state = {"final": False, "current": None}
    real_validate = originals.PageOriginals.validate
    real_retained = LibraryApp._retained_source
    real_hash = retained_assets._hash_stream
    closed = _release_observer(monkeypatch)

    def validate(self, *args, **options):
        # The second real original was reached by the genuine lookahead.
        assert len(self._originals) == 2
        state["final"] = True
        try:
            return real_validate(self, *args, **options)
        finally:
            state["final"] = False

    def retained(self, db, selected_workspace, identity, **options):
        if not state["final"]:
            return real_retained(self, db, selected_workspace, identity, **options)
        state["current"] = identity
        try:
            result = real_retained(self, db, selected_workspace, identity, **options)
            completed.append((identity, result))
            return result
        finally:
            state["current"] = None

    class ObservedLaterRead:
        def __init__(self, stream):
            self.stream = stream

        def read(self, size):
            block = self.stream.read(size)
            if block and not mutations:
                identity, result = completed[0]
                target, original = originals_by_hash[identity.asset_sha256]
                assert Path(result[0]) == target
                before_stat = target.stat()
                if change == "bytes":
                    altered = original.replace(b"Needle", b"Noodle", 1)
                    assert altered != original and len(altered) == len(original)
                    with target.open("r+b") as output:
                        output.write(altered)
                        output.flush()
                        os.fsync(output.fileno())
                    # This case exercises a real changed stamp on both OS,
                    # including Windows' creation-time meaning of st_ctime.
                    os.utime(target, ns=(before_stat.st_atime_ns,
                                         before_stat.st_mtime_ns + 1_000_000_000))
                    assert target.stat().st_mtime_ns != before_stat.st_mtime_ns
                else:
                    replacement = target.with_name(target.name + ".new-inode")
                    replacement.write_bytes(original)
                    os.utime(replacement, ns=(before_stat.st_atime_ns,
                                              before_stat.st_mtime_ns))
                    assert replacement.stat().st_ino != before_stat.st_ino
                    os.replace(replacement, target)
                    assert target.stat().st_ino != before_stat.st_ino
                    assert target.read_bytes() == original
                mutations.append(identity)
            return block

    def hashed(stream, **options):
        if state["final"] and len(completed) == 1:
            assert state["current"] != completed[0][0]
            return real_hash(ObservedLaterRead(stream), **options)
        return real_hash(stream, **options)

    monkeypatch.setattr(originals.PageOriginals, "validate", validate)
    monkeypatch.setattr(LibraryApp, "_retained_source", retained)
    monkeypatch.setattr(retained_assets, "_hash_stream", hashed)
    try:
        with pytest.raises(ValueError, match="original.*changed|Original.*changed"):
            app.search_page(workspace, "Needle", limit=1)
        assert len(completed) == 2 and completed[0][0] != completed[1][0]
        assert mutations == [completed[0][0]]
        unchanged_path, unchanged_bytes = originals_by_hash[completed[1][0].asset_sha256]
        assert unchanged_path.read_bytes() == unchanged_bytes
        assert len(closed) == 1 and closed[0].closed and not closed[0]._originals
        assert not closed[0]._wanted and closed[0].bytes == closed[0]._wanted_count == 0
        assert app._selections == before
    finally:
        assert app.close_search()
