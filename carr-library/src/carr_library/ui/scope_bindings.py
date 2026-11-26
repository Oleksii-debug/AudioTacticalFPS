"""Compose existing scoped services; host retains authorization and migration.

The host supplies an authorized transaction factory (workspace, topic) which
rechecks permission on each entry and owns commit/rollback. Search/context/copy
and historical attestors must use that same connection, not a separate snapshot.
No production schema, source attestation or retrieval algorithm is defined here.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4


@dataclass(frozen=True)
class ScopeServices:
    workspace: str
    topic: str | None
    search: Callable
    load_context: Callable
    verified_copy: Callable
    catalog_tx: Callable
    collections: Callable
    topic_tx: Callable
    evidence_tx: Callable | None = None
    save_hit: Callable | None = None
    select_collection: Callable | None = None
    preview_search: Callable | None = None


def bind_scope_services(workspace, topic, *, authorized_transaction, search,
                        load_context, verified_copy, load_page, attest_metadata,
                        collection_search=None, preview_search=None):
    """Bind actual catalog, topics and evidence; never cache authorization.

    All host callbacks accept db as their first argument. Source loaders receive
    (identity, locator) or (identity, metadata) after db; the other callbacks
    retain the existing search-panel signatures. Return values must be detached
    from the transaction; search iterators are fully consumed inside it.
    """
    from ..catalog.membership import Catalog
    from ..catalog.read_views import active_collections
    from ..research.topic_tree import TopicTree
    from ..research.saved_evidence import SavedEvidenceStore
    from ..reader.context import Context

    if not isinstance(workspace, str) or not workspace.strip() or (
            topic is not None and (not isinstance(topic, str) or not topic.strip())):
        raise ValueError('explicit scope required')
    if not all(callable(cb) for cb in (authorized_transaction, search, load_context,
                                       verified_copy, load_page, attest_metadata)):
        raise TypeError('authorized transaction and trusted services required')
    if collection_search is not None and not callable(collection_search):
        raise TypeError('collection search service must be callable')
    if preview_search is not None and not callable(preview_search):
        raise TypeError('preview search service must be callable')
    selected_collection = None

    @contextmanager
    def transaction():
        with authorized_transaction(workspace, topic) as db:
            if (not db.in_transaction or not db.execute('PRAGMA foreign_keys').fetchone()[0]
                    or db.execute('PRAGMA read_uncommitted').fetchone()[0]):
                raise ValueError('consistent authorized transaction required')
            yield db
            if not db.in_transaction:
                raise ValueError('service ended caller transaction')

    def check_workspace(requested):
        if requested != workspace:
            raise ValueError('scope mismatch')

    @contextmanager
    def catalog_tx():
        with transaction() as db:
            yield Catalog(db, workspace)

    @contextmanager
    def topic_tx():
        with transaction() as db:
            yield TopicTree(db, workspace)

    @contextmanager
    def evidence_tx():
        if topic is None:
            raise ValueError('select a research topic')
        with transaction() as db:
            def page(identity, locator):
                check_workspace(identity.workspace)
                return load_page(db, identity, locator)

            def metadata(identity, value):
                check_workspace(identity.workspace)
                return attest_metadata(db, identity, value)

            yield SavedEvidenceStore(db, workspace, page, metadata)

    def scoped_search(requested, *args):
        check_workspace(requested)
        with transaction() as db:
            if selected_collection is not None:
                Catalog(db, workspace)._active_collection(selected_collection)
            if collection_search is None:
                rows = tuple(search(db, workspace, *args))
            else:
                rows = tuple(collection_search(db, workspace, *args,
                                               collection=selected_collection))
            if any(row.workspace != workspace for row in rows):
                raise ValueError('foreign search result')
            return rows

    def select_collection(requested, collection):
        nonlocal selected_collection
        check_workspace(requested)
        with transaction() as db:
            if collection is not None:
                Catalog(db, workspace)._active_collection(collection)
        # Never change the selection if authorization or commit failed.
        selected_collection = collection

    def scoped_preview_search(requested, *args):
        check_workspace(requested)
        with transaction() as db:
            if selected_collection is not None:
                Catalog(db, workspace)._active_collection(selected_collection)
            rows = tuple(preview_search(db, workspace, *args, collection=selected_collection))
            if any(row.workspace != workspace for row in rows):
                raise ValueError('foreign preview result')
            return rows

    def scoped_context(requested, *args):
        check_workspace(requested)
        with transaction() as db:
            return load_context(db, workspace, *args)

    def scoped_copy(requested, ref, revision, field):
        check_workspace(requested)
        if field not in ('quote', 'citation'):
            raise ValueError('unknown copy field')
        with transaction() as db:
            return verified_copy(db, workspace, ref, revision, field)

    def save_hit(requested, ref, revision, expected_quote, expected_citation):
        """Resolve and save the displayed hit in one authorized transaction.

        The UI passes identity and visible strings, never a cached source anchor.
        A fresh Context and historical attestations must agree before publication.
        """
        check_workspace(requested)
        if topic is None:
            raise ValueError('select a research topic')
        if any(not isinstance(value, str) or not value for value in
               (ref, revision, expected_quote, expected_citation)):
            raise ValueError('complete displayed hit required')
        evidence_id = uuid4().hex
        with transaction() as db:
            context = load_context(db, workspace, ref, revision)
            if not isinstance(context, Context):
                raise ValueError('verified source context required')
            matching = [page for page in context.pages
                        if page.anchor.identity.workspace == workspace
                        and page.anchor.identity.revision == revision
                        and page.quote == expected_quote
                        and page.citation == expected_citation]
            if len(matching) != 1:
                raise ValueError('displayed source changed or is ambiguous')

            def page(identity, locator):
                check_workspace(identity.workspace)
                return load_page(db, identity, locator)

            def metadata(identity, value):
                check_workspace(identity.workspace)
                return attest_metadata(db, identity, value)

            SavedEvidenceStore(db, workspace, page, metadata).save(
                topic, evidence_id, matching[0].anchor)
        return evidence_id

    # Do not show a new scope until it has been checked in an actual transaction.
    with transaction() as db:
        Catalog(db, workspace)
        if topic is not None and not any(t.id == topic for t in
                TopicTree(db, workspace).list_topics(include_archived=True)):
            raise ValueError('unknown topic')

    return ScopeServices(workspace, topic, scoped_search, scoped_context, scoped_copy,
                         catalog_tx, active_collections, topic_tx,
                         evidence_tx if topic is not None else None,
                         save_hit if topic is not None else None,
                         select_collection if collection_search is not None else None,
                         scoped_preview_search if preview_search is not None else None)
