"""Versioned multi-page evidence payload; no schema migration or implicit write."""
from dataclasses import asdict
from itertools import islice
import json
import re

from ..citations.anchors import Anchor, Identity, Locator, Metadata, digest, validate_span
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


def decode_saved_span(payload, checksum):
    """Validate stored bytes and decode version 1 or canonical legacy evidence.

    This is an integrity codec, not an authorization or source attestation. The
    original checksum is checked before parsing. Legacy bytes are never written
    back or replaced with a normalized checksum; fresh copy still binds the
    displayed checksum to the exact selected database record.
    """
    if not isinstance(payload, str) or len(payload) > MAX_PAYLOAD:
        raise ValueError('invalid evidence snapshot size')
    try:
        if len(payload.encode('utf-8')) > MAX_PAYLOAD or digest(payload) != checksum:
            raise ValueError('evidence snapshot drift')
        data = json.loads(payload)
        if type(data) is not dict:
            raise ValueError('invalid evidence snapshot')
        legacy = 'version' not in data
        if legacy:
            items = [data]
        else:
            if type(data['version']) is not int or data['version'] != 1:
                raise ValueError('unsupported evidence snapshot version')
            items = data['anchors']
            if type(items) is not list or not 1 <= len(items) <= MAX_PAGES:
                raise ValueError('invalid span size')
        anchors = []
        for item in items:
            item = dict(item)
            item['identity'] = Identity(**item['identity'])
            item['locator'] = Locator(**item['locator'])
            item['metadata'] = Metadata(**item['metadata'])
            anchor = Anchor(**item)
            if (type(anchor.raw_length) is not int or type(anchor.start) is not int
                    or type(anchor.end) is not int
                    or not 0 <= anchor.start < anchor.end <= anchor.raw_length
                    or type(anchor.quote) is not str
                    or len(anchor.quote) != anchor.end - anchor.start
                    or type(anchor.raw_sha256) is not str
                    or not re.fullmatch('[0-9a-f]{64}', anchor.raw_sha256)):
                raise ValueError('invalid evidence anchor')
            anchors.append(anchor)
        canonical = (json.dumps(asdict(anchors[0]), ensure_ascii=False,
                                sort_keys=True, separators=(',', ':'))
                     if legacy else _encode(anchors))
        if canonical != payload:
            raise ValueError('noncanonical evidence snapshot')
    except (KeyError, TypeError, RecursionError, AttributeError, UnicodeError) as exc:
        raise ValueError('invalid evidence snapshot') from exc
    return validate_span(anchors)


def restore_span(workspace, payload, checksum, load_page, attest_metadata):
    """Return a verified Context from either supported immutable payload format.

    Caller independently authorizes topic and record membership and supplies
    historical readers within one consistent read transaction. Neither the
    decoder nor checksum substitutes for those source/metadata attestations.
    """
    return _verify(workspace, decode_saved_span(payload, checksum),
                   load_page, attest_metadata)
