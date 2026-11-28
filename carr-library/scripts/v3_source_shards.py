"""Select the exact reviewed V3 source suites for three independent Windows jobs.

This helper proves coverage only. The unchanged ci_check.py remains the test
runner and retains its isolated child, offline policy, 600-second suite bound,
JUnit validation and rejection of missing, skipped or expected-failure tests.
"""
import argparse
from hashlib import sha1, sha256
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name('v3_source_suites.json')
MANIFEST_GIT_BLOB = '37c38b6153c183f8dd5924a97ac8edef35f83108'
SOURCE_SUITE_COUNT = 139
SHARD_COUNT = 3
DEDICATED_BOOK_BATCH = 'tests/studio/test_book_batch.py'
MAX_MANIFEST_BYTES = 64 * 1024


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate V3 coverage manifest field.')
        value[key] = item
    return value


def required_suites(manifest=MANIFEST):
    with Path(manifest).open('rb') as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError('The V3 coverage manifest is too large.')
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if (type(value) is not dict or set(value) != {'schema', 'suites'}
            or value['schema'] != 'carr-v3-required-source/1'
            or type(value['suites']) is not list
            or len(value['suites']) != SOURCE_SUITE_COUNT
            or any(type(item) is not str or re.fullmatch(
                r'tests/(?:[a-z0-9_]+/)+test_[a-z0-9_]+\.py', item) is None
                for item in value['suites'])
            or len(set(value['suites'])) != SOURCE_SUITE_COUNT):
        raise ValueError('The V3 coverage manifest must declare exactly 139 distinct test files.')
    # Canonical JSON makes the reviewed Git identity independent of checkout
    # newline conversion and harmless JSON key/whitespace presentation.
    canonical = (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode('utf-8')
    digest = sha1(b'blob ' + str(len(canonical)).encode('ascii') + b'\0' + canonical).hexdigest()
    if digest != MANIFEST_GIT_BLOB:
        raise ValueError('The exact reviewed V3 source-suite inventory changed.')
    return tuple(value['suites'])


def _reviewed_assignment(required):
    # Keep the original two alternating assignments in order. Only the measured
    # long Book batch moves to its own required VM and starts without queued work.
    ordinary = tuple(
        tuple(suite for suite in required[index::2] if suite != DEDICATED_BOOK_BATCH)
        for index in range(2)
    )
    return (*ordinary, (DEDICATED_BOOK_BATCH,))


def validate_partition(required, shards):
    if (len(shards) != SHARD_COUNT or any(not part for part in shards)
            or sum(map(len, shards)) != len(required)
            or len({suite for part in shards for suite in part}) != len(required)
            or {suite for part in shards for suite in part} != set(required)):
        raise ValueError('V3 shards must cover every required suite exactly once.')
    if tuple(tuple(part) for part in shards) != _reviewed_assignment(required):
        raise ValueError('The reviewed V3 source-shard assignment changed.')


def selection(shard, *, manifest=MANIFEST, root=ROOT):
    if type(shard) is not int or shard not in (1, 2, 3):
        raise ValueError('Choose V3 source shard 1, 2 or 3.')
    required = required_suites(manifest)
    shards = _reviewed_assignment(required)
    validate_partition(required, shards)
    root = Path(root).resolve(strict=True)
    for suite in required:
        path = (root / suite).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('Missing or out-of-tree V3 required suite: ' + suite)
    return {
        'schema': 'carr-v3-source-coverage/1', 'status': 'COVERAGE_VALID',
        'manifest_git_blob': MANIFEST_GIT_BLOB,
        'required_count': len(required), 'shard_count': SHARD_COUNT,
        'required_sha256': sha256(('\n'.join(required) + '\n').encode('ascii')).hexdigest(),
        'shard': shard, 'suites': list(shards[shard - 1]),
        'shard_counts': [len(part) for part in shards],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shard', type=int, choices=(1, 2, 3), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = selection(args.shard)
        # A selection receipt is not an execution verdict. Do not overwrite a
        # previous receipt when a trusted workflow unexpectedly reuses a path.
        with args.output.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(report, stream, ensure_ascii=True, sort_keys=True, indent=2)
            stream.write('\n')
        print(json.dumps(report, ensure_ascii=True, sort_keys=True), flush=True)
    except (OSError, ValueError) as error:
        print('FAIL: V3 required-source coverage: ' + str(error), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
