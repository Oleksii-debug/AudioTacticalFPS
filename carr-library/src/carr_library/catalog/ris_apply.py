"""Review selected RIS hints before an existing atomic catalog correction.

RIS identifiers are unverified bibliographic hints. They never establish a
source, edition, page, quotation or attachment identity.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import stat

from .read_views import EditionView
from .ris_preview import MAX_BYTES, RisHint, preview_ris


FIELDS = ('title', 'authors', 'language', 'edition', 'volume')
_HINT_FIELDS = dict(title='titles', authors='authors', language='languages',
                    edition='editions', volume='volumes')


@dataclass(frozen=True)
class RisReview:
    library_root: Path
    workspace: str
    metadata: EditionView
    record: RisHint


def _check(cancelled):
    if cancelled():
        raise TimeoutError('Bibliography import cancelled. No metadata was applied.')


def read_ris_file(path, *, cancelled=lambda: False):
    """Read at most the existing preview limit; never follow attachment hints."""
    _check(cancelled)
    path = Path(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('Choose a regular local RIS file, not a link or device.')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError('Choose a regular local RIS file.')
        data = source.read(MAX_BYTES + 1)
    _check(cancelled)
    try:
        records = preview_ris(data)
    except UnicodeError as exc:
        raise ValueError('RIS must be a UTF-8 text file. No metadata was applied.') from exc
    _check(cancelled)
    return records


def prepare_ris_review(app, workspace, edition_id, record, *, cancelled=lambda: False):
    """Bind a record to the actual selected edition and its current work."""
    _check(cancelled)
    if not isinstance(record, RisHint) or record.metadata_verified is not False:
        raise ValueError('Choose an unverified record from the RIS preview.')
    current = app.edition_metadata(workspace, edition_id)
    with app.read(workspace) as db:
        if not db.execute('SELECT 1 FROM active WHERE workspace=? AND edition=?',
                          (workspace, edition_id)).fetchone():
            raise ValueError('Select an imported edition in this library.')
    _check(cancelled)
    return RisReview(app.root, workspace, current, record)


def selected_metadata(review, selected):
    """Keep omitted fields exactly; imported fields require explicit selection.

    Single-value fields must choose one displayed RIS candidate. Authors retain
    their original order, including repeated names. Empty or absent hints cannot
    clear existing catalog metadata.
    """
    if not isinstance(review, RisReview) or not isinstance(review.metadata, EditionView):
        raise ValueError('Review an existing edition first.')
    if not isinstance(review.record, RisHint) or review.record.metadata_verified is not False:
        raise ValueError('RIS metadata must be shown as unverified.')
    if not isinstance(selected, dict) or not selected or set(selected) - set(FIELDS):
        raise ValueError('Select at least one supported RIS field to apply.')
    values = {key: getattr(review.metadata, key) for key in FIELDS}
    for key, value in selected.items():
        candidates = getattr(review.record, _HINT_FIELDS[key])
        if not candidates:
            raise ValueError(f'The RIS record has no {key}; its existing value is preserved.')
        if key == 'authors':
            if not isinstance(value, (tuple, list)) or tuple(value) != candidates:
                raise ValueError('Review the complete ordered RIS author list.')
            if len(value) > 100 or any(not name.strip() or len(name) > 512 or '\x00' in name
                                      for name in value):
                raise ValueError('RIS authors exceed the catalog limits.')
            value = tuple(value)
        elif not isinstance(value, str) or value not in candidates or not value.strip():
            raise ValueError(f'Choose a non-empty {key} shown in the RIS preview.')
        elif len(value) > (512 if key == 'title' else 4000) or '\x00' in value:
            raise ValueError(f'RIS {key} exceeds the catalog limits.')
        values[key] = value
    if values == {key: getattr(review.metadata, key) for key in FIELDS}:
        raise ValueError('The selected RIS values already match this edition. Nothing was changed.')
    return values


def apply_ris_metadata(app, workspace, review, selected, *, cancelled=lambda: False):
    """Revalidate scope and use the application's optimistic atomic update.

    Cancellation is checked before publication. Once its single transaction
    starts, the existing service commits all metadata or rolls it all back.
    """
    _check(cancelled)
    values = selected_metadata(review, selected)
    if app.root != review.library_root or workspace != review.workspace:
        raise ValueError('The selected library changed. Review the RIS record again.')
    current = app.edition_metadata(workspace, review.metadata.edition_id)
    if current != review.metadata:
        raise ValueError('Book metadata changed. Review the current edition before applying RIS.')
    _check(cancelled)
    return app.update_metadata(workspace, current.edition_id,
                               expected_metadata=review.metadata,
                               selected_fields=tuple(selected), **values)
