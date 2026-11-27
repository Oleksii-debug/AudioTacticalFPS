"""Streamed text through the existing Ingest journal and atomic publisher."""
from contextlib import closing
import json
from pathlib import Path

from .pipeline import Ingest, IngestError, canonical, digest
from .streamed_text import (
    MAX_FRAGMENTS, Fragment, TextProfile, check_cancelled, fragments,
    inspect_text, profile_from_value,
)
from ..storage.revisions import Page

MAX_RECORD_BYTES = 2 * 1024**2


def checkpoint(fragment, asset):
    return dict(physical_index=None, printed_label=None, printed_label_verified=False,
                raw_text=fragment.text, status='candidate', blocks=[], warnings=[],
                provenance=fragment.provenance(asset))


def checked_checkpoint(row, asset, identity, ordinal):
    """Validate one bounded record against independently scanned source hashes."""
    raw = row['payload']
    if (row['ordinal'] != ordinal or not isinstance(raw, str)
            or len(raw.encode('utf-8')) > MAX_RECORD_BYTES or digest(raw) != row['digest']):
        raise IngestError('streamed checkpoint integrity failure')
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get('raw_text'), str):
        raise IngestError('invalid streamed checkpoint')
    text = value['raw_text']
    wanted = checkpoint(Fragment(text, identity.field, identity.offset), asset)
    if (value != wanted or canonical(value) != raw or len(text) != identity.characters
            or digest(text) != identity.sha256):
        raise IngestError('streamed checkpoint differs from exact original bytes')
    return value


class StreamedTextIngest(Ingest):
    """A versioned TXT adapter; schema, journal and publication stay canonical.

    Only a bounded source-hash index is retained in memory. Checkpoints and
    published pages are consumed one at a time, including cross-DB recovery.
    """
    def __init__(self, journal, storage, *, profile):
        self.profile = profile_from_value(profile)
        super().__init__(journal, storage)

    def _source(self, root, relative, *, cancelled=lambda: False):
        from ..audio.jobs import checked_directory, filename_component
        filename_component(relative)
        path = checked_directory(root) / relative
        return path, inspect_text(path, self.profile, cancelled=cancelled)

    def start(self, workspace, edition, root, relative, config=None, *, cancelled=lambda: False):
        if not all(isinstance(value, str) and value for value in (workspace, edition)):
            raise IngestError('missing scope or edition')
        _, plan = self._source(root, relative, cancelled=cancelled)
        settings = canonical(self.profile.configuration())
        job = digest(canonical([workspace, edition, plan.digest, settings]))
        with self.transaction():
            self.db.execute('INSERT INTO job VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                (job, workspace, edition, plan.digest, settings, len(plan.fragments),
                 self._active(workspace, edition), 'pending', None))
            previous = self._job(workspace, job)
            if (previous['edition'], previous['asset'], previous['config'], previous['count']) != (
                    edition, plan.digest, settings, len(plan.fragments)):
                raise IngestError('streamed journal identity changed')
        return job

    def retry(self, workspace, job):
        with self.transaction():
            row = self._job(workspace, job)
            if row['config'] != canonical(self.profile.configuration()):
                raise IngestError('streamed retry profile changed')
            if row['state'] not in ('cancelled', 'failed'):
                raise IngestError('streamed job is not retryable')
            # No source byte is trusted here; run revalidates the complete
            # original and every surviving checkpoint before accepting them.
            self._set(job, 'pending')

    def _rows(self, job, maximum):
        # Reject a malicious over-budget SQLite value before materializing it.
        return self.db.execute('SELECT ordinal, CASE WHEN length(CAST(payload AS BLOB))<=? '
            'THEN payload ELSE NULL END AS payload, digest FROM checkpoint '
            'WHERE job=? AND ordinal<=? ORDER BY ordinal', (MAX_RECORD_BYTES, job, maximum))

    def _pages(self, row, plan, cancelled):
        count = 0
        for count, saved in enumerate(self._rows(row['id'], row['count']), 1):
            check_cancelled(cancelled)
            value = checked_checkpoint(saved, row['asset'], plan.fragments[count - 1], count)
            yield Page(value['raw_text'])
        if count != row['count'] or self.db.execute('SELECT count(*) FROM checkpoint WHERE job=?',
                                                   (row['id'],)).fetchone()[0] != count:
            raise IngestError('streamed checkpoint count mismatch')

    def _published(self, row, plan, cancelled):
        revision = self.storage.db.execute('SELECT workspace,edition,asset,config FROM revision WHERE id=?',
                                           (row['id'],)).fetchone()
        if revision is None:
            return False
        if tuple(revision) != (row['workspace'], row['edition'], row['asset'], row['config']):
            raise IngestError('published streamed identity mismatch')
        if self.storage.db.execute('SELECT 1 FROM sealed WHERE revision=?', (row['id'],)).fetchone() is None:
            raise IngestError('incomplete published streamed revision')
        published = iter(self.storage.db.execute('SELECT ordinal,physical,printed,verified,raw,digest '
                            'FROM page WHERE revision=? ORDER BY ordinal', (row['id'],)))
        for ordinal, page in enumerate(self._pages(row, plan, cancelled), 1):
            saved = next(published, None)
            if saved is None or tuple(saved) != (ordinal, None, None, 0, page.raw, digest(page.raw)):
                raise IngestError('published streamed page mismatch')
        if next(published, None) is not None:
            raise IngestError('published streamed count mismatch')
        self.storage.check()
        with self.transaction():
            self._set(row['id'], 'complete' if self._active(row['workspace'], row['edition']) == row['id'] else 'superseded')
        return True

    def run(self, workspace, job, root, relative, config=None, *,
            max_steps=None, cancelled=lambda: False):
        maximum = self.profile.maximum_fragments + 1
        if max_steps is None:
            max_steps = maximum
        if type(max_steps) is not int or not 1 <= max_steps <= maximum:
            raise IngestError('invalid streamed step limit')
        row = self._job(workspace, job)
        try:
            path, plan = self._source(root, relative, cancelled=cancelled)
            if (plan.digest, len(plan.fragments), canonical(self.profile.configuration())) != (
                    row['asset'], row['count'], row['config']):
                raise IngestError('streamed source or configuration changed')
            if self._published(row, plan, cancelled):
                return self.inspect(workspace, job)
            if row['state'] != 'pending':
                return self.inspect(workspace, job)
            existing = self.db.execute('SELECT count(*) FROM checkpoint WHERE job=?', (job,)).fetchone()[0]
            if existing > row['count']:
                raise IngestError('streamed checkpoint count mismatch')
            previous = iter(self._rows(job, existing))
            performed = 0
            with closing(fragments(path, self.profile, expected_digest=plan.digest,
                    expected_size=plan.size, expected_identity=plan.identity, cancelled=cancelled)) as incoming:
                for ordinal, fragment in enumerate(incoming, 1):
                    check_cancelled(cancelled)
                    if ordinal <= existing:
                        saved = next(previous, None)
                        if saved is None:
                            raise IngestError('streamed checkpoint sequence changed')
                        checked_checkpoint(saved, row['asset'], plan.fragments[ordinal - 1], ordinal)
                        continue
                    if performed >= max_steps:
                        return self.inspect(workspace, job)
                    payload = canonical(checkpoint(fragment, plan.digest))
                    if len(payload.encode('utf-8')) > MAX_RECORD_BYTES:
                        raise IngestError('streamed checkpoint byte budget exceeded')
                    with self.transaction():
                        self.db.execute('INSERT INTO checkpoint VALUES(?,?,?,?)',
                                        (job, ordinal, payload, digest(payload)))
                    performed += 1
            if next(previous, None) is not None:
                raise IngestError('streamed checkpoint sequence changed')
            if performed >= max_steps:
                return self.inspect(workspace, job)
            check_cancelled(cancelled)
            if self._active(workspace, row['edition']) != row['expected_active']:
                with self.transaction():
                    self._set(job, 'conflict', 'active_revision_changed')
                return self.inspect(workspace, job)
            # The final source checksum and native identity have been checked.
            # A cancelled page generator rolls back the whole existing catalog
            # transaction. Once it commits, report its durable completion.
            self.storage.publish_revision(workspace, row['edition'], job, plan.digest, row['config'],
                                          self._pages(row, plan, cancelled))
            with self.transaction():
                self._set(job, 'complete')
            return self.inspect(workspace, job)
        except InterruptedError:
            with self.transaction():
                self._set(job, 'cancelled')
            return self.inspect(workspace, job)
        except Exception:
            # A post-commit journal failure can still be reconciled from the
            # exact stored source on the next run; no partial revision is made.
            with self.transaction():
                self._set(job, 'failed', 'streamed_validation_failed')
            raise
