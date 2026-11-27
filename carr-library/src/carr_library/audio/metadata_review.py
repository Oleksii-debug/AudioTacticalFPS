"""One passive metadata review, including explicitly reviewed local artwork.

The record restores editable choices and evidence, never a source path, output
approval, native owner or replay permission. Artwork bytes leave this record
only through an explicit no-clobber export and subsequent fresh review.
"""
from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from hashlib import sha256
import os
from pathlib import Path

from .metadata import (MAX_COVER_BYTES, MetadataChanges, MetadataPlan, _check,
                       _file, _input_stream, _picture, _stamp)
from .models import AudioError
from ..durable_state import atomic_json, encode, local_path, read_json_snapshot, regular, state_lock
from ..research.export_file import _filename, write_binary_export


SIDECAR = 'state/audio-metadata/choices.json'
MAX_BYTES = 12 * 1024**2
SCHEMA = 'carr-audio-metadata-choices/1'


def _fields(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise ValueError('Unknown or missing saved audio metadata fields.')


def _digest(value):
    if (type(value) is not str or len(value) != 64
            or any(char not in '0123456789abcdef' for char in value)):
        raise ValueError('Invalid saved audio metadata digest.')


def _size(value, maximum=2**63 - 1):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError('Invalid saved audio metadata byte count.')


def validate_choices(value):
    """Pure bounded validation; no selected path, engine or image decoder."""
    _fields(value, {'schema', 'review_required', 'source', 'output_name', 'changes', 'cover'})
    if value['schema'] != SCHEMA or value['review_required'] is not True:
        raise ValueError('Saved metadata choices require a fresh source and output review.')
    source = value['source']
    _fields(source, {'name', 'size', 'sha256'})
    _filename(source['name']); _size(source['size']); _digest(source['sha256'])
    _filename(value['output_name'])
    changes = value['changes']
    _fields(changes, {'tags', 'cover_mode'})
    if type(changes['tags']) is not dict:
        raise ValueError('Saved portable tags must be a typed mapping.')
    selected = changes['cover_mode'] == 'replace'
    typed = MetadataChanges(changes['tags'], changes['cover_mode'],
                            'explicit-local-cover-required' if selected else None)
    if dict(typed.tags) != changes['tags']:
        raise ValueError('Saved portable tag choices changed.')
    cover = value['cover']
    if not selected:
        if cover is not None:
            raise ValueError('Only a reviewed replacement may retain artwork bytes.')
        return None
    _fields(cover, {'codec', 'width', 'height', 'size', 'sha256', 'base64'})
    _size(cover['size'], MAX_COVER_BYTES); _digest(cover['sha256'])
    encoded = cover['base64']
    if type(encoded) is not str or len(encoded) != 4 * ((cover['size'] + 2) // 3):
        raise ValueError('Saved cover bytes exceed or differ from their declared bound.')
    try:
        raw = b64decode(encoded, validate=True)
    except (ValueError, Base64Error) as exc:
        raise ValueError('Saved cover encoding is invalid.') from exc
    if (len(raw) != cover['size'] or sha256(raw).hexdigest() != cover['sha256']
            or b64encode(raw).decode('ascii') != encoded):
        raise ValueError('Saved cover bytes differ from their reviewed identity.')
    picture = _picture(cover['codec'], cover['width'], cover['height'], raw)
    if picture.sha256 != cover['sha256'] or picture.size != cover['size']:
        raise ValueError('Saved cover identity is inconsistent.')
    return raw


def _directory(root, *, create=False):
    root = local_path(root, exists=True)
    regular(root, directory=True)
    directory = root / 'state' / 'audio-metadata'
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.path.lexists(directory):
        directory = local_path(directory, exists=True)
        regular(directory, directory=True)
        if not directory.is_relative_to(root):
            raise ValueError('Saved metadata state escaped its selected library.')
    return directory


def load_choices(root):
    directory = _directory(root)
    path = directory / 'choices.json'
    if not os.path.lexists(path):
        return None
    regular(path)
    value = read_json_snapshot(path, max_bytes=MAX_BYTES)[0]
    validate_choices(value)
    return value


def save_choices(root, runtime, plan, *, cancelled=lambda: False):
    """Persist only an actual reviewed plan; this never publishes the audio."""
    if not isinstance(plan, MetadataPlan):
        raise ValueError('Review the current source, metadata and cover before saving choices.')
    _check(cancelled)
    _file(runtime, plan.snapshot.source.path, runtime.budget.max_output_bytes, cancelled,
          expected=plan.snapshot.source)
    cover = None
    if plan.cover_source is not None:
        bound, picture = plan.cover_source, plan.replacement_cover
        before = regular(bound.path)
        if before.st_size != bound.size or not 0 < bound.size <= MAX_COVER_BYTES:
            raise AudioError('The reviewed cover changed before retaining its choices.')
        with _input_stream(bound.path) as stream:
            raw = stream.read(bound.size + 1)
            after_open = os.fstat(stream.fileno())
        _check(cancelled)
        if (_stamp(before) != _stamp(after_open) or _stamp(before) != _stamp(regular(bound.path))
                or len(raw) != bound.size or sha256(raw).hexdigest() != bound.sha256):
            raise AudioError('The reviewed cover changed before retaining its choices.')
        cover = {'codec': picture.codec, 'width': picture.width, 'height': picture.height,
                 'size': bound.size, 'sha256': bound.sha256,
                 'base64': b64encode(raw).decode('ascii')}
    value = {'schema': SCHEMA, 'review_required': True,
        'source': {'name': plan.snapshot.source.path.name, 'size': plan.snapshot.source.size,
                   'sha256': plan.snapshot.source.sha256},
        'output_name': plan.output.name,
        'changes': {'tags': dict(plan.changes.tags), 'cover_mode': plan.changes.cover_mode},
        'cover': cover}
    validate_choices(value)
    if len(encode(value)) > MAX_BYTES:
        raise ValueError('Saved metadata choices exceed their byte allowance.')
    directory = _directory(root, create=True)
    with state_lock(directory, 'choices'):
        _check(cancelled)
        atomic_json(directory / 'choices.json', value, replace=True)
    return value


def export_saved_cover(root, destination, *, cancelled=lambda: False):
    """Explicitly export inert retained artwork; it is not a metadata approval."""
    _check(cancelled)
    value = load_choices(root)
    if value is None or value['cover'] is None:
        raise ValueError('The last saved review has no retained replacement artwork.')
    raw = validate_choices(value)
    suffix = Path(destination).suffix.casefold()
    if suffix not in (('.png',) if value['cover']['codec'] == 'png' else ('.jpg', '.jpeg')):
        raise ValueError('Choose the saved artwork\'s actual PNG or JPEG extension.')
    return write_binary_export(destination, raw, private_root=root, cancelled=cancelled,
                               require_absent=True)
