"""Offline inspection and reproducible derivation of the selected catalog."""
import argparse
import json
from pathlib import Path
import stat
import sys

from . import (CatalogError, MAX_SOURCE_BYTES, REGIONS, RESOURCE_STATUSES,
               SOURCES, derive_catalog, load_catalog)


def _read(path):
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
            or not 0 < info.st_size <= MAX_SOURCE_BYTES):
        raise CatalogError('Choose a bounded ordinary local source file without links.')
    with path.open('rb') as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) != info.st_size:
        raise CatalogError('A source file changed while reading.')
    return raw


def main(argv=None):
    parser = argparse.ArgumentParser(description='Inspect the canonical selection; resource counts are not speech acceptance.')
    subparsers = parser.add_subparsers(dest='command', required=True)
    report = subparsers.add_parser('report', help='Search the pinned recorded inventory without loading any model.')
    report.add_argument('--query', default='')
    report.add_argument('--region', choices=REGIONS)
    report.add_argument('--resource-status', choices=sorted(RESOURCE_STATUSES))
    report.add_argument('--json', action='store_true')
    derive = subparsers.add_parser('derive', help='Validate four exact upstream source files and print deterministic JSON.')
    for name in SOURCES:
        derive.add_argument('--' + name.replace('_', '-'), required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == 'derive':
            raw = derive_catalog(**{name: _read(getattr(arguments, name)) for name in SOURCES})
            sys.stdout.write(raw.decode('utf-8'))
            return 0
        value = load_catalog().report(arguments.query, region=arguments.region, resource_status=arguments.resource_status)
        if arguments.json:
            print(json.dumps(value, ensure_ascii=False, indent=2))
        else:
            print(f"{value['matching_entries']} matching entries in the canonical {value['total_selected_entries']}-entry selection.")
            print(value['evidence_scope'])
            print('Final language qualification follows core features and secure delivery.')
            for entry in value['entries']:
                print('\n' + entry['id'] + ': ' + entry['name_uk'])
                for place in entry['geography']:
                    print(place['region'] + ': ' + ', '.join(place['countries']))
                print('Recorded resources: ' + entry['recorded_resource_status'])
                print('Recorded runtime identifier: ' + entry['recorded_runtime_code'])
                print('Recorded backend: ' + entry['recorded_backend'])
                if entry['identity_review'] != 'retain_selected_identity':
                    print('Identity review: ' + entry['identity_review'])
                if entry['standard_note']:
                    print(entry['standard_note'])
                print('Installed voice compatibility and all final capabilities: not assessed.')
        return 0
    except (CatalogError, OSError) as exc:
        parser.exit(2, 'Catalog check did not complete: ' + str(exc) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
