"""Read-only reconciliation of a stopped worker's exact sealed publication.

The existing source revision is the receipt. This never restarts extraction,
registers an edition, adds memberships, or treats a request journal as success.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time

from ..catalog.membership import Catalog
from ..catalog.read_views import EditionView, get_edition
from ..durable_state import regular
from ..storage.revisions import _schema_signature, _expected_signature
from .pipeline import canonical, digest
from .retained_assets import MAX_BYTES, SUFFIXES
from .writer_guard import exclusive_writer, _regular_if_present


@contextmanager
def _read_database(path, expired):
    regular(path)
    for suffix in ('-wal', '-shm', '-journal'):
        _regular_if_present(Path(str(path) + suffix))
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                         isolation_level=None, timeout=0)
    try:
        db.row_factory = sqlite3.Row
        db.set_progress_handler(lambda: int(expired()), 1000)
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        yield db
    finally:
        db.set_progress_handler(None, 0)
        db.close()


def completed_request(root, workspace, request_id, *, timeout=1.0):
    """Return a verified completion, or None when publication is not established.

    One additional second at most is budgeted cooperatively for local SQLite
    work and bounded chunk hashing. Busy writers never delay cancellation. A
    failed/deadline-limited verification leaves the original resumable outcome.
    """
    from ..application import LibraryApp
    from ..pending_backup import _checkpoint, _entry, _journal_schema, _request, MAX_PAYLOAD
    from .worker import load_request

    if type(timeout) not in (int, float) or not 0 < timeout <= 1:
        raise ValueError('Publication reconciliation is limited to one second.')
    deadline = time.monotonic() + timeout

    def expired():
        return time.monotonic() >= deadline

    def check():
        if expired():
            raise TimeoutError('Publication reconciliation time limit reached.')

    root = Path(root).resolve()
    with exclusive_writer(root, timeout=0):
        check()
        request = load_request(root, request_id)
        if request.get('workspace') != workspace or not request.get('asset_sha256'):
            return None
        with _read_database(root / 'library.sqlite', expired) as db:
            if (db.execute('PRAGMA user_version').fetchone()[0] != 4
                    or _schema_signature(db) != _expected_signature(4)):
                raise ValueError('The library schema has changed.')
            LibraryApp._authorize(db, workspace)
            _request(request, request_id, str(root), db)
            metadata = request['metadata']
            edition, asset = metadata['edition_id'], request['asset_sha256']
            row = db.execute('SELECT r.id,r.config,a.mime,a.size FROM revision r '
                'JOIN sealed s ON s.revision=r.id JOIN active v ON v.revision=r.id '
                'JOIN catalog_asset a ON a.digest=r.asset '
                'WHERE r.workspace=? AND r.edition=? AND r.asset=? '
                'AND v.workspace=r.workspace AND v.edition=r.edition',
                (workspace, edition, asset)).fetchone()
            if row is None:
                return None
            catalog = Catalog(db, workspace)
            expected = EditionView(edition, metadata['work_id'], asset,
                metadata['title'], metadata['language'], metadata['edition'],
                metadata['volume'], tuple(metadata['authors']))
            if get_edition(catalog, edition) != expected:
                return None
            for collection in metadata['collections']:
                catalog._active_collection(collection)
                if db.execute('SELECT 1 FROM catalog_membership WHERE workspace=? '
                        'AND collection=? AND edition=?',
                        (workspace, collection, edition)).fetchone() is None:
                    return None
            settings = row['config']
            if (len(settings.encode('utf-8')) > 4 * 1024 * 1024
                    or row['id'] != digest(canonical([workspace, edition, asset, settings]))):
                raise ValueError('Published revision identity changed.')
            config = json.loads(settings)
            if not isinstance(config, dict):
                raise ValueError('Invalid published extraction settings.')
            mime = row['mime']
            adapter = {'text/plain': 'strict-utf8-v1', 'application/pdf': 'page_ocr-v1',
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'python-docx-ordered-v1',
                'application/epub+zip': 'epub-spine-v1'}.get(mime)
            if adapter is None or config.get('adapter') != adapter:
                return None
            if request.get('expected_manifest', dict(sha256=asset, mime=mime, size=row['size'])) != dict(
                    sha256=asset, mime=mime, size=row['size']):
                raise ValueError('Published source differs from the planned manifest.')
            retained = root / 'originals' / (asset + SUFFIXES[mime])
            regular(retained.parent, directory=True)
            if not 0 < regular(retained).st_size == row['size'] <= MAX_BYTES:
                raise ValueError('Retained source size changed.')
            if sum((root / 'originals' / (asset + suffix)).exists() for suffix in SUFFIXES.values()) != 1:
                raise ValueError('Retained source identity is ambiguous.')
            if _entry(retained, retained.name, expired)['sha256'] != asset:
                raise ValueError('Retained source digest changed.')
            check()
            count = db.execute('SELECT count(*) FROM page WHERE revision=?',
                               (row['id'],)).fetchone()[0]
            if not 1 <= count <= (1000 if mime in ('text/plain', 'application/pdf') else 10000):
                raise ValueError('Published source page count is invalid.')

            def verify_pages(journal=None, job=None):
                pages = db.execute('SELECT ordinal,physical,printed,verified,digest,'
                    'CASE WHEN length(CAST(raw AS BLOB))<=? THEN raw END AS raw '
                    'FROM page WHERE revision=? ORDER BY ordinal', (MAX_PAYLOAD, row['id']))
                for ordinal, page in enumerate(pages, 1):
                    check()
                    if (page['ordinal'] != ordinal or not isinstance(page['raw'], str)
                            or digest(page['raw']) != page['digest']):
                        raise ValueError('Published source page integrity changed.')
                    if journal is not None:
                        checkpoint = journal.execute('SELECT ordinal,digest,'
                            'CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload END AS payload '
                            'FROM checkpoint WHERE job=? AND ordinal=?',
                            (MAX_PAYLOAD, row['id'], ordinal)).fetchone()
                        if checkpoint is None:
                            raise ValueError('Published source checkpoint is missing.')
                        _checkpoint(checkpoint, job, ordinal, config['kind'])
                        original = json.loads(checkpoint['payload'])
                        # This column verifies a printed-page label, so it is
                        # always zero for logical TXT segments. The returned
                        # TXT result's verified flag concerns source text.
                        if (page['raw'], page['physical'], page['printed'], page['verified']) != (
                                original['raw_text'], ordinal if mime == 'application/pdf' else None, None, 0):
                            raise ValueError('Publication differs from its source checkpoint.')

            if mime in ('text/plain', 'application/pdf'):
                if request.get('job') != row['id']:
                    return None
                if mime == 'application/pdf':
                    if config.get('kind') != 'physical_page' or config.get('config') != request['config']:
                        return None
                elif config != {'adapter': 'strict-utf8-v1', 'kind': 'logical_segment'}:
                    return None
                with _read_database(root / 'imports.sqlite', expired) as journal:
                    journal.row_factory = None
                    _journal_schema(journal)
                    journal.row_factory = sqlite3.Row
                    job = journal.execute('SELECT * FROM job WHERE id=?', (row['id'],)).fetchone()
                    if (job is None or (job['workspace'], job['edition'], job['asset'], job['config'], job['count']) !=
                            (workspace, edition, asset, settings, count)
                            or journal.execute('SELECT count(*) FROM checkpoint WHERE job=?',
                                               (row['id'],)).fetchone()[0] != count):
                        return None
                    verify_pages(journal, job)
            else:
                provenance = config.get('provenance')
                if (config.get('kind') != 'logical_segment'
                        or not isinstance(provenance, dict)
                        or provenance.get('asset_sha256') != asset):
                    return None
                verify_pages()
            check()
            return dict(state='complete', workspace=workspace, edition=edition,
                        asset=asset, id=row['id'], completed=count, total=count,
                        verified=mime == 'text/plain', error=None,
                        message='Import completed before its worker stopped; the durable source publication was verified.')
