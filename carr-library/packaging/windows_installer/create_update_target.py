"""Create an unsigned TUF target from an exact locally compiled core setup.

This is not a publisher, signer, uploader or release qualification. The existing
delivery inventory gate still refuses owner-local audio/runtime payloads, even
inside an opaque installer. Independently configured TUF metadata must authorize
the resulting target before the application can admit an update.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from carr_library.security._files import read_bytes
from carr_library.security.managed_installation import validate_setup_receipt


HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


builder = _load('carr_managed_target_builder', HERE / 'build_setup.py')
delivery = _load('carr_managed_target_delivery', HERE.parent / 'create_delivery.py')
TARGET = 'managed-update.zip'
MAX_TARGET = 2 * 1024**3


def _checked_source(prepared, expected_source_commit):
    raw = read_bytes(prepared / 'setup-build.json', 128 * 1024)
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=builder.maintenance.unique)
    if type(value) is not dict or value.get('source_commit') != expected_source_commit:
        raise ValueError('The compiled setup is not from the selected exact source commit.')
    manifest_raw = builder.maintenance.read(prepared / 'payload/bundle-manifest.json')
    if hashlib.sha256(manifest_raw).hexdigest() != value.get('bundle_manifest_sha256'):
        raise ValueError('The compiled setup payload manifest changed after its build.')
    manifest = json.loads(manifest_raw.decode('utf-8'), object_pairs_hook=builder.maintenance.unique)
    if type(manifest) is not dict or type(manifest.get('files')) is not dict:
        raise ValueError('The compiled setup has no exact sealed payload inventory.')
    # Reuse the one existing rights gate. Flags alone do not hide a sealed
    # audio/ or audio-runtime/ namespace; neither layout is transferable.
    delivery._require_transferable_audio_inventory(manifest)
    document = builder.maintenance.bundle(prepared / 'payload', value.get('bundle_manifest_sha256'))
    if document['source_commit'] != expected_source_commit:
        raise ValueError('The compiled setup payload has another source commit.')
    version = builder.application_version(prepared / 'payload')
    setup = prepared / 'compiled/ResonanceSetup.exe'
    receipt = validate_setup_receipt(setup, raw,
        source_commit=expected_source_commit, version=version)
    if builder.maintenance.sha(prepared / 'ResonanceSetup.iss') != receipt['template_sha256']:
        raise ValueError('The compiled setup template changed after its build.')
    if set((prepared / 'helpers').iterdir()) != {prepared / 'helpers' / name for name in receipt['maintenance_sha256']}:
        raise ValueError('The captured installer helper inventory changed.')
    for name, digest in receipt['maintenance_sha256'].items():
        if builder.maintenance.sha(prepared / 'helpers' / name) != digest:
            raise ValueError('The compiled setup helper bytes changed after its build.')
    return raw, receipt, setup


def create_update_target(prepared, expected_source_commit, output, *, cancelled=lambda: False):
    delivery._check(cancelled)
    # Keep both the compiler inputs and this transfer artifact outside Git.
    prepared = builder.outside_repository(prepared)
    output = builder.outside_repository(output)
    delivery._directories(prepared)
    parents = delivery._directories(output.parent)
    if output.is_relative_to(prepared) or prepared.is_relative_to(output):
        raise ValueError('The update target must be outside the compiled setup directory.')
    if os.path.lexists(output):
        raise FileExistsError('Choose a new update-target destination.')
    raw, receipt, setup = _checked_source(prepared, expected_source_commit)
    if receipt['setup_bytes'] + len(raw) + 4096 > MAX_TARGET:
        raise ValueError('The standard setup exceeds the default signed target byte budget.')
    parent_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW) if os.name != 'nt' else None
    stage = identity = None
    try:
        delivery._unchanged_directories(parents)
        stage = Path(tempfile.mkdtemp(prefix='.carr-managed-target-', dir=output.parent))
        identity = delivery._identity(delivery._plain(stage, directory=True))
        with (stage / TARGET).open('xb') as destination:
            with ZipFile(destination, 'w', compression=ZIP_STORED) as archive:
                info = ZipInfo('ResonanceSetup.exe', (1980, 1, 1, 0, 0, 0))
                info.create_system, info.external_attr = 3, 0o100644 << 16
                with archive.open(info, 'w', force_zip64=True) as target, delivery._file(setup) as source:
                    checksum, size = delivery._stream(source, cancelled, destination=target,
                                                        limit=receipt['setup_bytes'])
                if (size, checksum) != (receipt['setup_bytes'], receipt['setup_sha256']):
                    raise ValueError('The standard setup changed while creating its target.')
                info = ZipInfo('setup-build.json', (1980, 1, 1, 0, 0, 0))
                info.create_system, info.external_attr = 3, 0o100644 << 16
                archive.writestr(info, raw)
            destination.flush()
            os.fsync(destination.fileno())
        with delivery._file(stage / TARGET) as stream:
            checksum, size = delivery._stream(stream, cancelled, limit=MAX_TARGET)
        target_stamp = delivery._stamp(delivery._plain(stage / TARGET))
        report = dict(schema=1, kind='unsigned-managed-update-target', source_commit=expected_source_commit,
            application_version=receipt['application_version'], target=TARGET,
            bytes=size, sha256=checksum, setup_sha256=receipt['setup_sha256'],
            setup_receipt_sha256=hashlib.sha256(raw).hexdigest(),
            bundle_manifest_sha256=receipt['bundle_manifest_sha256'],
            managed_upgrade_protocol=receipt['managed_upgrade_protocol'],
            publisher_authority='NOT_PROVISIONED', native_update='NOT_RUN',
            audio_transfer_payload=False)
        with (stage / 'target-build.json').open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        report_stamp = delivery._stamp(delivery._plain(stage / 'target-build.json'))
        delivery._check(cancelled)
        if _checked_source(prepared, expected_source_commit)[0] != raw:
            raise ValueError('The compiled setup receipt changed before target publication.')
        delivery._unchanged_directories(parents)
        if (delivery._identity(delivery._plain(stage, directory=True)) != identity
                or {path.name for path in stage.iterdir()} != {TARGET, 'target-build.json'}
                or delivery._stamp(delivery._plain(stage / TARGET)) != target_stamp
                or delivery._stamp(delivery._plain(stage / 'target-build.json')) != report_stamp):
            raise ValueError('The update target staging inventory changed.')
        delivery._check(cancelled)
        delivery._publish(stage, output, parent_fd)
        stage = None
        return report
    finally:
        try:
            if stage is not None and identity is not None:
                delivery._cleanup(stage, identity, parent_fd)
        finally:
            if parent_fd is not None:
                os.close(parent_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared', required=True, type=Path)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--output', required=True, type=Path)
    arguments = parser.parse_args(argv)
    print(json.dumps(create_update_target(arguments.prepared, arguments.source_commit,
                                          arguments.output), sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
