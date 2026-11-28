"""Read-only file-inventory evidence before the authoritative Inno build.

This helper never repairs a bundle or grants installation authority. Its JSON is
an observation, including when inspection is unavailable; build_setup.py must
still perform its original maintenance.bundle verification.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re


MAX_DIFFERENCES = 128
MAX_OUTPUT_BYTES = 1024 * 1024


def _maintenance():
    path = Path(__file__).resolve().with_name('maintenance.py')
    spec = importlib.util.spec_from_file_location('resonance_inventory_observer_maintenance', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _empty():
    return dict(schema=1, scope='standard-installer-file-inventory',
        status='UNAVAILABLE', expected_bundle_manifest_sha256=None,
        observed_bundle_manifest_sha256=None, source_commit=None,
        excluded_top_directories=['.venv'], expected_files=None, actual_files=None,
        counts=None, differences=[], differences_truncated=False,
        all_differences_sha256=None)


def inspect_bundle(bundle, expected_manifest_sha256):
    """Observe the same file comparison, retaining fresh manifest fences."""
    report = _empty()
    try:
        maintenance = _maintenance()
        expected_hash = maintenance.digest(expected_manifest_sha256)
        report['expected_bundle_manifest_sha256'] = expected_hash
        root = Path(bundle).absolute()
        manifest_path = root / 'bundle-manifest.json'
        raw = maintenance.read(manifest_path)
        observed_hash = hashlib.sha256(raw).hexdigest()
        report['observed_bundle_manifest_sha256'] = observed_hash
        if observed_hash != expected_hash:
            raise ValueError('Bundle manifest differs from the captured installer identity.')
        document = json.loads(raw, object_pairs_hook=maintenance.unique)
        if type(document) is not dict or type(document.get('files')) is not dict:
            raise ValueError('Bundle manifest has no file mapping.')
        files = document['files']
        if not files or len(files) > maintenance.MAX_FILES:
            raise ValueError('Bundle manifest file mapping exceeds its entry budget.')
        commit = document.get('source_commit')
        if type(commit) is not str or re.fullmatch('[0-9a-f]{40}', commit) is None:
            raise ValueError('Bundle manifest has no exact source commit.')
        report['source_commit'] = commit
        expected = dict(files, **{'bundle-manifest.json': expected_hash})
        for name, checksum in expected.items():
            maintenance.relative(name)
            maintenance.digest(checksum)
        # This is the production scanner, including path/type/link/hash fences.
        # Only the same top-level installed environment is excluded.
        actual, _directories = maintenance.inventory(root, exclude_top=('.venv',))
        if (actual.get('bundle-manifest.json') != expected_hash
                or maintenance.read(manifest_path) != raw):
            raise ValueError('Bundle manifest changed during inventory observation.')
        counts = dict(missing=0, changed=0, undeclared=0)
        differences = []
        complete_digest = hashlib.sha256()
        # SHA256 over every differing row, in exact relative-path order:
        # ASCII JSON, sorted keys, compact separators, and one LF per row.
        for name in sorted(expected.keys() | actual.keys()):
            before, after = expected.get(name), actual.get(name)
            if before == after:
                continue
            kind = 'missing' if name not in actual else (
                'undeclared' if name not in expected else 'changed')
            row = dict(kind=kind, path=name, expected_sha256=before, actual_sha256=after)
            complete_digest.update((json.dumps(row, ensure_ascii=True, sort_keys=True,
                separators=(',', ':')) + '\n').encode('ascii'))
            counts[kind] += 1
            if len(differences) < MAX_DIFFERENCES:
                differences.append(row)
        total = sum(counts.values())
        report.update(status='DIFFERENT' if total else 'MATCH',
            expected_files=len(expected), actual_files=len(actual), counts=counts,
            differences=differences, differences_truncated=total > len(differences),
            all_differences_sha256=complete_digest.hexdigest())
    except Exception as error:
        # Partial inventories are not evidence of an exact match/difference.
        report.update(status='UNAVAILABLE', expected_files=None, actual_files=None,
            counts=None, differences=[], differences_truncated=False,
            all_differences_sha256=None, error_type=type(error).__name__[:80],
            error=str(error)[:512])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--bundle-manifest-sha256', required=True)
    args = parser.parse_args(argv)
    report = inspect_bundle(args.bundle, args.bundle_manifest_sha256)
    encoded = json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
    if len(encoded.encode('ascii')) + 1 > MAX_OUTPUT_BYTES:
        report = _empty()
        report.update(error_type='OutputBudget', error='Inventory observation exceeds its output budget.')
        encoded = json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
    print(encoded, flush=True)
    # Observation failures do not replace or suppress the subsequent real build.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
