"""Reproduce the reviewed code-only Spylls wheel; never fetch input or dictionaries."""
from __future__ import annotations

import argparse
import base64
import csv
from hashlib import sha256
from io import BytesIO, StringIO
import json
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

UPSTREAM_NAME = 'spylls-0.1.7-py2.py3-none-any.whl'
UPSTREAM_SHA256 = '0c7fa4b66615f390bd12fd37939b85934c012309fd3cce8584844c54270b7776'
UPSTREAM_SIZE = 1_509_482
VERSION = '0.1.7+carr.1'
OUTPUT_NAME = f'spylls-{VERSION}-py2.py3-none-any.whl'
OLD_INFO = 'spylls-0.1.7.dist-info/'
NEW_INFO = f'spylls-{VERSION}.dist-info/'


def repack(source, output):
    source, output = Path(source), Path(output)
    if source.stat().st_size != UPSTREAM_SIZE:
        raise ValueError('The Spylls input size differs from the reviewed wheel.')
    raw = source.read_bytes()
    if sha256(raw).hexdigest() != UPSTREAM_SHA256:
        raise ValueError('The Spylls input hash differs from the reviewed wheel.')
    members, removed, sources = {}, [], {}
    with ZipFile(BytesIO(raw)) as upstream:
        for info in upstream.infolist():
            name = info.filename
            if name in members or name.startswith('/') or '..' in name.split('/'):
                raise ValueError('Invalid upstream wheel inventory.')
            if name.endswith(('.aff', '.dic')):
                removed.append(name)
                continue
            if name == OLD_INFO + 'RECORD':
                continue
            content = upstream.read(info)
            if name.endswith('.py'):
                sources[name] = sha256(content).hexdigest()
            if name.startswith(OLD_INFO):
                name = NEW_INFO + name[len(OLD_INFO):]
            if name == NEW_INFO + 'METADATA':
                content = content.replace(b'\nVersion: 0.1.7\n', f'\nVersion: {VERSION}\n'.encode())
                content = content.replace(b'\nLicense: UNKNOWN\n', b'\nLicense: MPL-2.0\n')
                content = content.replace(b'Classifier: License :: OSI Approved :: MIT License',
                    b'Classifier: License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)')
                content += (b'\nResonance code-only repack: Python source is unchanged. All bundled .aff/.dic '
                    b'example dictionaries were removed. Select your own separately licensed pair.\n')
            members[name] = content
    if len(removed) != 6 or not sources or NEW_INFO + 'LICENSE' not in members:
        raise ValueError('The reviewed Spylls wheel layout changed.')
    provenance = {'schema': 1, 'upstream_wheel': UPSTREAM_NAME,
        'upstream_sha256': UPSTREAM_SHA256, 'upstream_size': UPSTREAM_SIZE,
        'version': VERSION, 'license': 'MPL-2.0', 'removed': sorted(removed),
        'unchanged_python_sha256': dict(sorted(sources.items()))}
    members[NEW_INFO + 'CARR_REPACK.json'] = (
        json.dumps(provenance, sort_keys=True, indent=2) + '\n').encode('utf-8')
    records = StringIO(newline='')
    writer = csv.writer(records, lineterminator='\n')
    for name, content in sorted(members.items()):
        digest = base64.urlsafe_b64encode(sha256(content).digest()).rstrip(b'=').decode('ascii')
        writer.writerow((name, 'sha256=' + digest, len(content)))
    writer.writerow((NEW_INFO + 'RECORD', '', ''))
    members[NEW_INFO + 'RECORD'] = records.getvalue().encode('utf-8')
    # Fixed order, timestamp, permissions and uncompressed storage make this
    # artifact reproducible across platforms and zlib releases.
    result = BytesIO()
    with ZipFile(result, 'w', compression=ZIP_STORED) as wheel:
        for name, content in sorted(members.items()):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system, info.external_attr = 3, 0o100644 << 16
            wheel.writestr(info, content)
    payload = result.getvalue()
    if output.name != OUTPUT_NAME:
        raise ValueError('Use the reviewed code-only wheel filename.')
    with output.open('xb') as stream:
        stream.write(payload)
    return {'wheel': output.name, 'size': len(payload), 'sha256': sha256(payload).hexdigest(),
            'provenance': provenance}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(repack(args.source, args.output), sort_keys=True))


if __name__ == '__main__':
    main()
