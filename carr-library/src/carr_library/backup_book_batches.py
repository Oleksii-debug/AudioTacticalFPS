"""Manifest-bound passive ordered Book selections; no output/runtime authority."""
from hashlib import sha256
from pathlib import Path
import re
import unicodedata

from .durable_state import local_path

PREFIX = 'state/studio/book-batches'
_RECORD = re.compile(PREFIX + r'/([0-9a-f]{64})/([0-9a-f]{32})\.json\Z')
_NAMESPACE = re.compile(PREFIX + r'/[0-9a-f]{64}\Z')
_LOCK = re.compile(PREFIX + r'/[0-9a-f]{64}/\.batch-[0-9a-f]{32}\.lock\Z')


def reserved(name):
    value = unicodedata.normalize('NFC', name).casefold()
    return value == PREFIX or value.startswith(PREFIX + '/')


def ignored(name):
    """Only local snapshot temporaries; never accepted incoming payloads."""
    if not reserved(name):
        return False
    parent, _, leaf = name.rpartition('/')
    return bool(_LOCK.fullmatch(name) or _NAMESPACE.fullmatch(parent)
                and re.fullmatch(r'\.pending-[A-Za-z0-9_]{8}', leaf))


def validate_directory(name):
    if reserved(name) and name != PREFIX and _NAMESPACE.fullmatch(name) is None:
        raise ValueError('Invalid reserved book-batch directory.')


def limit(name):
    if not reserved(name):
        return None
    if _RECORD.fullmatch(name) is None:
        raise ValueError('Invalid reserved book-batch record path.')
    from .studio.book_batch import MAX_BATCH_BYTES
    return MAX_BATCH_BYTES


def validate_record(name, value):
    from .studio.book_batch import validate_saved_batch
    match = _RECORD.fullmatch(name)
    if match is None:
        raise ValueError('Invalid saved book-batch filename.')
    return validate_saved_batch(value, namespace=match[1], identity=match[2])


def collection_budgets(entries, *, cancelled=lambda: False):
    from .studio.book_batch import MAX_BATCH_BYTES, MAX_SAVED_BATCHES, MAX_COLLECTION_BYTES
    counts, total, seen = {}, 0, set()
    for entry in entries:
        if cancelled():
            raise InterruptedError('Book-batch backup validation cancelled.')
        name = entry['filename']
        if not reserved(name):
            continue
        match = _RECORD.fullmatch(name)
        if (match is None or name in seen or type(entry['size_bytes']) is not int
                or not 1 <= entry['size_bytes'] <= MAX_BATCH_BYTES):
            raise ValueError('Invalid bounded book-batch inventory.')
        seen.add(name)
        counts[match[1]] = counts.get(match[1], 0) + 1
        total += entry['size_bytes']
        if counts[match[1]] > MAX_SAVED_BATCHES or total > MAX_COLLECTION_BYTES:
            raise ValueError('Saved book batches exceed their metadata collection budget.')


def validate_collection(copied_state, entries, *, cancelled=lambda: False):
    """Read copied manifest bytes, bind exact ordered plans, retain no permission.

    The existing backup_book_audio verifier separately binds those same plans
    to their Studio jobs and, for full backups, to the copied source database.
    This adapter adds only immutable ordered membership and aggregate bounds.
    """
    from .backup_sidecars import _json, read_regular
    from .studio.book_audio import MAX_PLAN_BYTES, validate_saved_plan
    from .studio.book_batch import MAX_BATCH_BYTES, MAX_TOTAL_CHARACTERS, MAX_TOTAL_PARTS
    root = Path(copied_state)
    entries = tuple(entries)
    collection_budgets(({**row, 'filename': 'state/' + row['filename']} for row in entries),
                       cancelled=cancelled)
    inventory = {row['filename']: row for row in entries}
    if len(inventory) != len(entries):
        raise ValueError('Duplicate copied book-batch inventory path.')

    def check():
        if cancelled():
            raise InterruptedError('Book-batch backup validation cancelled.')

    def read(name, maximum):
        check()
        entry = inventory.get(name)
        if (entry is None or type(entry['size_bytes']) is not int
                or not 1 <= entry['size_bytes'] <= maximum):
            raise ValueError('A saved batch references missing or oversized copied metadata.')
        path = local_path(root / name, root=root, exists=True)
        raw = read_regular(path, maximum)
        check()
        if len(raw) != entry['size_bytes'] or sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('Copied book-batch metadata differs from its verified manifest.')
        return _json(raw)

    plans = {}
    for name in sorted(inventory):
        check()
        full = 'state/' + name
        match = _RECORD.fullmatch(full)
        if match is None:
            continue
        batch = validate_record(full, read(name, MAX_BATCH_BYTES))
        characters = parts = 0
        for item in batch.items:
            check()
            plan_name = 'studio/book-plans/' + match[1] + '/' + item.plan_id + '.json'
            if plan_name not in plans:
                plan = validate_saved_plan(read(plan_name, MAX_PLAN_BYTES),
                                           namespace=match[1], identity=item.plan_id)
                source = plan.source
                # Cache only verified scalar bindings; never retain all source
                # texts or trust a self-declared manifest summary.
                plans[plan_name] = (plan.fingerprint, source['source_anchor']['workspace'],
                                    source['source_topic'], len(source['text']), len(plan.parts))
            fingerprint, workspace, topic, text_size, part_count = plans[plan_name]
            if (fingerprint != item.plan_sha256 or workspace != batch.workspace or topic != batch.topic):
                raise ValueError('A saved batch differs from its exact ordered copied Book plans.')
            characters += text_size
            parts += part_count
            if characters > MAX_TOTAL_CHARACTERS or parts > MAX_TOTAL_PARTS:
                raise ValueError('The saved batch exceeds its reviewed source or part budget.')
    check()
