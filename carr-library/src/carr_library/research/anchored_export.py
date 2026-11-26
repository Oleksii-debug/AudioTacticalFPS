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
        """Ordered source slices; ``anchor`` remains the first-page convenience."""
        return self.span or (self.anchor,)


def export_saved(workspace, topic, rows, load_page, *, format='text', title='Research dossier'):
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
                or not isinstance(row.evidence_id, str) or not row.evidence_id.strip()
                or row.evidence_id in seen or not isinstance(row.note, str)
                or type(row.span) is not tuple or row.anchors[0] != row.anchor
                or any(not isinstance(a, Anchor) or a.identity.workspace != workspace
                       for a in row.anchors)):
            raise ValueError('invalid or cross-scope saved evidence')
        seen.add(row.evidence_id)
    resolved = []
    for row in rows:
        pairs = resolve_span(row.anchors, load_page)
        for position, (anchor, raw) in enumerate(pairs, 1):
            i, loc, meta = anchor.identity, anchor.locator, anchor.metadata
            resolved.append(Evidence(
                workspace_id=workspace, topic_id=topic, evidence_id=row.evidence_id,
                edition_id=i.edition_id, revision_id=i.revision, asset_sha256=i.asset_sha256,
                raw_text_sha256=anchor.raw_sha256, kind=loc.kind,
                physical_index=loc.index if loc.kind == 'physical_page' else None,
                segment_index=loc.index if loc.kind == 'logical_segment' else None,
                printed_label=loc.printed, printed_label_verified=loc.verified,
                raw_text=raw, start=anchor.start, end=anchor.end, quote=anchor.quote,
                title=meta.title, author=meta.author or '', edition=meta.edition or '',
                volume=meta.volume or '', verified=True, note=row.note,
                span_index=position, span_count=len(pairs)))
    return export_topic(workspace, topic, resolved, format=format, title=title)
