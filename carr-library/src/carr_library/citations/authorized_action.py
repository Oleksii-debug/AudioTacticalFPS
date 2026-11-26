"""Fresh authorization boundary for saved multi-page citation copy actions."""
from dataclasses import dataclass
import re
from typing import Callable

from ..research.span_snapshot import restore_span


@dataclass(frozen=True)
class AuthorizedSelection:
    workspace: str
    topic: str
    evidence: str
    payload: str
    checksum: str
    load_page: Callable
    attest_metadata: Callable


def copy_authorized_span(workspace, topic, evidence, expected_checksum,
                         position, mode, open_read):
    """Return one page's exact text/citation from a fresh authorized read.

    The trusted application service must supply open_read(workspace, topic,
    evidence), a context manager starting a NEW consistent read transaction,
    authorizing active workspace/topic/evidence membership and providing the
    historical source and metadata attestors in that same transaction.
    Missing authorization must yield None or raise. Never pass a cached Context.
    expected_checksum is the displayed immutable snapshot's checksum, not an
    authorization token. Notes may change; changed evidence requires reselection.
    The transaction is the action's authorization point; this is not a promise
    against subsequent revocation or an OS clipboard implementation.
    """
    if not all(type(x) is str and x.strip() for x in (workspace, topic, evidence)):
        raise ValueError('missing selection identity')
    if (type(expected_checksum) is not str or
            not re.fullmatch('[0-9a-f]{64}', expected_checksum)):
        raise ValueError('invalid selection checksum')
    if type(position) is not int or position < 0 or mode not in ('quote', 'citation', 'both'):
        raise ValueError('invalid copy action')
    if not callable(open_read):
        raise ValueError('trusted action reader required')
    with open_read(workspace, topic, evidence) as selection:
        if (type(selection) is not AuthorizedSelection or
                (selection.workspace, selection.topic, selection.evidence) !=
                (workspace, topic, evidence)):
            raise ValueError('selection not authorized')
        if selection.checksum != expected_checksum:
            raise ValueError('selection changed; reopen evidence')
        context = restore_span(workspace, selection.payload, selection.checksum,
                               selection.load_page, selection.attest_metadata)
        result = context.clipboard(workspace, position, mode, selection.load_page)
    return result
