"""Independent byte-to-text verification for the existing strict UTF-8 adapter."""
from hashlib import sha256
import re

from .anchors import Identity, Locator, digest
from ..catalog.intake import FileSnapshot, text_segments, source_limit

# Exact canonical extraction configuration from CARR-10 PR31.
CONFIG = '{"adapter":"strict-utf8-v1","kind":"logical_segment"}'
MAX_BYTES = 64 * 1024 * 1024
MAX_SEGMENTS = 1000


def strict_text_attestor(db, workspace, resolve_asset, *, cancelled=lambda: False):
    """Return a callback for historical_loader, never approve PDF or OCR here.

    resolve_asset(identity) must return bounded immutable original bytes from an
    authorized local catalog/asset snapshot (not a user-supplied path or a text
    cache). Caller authenticates metadata and topic membership and owns one
    consistent read transaction. No network, writes, or mutable cache here.
    """
    if not isinstance(workspace, str) or not workspace.strip() or not callable(resolve_asset):
        raise ValueError('workspace and authorized asset resolver required')

    def attest(identity, locator, raw_sha256):
        if not db.in_transaction or db.execute('PRAGMA read_uncommitted').fetchone()[0]:
            return False
        if (not isinstance(identity, Identity) or identity.workspace != workspace
                or not isinstance(locator, Locator) or locator.kind != 'logical_segment'
                or locator.index > 2 ** 31 - 1):
            return False
        row = db.execute('''SELECT r.config, (SELECT count(*) FROM page p WHERE p.revision=r.id)
            FROM revision r JOIN sealed s ON s.revision=r.id
            WHERE r.workspace=? AND r.edition=? AND r.asset=? AND r.id=?''',
            (workspace, identity.edition_id, identity.asset_sha256, identity.revision)).fetchone()
        if row is None or row[0] != CONFIG or not 1 <= row[1] <= 2 ** 31 - 1:
            return False
        try:
            data = resolve_asset(identity)
        except InterruptedError:
            raise
        except (OSError, LookupError, ValueError):
            return False
        if isinstance(data, FileSnapshot):
            if (data.mime != 'text/plain' or data.digest != identity.asset_sha256
                    or not 0 < data.size <= source_limit(data.mime, data.size)):
                return False
            try:
                selected, count, nonempty = None, 0, False
                for count, segment in enumerate(text_segments(data, cancelled=cancelled,
                        max_segments=row[1], max_segment_bytes=MAX_BYTES), 1):
                    nonempty = nonempty or bool(segment.strip())
                    if count == locator.index:
                        selected = digest(segment)
                return count == row[1] and nonempty and selected == raw_sha256
            except InterruptedError:
                raise
            except (OSError, LookupError, ValueError, UnicodeError):
                return False
        if (row[1] > MAX_SEGMENTS or type(data) is not bytes or not 0 < len(data) <= MAX_BYTES
                or sha256(data).hexdigest() != identity.asset_sha256):
            return False
        try:
            text = data.decode('utf-8-sig', errors='strict')
        except UnicodeError:
            return False
        # Match the existing intake/ingest contract, including exact CRLF and form feeds.
        if re.search(r'[\x00-\x08\x0b\x0e-\x1f]', text) or not text.strip():
            return False
        segments = text.split('\f', MAX_SEGMENTS)
        if len(segments) != row[1] or not 1 <= locator.index <= len(segments):
            return False
        return digest(segments[locator.index - 1]) == raw_sha256

    return attest
