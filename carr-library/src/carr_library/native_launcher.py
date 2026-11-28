"""Exact public identity contracts for the optional private Win32 entry.

The pinned executable is unsigned. Matching it establishes byte identity, not
publisher signing, installation, GUI accessibility or listening acceptance.
No executable is loaded by this module.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat


LAUNCHER_NAME = 'ResearchLibrary.exe'
LAUNCHER_BYTES = 15_872
LAUNCHER_SHA256 = 'd8116a69d4a04983e032d80adef6e36c861fbe48b672633066f383193c3d616a'
SOURCE_COMMIT = 'c8baf30c2cf85a6d0683ff485795509244bf76d5'
SOURCE_SHA256 = {
    'launcher.c': '052c2d285d6be44013e0da3b9ca1a155e5d6bcf991ba1e9fa30ebde03d1752bd',
    'launcher_core.h': 'd6dcc488585e38acdc2d11ab014c18b0a38d3b23650b3eab1c3f2aa7310a0a1b',
    'launcher.rc': '1b5c2ee4213d3967649d8f2bb469081bbc910f0614125968e668a79c072a3db2',
    'launcher.manifest': 'a1a68c930e5bd6380727f6fc88c49c4ba9b84dd8e688a48fb32857c58b6b1f1a',
    'build_msvc.ps1': '3bab5f4319b3f025ecb1aa027befd448bbf1cf567a660441e39744e2570255c8',
    'probe_pe.py': '149e147197c94c067cb6211c9ecd51d3316b0c978ff830aa040e365d664759e3',
    'windows_child_probe.c': '2e7e8b2dae95b2a7a8b89675815a7804644ac4e90c02622290002729d3179b15',
}
WINDOWS_RUN = 46_008_469_436
WINDOWS_JOB = 140_537_540_906


def identity_record():
    """Return an independent record of the exact existing native evidence."""
    return {
        'schema': 1, 'file': LAUNCHER_NAME, 'bytes': LAUNCHER_BYTES,
        'sha256': LAUNCHER_SHA256, 'source_commit': SOURCE_COMMIT,
        'source_sha256': dict(SOURCE_SHA256),
        'toolchain': 'MSVC 19.44.35230.0 / Windows x64',
        'compiler_evidence': {
            'cl.exe': {'version': '19.44.35230.0',
                       'sha256': 'e87e78b3ef6580b75aebda843f8a22e874612d9f766344046551399f898eb226'},
            'link.exe': {'version': '14.44.35230.0',
                         'sha256': '49a5df25c3182a22684375b1d62a025016deffccea247894fe5ca2c85610c6a6'},
            'rc.exe': {'version': '10.0.26100.9457 (WinBuild.160101.0800)',
                       'sha256': '9e2ee68da28b3318f50a9f1008d64567a9b402bda384a007a9b71a3f16bd011b'},
        },
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


def _historical_identity_record():
    """Read the exact old Zig tuple; it never qualifies new source or bytes."""
    return json.loads(r'''{
  "schema": 1,
  "file": "ResearchLibrary.exe",
  "bytes": 13824,
  "sha256": "5ba9218a329f8d002539bd57f73d91170125f5ec91c78d49ff85ca6c74443cec",
  "source_commit": "687506ddca74c33fa3ee62c7925ae98b3b1afa3b",
  "source_sha256": {
    "launcher.c": "e990a1db29d3ee7d352759ab44ea92c670cadb43a50020b96d0207fef248a6b2",
    "launcher_core.h": "d6dcc488585e38acdc2d11ab014c18b0a38d3b23650b3eab1c3f2aa7310a0a1b",
    "launcher.rc": "1b5c2ee4213d3967649d8f2bb469081bbc910f0614125968e668a79c072a3db2",
    "launcher.manifest": "a1a68c930e5bd6380727f6fc88c49c4ba9b84dd8e688a48fb32857c58b6b1f1a",
    "toolchain.lock.json": "8b54b661859effc9a0a4447da2636dbe5b4bf68918ac5d3ec1e2c163bb79bd50"
  },
  "toolchain": "Zig 0.14.1 / x86_64-windows-gnu",
  "use_scope": "private-local-installation",
  "release_status": "unsigned-private-candidate",
  "windows_process_acceptance": {
    "run_id": 45912346606,
    "job_id": 140223449645,
    "url": "https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45912346606",
    "checks": 9,
    "synthetic_child": true
  },
  "authenticode_signed": false,
  "actual_installed_application_verified": false,
  "gui_verified": false,
  "nvda_verified": false
}''')


def _matching_identity(value):
    if type(value) is not dict:
        return None
    supplied = json.dumps(value, sort_keys=True, allow_nan=False)
    for record in (identity_record(), _historical_identity_record()):
        if supplied == json.dumps(record, sort_keys=True, allow_nan=False):
            return record
    return None


def validate_manifest_entry(manifest):
    """Reject unsupported identities and contradictory sealed inventories."""
    value = manifest.get('native_launcher')
    included = LAUNCHER_NAME in manifest['files']
    if value is None and not included and 'native_launcher' not in manifest:
        return False
    if 'native_launcher' not in manifest:
        # Current bundles bind the independently qualified launcher directly to
        # their complete source-bound seal. Absence of the historical pinned
        # record never transfers that older build's Windows acceptance.
        if (manifest.get('bundled_python') is not True
                or not isinstance(manifest.get('source_commit'), str)
                or not re.fullmatch('[0-9a-f]{40}', manifest['source_commit'])
                or not isinstance(manifest['files'].get(LAUNCHER_NAME), str)
                or not re.fullmatch('[0-9a-f]{64}', manifest['files'][LAUNCHER_NAME])):
            raise ValueError('A sealed native launcher requires its exact source and file identity.')
        return True
    record = _matching_identity(value)
    if (record is None or manifest['bundled_python'] is not True
            or 'source_commit' not in manifest
            or manifest['files'].get(LAUNCHER_NAME) != record['sha256']):
        raise ValueError('The native launcher identity or sealed inventory is not approved.')
    return True


def verify_payload(root, manifest=None):
    """Verify exact measured records or current source-bound sealed bytes.

    The current seal is byte identity only. Independent build and native CI
    qualification are deliberately not inferred from the manifest.
    """
    sealed = manifest is not None and 'native_launcher' not in manifest
    record = identity_record()
    if manifest is not None:
        if not validate_manifest_entry(manifest):
            raise ValueError('The installation has no native launch entry.')
        if not sealed:
            record = _matching_identity(manifest['native_launcher'])
    expected = manifest['files'][LAUNCHER_NAME] if sealed else record['sha256']
    maximum = 2 * 1024 * 1024 if sealed else record['bytes']
    path = Path(root).absolute() / LAUNCHER_NAME
    before = path.lstat()
    if (not stat.S_ISREG(before.st_mode) or path.is_symlink()
            or getattr(before, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not 0 < before.st_size <= maximum
            or not sealed and before.st_size != record['bytes']):
        raise ValueError('The native launch entry is missing, linked or has an invalid size.')
    stamp = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        if stamp(os.fstat(stream.fileno())) != stamp(before):
            raise ValueError('The native launch entry changed before verification.')
        payload = stream.read(maximum + 1)
        if (stamp(os.fstat(stream.fileno())) != stamp(before)
                or stamp(path.lstat()) != stamp(before)):
            raise ValueError('The native launch entry changed during verification.')
    if len(payload) != before.st_size or hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError('The native launch entry checksum is not approved.')
    result = {'status': 'PASS', 'sha256': expected,
              'authenticode_signed': False, 'execution': 'NOT_RUN', 'windows_nvda': 'NOT_RUN'}
    if sealed:
        result.update(identity_scope='source-bound sealed bytes', source_commit=manifest['source_commit'],
                      historical_native_acceptance='NOT_CLAIMED', native_gui='NOT_RUN')
    return result
