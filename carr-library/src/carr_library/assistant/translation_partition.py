"""Pure, bounded layout partitions of an unchanged retained translation source.

A partition is mapping evidence, never another source or request authority.
The enclosing translation/2 job and its existing source/usage checks remain
authoritative. No filesystem, provider or document parser is used here.
"""
import re

from .models import text

SCHEMA = 'carr-translation-partition/1'
PROFILES = frozenset(('carr-docx-body-layout/1', 'carr-epub-xhtml-layout/1'))
MAX_PARTS = 4096


def partition_ranges(value, partition):
    text(value, 'partitioned translation source', maximum=2_000_000)
    if (type(partition) is not dict
            or set(partition) != {'schema', 'profile', 'mapping_sha256', 'boundaries'}
            or partition.get('schema') != SCHEMA
            or type(partition.get('profile')) is not str
            or partition.get('profile') not in PROFILES
            or type(partition.get('mapping_sha256')) is not str
            or re.fullmatch(r'[0-9a-f]{64}', partition['mapping_sha256']) is None
            or type(partition.get('boundaries')) is not list
            or not 1 <= len(partition['boundaries']) <= MAX_PARTS):
        raise ValueError('The retained layout partition is invalid.')
    first = 0
    ranges = []
    for last in partition['boundaries']:
        if type(last) is not int or not first < last <= len(value):
            raise ValueError('Layout partitions must cover the original text once, in order.')
        ranges.append((first, last))
        first = last
    if first != len(value):
        raise ValueError('Layout partitions must cover the complete original text.')
    return tuple(ranges)


def partition_chunk_groups(value, partition):
    from .translation import split_text
    groups = []
    for first, last in partition_ranges(value, partition):
        selected = value[first:last]
        # The existing gateway preserves whitespace receipts without transfer.
        # split_text deliberately rejects an entirely blank selection, so its
        # existing size bound is reproduced only for these untouched separators.
        groups.append(split_text(selected) if selected.strip() else
                      tuple(selected[index:index + 4500]
                            for index in range(0, len(selected), 4500)))
    return tuple(groups)


def partition_chunks(value, partition):
    return tuple(chunk for group in partition_chunk_groups(value, partition)
                 for chunk in group)


def make_partition(value, profile, mapping_sha256, boundaries):
    result = {'schema': SCHEMA, 'profile': profile, 'mapping_sha256': mapping_sha256,
              'boundaries': list(boundaries)}
    partition_ranges(value, result)
    return result
