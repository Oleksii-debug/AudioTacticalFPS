"""Citation boundary: exact Unicode offsets and immutable metadata snapshots."""
from dataclasses import dataclass
from hashlib import sha256
import re
import unicodedata


def digest(text):
    return sha256(text.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class Locator:
    kind: str
    index: int
    printed: str | None = None
    verified: bool = False

    def __post_init__(self):
        if self.kind not in ('physical_page', 'logical_segment') or type(self.index) is not int or self.index < 1:
            raise ValueError('invalid locator')
        if type(self.verified) is not bool or (self.printed is not None and not isinstance(self.printed, str)):
            raise ValueError('invalid label state')
        if self.printed is not None:
            if '\x00' in self.printed:
                raise ValueError('invalid printed label')
            try:
                self.printed.encode('utf-8', errors='strict')
            except UnicodeError as exc:
                raise ValueError('invalid printed label') from exc
        if self.verified and self.printed is not None and any(
                unicodedata.category(ch) in ('Cc', 'Zl', 'Zp') or
                ch in '\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069'
                for ch in self.printed):
            raise ValueError('invalid printed label')
        if self.kind == 'logical_segment' and (self.printed is not None or self.verified):
            raise ValueError('unpaginated source cannot have printed page')
        if self.verified and not (isinstance(self.printed, str) and self.printed.strip()):
            raise ValueError('verified label required')

    def display(self):
        if self.kind == 'logical_segment':
            return f'Segment {self.index} (unpaginated)'
        if self.verified:
            return f'p. {self.printed} (physical page {self.index})'
        return f'Physical page {self.index} (printed page unverified)'


def pdf_label_candidates(reader):
    """Reuse pypdf's label decoder; metadata is not proof of printed pagination."""
    count = len(reader.pages)
    if '/PageLabels' not in reader.root_object:
        return tuple(Locator('physical_page', i) for i in range(1, count + 1))
    labels = reader.page_labels
    if len(labels) != count:
        raise ValueError('page label count mismatch')
    return tuple(Locator('physical_page', i, label or None) for i, label in enumerate(labels, 1))


@dataclass(frozen=True)
class Identity:
    workspace: str
    edition_id: str
    asset_sha256: str
    revision: str

    def __post_init__(self):
        if not all(isinstance(v, str) and v.strip() for v in (self.workspace, self.edition_id, self.revision)):
            raise ValueError('missing source identity')
        if not re.fullmatch('[0-9a-f]{64}', self.asset_sha256):
            raise ValueError('invalid asset digest')


@dataclass(frozen=True)
class Metadata:
    author: str | None
    title: str
    volume: str | None = None
    edition: str | None = None


@dataclass(frozen=True)
class Anchor:
    identity: Identity
    locator: Locator
    raw_sha256: str
    raw_length: int
    start: int
    end: int
    quote: str
    metadata: Metadata


def make_anchor(identity, locator, raw, start, end, metadata, *, text_verified):
    if text_verified is not True:
        raise ValueError('unverified extraction')
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(raw):
        raise ValueError('invalid source offsets')
    return Anchor(identity, locator, digest(raw), len(raw), start, end, raw[start:end], metadata)


def resolve(anchor, identity, locator, raw):
    """Caller loads the authorized exact sealed revision, not the current page."""
    if anchor.identity != identity or anchor.locator != locator:
        raise ValueError('source identity or locator mismatch')
    rebuilt = make_anchor(identity, locator, raw, anchor.start, anchor.end,
                          anchor.metadata, text_verified=True)
    if rebuilt != anchor:
        raise ValueError('source digest or quotation mismatch')
    return raw


def validate_span(anchors):
    """Cross-page evidence is an ordered tuple of exact slices, never joined as a raw quote."""
    anchors = tuple(anchors)
    if not anchors:
        raise ValueError('empty span')
    for a, b in zip(anchors, anchors[1:]):
        if (a.identity != b.identity or a.metadata != b.metadata or
                a.locator.kind != b.locator.kind or b.locator.index != a.locator.index + 1 or
                a.end != a.raw_length or b.start != 0):
            raise ValueError('non-contiguous or mixed source span')
    return tuple(anchors)


def format_citation(anchor):
    m = anchor.metadata
    fields = [m.author or 'Unknown author', m.title or 'Unknown title']
    if m.volume:
        fields.append(f'vol. {m.volume}')
    if m.edition:
        fields.append(f'edition {m.edition}')
    return ', '.join([*fields, anchor.locator.display()])


@dataclass(frozen=True)
class LabelCorrection:
    """An explicit proposed map revision; persist append-only in storage, not in place."""
    identity: Identity
    previous: Locator
    replacement: Locator
    reason: str
    reviewer: str
    evidence_sha256: str

    def __post_init__(self):
        if (self.previous.kind != 'physical_page' or self.replacement.kind != 'physical_page'
                or self.previous.index != self.replacement.index):
            raise ValueError('label correction cannot change physical source')
        if self.previous == self.replacement:
            raise ValueError('no label change')
        if not all(isinstance(v, str) and v.strip() for v in (self.reason, self.reviewer)):
            raise ValueError('manual verification requires provenance')
        if not isinstance(self.evidence_sha256, str) or not re.fullmatch('[0-9a-f]{64}', self.evidence_sha256):
            raise ValueError('invalid verification evidence digest')


def resolve_span(anchors, load_page):
    """Resolve every page through caller's authorized historical source reader.

    load_page(identity, locator) -> (identity, locator, raw_text, text_verified).
    Returns separate (anchor, raw context) pairs; no artificial joining of quotations.
    """
    pairs = []
    for a in validate_span(anchors):
        identity, locator, raw, verified = load_page(a.identity, a.locator)
        if verified is not True:
            raise ValueError('unverified extraction')
        pairs.append((a, resolve(a, identity, locator, raw)))
    return tuple(pairs)


def format_span_citation(anchors, load_page):
    """Format only fully resolved pages, enumerating labels rather than inventing ranges."""
    pairs = resolve_span(anchors, load_page)
    first = pairs[0][0]
    if len(pairs) == 1:
        return format_citation(first)
    # Reuse the single-page formatter, retaining its exact metadata prefix.
    prefix = format_citation(first).removesuffix(first.locator.display())
    return prefix + '; '.join(a.locator.display() for a, _ in pairs)
