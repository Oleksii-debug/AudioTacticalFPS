"""Bounded immutable originals for one outer public search-page operation.

A capture is never reusable by another page, copy action or cursor call. Current
scope, active revision, complete original bytes and exact path identities are
rechecked after every internal search/lookahead and before granting the page.
"""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from ..citations.anchors import Identity, digest
from ..catalog.intake import FileSnapshot, text_segments
from ..durable_state import regular

MAX_IDENTITIES = 101  # At most 100 returned hits plus one current lookahead hit.
MAX_CAPTURE_BYTES = 64 * 1024**2
MAX_DIGESTS = 101  # Aggregate requested logical ordinals, including lookahead.


def _identity(path):
    file = regular(path)
    parent = regular(path.parent, directory=True)
    return (
        (file.st_dev, file.st_ino, file.st_mode, file.st_nlink, file.st_size,
         file.st_mtime_ns, file.st_ctime_ns, getattr(file, "st_file_attributes", 0)),
        (parent.st_dev, parent.st_ino, parent.st_mode,
         getattr(parent, "st_file_attributes", 0)),
    )


@dataclass
class _Original:
    identity: Identity
    path: Path
    mime: str
    size: int
    stamp: tuple
    data: bytes | None = None
    streamed: FileSnapshot | None = None
    segment_count: int | None = None
    digests: dict | None = None


class PageOriginals:
    """Operation-local current-source proof; owns no file handle or executor."""

    def __init__(self, app, workspace, resources):
        self._app = app
        self._binding = self._app_binding(app)
        self.workspace = workspace
        self.limit = min(resources.memory_bytes // 8, MAX_CAPTURE_BYTES)
        self.bytes = 0
        self.closed = False
        self._originals = {}
        self._wanted = {}
        self._wanted_count = 0

    @staticmethod
    def _app_binding(app):
        return app.root, app.database, app.assets_dir, app.assets.root, id(app.assets)

    def _check(self, app, workspace, cancelled):
        if cancelled():
            raise InterruptedError("Source verification cancelled.")
        if (self.closed or workspace != self.workspace
                or self._app_binding(app) != self._binding):
            raise ValueError("The source-page proof or library owner changed.")

    def plan(self, app, db, workspace, hits, *, cancelled):
        """Plan only actual requested ordinals; this grants no source authority."""
        self._check(app, workspace, cancelled)
        for position, hit in enumerate(hits, 1):
            self._check(app, workspace, cancelled)
            if position > 100:
                raise ValueError("The source-page result allowance was exceeded.")
            row = db.execute(
                "SELECT p.ordinal,r.asset FROM page p JOIN revision r ON r.id=p.revision "
                "JOIN sealed s ON s.revision=r.id WHERE r.workspace=? AND r.edition=? "
                "AND r.id=? AND p.id=?",
                (workspace, hit['edition_id'], hit['revision_id'], hit['page_id'])).fetchone()
            if row is None or type(row[0]) is not int or not 1 <= row[0] <= 2**31 - 1:
                raise ValueError("The search returned an invalid source ordinal.")
            identity = Identity(workspace, hit['edition_id'], row[1], hit['revision_id'])
            wanted = self._wanted.setdefault(identity, set())
            if row[0] not in wanted:
                if self._wanted_count >= MAX_DIGESTS:
                    raise ValueError("The source-page digest allowance was exceeded.")
                wanted.add(row[0])
                self._wanted_count += 1

    def register(self, app, db, workspace, identity, checked_assets, *, cancelled):
        """Register every result, including previews with no text attestation."""
        self._check(app, workspace, cancelled)
        if type(identity) is not Identity or identity.workspace != workspace:
            raise ValueError("The original is outside this source-page proof.")
        # This preserves the existing per-edition authorization even when the
        # enclosing result loop already verified the same original's path.
        path, mime, size = app._retained_source(
            db, workspace, identity, checked_assets=checked_assets, cancelled=cancelled)
        path = Path(path)
        stamp = _identity(path)
        found = self._originals.get(identity)
        if found is None:
            if len(self._originals) >= MAX_IDENTITIES:
                raise ValueError("The source-page identity allowance was exceeded.")
            found = _Original(identity, path, mime, size, stamp)
            self._originals[identity] = found
        elif (found.path, found.mime, found.size, found.stamp) != (path, mime, size, stamp):
            raise ValueError("An original changed during this search page.")
        self._check(app, workspace, cancelled)

    def resolve(self, app, db, workspace, identity, *, cancelled):
        """Reuse only this operation\'s exact immutable TXT capture or snapshot."""
        self._check(app, workspace, cancelled)
        found = self._originals.get(identity)
        if found is None:
            raise ValueError("Register the current result before resolving its original.")
        # Cached bytes are useful only inside the already authorized read. A
        # pathname alone never authorizes them, and each edition is still checked.
        app._authorize(db, workspace)
        row = db.execute(
            "SELECT a.mime,a.size FROM catalog_edition e "
            "JOIN catalog_asset a ON a.digest=e.asset "
            "WHERE e.workspace=? AND e.id=? AND e.asset=?",
            (workspace, identity.edition_id, identity.asset_sha256)).fetchone()
        if row is None or tuple(row) != (found.mime, found.size):
            raise ValueError("The original source catalog binding changed.")
        before = _identity(found.path)
        if before != found.stamp:
            raise ValueError("An original changed before its source-page capture.")
        if found.data is not None:
            self._check(app, workspace, cancelled)
            return found.data
        if found.streamed is not None:
            self._check(app, workspace, cancelled)
            return found.streamed
        # Keep every current resolver/format/hash check for first, over-budget
        # and PDF reads. The initial sample brackets these exact bytes.
        data = app._asset_source(db, workspace, identity, cancelled=cancelled)
        if _identity(found.path) != before:
            raise ValueError("An original changed during its source-page capture.")
        self._check(app, workspace, cancelled)
        if type(data) is bytes:
            if len(data) != found.size or sha256(data).hexdigest() != identity.asset_sha256:
                raise ValueError("Captured original bytes differ from their source identity.")
            if found.mime == "text/plain" and found.size <= self.limit - self.bytes:
                found.data = data
                self.bytes += found.size
        elif type(data) is FileSnapshot:
            if (data.path != found.path or data.digest != identity.asset_sha256
                    or data.mime != found.mime or data.size != found.size
                    or data.identity != before[0]):
                raise ValueError("The streamed original differs from the captured source.")
            if found.mime == "text/plain":
                # This immutable descriptor recipe is useful only after the
                # complete stream proof below and fresh final original checking.
                found.streamed = data
        else:
            raise ValueError("The current original resolver returned an invalid source.")
        self._check(app, workspace, cancelled)
        return data

    def attest_stream(self, identity, ordinal, expected_count, snapshot, raw_sha256, *, cancelled):
        """Complete one real stream before publishing bounded requested digests."""
        self._check(self._app, self.workspace, cancelled)
        found = self._originals.get(identity)
        wanted = self._wanted.get(identity, ())
        if (found is None or found.streamed is not snapshot or ordinal not in wanted
                or type(expected_count) is not int or not 1 <= expected_count <= 2**31 - 1
                or _identity(found.path) != found.stamp):
            raise ValueError("The streamed source proof has no exact current binding.")
        if found.segment_count is not None and found.segment_count != expected_count:
            raise ValueError("The sealed source segment count changed.")
        if found.digests is not None and ordinal in found.digests:
            self._check(self._app, self.workspace, cancelled)
            return found.digests[ordinal] == raw_sha256

        # Only requested ordinals consume metadata. A late invalid record, count,
        # original hash, path change or cancellation cannot publish this table.
        selected, count, nonempty = {}, 0, False
        for count, segment in enumerate(text_segments(snapshot, cancelled=cancelled,
                max_segments=expected_count, max_segment_bytes=MAX_CAPTURE_BYTES), 1):
            if cancelled():
                raise InterruptedError("Source verification cancelled.")
            nonempty = nonempty or bool(segment.strip())
            if count in wanted:
                selected[count] = digest(segment)
        if count != expected_count or not nonempty or set(selected) != wanted:
            return False
        if _identity(found.path) != found.stamp:
            raise ValueError("The original changed during complete stream verification.")
        self._check(self._app, self.workspace, cancelled)
        found.segment_count, found.digests = expected_count, selected
        return selected[ordinal] == raw_sha256

    def validate(self, owner, epoch, budget):
        """Fresh current originals and scope after lookahead, before any grant."""
        app = self._app
        self._check(app, self.workspace, budget.stopped)
        owner.check(app, epoch)
        with app._search_read(self.workspace, budget.remaining(), budget.stopped) as (db, active):
            for found in self._originals.values():
                active.check()
                if _identity(found.path) != found.stamp:
                    raise ValueError("An original changed before publishing this search page.")
                # This is a fresh authorized read after internal lookahead. The
                # same owner version is checked again after the whole proof.
                current = db.execute(
                    "SELECT 1 FROM active_page WHERE workspace=? AND edition=? "
                    "AND revision=? LIMIT 1",
                    (self.workspace, found.identity.edition_id,
                     found.identity.revision)).fetchone()
                if current is None:
                    raise ValueError("The active source revision changed during this search page.")
                path, mime, size = app._retained_source(
                    db, self.workspace, found.identity, cancelled=active.stopped)
                if ((Path(path), mime, size) != (found.path, found.mime, found.size)
                        or _identity(found.path) != found.stamp):
                    raise ValueError("An original changed during final source verification.")
                active.check()
        # A later original's full hash must not hide a change to an earlier
        # captured source. Recheck the complete bounded identity set after all
        # byte proofs, before the final catalog epoch and page publication.
        for found in self._originals.values():
            self._check(app, self.workspace, budget.stopped)
            if _identity(found.path) != found.stamp:
                raise ValueError("An original changed before publishing this search page.")
        self._check(app, self.workspace, budget.stopped)
        owner.check(app, epoch)

    def close(self):
        """Drop private immutable captures on success, cancellation or failure."""
        self.closed = True
        self._originals.clear()
        self._wanted.clear()
        self._wanted_count = 0
        self.bytes = 0
