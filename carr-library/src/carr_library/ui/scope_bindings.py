"""Compose existing scoped services; host retains authorization and migration.

The host supplies an authorized transaction factory (workspace, topic) which
rechecks permission on each entry and owns commit/rollback. Search/context/copy
and historical attestors must use that same connection, not a separate snapshot.
No production schema, source attestation or retrieval algorithm is defined here.
"""
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable
import unicodedata


def scope_labels(rows):
    """Render distinct spoken labels without changing the authorized row order."""
    def key(label):
        # Compare presentation only. Keep stored/displayed names and scoped IDs
        # untouched, including canonically equivalent Unicode and whitespace.
        return ' '.join(unicodedata.normalize('NFC', label).split()).casefold()

    rows = tuple(rows)
    label_counts = Counter(key(label) for _, _, label in rows)
    rendered = set()
    labels = []
    for workspace, topic, label in rows:
        if label_counts[key(label)] > 1:
            kind = 'library only' if topic is None else f'topic {topic}'
            label = f'{label} (library {workspace}; {kind})'
        original, number = label, 1
        while key(label) in rendered:
            number += 1
            label = f'{original} (choice {number})'
        rendered.add(key(label))
        labels.append(label)
    return labels


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


def bind_scope_services(workspace, topic, *, authorized_transaction, search,
                        load_context, verified_copy, load_page, attest_metadata):
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

    if not isinstance(workspace, str) or not workspace.strip() or (
            topic is not None and (not isinstance(topic, str) or not topic.strip())):
        raise ValueError('explicit scope required')
    if not all(callable(cb) for cb in (authorized_transaction, search, load_context,
                                       verified_copy, load_page, attest_metadata)):
        raise TypeError('authorized transaction and trusted services required')

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
            rows = tuple(search(db, workspace, *args))
            if any(row.workspace != workspace for row in rows):
                raise ValueError('foreign search result')
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

    # Do not show a new scope until it has been checked in an actual transaction.
    with transaction() as db:
        Catalog(db, workspace)
        if topic is not None and not any(t.id == topic for t in
                TopicTree(db, workspace).list_topics(include_archived=True)):
            raise ValueError('unknown topic')

    return ScopeServices(workspace, topic, scoped_search, scoped_context, scoped_copy,
                         catalog_tx, active_collections, topic_tx,
                         evidence_tx if topic is not None else None)
