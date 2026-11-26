"""Read-only export of immutable, verified evidence snapshots.

Repository handoff must supply historical raw text from the scoped revision,
not active-page text or mutable catalog metadata. No database or file writes.
"""
from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata


@dataclass(frozen=True)
class Evidence:
    workspace_id: str
    topic_id: str
    evidence_id: str
    edition_id: str
    revision_id: str
    asset_sha256: str
    raw_text_sha256: str
    kind: str
    physical_index: int | None
    segment_index: int | None
    printed_label: str | None
    printed_label_verified: bool
    raw_text: str
    start: int
    end: int
    quote: str
    title: str
    author: str
    edition: str
    verified: bool
    note: str = ""
    volume: str = ""


def _check(scope, topic, row):
    if row.workspace_id != scope or row.topic_id != topic:
        raise ValueError("evidence outside selected workspace/topic")
    if not all(isinstance(v, str) and v for v in (
        scope, topic, row.evidence_id, row.edition_id, row.revision_id
    )):
        raise ValueError("missing identity")
    if row.verified is not True:
        raise ValueError("unverified source")
    if not re.fullmatch(r"[0-9a-f]{64}", row.asset_sha256):
        raise ValueError("invalid asset digest")
    if hashlib.sha256(row.raw_text.encode("utf-8")).hexdigest() != row.raw_text_sha256:
        raise ValueError("raw text drift")
    if (type(row.start) is not int or type(row.end) is not int
            or not 0 <= row.start < row.end <= len(row.raw_text)
            or row.raw_text[row.start:row.end] != row.quote):
        raise ValueError("invalid exact span")
    if type(row.printed_label_verified) is not bool:
        raise ValueError("invalid label state")
    if row.kind == "physical_page":
        if (type(row.physical_index) is not int or row.physical_index < 1
                or row.segment_index is not None):
            raise ValueError("invalid physical locator")
        if row.printed_label_verified and not row.printed_label:
            raise ValueError("missing verified printed label")
    elif row.kind == "logical_segment":
        if (type(row.segment_index) is not int or row.segment_index < 1
                or row.physical_index is not None or row.printed_label is not None
                or row.printed_label_verified):
            raise ValueError("invalid logical locator")
    else:
        raise ValueError("unknown locator kind")


def _locator(row):
    if row.kind == "logical_segment":
        return f"Segment {row.segment_index} (unpaginated)"
    printed = row.printed_label if row.printed_label_verified else "unknown"
    return f"PDF page {row.physical_index}; printed page {printed}"


def _records(workspace_id, topic_id, rows):
    if not workspace_id or not topic_id:
        raise ValueError("scope required")
    seen = set()
    for row in rows:
        _check(workspace_id, topic_id, row)
        if row.evidence_id in seen:
            raise ValueError("duplicate evidence ID")
        seen.add(row.evidence_id)
        yield row


def _citation(row):
    return (f"{row.author or 'Unknown author'}, {row.title or 'Unknown title'}, "
            f"edition {row.edition or 'unknown'}, volume {row.volume or 'unknown'}, "
            f"{_locator(row)}")


def _encoded(value):
    # JSON string syntax protects all line breaks and control characters.
    # Escape Unicode format/control characters too, without changing source data.
    encoded = json.dumps(value, ensure_ascii=False)
    return ''.join(json.dumps(c, ensure_ascii=True)[1:-1]
                   if unicodedata.category(c).startswith('C') or c in '\u2028\u2029'
                   else c for c in encoded)


def _block(value):
    encoded = _encoded(value)
    # A dynamically sized fenced block cannot be closed by untrusted data.
    size = max([2] + [len(x) for x in re.findall(r'`+', encoded)]) + 1
    fence = '`' * size
    return f"{fence}text\n{encoded}\n{fence}"


def export_topic(workspace_id, topic_id, rows, *, format="text"):
    """Return a local string; caller selects destination, never from source metadata.

    Text/Markdown use reversible JSON string encoding for each untrusted field.
    RIS is bibliography only; no private notes or quotes are included.
    """
    items = list(_records(workspace_id, topic_id, rows))
    if format == "ris":
        from ._vendor_rispy.writer import dumps
        refs = []
        for row in items:
            def single(value):
                if any(unicodedata.category(c).startswith('C') or c in '\u2028\u2029' for c in value):
                    raise ValueError("unsafe multiline RIS metadata")
                return value
            ref = dict(type_of_reference="GEN", id=single(row.evidence_id),
                       title=single(row.title), authors=[single(row.author)] if row.author else [],
                       edition=single(row.edition), volume=single(row.volume))
            if row.printed_label_verified:
                ref['start_page'] = single(row.printed_label)
            refs.append(ref)
        return dumps(refs)
    if format not in ("text", "markdown"):
        raise ValueError("unsupported export format")
    chunks = ["# Research evidence" if format == "markdown" else "Research evidence"]
    for n, row in enumerate(items, 1):
        chunks.append(f"## Evidence {n}" if format == "markdown" else f"Evidence {n}")
        fields = (("Citation", _citation(row)), ("Exact quotation", row.quote),
                  ("Research note", row.note), ("Evidence ID", row.evidence_id),
                  ("Edition ID", row.edition_id), ("Revision ID", row.revision_id),
                  ("Asset SHA-256", row.asset_sha256), ("Raw text SHA-256", row.raw_text_sha256),
                  ("Offsets (half-open Unicode)", f"{row.start}:{row.end}"))
        for label, value in fields:
            chunks.append(f"### {label}\n\n{_block(value)}" if format == "markdown"
                          else f"{label}: {_encoded(value)}")
    return "\n\n".join(chunks) + "\n"
