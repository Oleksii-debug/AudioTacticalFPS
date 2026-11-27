"""One exact, public identity contract for the optional private Win32 entry.

The pinned executable is unsigned. Matching it establishes byte identity, not
publisher signing, installation, GUI accessibility or listening acceptance.
No executable is loaded by this module.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat


LAUNCHER_NAME = 'ResearchLibrary.exe'
LAUNCHER_BYTES = 13_824
LAUNCHER_SHA256 = '5ba9218a329f8d002539bd57f73d91170125f5ec91c78d49ff85ca6c74443cec'
SOURCE_COMMIT = '687506ddca74c33fa3ee62c7925ae98b3b1afa3b'
SOURCE_SHA256 = {
    'launcher.c': 'e990a1db29d3ee7d352759ab44ea92c670cadb43a50020b96d0207fef248a6b2',
    'launcher_core.h': 'd6dcc488585e38acdc2d11ab014c18b0a38d3b23650b3eab1c3f2aa7310a0a1b',
    'launcher.rc': '1b5c2ee4213d3967649d8f2bb469081bbc910f0614125968e668a79c072a3db2',
    'launcher.manifest': 'a1a68c930e5bd6380727f6fc88c49c4ba9b84dd8e688a48fb32857c58b6b1f1a',
    'toolchain.lock.json': '8b54b661859effc9a0a4447da2636dbe5b4bf68918ac5d3ec1e2c163bb79bd50',
}
WINDOWS_RUN = 45_912_346_606
WINDOWS_JOB = 140_223_449_645


def identity_record():
    """Return an independent record of the exact existing native evidence."""
    return {
        'schema': 1, 'file': LAUNCHER_NAME, 'bytes': LAUNCHER_BYTES,
        'sha256': LAUNCHER_SHA256, 'source_commit': SOURCE_COMMIT,
        'source_sha256': dict(SOURCE_SHA256),
        'toolchain': 'Zig 0.14.1 / x86_64-windows-gnu',
        'use_scope': 'private-local-installation',
        'release_status': 'unsigned-private-candidate',
        'windows_process_acceptance': {
            'run_id': WINDOWS_RUN, 'job_id': WINDOWS_JOB,
            'url': 'https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/' + str(WINDOWS_RUN),
            'checks': 9, 'synthetic_child': True,
        },
        'authenticode_signed': False, 'actual_installed_application_verified': False,
        'gui_verified': False, 'nvda_verified': False,
    }


def validate_manifest_entry(manifest):
    """Reject unsupported identities and contradictory sealed inventories."""
    value = manifest.get('native_launcher')
    included = LAUNCHER_NAME in manifest['files']
    if value is None and not included and 'native_launcher' not in manifest:
        return False
    if (type(value) is not dict or manifest['bundled_python'] is not True
            or 'source_commit' not in manifest
            or manifest['files'].get(LAUNCHER_NAME) != LAUNCHER_SHA256
            or json.dumps(value, sort_keys=True, allow_nan=False) !=
               json.dumps(identity_record(), sort_keys=True, allow_nan=False)):
        raise ValueError('The native launcher identity or sealed inventory is not approved.')
    return True


def verify_payload(root):
    """Hash the small pinned entry without executing or trusting its metadata."""
    path = Path(root).absolute() / LAUNCHER_NAME
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or path.is_symlink()
            or getattr(before, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or before.st_size != LAUNCHER_BYTES):
        raise ValueError('The native launch entry is missing, linked or has an invalid size.')
    stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        if stamp(os.fstat(stream.fileno())) != stamp(before):
            raise ValueError('The native launch entry changed before verification.')
        payload = stream.read(LAUNCHER_BYTES + 1)
        if (stamp(os.fstat(stream.fileno())) != stamp(before)
                or stamp(path.lstat()) != stamp(before)):
            raise ValueError('The native launch entry changed during verification.')
    if len(payload) != LAUNCHER_BYTES or hashlib.sha256(payload).hexdigest() != LAUNCHER_SHA256:
        raise ValueError('The native launch entry checksum is not approved.')
    return {'status': 'PASS', 'sha256': LAUNCHER_SHA256,
            'authenticode_signed': False, 'execution': 'NOT_RUN', 'windows_nvda': 'NOT_RUN'}
