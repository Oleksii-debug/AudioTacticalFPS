"""Runnable local application composed from the existing CARR services.

The private directory belongs to the current OS user. Workspace IDs isolate
that user's libraries; they are never treated as passwords or access tokens.
Each UI action reopens the shared database and authorizes its scope in the same
transaction used for retrieval, reading, evidence, and copy.
"""
from contextlib import closing, contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from uuid import uuid4

from .catalog.intake import snapshot
from .catalog.membership import Catalog
from .catalog.registration import Registration
from .citations.anchors import Identity, Locator, make_anchor
from .citations.historical import historical_loader
from .citations.text_attestation import strict_text_attestor
from .ingest.catalog_pipeline import CatalogIngest
from .ingest.retained_assets import RetainedAssets
from .ingest.writer_guard import exclusive_writer, _regular_if_present
from .reader.context import open_context
from .reader.source_preview import SourcePreview, open_pdf_preview
from .research.topic_tree import TopicTree
from .storage.attestations import MetadataSnapshots
from .storage.revisions import Storage
from .storage.workspace import WorkspaceStore


def default_data_root():
    from platformdirs import PlatformDirs
    return PlatformDirs('Universal Research Library', appauthor=False,
                        roaming=False, use_site_for_root=False).user_data_path


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class Library:
    def __init__(self, root=None, *, semantic=None):
        self.semantic = semantic
        path = Path(root) if root is not None else default_data_root()
        path = path.expanduser().absolute()
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('library data must be a private local directory')
        self.root = path.resolve(strict=True)
        if os.path.lexists(self.root / 'restore.pending'):
            raise ValueError('library restore is incomplete; choose a new restore destination')
        self.database = self.root / 'library.sqlite'
        self.journal = self.root / 'ingest.sqlite'
        self.originals = self.root / 'originals'
        with exclusive_writer(self.root):
            self.originals.mkdir(mode=0o700, exist_ok=True)
            RetainedAssets(self.originals)
            self._check_files()
            with closing(Storage(self.database, schema_version=3)) as storage:
                storage.check()

    def _check_files(self):
        for path in (self.database, self.journal):
            _regular_if_present(path)
            for suffix in ('-wal', '-shm', '-journal'):
                _regular_if_present(Path(str(path) + suffix))

    @contextmanager
    def storage(self):
        with exclusive_writer(self.root, timeout=5):
            self._check_files()
            with closing(Storage(self.database, schema_version=3)) as storage:
                yield storage

    @contextmanager
    def authorized_transaction(self, workspace, topic=None):
        with self.storage() as storage:
            with storage.transaction():
                WorkspaceStore(storage.db).require_active(workspace)
                if topic is not None:
                    TopicTree(storage.db, workspace)._visible(topic)
                yield storage.db

    def create_workspace(self, name, description='', *, workspace=None):
        workspace = workspace or uuid4().hex
        with self.storage() as storage:
            with storage.transaction():
                return WorkspaceStore(storage.db).create(workspace, name, description)

    def workspaces(self, *, include_archived=False):
        with self.storage() as storage:
            with storage.transaction():
                return WorkspaceStore(storage.db).list_workspaces(include_archived=include_archived)

    def update_workspace(self, workspace, **changes):
        with self.authorized_transaction(workspace) as db:
            return WorkspaceStore(db).update(workspace, **changes)

    def archive_workspace(self, workspace):
        with self.authorized_transaction(workspace) as db:
            WorkspaceStore(db).archive(workspace)

    def create_topic(self, workspace, name, *, parent=None, topic=None):
        topic = topic or uuid4().hex
        with self.authorized_transaction(workspace) as db:
            TopicTree(db, workspace).create(topic, name, parent=parent)
        return topic

    def list_scopes(self):
        with self.storage() as storage:
            with storage.transaction():
                scopes = []
                for workspace in WorkspaceStore(storage.db).list_workspaces():
                    scopes.append((workspace.id, None, workspace.name))
                    for topic in TopicTree(storage.db, workspace.id).list_topics():
                        scopes.append((workspace.id, topic.id,
                                       workspace.name + ' / ' + topic.name))
                return tuple(scopes)

    def import_file(self, workspace, path, *, title=None, authors=(), language=None,
                    edition=None, volume=None, work_id=None, edition_id=None,
                    collections=(), cancelled=lambda: False, max_steps=1001):
        selected = Path(path).absolute()
        asset = snapshot(selected.parent, selected.name)
        record = Registration(work_id or uuid4().hex, edition_id or uuid4().hex,
                              asset.manifest(), title, tuple(authors), language,
                              edition, volume, tuple(collections))
        with self.storage() as storage:
            with storage.transaction():
                WorkspaceStore(storage.db).require_active(workspace)
            retained = RetainedAssets(self.originals)
            source = retained.retain(asset.data, asset.mime, asset.digest)
            with closing(CatalogIngest(self.journal, storage, retained)) as ingest:
                job = ingest.start(workspace, record, source)
                state = ingest.run(workspace, job, cancelled=cancelled, max_steps=max_steps)
        return dict(state, work_id=record.work, edition_id=record.edition_id,
                    citation_status='byte_verified' if asset.mime == 'text/plain'
                    and state['state'] == 'complete' else 'review_required')

    def import_text(self, workspace, text, **metadata):
        if not isinstance(text, str) or not text.strip():
            raise ValueError('original text is required')
        with TemporaryDirectory(prefix='carr-entered-text-') as temporary:
            path = Path(temporary) / 'original.txt'
            path.write_bytes(text.encode('utf-8'))
            return self.import_file(workspace, path, **metadata)

    def resume_import(self, workspace, job, *, cancelled=lambda: False, retry=False):
        with self.storage() as storage:
            with closing(CatalogIngest(self.journal, storage,
                                      RetainedAssets(self.originals))) as ingest:
                return ingest.run(workspace, job, cancelled=cancelled, retry=retry)

    def import_jobs(self, workspace):
        with self.storage() as storage:
            with storage.transaction():
                WorkspaceStore(storage.db).require_active(workspace)
            with closing(CatalogIngest(self.journal, storage,
                                      RetainedAssets(self.originals))) as ingest:
                ids = ingest.ingest.db.execute(
                    'SELECT id FROM job WHERE workspace=? ORDER BY id', (workspace,)).fetchall()
                return tuple(ingest.ingest.inspect(workspace, row[0]) for row in ids)

    def _asset_bytes(self, db, identity):
        WorkspaceStore(db).require_active(identity.workspace)
        row = db.execute('SELECT a.size FROM catalog_edition e JOIN catalog_asset a '
                         'ON a.digest=e.asset WHERE e.workspace=? AND e.id=? AND e.asset=?',
                         (identity.workspace, identity.edition_id, identity.asset_sha256)).fetchone()
        if row is None:
            raise ValueError('source is not authorized in this workspace')
        path = RetainedAssets(self.originals).resolve(identity.asset_sha256)
        with path.open('rb') as source:
            data = source.read(row[0] + 1)
        if len(data) != row[0] or sha256(data).hexdigest() != identity.asset_sha256:
            raise ValueError('retained original changed')
        return data

    def load_page(self, db, identity, locator):
        WorkspaceStore(db).require_active(identity.workspace)
        # Exact UTF-8 bytes provide independent text verification. PDF/OCR does
        # not inherit this authority and stays unavailable for verified copying.
        attest = strict_text_attestor(db, identity.workspace,
                                     lambda source: self._asset_bytes(db, source))
        return historical_loader(db, identity.workspace, attest)(identity, locator)

    def attest_metadata(self, db, identity, metadata):
        WorkspaceStore(db).require_active(identity.workspace)
        return MetadataSnapshots(db, identity.workspace).attest(identity, metadata)

    def load_context(self, db, workspace, reference, revision):
        WorkspaceStore(db).require_active(workspace)
        if not isinstance(reference, str) or len(reference) > 4000:
            raise ValueError('invalid source reference')
        try:
            values = json.loads(reference)
            if (not isinstance(values, list) or len(values) != 5
                    or _canonical(values) != reference):
                raise ValueError('invalid source reference')
            ref_revision, page_id, start, end, collection = values
        except (TypeError, RecursionError) as exc:
            raise ValueError('invalid source reference') from exc
        if (ref_revision != revision or type(page_id) is not int or page_id < 1
                or type(start) is not int or type(end) is not int
                or (collection is not None and not isinstance(collection, str))):
            raise ValueError('source reference changed')
        row = db.execute('SELECT r.edition,r.asset,p.ordinal,p.physical,p.printed,p.verified,p.raw, '
                         'p.digest,r.config,a.mime '
                         'FROM page p JOIN revision r ON r.id=p.revision '
                         'JOIN sealed s ON s.revision=r.id '
                         'JOIN catalog_edition e ON e.workspace=r.workspace AND e.id=r.edition '
                         'AND e.asset=r.asset JOIN catalog_asset a ON a.digest=e.asset '
                         'WHERE r.workspace=? AND r.id=? AND p.id=?',
                         (workspace, revision, page_id)).fetchone()
        if row is None:
            raise ValueError('source unavailable')
        if row[0] not in {entry[0] for entry in Catalog(db, workspace).editions(collection)}:
            raise ValueError('source no longer in selected collection')
        identity = Identity(workspace, row[0], row[1], revision)
        locator = (Locator('logical_segment', row[2]) if row[3] is None else
                   Locator('physical_page', row[3], row[4], bool(row[5])))
        metadata = MetadataSnapshots(db, workspace).load(identity)
        # Native PDF extraction is useful for reading and discovery while its
        # agreement with visible glyphs remains unverified. Choose this explicit
        # path by registered format/configuration, never by catching an integrity
        # failure from the verified-text loader.
        if row[9] == 'application/pdf':
            settings = json.loads(row[8])
            if (not isinstance(settings, dict) or settings.get('adapter') != 'page_ocr-v1'
                    or settings.get('kind') != 'physical_page'):
                raise ValueError('unsupported PDF extraction configuration')
            return open_pdf_preview(workspace, identity, locator, row[6], start, end,
                                    metadata, stored_digest=row[7],
                                    original_bytes=self._asset_bytes(db, identity))
        _, _, raw, verified = self.load_page(db, identity, locator)
        anchor = make_anchor(identity, locator, raw, start, end, metadata,
                             text_verified=verified)
        return open_context(workspace, (anchor,),
                            lambda source, location: self.load_page(db, source, location))

    def verified_copy(self, db, workspace, reference, revision, field):
        context = self.load_context(db, workspace, reference, revision)
        return context.clipboard(workspace, 0, field,
                                 lambda source, location: self.load_page(db, source, location))

    def search_hits(self, db, workspace, query, retrieval_mode='literal',
                    result_mode='short', limit=100, *, collection=None,
                    include_unverified=False):
        from .search.scoped import collection_search
        from .search.thematic import search_active
        from .search.eligibility import eligible_sources
        from .ui.search_model import Hit
        WorkspaceStore(db).require_active(workspace)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('search limit must be an integer between 1 and 100')
        if retrieval_mode not in ('literal', 'thematic') or result_mode not in ('short', 'thematic'):
            raise ValueError('unknown search mode')
        if type(include_unverified) is not bool:
            raise ValueError('explicit preview selection required')
        eligibility = eligible_sources(db, workspace, collection=collection)
        if retrieval_mode == 'literal':
            found = collection_search(db, workspace, query, collection=collection, limit=limit,
                                      eligible_edition_ids=eligibility.edition_to_work)
        elif self.semantic is not None:
            found = self.semantic.search_catalog(db, workspace, query,
                                                  collection=collection, limit=limit)
        else:
            found = search_active(db, workspace, eligibility.edition_to_work, query, limit=limit)
        if include_unverified and len(found) < limit:
            # Explicit discovery mode appends clearly marked PDF previews.
            # They never consume the verified-source result budget, enter the
            # semantic model, or become citation Anchors through this route.
            preview_editions = eligibility.preview_edition_to_work
            remaining = limit - len(found)
            if preview_editions:
                if retrieval_mode == 'literal':
                    previews = collection_search(db, workspace, query, collection=collection,
                        limit=remaining, eligible_edition_ids=preview_editions)
                else:
                    previews = search_active(db, workspace, preview_editions, query, limit=remaining)
                found = [*found, *previews]
        hits = []
        for match in found:
            start, end = match['start'], match['end']
            if result_mode == 'thematic':
                raw = db.execute('SELECT raw FROM page WHERE id=? AND revision=?',
                                 (match['page_id'], match['revision_id'])).fetchone()[0]
                start, end = max(0, start - 500), min(len(raw), end + 500)
            reference = _canonical([match['revision_id'], match['page_id'], start, end, collection])
            context = self.load_context(db, workspace, reference, match['revision_id'])
            page = context.page(0)
            preview = isinstance(context, SourcePreview)
            metadata = page.metadata if preview else page.anchor.metadata
            hits.append(Hit(workspace, reference, match['revision_id'], metadata.title,
                            metadata.author or 'Unknown author', metadata.edition or 'Unknown edition',
                            page.page_label if preview else page.anchor.locator.display(),
                            page.quote, page.citation,
                            citation_verified=not preview,
                            verification_status=page.verification_status if preview else
                            'Verified source text'))
        return tuple(hits)

    def search(self, workspace, query, *, collection=None, retrieval_mode='literal',
               result_mode='short', limit=100, include_unverified=False):
        with self.authorized_transaction(workspace) as db:
            return self.search_hits(db, workspace, query, retrieval_mode, result_mode,
                                    limit, collection=collection, include_unverified=include_unverified)

    def search_status(self, workspace, *, collection=None):
        """Report verified coverage plus the review-required PDF preview subset."""
        from .search.eligibility import eligible_sources
        with self.authorized_transaction(workspace) as db:
            return eligible_sources(db, workspace, collection=collection).status()

    def search_report(self, workspace, query, *, collection=None, retrieval_mode='literal',
                      result_mode='short', limit=100, include_unverified=False):
        """Return hits and eligibility counts from the same authorized snapshot."""
        from .search.eligibility import eligible_sources
        with self.authorized_transaction(workspace) as db:
            status = eligible_sources(db, workspace, collection=collection).status()
            hits = self.search_hits(db, workspace, query, retrieval_mode, result_mode,
                                    limit, collection=collection, include_unverified=include_unverified)
            return dict(hits=hits, status=status)

    def bind_scope(self, workspace, topic=None):
        from .ui.scope_bindings import bind_scope_services
        return bind_scope_services(workspace, topic,
            authorized_transaction=self.authorized_transaction, search=self.search_hits,
            collection_search=self.search_hits, load_context=self.load_context,
            verified_copy=self.verified_copy, load_page=self.load_page,
            attest_metadata=self.attest_metadata,
            preview_search=lambda db, scope, *args, **kwargs: self.search_hits(
                db, scope, *args, **kwargs, include_unverified=True))

    def export_topic(self, workspace, topic, *, format='markdown'):
        from .research.anchored_export import export_saved
        with self.bind_scope(workspace, topic).evidence_tx() as evidence:
            rows = evidence.list_saved(topic)
            return export_saved(workspace, topic, rows, evidence.load_page, format=format)

    def check(self):
        from .ingest.catalog_asset_copy import catalog_inventory
        with self.storage() as storage:
            storage.check()
            with storage.transaction():
                entries = catalog_inventory(storage.db, include_all_workspaces=True)
                originals = RetainedAssets(self.originals)
                for entry in entries:
                    path = originals.resolve(entry.digest)
                    if path.name != entry.filename or path.stat().st_size != entry.size:
                        raise ValueError('original differs from the catalog manifest')
        return {'schema_version': 3, 'integrity': 'ok', 'originals_verified': len(entries)}

    def backup(self, destination, *, include_all_workspaces=False):
        """Back up all published library data and registered original bytes."""
        from .ingest.workspace_bundle import create_workspace_bundle
        with self.storage() as storage:
            return create_workspace_bundle(storage, RetainedAssets(self.originals), destination,
                                           include_all_workspaces=include_all_workspaces)

    @classmethod
    def restore(cls, bundle, destination, expected_manifest_sha256):
        """Restore a verified bundle into a new private library directory."""
        from .ingest.workspace_bundle import restore_workspace_bundle
        path = restore_workspace_bundle(bundle, destination,
                                        expected_manifest_sha256=expected_manifest_sha256)
        return cls(path)
