"""Typed audio queue state for the existing complete-library backup.

The adapter never opens an executable or an old external input/output path.
Only complete immutable source captures, typed controls, verified output
receipts, and existing managed Studio children are retained. Explicit pending
scratch is derived work; it is never interpreted as a completed item.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re

from .backup_files import local_path, regular, optional_present
from .durable_state import state_lock
from .audio.batch_models import (BatchState, BatchControl, MAX_INPUT_BYTES,
    MAX_STATE_BYTES, MAX_ITEMS, canonical, receipt_value)
from .audio.batches import DIRECTORY, MAX_BATCHES, document_plan, child_document, AudioBatchService
from .audio.jobs import read_json, check_cancelled, sha256_file
from .audio.ffmpeg import wav_info
from .audio.managed import ManagedProjects

_ID = re.compile(r'[0-9a-f]{32}\Z')
_PENDING_CREATE = re.compile(r'\.pending-create-[A-Za-z0-9_-]+\Z')
_PENDING_ITEM = re.compile(r'\.pending-item-([0-9a-f]{32})-[A-Za-z0-9_-]+\Z')
_CHECKPOINT = re.compile(r'\.checkpoint-[A-Za-z0-9_-]+\Z')
MAX_FILES = 100000
MAX_OUTPUT_BYTES = 32 * 1024**3


def state_path(value):
    if type(value) is not str or '\\' in value:
        return False
    parts = value.split('/')
    if len(parts) < 3 or parts[0] != DIRECTORY or _ID.fullmatch(parts[1]) is None:
        return False
    if parts[2:] in (['batch.json'], ['control.json']):
        return True
    if len(parts) == 4 and parts[2] == 'sources':
        return bool(re.fullmatch(r'[0-9a-f]{32}(?:-[0-9a-f]{32})?\.(?:studio\.json|wav|flac|mp3|m4a|m4b|aac|aif|aiff|ogg|opus|wma|mp4|m4v|mov|mkv|webm|avi)', parts[3]))
    if len(parts) == 5 and parts[2] == 'outputs' and _ID.fullmatch(parts[3]):
        from .audio.paths import filename_component
        try:
            filename_component(parts[4])
        except ValueError:
            return False
        return (parts[4] == 'receipt.json'
                or bool(re.fullmatch(r'[0-9]{4}-.+-[0-9a-f]{32}(?:-chapter-[0-9]{4,5})?\.(?:wav|flac|mp3|opus|m4a|m4b|aiff|ogg|aac|wma)', parts[4])))
    return False


def state_limit(value):
    if not state_path(value):
        raise ValueError('Invalid audio queue backup member.')
    if value.endswith('.json'):
        return MAX_STATE_BYTES
    return MAX_INPUT_BYTES if '/sources/' in value else MAX_OUTPUT_BYTES


def _children(path, *, limit):
    regular(path, directory=True)
    result = []
    for index, child in enumerate(path.iterdir()):
        if index >= limit:
            raise ValueError('Audio queue inventory exceeds its item allowance.')
        result.append(child)
    return tuple(sorted(result))


def _batch_directories(root):
    base = Path(root) / DIRECTORY
    if not optional_present(base):
        return ()
    local_path(base, root=root, exists=True)
    result = []
    for child in _children(base, limit=MAX_BATCHES * 2 + 1):
        if child.name == '.batch.lock':
            regular(child)
        elif _PENDING_CREATE.fullmatch(child.name):
            regular(child, directory=True)
        elif _ID.fullmatch(child.name):
            regular(child, directory=True)
            result.append(child)
        else:
            raise ValueError('Unknown audio queue state must not be silently omitted.')
    if len(result) > MAX_BATCHES:
        raise ValueError('The audio queue count exceeds its backup allowance.')
    return tuple(result)


def _batch(path):
    value = BatchState.from_dict(read_json(path / 'batch.json', limit=MAX_STATE_BYTES))
    if value.id != path.name:
        raise ValueError('Audio queue identity differs from its owned folder.')
    control = BatchControl.from_dict(read_json(path / 'control.json', limit=8192))
    if control.batch_id != value.id or control.sequence < value.control_sequence:
        raise ValueError('Audio queue controls are stale or belong to another queue.')
    return value


def _pending(path, batch):
    match = _PENDING_ITEM.fullmatch(path.name)
    if match is None or match.group(1) not in {item.id for item in batch.items}:
        raise ValueError('Unknown audio queue scratch must not be silently omitted.')
    regular(path, directory=True)
    # An empty directory can remain if the process died before its owner record.
    entries = _children(path, limit=8)
    if not entries:
        return
    checkpoints = [entry for entry in entries if _CHECKPOINT.fullmatch(entry.name)]
    for entry in checkpoints:
        info = regular(entry)
        if info.st_size > MAX_STATE_BYTES:
            raise ValueError('An interrupted audio checkpoint exceeds its metadata allowance.')
    entries = tuple(entry for entry in entries if entry not in checkpoints)
    if not entries:
        return
    if {entry.name for entry in entries} - {'owner.json', 'work', 'publish'}:
        raise ValueError('An interrupted audio stage contains undeclared state.')
    owner = read_json(path / 'owner.json', limit=8192)
    if owner != {'batch_id': batch.id, 'item_id': match.group(1), 'plan_sha256': batch.plan_sha256}:
        raise ValueError('An interrupted audio stage lost its exact owner.')
    item = next(item for item in batch.items if item.id == match.group(1))
    for entry in entries:
        if entry.name == 'owner.json':
            regular(entry)
            continue
        allowed = {'decoded.wav', 'verified.wav'} if entry.name == 'work' else {*item.outputs, 'receipt.json'}
        for file in _children(entry, limit=len(allowed) + 1):
            if file.name not in allowed and not file.name.startswith('.checkpoint-'):
                raise ValueError('An interrupted audio stage contains undeclared work.')
            regular(file)
            if file.stat().st_size > MAX_OUTPUT_BYTES:
                raise ValueError('An interrupted audio stage exceeds its file allowance.')


def state_paths(library_root, *, cancelled=lambda: False):
    root = Path(library_root)
    result = []
    for directory in _batch_directories(root):
        check_cancelled(cancelled)
        batch = _batch(directory)
        for entry in _children(directory, limit=2 * MAX_ITEMS + 8):
            if entry.name in ('batch.json', 'control.json'):
                regular(entry)
                result.append(entry)
            elif entry.name == '.control.lock':
                regular(entry)
            elif _CHECKPOINT.fullmatch(entry.name):
                if regular(entry).st_size > MAX_STATE_BYTES:
                    raise ValueError('An interrupted queue checkpoint exceeds its metadata allowance.')
            elif entry.name in ('sources', 'outputs'):
                regular(entry, directory=True)
            elif _PENDING_ITEM.fullmatch(entry.name):
                _pending(entry, batch)
            else:
                raise ValueError('Unknown audio queue state must not be silently omitted.')
        source_names = {item.source.file for item in batch.items}
        source_names.update(asset.file for item in batch.items for asset in item.source.assets)
        observed_sources = _children(directory / 'sources', limit=MAX_FILES)
        if {path.name for path in observed_sources} != source_names:
            raise ValueError('An audio queue lost retained originals or contains unclaimed inputs.')
        result.extend(observed_sources)
        items = {item.id: item for item in batch.items}
        outputs = directory / 'outputs'
        inventory = _children(outputs, limit=MAX_ITEMS) if optional_present(outputs) else ()
        for output in inventory:
            if output.name not in items:
                raise ValueError('An audio output belongs to an unknown queue item.')
            files = _children(output, limit=len(items[output.name].outputs) + 2)
            if {path.name for path in files} != {*items[output.name].outputs, 'receipt.json'}:
                raise ValueError('An audio output is incomplete or contains undeclared files.')
            result.extend(files)
    if len(result) > MAX_FILES:
        raise ValueError('Audio queue files exceed the backup allowance.')
    for path in result:
        check_cancelled(cancelled)
        name = path.relative_to(root).as_posix()
        local_path(path, root=root, exists=True)
        info = regular(path)
        if not 0 < info.st_size <= state_limit(name):
            raise ValueError('Audio queue member exceeds its typed byte allowance.')
    return tuple(sorted(result))


@contextmanager
def snapshot_guard(library_root):
    root = Path(library_root) / DIRECTORY
    if not optional_present(root):
        yield
        return
    local_path(root, root=library_root, exists=True)
    regular(root, directory=True)
    with state_lock(root, 'batch'):
        yield


def validate_state_tree(library_root, *, db=None, cancelled=lambda: False):
    """Validate byte bindings; an optional catalog checks exact book references."""
    root = Path(library_root)
    state_paths(root, cancelled=cancelled)
    completed = []
    for directory in _batch_directories(root):
        batch = _batch(directory)
        for item in batch.items:
            check_cancelled(cancelled)
            source = directory / 'sources' / item.source.file
            AudioBatchService._verify_file(source, item.source.bytes, item.source.sha256, cancelled)
            for asset in item.source.assets:
                path = directory / 'sources' / asset.file
                AudioBatchService._verify_file(path, asset.bytes, asset.sha256, cancelled)
                wav_info(path)
            if item.kind == 'book':
                payload = read_json(source, limit=MAX_STATE_BYTES)
                plan = document_plan(payload)
                if payload['project_id'] != item.source.project_id:
                    raise ValueError('Retained book snapshot lost its selected original project.')
                if db is not None:
                    from .library_backup import _validate_studio_source
                    _validate_studio_source(db, payload, root=root)
                child = child_document(payload, item.id)
                from hashlib import sha256
                if sha256(canonical(child)).hexdigest() != item.source.child_document_sha256:
                    raise ValueError('Retained book snapshot lost its child-document binding.')
                _, document, audio = ManagedProjects(root).paths(item.id)
                if optional_present(document):
                    AudioBatchService._verify_file(document, len(canonical(child)),
                                                   item.source.child_document_sha256, cancelled)
                    for asset in item.source.assets:
                        AudioBatchService._verify_file(audio / 'assets' / asset.name,
                                                       asset.bytes, asset.sha256, cancelled)
                elif item.state == 'complete' or optional_present(directory / 'outputs' / item.id):
                    raise ValueError('Completed batch book lost its managed Studio child.')
                from .audio.batch_models import output_names
                if output_names(item.id, item.ordinal, item.title, item.preset, len(plan.scenes)) != item.outputs:
                    raise ValueError('Restored book chapter preview differs from its exact saved plan.')
            output = directory / 'outputs' / item.id
            if not optional_present(output):
                if item.state == 'complete':
                    raise ValueError('A completed queue item lost its owned output receipt.')
                continue
            value = receipt_value(read_json(output / 'receipt.json', limit=MAX_STATE_BYTES), batch, item)
            if item.kind == 'book':
                from .audio.models import digest as production_digest
                if (value['render']['plan_sha256'] != production_digest(plan.to_dict())
                        or value['render']['source_sha256'] != plan.source_sha256):
                    raise ValueError('An audiobook receipt differs from its retained exact plan.')
            for record in value['files']:
                path = output / record['name']
                AudioBatchService._verify_file(path, record['bytes'], record['sha256'], cancelled)
                if item.preset.output_format == 'wav':
                    info = wav_info(path)
                    if (info.rate, info.channels, info.frames) != (
                            record['sample_rate'], record['channels'], record['frames']):
                        raise ValueError('A retained WAV differs from its completed sample receipt.')
            completed.append((batch.id, item.id))
    return tuple(completed)
