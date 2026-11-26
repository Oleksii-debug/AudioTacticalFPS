"""Versioned multi-page evidence payload; no schema migration or implicit write."""
from dataclasses import asdict
from itertools import islice
import json

from ..citations.anchors import Anchor, Identity, Locator, Metadata, digest
from ..reader.context import open_context

MAX_PAGES = 64
MAX_PAYLOAD = 2_000_000


def _encode(anchors):
    payload = json.dumps({'version': 1, 'anchors': [asdict(a) for a in anchors]},
                         ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if len(payload.encode('utf-8')) > MAX_PAYLOAD:
        raise ValueError('evidence snapshot too large')
    return payload


def _verify(workspace, anchors, load_page, attest_metadata):
    if not callable(attest_metadata) or not callable(load_page):
        raise ValueError('trusted readers required')
    if not 1 <= len(anchors) <= MAX_PAGES:
        raise ValueError('invalid span size')
    if any(not isinstance(a, Anchor) or a.identity.workspace != workspace for a in anchors):
        raise ValueError('cross-workspace or invalid evidence')
    if any(attest_metadata(a.identity, a.metadata) is not True for a in anchors):
        raise ValueError('unattested metadata')
    return open_context(workspace, anchors, load_page)


def seal_span(workspace, anchors, load_page, attest_metadata):
    """Return payload/checksum for caller's authorized topic transaction.

    Notes and topic membership are separate mutable/repository-managed fields.
    The checksum detects accidental corruption, not hostile DB rewriting.
    """
    anchors = tuple(islice(anchors, MAX_PAGES + 1))
    _verify(workspace, anchors, load_page, attest_metadata)
    payload = _encode(anchors)
    return payload, digest(payload)


def restore_span(workspace, payload, checksum, load_page, attest_metadata):
    """Return verified Context; no active-revision fallback or joined quote.

    Caller must independently authorize topic and record membership and supply
    historical readers within one consistent read transaction.
    """
    if not isinstance(payload, str) or len(payload) > MAX_PAYLOAD:
        raise ValueError('invalid evidence snapshot size')
    if len(payload.encode('utf-8')) > MAX_PAYLOAD or digest(payload) != checksum:
        raise ValueError('evidence snapshot drift')
    try:
        data = json.loads(payload)
        if type(data['version']) is not int or data['version'] != 1:
            raise ValueError('unsupported evidence snapshot version')
        if not isinstance(data['anchors'], list) or not 1 <= len(data['anchors']) <= MAX_PAGES:
            raise ValueError('invalid span size')
        anchors = []
        for item in data['anchors']:
            item = dict(item)
            item['identity'] = Identity(**item['identity'])
            item['locator'] = Locator(**item['locator'])
            item['metadata'] = Metadata(**item['metadata'])
            anchors.append(Anchor(**item))
        if _encode(anchors) != payload:
            raise ValueError('noncanonical evidence snapshot')
    except (KeyError, TypeError, RecursionError, AttributeError) as exc:
        raise ValueError('invalid evidence snapshot') from exc
    return _verify(workspace, tuple(anchors), load_page, attest_metadata)
