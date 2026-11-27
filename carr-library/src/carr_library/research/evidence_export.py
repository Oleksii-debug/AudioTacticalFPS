"""Read-only export of immutable, verified evidence snapshots.

Repository handoff must supply historical raw text from the scoped revision,
not active-page text or mutable catalog metadata. No database or file writes.
"""
from dataclasses import dataclass
import csv
import hashlib
import io
import json
import re
import unicodedata
from ..citations.anchors import Locator


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
    span_index: int = 1
    span_count: int = 1


def _check(scope, topic, row):
    if row.workspace_id != scope or row.topic_id != topic:
        raise ValueError("evidence outside selected workspace/topic")
    if not all(isinstance(v, str) and v.strip() for v in (
        scope, topic, row.evidence_id, row.edition_id, row.revision_id
    )):
        raise ValueError("missing identity")
    if row.verified is not True:
        raise ValueError("unverified source")
    if (type(row.span_index) is not int or type(row.span_count) is not int
            or not 1 <= row.span_index <= row.span_count):
        raise ValueError("invalid source slice position")
    if not re.fullmatch(r"[0-9a-f]{64}", row.asset_sha256):
        raise ValueError("invalid asset digest")
    if hashlib.sha256(row.raw_text.encode("utf-8")).hexdigest() != row.raw_text_sha256:
        raise ValueError("raw text drift")
    if (type(row.start) is not int or type(row.end) is not int
            or not 0 <= row.start < row.end <= len(row.raw_text)
            or row.raw_text[row.start:row.end] != row.quote):
        raise ValueError("invalid exact span")
    if (type(row.printed_label_verified) is not bool
            or (row.printed_label is not None and not isinstance(row.printed_label, str))):
        raise ValueError("invalid label state")
    if row.kind == "physical_page":
        if (type(row.physical_index) is not int or row.physical_index < 1
                or row.segment_index is not None):
            raise ValueError("invalid physical locator")
        if row.printed_label_verified and not (row.printed_label and row.printed_label.strip()):
            raise ValueError("missing verified printed label")
    elif row.kind == "logical_segment":
        if (type(row.segment_index) is not int or row.segment_index < 1
                or row.physical_index is not None or row.printed_label is not None
                or row.printed_label_verified):
            raise ValueError("invalid logical locator")
    else:
        raise ValueError("unknown locator kind")
    # Direct export callers must satisfy the same authoritative label boundary
    # as reader citations and persisted pages before any format is rendered.
    Locator(row.kind,
            row.physical_index if row.kind == "physical_page" else row.segment_index,
            row.printed_label, row.printed_label_verified)


def _locator(row):
    if row.kind == "logical_segment":
        return f"Segment {row.segment_index} (unpaginated)"
    printed = row.printed_label if row.printed_label_verified else "unknown"
    return f"Physical page {row.physical_index}; printed page {printed}"


def _records(workspace_id, topic_id, rows):
    if not all(isinstance(v, str) and v.strip() for v in (workspace_id, topic_id)):
        raise ValueError("scope required")
    seen = {}
    for row in rows:
        _check(workspace_id, topic_id, row)
        previous = seen.get(row.evidence_id)
        if previous is None:
            if row.span_index != 1:
                raise ValueError("missing first source slice")
        else:
            if row.span_index != previous.span_index + 1:
                raise ValueError("duplicate evidence ID or source slice")
            stable = ('edition_id', 'revision_id', 'asset_sha256', 'kind',
                      'title', 'author', 'edition', 'volume', 'note', 'span_count')
            if any(getattr(row, key) != getattr(previous, key) for key in stable):
                raise ValueError("inconsistent evidence span")
            index = row.physical_index if row.kind == 'physical_page' else row.segment_index
            prior = (previous.physical_index if previous.kind == 'physical_page'
                     else previous.segment_index)
            if index != prior + 1 or row.start != 0 or previous.end != len(previous.raw_text):
                raise ValueError("noncontiguous evidence span")
        seen[row.evidence_id] = row
        yield row
    if any(row.span_index != row.span_count for row in seen.values()):
        raise ValueError("incomplete evidence span")


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


def _csv(items):
    """Use the standard CSV writer and explicit, reversible JSON text cells.

    Every untrusted text cell starts with a literal JSON quote, so spreadsheets
    cannot interpret a source quotation, note, identifier or title as a formula.
    The ``*_json`` headers and encoding column make that representation explicit;
    json.loads restores the exact original Unicode, including CR/LF and controls.
    """
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(('text_encoding', 'workspace_id_json', 'topic_id_json',
                     'evidence_id_json', 'source_slice', 'source_slices',
                     'edition_id_json', 'revision_id_json', 'asset_sha256_json',
                     'raw_text_sha256_json', 'locator_kind', 'physical_page',
                     'logical_segment', 'printed_page_json', 'printed_page_verified',
                     'start', 'end', 'citation_json', 'quotation_json',
                     'research_note_json', 'title_json', 'author_json',
                     'edition_json', 'volume_json'))
    for row in items:
        writer.writerow(('json-string-v1', _encoded(row.workspace_id),
                         _encoded(row.topic_id), _encoded(row.evidence_id),
                         row.span_index, row.span_count, _encoded(row.edition_id),
                         _encoded(row.revision_id), _encoded(row.asset_sha256),
                         _encoded(row.raw_text_sha256), row.kind,
                         row.physical_index or '', row.segment_index or '',
                         _encoded(row.printed_label if row.printed_label_verified else None),
                         'true' if row.printed_label_verified else 'false',
                         row.start, row.end, _encoded(_citation(row)),
                         _encoded(row.quote), _encoded(row.note), _encoded(row.title),
                         _encoded(row.author), _encoded(row.edition), _encoded(row.volume)))
    return output.getvalue()


def _bibtex_text(value):
    if not isinstance(value, str):
        raise ValueError('invalid BibTeX metadata')
    # BibTeX counts braces even after a backslash. A simple \\{ / \\} escape
    # therefore cannot safely represent an unbalanced literal brace. Fixed
    # text macros keep both the BibTeX grouping and the rendered LaTeX safe.
    escapes = {'\\': r'{\textbackslash}', '{': r'{\textbraceleft}',
               '}': r'{\textbraceright}', '$': r'\$', '&': r'\&',
               '#': r'\#', '%': r'\%', '_': r'\_',
               '^': r'{\textasciicircum}', '~': r'{\textasciitilde}'}
    result = []
    for character in value:
        if character in '\t\r\n\u2028\u2029':
            result.append(' ')
        elif unicodedata.category(character).startswith('C'):
            raise ValueError('unsafe control character in BibTeX metadata')
        else:
            result.append(escapes.get(character, character))
    return ''.join(result)


def _bibtex(workspace, topic, items):
    """Bibliography only: no source quotation or private research note.

    Entry keys derive from scoped IDs; no source string becomes BibTeX syntax.
    Cite-specific page labels belong in the source-locator note, not in the
    publication's pages field. Enumerate them without inventing a page range.
    """
    grouped = {}
    for row in items:
        grouped.setdefault(row.evidence_id, []).append(row)
    chunks = ['% Bibliography only; source quotations and research notes are omitted.',
              '% Metadata line breaks and tabs are represented as spaces.']
    for evidence, slices in grouped.items():
        first = slices[0]
        key = hashlib.sha256(json.dumps([workspace, topic, evidence],
                             ensure_ascii=True, separators=(',', ':')).encode('ascii')).hexdigest()
        fields = [('title', first.title)]
        for name in ('author', 'edition', 'volume'):
            value = getattr(first, name)
            if value:
                fields.append((name, value))
        fields.append(('note', 'Source locator: ' + '; '.join(_locator(row) for row in slices)))
        entry = [f'@misc{{evidence_{key},']
        for name, value in fields:
            safe = _bibtex_text(value)
            # Metadata contains a single immutable author display string, not
            # a structured list of people. Retain it as a literal name rather
            # than interpreting a source's "and" or semicolons as separators.
            if name == 'author':
                safe = '{' + safe + '}'
            entry.append(f'  {name} = {{{safe}}},')
        entry.append('}')
        chunks.append('\n'.join(entry))
    return '\n\n'.join(chunks) + '\n'


def export_topic(workspace_id, topic_id, rows, *, format="text", title="Research dossier"):
    """Render local research; caller selects destination, never from source metadata.

    Text/Markdown and CSV use reversible JSON string encoding for untrusted
    text. CSV identifies those cells using *_json headers. RIS and BibTeX are
    bibliography only; no private notes or quotations are included. BibTeX
    metadata line breaks/tabs become spaces, as disclosed in its fixed header.
    DOCX returns immutable document bytes; HTML returns a self-contained string.
    Their title is descriptive text and never a filename or executable markup.
    """
    items = list(_records(workspace_id, topic_id, rows))
    if format in ('docx', 'html'):
        from .academic_documents import export_document
        return export_document(workspace_id, topic_id, items, format=format, title=title)
    if format == 'csv':
        return _csv(items)
    if format == 'bibtex':
        return _bibtex(workspace_id, topic_id, items)
    if format == "ris":
        from ._vendor_rispy.writer import dumps
        grouped = {}
        for row in items:
            grouped.setdefault(row.evidence_id, []).append(row)
        refs = []
        for slices in grouped.values():
            # A span is one bibliographic reference; each source quote remains
            # a separate slice in text/Markdown, never a fabricated joined quote.
            row = slices[0]
            def single(value):
                if any(unicodedata.category(c).startswith('C') or c in '\u2028\u2029' for c in value):
                    raise ValueError("unsafe multiline RIS metadata")
                return value
            ref = dict(type_of_reference="GEN", id=single(row.evidence_id),
                       title=single(row.title), authors=[single(row.author)] if row.author else [],
                       edition=single(row.edition), volume=single(row.volume))
            # Pinpoint locators are not the publication's start/end pages.
            # Only this generated locator note is exported, never row.note.
            ref['notes'] = [single('Source locator: ' + '; '.join(_locator(part) for part in slices))]
            refs.append(ref)
        return dumps(refs)
    if format not in ("text", "markdown"):
        raise ValueError("unsupported export format")
    chunks = ["# Research evidence" if format == "markdown" else "Research evidence"]
    numbers = {}
    for row in items:
        n = numbers.setdefault(row.evidence_id, len(numbers) + 1)
        heading = f"Evidence {n}"
        if row.span_count > 1:
            heading += f", source slice {row.span_index} of {row.span_count}"
        chunks.append(f"## {heading}" if format == "markdown" else heading)
        fields = (("Citation", _citation(row)), ("Exact quotation", row.quote),
                  ("Research note", row.note), ("Evidence ID", row.evidence_id),
                  ("Edition ID", row.edition_id), ("Revision ID", row.revision_id),
                  ("Asset SHA-256", row.asset_sha256), ("Raw text SHA-256", row.raw_text_sha256),
                  ("Offsets (half-open Unicode)", f"{row.start}:{row.end}"))
        for label, value in fields:
            chunks.append(f"### {label}\n\n{_block(value)}" if format == "markdown"
                          else f"{label}: {_encoded(value)}")
    return "\n\n".join(chunks) + "\n"
