"""Prove complete, unchanged selection for the two V3 broad-service jobs.

This is a coverage selector, not a test runner or an execution verdict. Each
required job runs the existing pytest command with its reviewed selectors and
unchanged options. Directory selectors continue to include every contained test.
"""
import argparse
from hashlib import sha1, sha256
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name('v3_service_selectors.json')
MANIFEST_GIT_BLOB = '784b5543881199f1117ebe9a4be1fef4223665c2'
SELECTOR_COUNT = 20
SHARD_COUNT = 2
MAX_MANIFEST_BYTES = 8 * 1024
# The completed original Windows job measured 1480.037 testcase seconds for
# these first four groups and 1023.941 for the remaining sixteen.
FIRST_SHARD_COUNT = 4


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate V3 broad-service manifest field.')
        value[key] = item
    return value


def required_selectors(manifest=MANIFEST):
    with Path(manifest).open('rb') as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError('The V3 broad-service manifest is too large.')
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if (type(value) is not dict or set(value) != {'schema', 'selectors'}
            or value['schema'] != 'carr-v3-broad-service/1'
            or type(value['selectors']) is not list
            or len(value['selectors']) != SELECTOR_COUNT
            or any(type(item) is not str or re.fullmatch(
                r'tests/(?:[a-z0-9_]+/)*(?:[a-z0-9_]+|test_[a-z0-9_]+\.py)', item) is None
                for item in value['selectors'])
            or len(set(value['selectors'])) != SELECTOR_COUNT):
        raise ValueError('The V3 broad-service manifest requires exactly 20 distinct selectors.')
    required = tuple(value['selectors'])
    if any(left != right and right.startswith(left + '/')
           for left in required for right in required):
        raise ValueError('V3 broad-service selectors must not contain one another.')
    canonical = (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + '\n').encode('utf-8')
    digest = sha1(b'blob ' + str(len(canonical)).encode('ascii') + b'\0' + canonical).hexdigest()
    if digest != MANIFEST_GIT_BLOB:
        raise ValueError('The exact reviewed V3 broad-service inventory changed.')
    return required


def _reviewed_assignment(required):
    return required[:FIRST_SHARD_COUNT], required[FIRST_SHARD_COUNT:]


def validate_partition(required, shards):
    if (len(shards) != SHARD_COUNT or any(not part for part in shards)
            or sum(map(len, shards)) != len(required)
            or len({selector for part in shards for selector in part}) != len(required)
            or {selector for part in shards for selector in part} != set(required)):
        raise ValueError('V3 broad-service shards must cover every selector exactly once.')
    if tuple(tuple(part) for part in shards) != _reviewed_assignment(required):
        raise ValueError('The reviewed V3 broad-service assignment changed.')


def selection(shard, *, manifest=MANIFEST, root=ROOT):
    if type(shard) is not int or shard not in (1, 2):
        raise ValueError('Choose V3 broad-service shard 1 or 2.')
    required = required_selectors(manifest)
    shards = _reviewed_assignment(required)
    validate_partition(required, shards)
    root = Path(root).resolve(strict=True)
    for selector in required:
        path = (root / selector).resolve(strict=True)
        kind_matches = path.is_file() if selector.endswith('.py') else path.is_dir()
        if not path.is_relative_to(root) or not kind_matches:
            raise ValueError('Missing, wrong-kind or out-of-tree broad-service selector: ' + selector)
    return {
        'schema': 'carr-v3-service-coverage/1', 'status': 'COVERAGE_VALID',
        'manifest_git_blob': MANIFEST_GIT_BLOB,
        'required_count': len(required), 'shard_count': SHARD_COUNT,
        'required_sha256': sha256(('\n'.join(required) + '\n').encode('ascii')).hexdigest(),
        'shard': shard, 'selectors': list(shards[shard - 1]),
        'shard_counts': [len(part) for part in shards],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shard', type=int, choices=(1, 2), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = selection(args.shard)
        with args.output.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(report, stream, ensure_ascii=True, sort_keys=True, indent=2)
            stream.write('\n')
        print(json.dumps(report, ensure_ascii=True, sort_keys=True), flush=True)
    except (OSError, ValueError) as error:
        print('FAIL: V3 broad-service coverage: ' + str(error), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
