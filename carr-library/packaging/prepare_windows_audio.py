"""Explicit, hash-locked private Windows FFmpeg setup; no automatic downloads."""
from __future__ import annotations

import argparse
import importlib.util
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unicodedata
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED


HERE = Path(__file__).resolve().parent
SOURCE_LOCK = HERE / 'audio-runtime' / 'source-lock.json'
# Standalone source setup keeps its existing bootstrap. Installed/importlib
# callers use the installed package and never prepend checkout source paths.
if importlib.util.find_spec('carr_library') is None:
    sys.path.insert(0, str(HERE.parent / 'src'))
from carr_library.audio.runtime import SOURCE_LOCK_SHA256, verify_audio_runtime
from carr_library.native_runtime import _directory, _plain as _checked_plain
MANIFEST = 'audio-runtime-manifest.json'
# Preserve the reviewed four-file setup contract for existing callers. The
# application bundle uses the complete, source-locked runtime below.
_legacy_spec = importlib.util.spec_from_file_location(
    'carr_private_audio_legacy', HERE / 'prepare_windows_audio_legacy.py')
_legacy = importlib.util.module_from_spec(_legacy_spec)
_legacy_spec.loader.exec_module(_legacy)
REQUIRED_FILES = _legacy.REQUIRED_FILES
urllib = _legacy.urllib
_url = _legacy._url
_publish_directory = _legacy._publish_directory
regular = _legacy.regular
local = _legacy.local
_pe64 = _legacy._pe64


# The installed module is the sole complete-pack producer. These aliases retain
# reviewed packaging callers; the compact compatibility producer stays separate.
from carr_library.audio import windows_setup as _full
SOURCE_LOCK = _full.SOURCE_LOCK
digest = _full.digest
_plain = _full._plain
_relative = _full._relative
_members = _full._members
_pe_x64 = _full._pe_x64
_ApprovedRedirects = _full._ApprovedRedirects
download_archive = _full.download_archive
verify_runtime = _full.verify_runtime
_prepare_full = _full.prepare
MAX_FILES = _full.MAX_FILES
MAX_UNPACKED_BYTES = _full.MAX_UNPACKED_BYTES


def load_lock(path=None):
    return _legacy.load_lock(path) if path is not None else _full.load_lock()

def prepare(source, cache_or_output, output=None, *, download=False):
    """Retain explicit two- and three-path setup contracts without conversion.

    prepare(archive, output) prepares the complete application runtime.
    prepare(lock, cache, output) preserves the compact compatibility lock.
    Their manifest formats remain distinct.
    """
    if output is not None:
        return _legacy.prepare(source, cache_or_output, output, download=download)
    if download:
        raise ValueError('Full-runtime archive preparation is offline; use --download for acquisition.')
    return _prepare_full(source, cache_or_output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--archive', type=Path, help='Already downloaded exact full-build ZIP; no network access')
    source.add_argument('--download', action='store_true', help='Explicitly download only the exact pinned public runtime')
    parser.add_argument('--output', type=Path, required=True, help='New private runtime directory')
    parser.add_argument('--cache', type=Path, help='Compatibility four-file runtime archive cache')
    parser.add_argument('--lock', type=Path, help='Compatibility four-file runtime lock; requires --cache')
    args = parser.parse_args(argv)
    if args.cache is not None or args.lock is not None:
        if args.cache is None or args.archive is not None:
            parser.error('--lock requires --cache; compatibility setup cannot use --archive')
        lock = args.lock or HERE / 'windows-audio.lock.json'
        result = prepare(lock, args.cache, args.output, download=args.download)
    elif args.download:
        with tempfile.TemporaryDirectory(prefix='carr-audio-download-') as temp:
            archive = download_archive(Path(temp)/'ffmpeg.zip')
            result = prepare(archive, args.output)
    elif args.archive is not None:
        result = prepare(args.archive, args.output)
    else:
        parser.error('Choose --archive or --download for the complete application runtime')
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Audio runtime was not prepared: {error}', file=sys.stderr)
        raise SystemExit(1)
