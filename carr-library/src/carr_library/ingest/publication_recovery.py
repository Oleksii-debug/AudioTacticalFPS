"""Read-only reconciliation after an isolated import loses its final reply.

The existing catalog, retained originals and extraction journal are the evidence.
No new journal is created and recovery never publishes or reactivates a revision.
A revision present before this invocation cannot prove a new replay committed.
"""
from contextlib import contextmanager, closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from .bundle import _plain
from .pipeline import canonical
from .retained_assets import RetainedAssets, SUFFIXES
from ..storage.schema_identity import expected_schema_fingerprint, schema_fingerprint


@contextmanager
def _catalog(root, workspace):
    from ..application import LibraryApp

    path = Path(root).resolve() / 'library.sqlite'
    if _plain(path).st_nlink != 1:
        raise ValueError('Import publication database has unexpected links.')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                 isolation_level=None, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if (db.execute('PRAGMA user_version').fetchone()[0] != 4
                or schema_fingerprint(db) != expected_schema_fingerprint(4)):
            raise ValueError('Import publication database schema changed.')
        LibraryApp._authorize(db, workspace)
        yield db


def published_before(root, request):
    """Capture prior revisions before launching this specific child process."""
    with _catalog(root, request['workspace']) as db:
        return frozenset(row[0] for row in db.execute(
            'SELECT id FROM revision WHERE workspace=? AND edition=?',
            (request['workspace'], request['metadata']['edition_id'])))


def _journal_pages(root, request, revision, pages, mime):
    path = Path(root).resolve() / 'imports.sqlite'
    if _plain(path).st_nlink != 1:
        raise ValueError('Import publication journal has unexpected links.')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                 isolation_level=None, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        job = db.execute('SELECT * FROM job WHERE id=?', (revision['id'],)).fetchone()
        if (request.get('job') != revision['id'] or job is None
                or tuple(job[key] for key in ('workspace', 'edition', 'asset', 'config'))
                != tuple(revision[key] for key in ('workspace', 'edition', 'asset', 'config'))
                or job['count'] != len(pages) or not 1 <= len(pages) <= 1000):
            raise ValueError('Import publication differs from its request-bound journal.')
        settings = json.loads(job['config'])
        if mime == 'application/pdf' and settings.get('config') != request['config']:
            raise ValueError('Import publication configuration differs from its request.')
        checkpoints = db.execute('SELECT ordinal,payload,digest FROM checkpoint '
                                 'WHERE job=? ORDER BY ordinal', (job['id'],)).fetchall()
        if len(checkpoints) != len(pages):
            raise ValueError('Import publication is missing its extraction checkpoints.')
        for checkpoint, page in zip(checkpoints, pages):
            payload = checkpoint['payload']
            if sha256(payload.encode('utf-8')).hexdigest() != checkpoint['digest']:
                raise ValueError('Import publication checkpoint integrity failed.')
            item = json.loads(payload)
            if (not isinstance(item, dict) or not isinstance(item.get('provenance'), dict)
                    or canonical(item) != payload or checkpoint['ordinal'] != page['ordinal']
                    or item.get('raw_text') != page['raw']
                    or item.get('physical_index') != page['physical']
                    or item['provenance'].get('asset_sha256') != revision['asset']):
                raise ValueError('Import publication pages differ from their checkpoints.')


def recover_interrupted(root, initial_request, request, before):
    """Return a verified fresh publication, or None when none was committed.

The caller has already stopped/reaped the child. A prior revision, a journal's
terminal flag or an arbitrary existing book never upgrades a cancelled replay.
"""
    identity = ('version', 'request_id', 'data_dir', 'workspace', 'source',
                'metadata', 'config', 'expected_manifest')
    if any(request.get(key) != initial_request.get(key) for key in identity):
        raise ValueError('Import request identity changed while its worker ran.')
    asset = request.get('asset_sha256')
    if not asset:
        return None
    workspace, metadata = request['workspace'], request['metadata']
    with _catalog(root, workspace) as db:
        revisions = [row for row in db.execute(
            'SELECT r.*,a.revision AS active_revision FROM revision r '
            'JOIN sealed s ON s.revision=r.id LEFT JOIN active a '
            'ON a.workspace=r.workspace AND a.edition=r.edition '
            'WHERE r.workspace=? AND r.edition=? AND r.asset=?',
            (workspace, metadata['edition_id'], asset)) if row['id'] not in before]
        if not revisions:
            return None
        if len(revisions) != 1:
            raise ValueError('Interrupted import has ambiguous new publications.')
        revision = revisions[0]
        expected_id = sha256(canonical([workspace, metadata['edition_id'], asset,
                                       revision['config']]).encode('utf-8')).hexdigest()
        if revision['id'] != expected_id or revision['active_revision'] is None:
            raise ValueError('Interrupted import publication identity is incomplete.')
        row = db.execute('SELECT e.work,e.asset,a.mime,a.size,e.language,e.edition,e.volume,w.title '
            'FROM catalog_edition e JOIN catalog_asset a ON a.digest=e.asset '
            'JOIN catalog_work w ON w.workspace=e.workspace AND w.id=e.work '
            'WHERE e.workspace=? AND e.id=?', (workspace, metadata['edition_id'])).fetchone()
        if row is None:
            raise ValueError('Interrupted import has no matching catalog registration.')
        mime, size = row['mime'], row['size']
        expected = (metadata['work_id'], asset, mime, size, metadata['language'],
                    metadata['edition'], metadata['volume'], metadata['title'])
        authors = [r[0] for r in db.execute('SELECT name FROM catalog_author '
            'WHERE workspace=? AND work=? ORDER BY position', (workspace, metadata['work_id']))]
        if tuple(row) != expected or authors != metadata['authors'] or mime not in SUFFIXES:
            raise ValueError('Interrupted import catalog metadata differs from its request.')
        manifest = dict(sha256=asset, mime=mime, size=size)
        if request.get('expected_manifest', manifest) != manifest:
            raise ValueError('Interrupted import differs from the selected source manifest.')
        for collection in metadata['collections']:
            if db.execute('SELECT 1 FROM catalog_membership m JOIN catalog_collection c '
                'ON c.workspace=m.workspace AND c.id=m.collection WHERE m.workspace=? '
                'AND m.edition=? AND m.collection=? AND c.archived=0',
                (workspace, metadata['edition_id'], collection)).fetchone() is None:
                raise ValueError('Interrupted import is missing its requested collection membership.')
        source = db.execute('SELECT author,title,volume,edition_label FROM source_metadata '
            'WHERE workspace=? AND edition=? AND revision=?',
            (workspace, metadata['edition_id'], revision['id'])).fetchone()
        if source is None or tuple(source) != ('; '.join(metadata['authors']) or None,
                metadata['title'] or 'Unknown title', metadata['volume'], metadata['edition']):
            raise ValueError('Interrupted import source metadata is not request-bound.')
        pages = db.execute('SELECT ordinal,physical,raw,digest FROM page WHERE revision=? '
                           'ORDER BY ordinal', (revision['id'],)).fetchall()
        if not pages or any(page['ordinal'] != i or sha256(page['raw'].encode('utf-8')).hexdigest()
                != page['digest'] for i, page in enumerate(pages, 1)):
            raise ValueError('Interrupted import sealed page integrity failed.')
        retained = RetainedAssets(Path(root).resolve() / 'originals').resolve(asset)
        if retained.suffix != SUFFIXES[mime] or retained.stat().st_size != size:
            raise ValueError('Interrupted import original bytes differ from its catalog.')
        if mime in ('text/plain', 'application/pdf'):
            _journal_pages(root, request, revision, pages, mime)
        if mime == 'text/plain':
            segments = retained.read_bytes().decode('utf-8-sig', errors='strict').split('\f')
            if [page['raw'] for page in pages] != segments:
                raise ValueError('Interrupted text publication differs from the original bytes.')
        state = 'complete' if revision['active_revision'] == revision['id'] else 'superseded'
        return dict(id=revision['id'], workspace=workspace, edition=metadata['edition_id'],
            asset=asset, state=state, completed=len(pages), total=len(pages), error=None,
            verified=mime == 'text/plain' and state == 'complete', recovered_after_termination=True,
            message='The worker ended after the import was committed. Its retained source, '
                    'sealed revision and requested catalog registration were verified.')
