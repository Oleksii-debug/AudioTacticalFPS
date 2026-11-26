"""Bridge authenticated saved anchors to existing export, without cached raw text."""
from dataclasses import dataclass
from ..citations.anchors import Anchor, resolve_span
from .evidence_export import Evidence, export_topic


@dataclass(frozen=True)
class SavedEvidence:
    topic_id: str
    evidence_id: str
    anchor: Anchor
    note: str = ''
    span: tuple[Anchor, ...] = ()
    checksum: str = ''

    @property
    def anchors(self):
        """Ordered source slices; anchor remains a compatible first-page view."""
        return self.span or (self.anchor,)


def export_saved(workspace, topic, rows, load_page, *, format='text'):
    """Use repository-authenticated rows and a scoped historical page loader.

    The caller must supply both within one consistent read transaction. A
    saved anchor is trusted only after repository authentication, including
    its metadata snapshot and topic membership. No active-revision fallback.
    """
    if not all(isinstance(v, str) and v.strip() for v in (workspace, topic)):
        raise ValueError('scope required')
    rows = tuple(rows)
    seen = set()
    for row in rows:
        if (not isinstance(row, SavedEvidence) or not isinstance(row.anchor, Anchor)
                or row.topic_id != topic or row.anchor.identity.workspace != workspace
                or type(row.anchors) is not tuple or row.anchors[0] != row.anchor
                or any(not isinstance(a, Anchor) or a.identity.workspace != workspace
                       for a in row.anchors)
                or not isinstance(row.evidence_id, str) or not row.evidence_id.strip()
                or row.evidence_id in seen or not isinstance(row.note, str)):
            raise ValueError('invalid or cross-scope saved evidence')
        seen.add(row.evidence_id)
    resolved = []
    for row in rows:
        pages = []
        for anchor, raw in resolve_span(row.anchors, load_page):
            i, loc, meta = anchor.identity, anchor.locator, anchor.metadata
            pages.append(Evidence(
                workspace_id=workspace, topic_id=topic, evidence_id=row.evidence_id,
                edition_id=i.edition_id, revision_id=i.revision, asset_sha256=i.asset_sha256,
                raw_text_sha256=anchor.raw_sha256, kind=loc.kind,
                physical_index=loc.index if loc.kind == 'physical_page' else None,
                segment_index=loc.index if loc.kind == 'logical_segment' else None,
                printed_label=loc.printed, printed_label_verified=loc.verified,
                raw_text=raw, start=anchor.start, end=anchor.end, quote=anchor.quote,
                title=meta.title, author=meta.author or '', edition=meta.edition or '',
                volume=meta.volume or '', verified=True, note=row.note))
        resolved.append(pages)
    if format == 'ris' or all(len(pages) == 1 for pages in resolved):
        # RIS is bibliography only. One record per evidence; never invent a
        # printed page range from separate physical source labels.
        return export_topic(workspace, topic, [pages[0] for pages in resolved], format=format)
    if format not in ('text', 'markdown'):
        raise ValueError('unsupported export format')
    chunks = ['# Research evidence' if format == 'markdown' else 'Research evidence']
    for number, pages in enumerate(resolved, 1):
        for position, page in enumerate(pages, 1):
            # Reuse the existing escaping/formatting for each exact source slice.
            # Every part retains the true stored evidence ID and its own locator,
            # offsets and digest. Page text is never concatenated as one quote.
            body = export_topic(workspace, topic, [page], format=format).split('\n\n', 2)[2]
            heading = f'Evidence {number}, source part {position} of {len(pages)}'
            chunks.extend([('## ' if format == 'markdown' else '') + heading, body.rstrip('\n')])
    return '\n\n'.join(chunks) + '\n'
