"""Explicit bounded UTF-8 source profile; original bytes stay on disk.

This is a versioned extension of strict TXT intake, not a new source catalog.
The original byte API and strict-utf8-v1 locators retain their exact meaning.
Large text is split at form feeds and, within each field, at fixed Unicode
offsets. Those fragments are unpaginated logical segments, never printed pages.
"""
from dataclasses import dataclass
import codecs
from hashlib import sha256
import json
import os
from pathlib import Path
import re

from ..audio.jobs import file_identity, regular_file, verified_reader

MIB = 1024 * 1024
MAX_BYTES = 128 * MIB
BLOCK_BYTES = 64 * 1024
FRAGMENT_CHARS = 64 * 1024
MAX_FRAGMENTS = 8192
ADAPTER = 'strict-utf8-stream-v1'
SCHEMA = 'carr-streamed-text-profile/1'
_CONTROLS = re.compile(r'[\x00-\x08\x0b\x0e-\x1f]')


def check_cancelled(cancelled):
    if cancelled():
        raise InterruptedError('Streamed text operation cancelled.')


@dataclass(frozen=True)
class TextProfile:
    """Explicit per-source allowance; no caller can remove the finite ceiling.

    The 128 MiB ceiling leaves room for JSON escaping and SQLite pages in the
    existing 512 MiB pending-journal backup budget. It is not a corpus limit.
    """
    maximum_bytes: int = MAX_BYTES

    def __post_init__(self):
        if type(self.maximum_bytes) is not int or not 1 <= self.maximum_bytes <= MAX_BYTES:
            raise ValueError('Streamed UTF-8 text needs a byte limit from 1 through 128 MiB.')

    def to_dict(self):
        return dict(schema=SCHEMA, maximum_bytes=self.maximum_bytes)

    def configuration(self):
        return dict(adapter=ADAPTER, kind='logical_segment', fragment_chars=FRAGMENT_CHARS,
                    maximum_fragments=MAX_FRAGMENTS, maximum_bytes=self.maximum_bytes)


def profile_from_value(value):
    if isinstance(value, TextProfile):
        return value
    if (not isinstance(value, dict) or set(value) != {'schema', 'maximum_bytes'}
            or value['schema'] != SCHEMA):
        raise ValueError('Invalid streamed UTF-8 source profile.')
    return TextProfile(value['maximum_bytes'])


def profile_from_config(value):
    """Dispatch without interpreting unknown or historical adapters as this one."""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or value.get('adapter') != ADAPTER:
        return None
    profile = TextProfile(value.get('maximum_bytes'))
    if value != profile.configuration():
        raise ValueError('Invalid streamed UTF-8 extraction configuration.')
    return profile


@dataclass(frozen=True)
class Fragment:
    text: str
    field: int
    offset: int

    def provenance(self, asset):
        return dict(asset_sha256=asset, method='strict_utf8_stream',
                    field=self.field, offset=self.offset)


@dataclass(frozen=True)
class FragmentIdentity:
    sha256: str
    characters: int
    field: int
    offset: int


@dataclass(frozen=True)
class TextSnapshot:
    digest: str
    size: int
    profile: TextProfile
    identity: tuple
    fragments: tuple[FragmentIdentity, ...]
    change_time: int | None = None
    mime: str = 'text/plain'

    def manifest(self):
        return dict(sha256=self.digest, mime=self.mime, size=self.size)


def _regular(path, profile):
    path = Path(path)
    info = regular_file(path)
    if info.st_nlink != 1 or not 0 < info.st_size <= profile.maximum_bytes:
        raise ValueError('Streamed text must be a nonempty, unaliased file within its selected byte limit.')
    if path.suffix.casefold() != '.txt':
        raise ValueError('The streamed source profile accepts explicit UTF-8 .txt files only.')
    return info


def _decoded(stream, profile, hashed, cancelled):
    decoder = codecs.getincrementaldecoder('utf-8-sig')(errors='strict')
    total, first = 0, True
    while True:
        check_cancelled(cancelled)
        block = stream.read(BLOCK_BYTES)
        if not block:
            tail = decoder.decode(b'', final=True)
            if tail:
                yield tail
            return total
        total += len(block)
        if total > profile.maximum_bytes:
            raise ValueError('Streamed text grew beyond its selected byte limit.')
        if first:
            from ..catalog._filetype.archive import Exe, Pdf, Rtf, Zip
            head = block[:262]
            if (head.startswith(b'\x7fELF') or any(kind().match(head) for kind in (Exe, Pdf, Rtf, Zip))):
                raise ValueError('The selected .txt source contains a different file format.')
            first = False
        hashed.update(block)
        decoded = decoder.decode(block, final=False)
        if _CONTROLS.search(decoded):
            raise ValueError('Binary control character in streamed UTF-8 text.')
        if decoded:
            yield decoded


def _fragments(chunks, cancelled):
    buffer, field, offset = '', 1, 0
    emitted, count, nonempty = False, 0, False

    def checked(text, source_field, start):
        nonlocal count, nonempty
        check_cancelled(cancelled)
        count += 1
        if count > MAX_FRAGMENTS:
            raise ValueError('Streamed UTF-8 text exceeds its logical-fragment limit.')
        nonempty = nonempty or bool(text.strip())
        return Fragment(text, source_field, start)

    for chunk in chunks:
        # Each decoded input block and its split results are bounded. Only one
        # unfinished <=64 Ki-character fragment survives the next read.
        parts = chunk.split('\f')
        for index, part in enumerate(parts):
            while part:
                take = min(len(part), FRAGMENT_CHARS - len(buffer))
                buffer += part[:take]
                part = part[take:]
                if len(buffer) == FRAGMENT_CHARS:
                    yield checked(buffer, field, offset)
                    offset += len(buffer)
                    buffer, emitted = '', True
            if index != len(parts) - 1:
                if buffer or not emitted:
                    yield checked(buffer, field, offset)
                buffer, field, offset, emitted = '', field + 1, 0, False
    if buffer or not emitted:
        yield checked(buffer, field, offset)
    if not nonempty:
        raise ValueError('Streamed UTF-8 text has no non-whitespace content.')


def fragments(path, profile, *, expected_digest=None, expected_size=None,
              expected_identity=None, cancelled=lambda: False, _summary=None):
    """Yield bounded exact fragments, then verify the complete source identity.

    Consumers must exhaust the iterator before accepting a result/publication.
    Saved checkpoints are only candidates until that final whole-byte check.
    The shared native reader detects same-size/timestamp-preserving writes too.
    """
    profile = profile_from_value(profile)
    info = _regular(path, profile)
    identity = file_identity(info)
    if expected_identity is not None and tuple(expected_identity) != identity:
        raise ValueError('The selected streamed source changed before opening.')
    if expected_size is not None and info.st_size != expected_size:
        raise ValueError('Streamed source size differs from its original manifest.')
    hashed = sha256()
    with verified_reader(path, expected_identity=identity) as stream:
        from ..native_file_metadata import stream_change_time
        change_time = stream_change_time(stream)
        yield from _fragments(_decoded(stream, profile, hashed, cancelled), cancelled)
        if stream.tell() != info.st_size or os.fstat(stream.fileno()).st_nlink != 1:
            raise ValueError('The streamed original changed while it was read.')
        actual = hashed.hexdigest()
        if expected_digest is not None and actual != expected_digest:
            raise ValueError('Streamed source checksum differs from its original bytes.')
        check_cancelled(cancelled)
    if _summary is not None:
        _summary.update(digest=actual, size=info.st_size, identity=identity, change_time=change_time)


def inspect_text(path, profile, *, expected_digest=None, expected_size=None,
                 expected_identity=None, cancelled=lambda: False):
    """Keep only bounded fragment hashes/offsets, never a complete decoded text."""
    profile = profile_from_value(profile)
    summary, identities = {}, []
    for fragment in fragments(path, profile, expected_digest=expected_digest,
            expected_size=expected_size, expected_identity=expected_identity,
            cancelled=cancelled, _summary=summary):
        identities.append(FragmentIdentity(sha256(fragment.text.encode('utf-8')).hexdigest(),
                                          len(fragment.text), fragment.field, fragment.offset))
    return TextSnapshot(summary['digest'], summary['size'], profile, summary['identity'],
                        tuple(identities), summary['change_time'])


def catalog_profile(db, asset, mime, size):
    """Large retained bytes need a sealed, typed extraction in this catalog.

    Small legacy assets retain their ordinary byte-oriented retention contract.
    A bare size change in catalog_asset never authorizes a larger source.
    """
    from ..catalog.intake import MAX_BYTES as LEGACY_MAX
    if type(size) is not int or size < 1:
        raise ValueError('Invalid original source size.')
    if size <= LEGACY_MAX:
        return None
    from ..catalog.streaming_pdf import valid_original_size
    if mime == 'application/pdf' and valid_original_size(mime, size):
        return None
    if mime != 'text/plain' or size > MAX_BYTES:
        raise ValueError('The original exceeds its supported format-specific byte limit.')
    selected = None
    for row in db.execute('SELECT DISTINCT r.config FROM revision r JOIN sealed s ON s.revision=r.id WHERE r.asset=?', (asset,)):
        profile = profile_from_config(row[0])
        if profile is not None and size <= profile.maximum_bytes:
            selected = profile
    if selected is None:
        raise ValueError('A large original has no sealed streamed-text extraction profile.')
    return selected
