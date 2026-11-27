"""Plan or explicitly prepare the existing pinned private Windows audio runtime.

The default only reports a plan. --prepare or an interactive YES uses an existing
archive. --download, or YES after an explicit download plan, first acquires the
one fixed archive through the same preparer. No engine/settings are activated.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
from uuid import uuid4

from carr_library.audio import runtime
from carr_library.audio.jobs import file_identity, verified_reader
from carr_library.audio.model_manifest import local_path
from carr_library.data_paths import guard_data_directory, program_directories

HERE = Path(__file__).absolute().parent
HEADROOM = 64 * 1024 ** 2


def _installation_root():
    if HERE.name == 'audio-setup' and HERE.parent.name == 'tools':
        return local_path(HERE.parent.parent)
    return local_path(HERE.parent)


def _load_preparer():
    path = local_path(HERE / 'prepare_ffmpeg_runtime.py')
    runtime.regular_file(path)
    specification = importlib.util.spec_from_file_location('carr_explicit_local_audio_prepare', path)
    if specification is None or specification.loader is None:
        raise RuntimeError('The installed audio preparation helper is unavailable.')
    module = importlib.util.module_from_spec(specification)
    previous_path, previous_bytecode = list(sys.path), sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        specification.loader.exec_module(module)
    finally:
        sys.path[:] = previous_path
        sys.dont_write_bytecode = previous_bytecode
    return module


def _default_destination():
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        raise ValueError('Choose an explicit local destination; LOCALAPPDATA is unavailable.')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    return Path(base) / 'Resonance' / 'runtimes' / ('audio-' + stamp + '-' + uuid4().hex[:8])


def _external_destination(value, library_root=None):
    selected = Path(value)
    if not selected.is_absolute() or '..' in selected.parts:
        raise ValueError('Choose an absolute local destination without parent traversal.')
    selected = local_path(guard_data_directory(selected, purpose='local audio runtime'), must_exist=False)
    roots = {_installation_root(), *(local_path(root) for root in program_directories())}
    if library_root is not None:
        library = local_path(library_root)
        if not library.is_dir():
            raise ValueError('The selected library root is not a directory.')
        roots.add(library)
    if any(selected.is_relative_to(root) or root.is_relative_to(selected) for root in roots):
        raise ValueError('Choose a new audio folder outside the application and selected library.')
    if os.path.lexists(selected):
        raise FileExistsError('Choose a new audio work folder; the selected destination already exists.')
    return selected


def _existing_parent(path):
    parent = path.parent
    while not os.path.lexists(parent):
        parent = parent.parent
    return runtime.trusted_directory(parent)


def plan(archive=None, destination=None, *, library_root=None, download=False):
    if type(download) is not bool or download and archive is not None:
        raise ValueError('Choose either the existing archive or an explicit fixed download.')
    output = _external_destination(destination if destination is not None else _default_destination(),
                                   library_root)
    selected, identity = None, None
    if archive is not None:
        value = Path(archive)
        if not value.is_absolute():
            raise ValueError('Choose the pinned archive using an absolute local path.')
        selected = local_path(value)
        runtime.regular_file(selected)
        info = selected.stat()
        if info.st_size != runtime.FFMPEG_ARCHIVE_BYTES:
            raise ValueError('The selected archive size differs from the pinned release.')
        identity = list(file_identity(info))
    payload_bytes = sum(record[1] for record in runtime.ARCHIVE_MEMBERS.values())
    return {
        'schema': 1, 'status': 'plan', 'runtime_version': runtime.FFMPEG_VERSION,
        'archive': str(selected) if selected is not None else None,
        'archive_identity': identity,
        'archive_url': runtime.FFMPEG_ARCHIVE_URL,
        'archive_bytes': runtime.FFMPEG_ARCHIVE_BYTES,
        'archive_sha256': runtime.FFMPEG_ARCHIVE_SHA256,
        'source_commit': runtime.FFMPEG_SOURCE_COMMIT,
        'destination': str(output), 'runtime_directory': str(output / 'runtime'),
        'library_root': str(local_path(library_root)) if library_root is not None else None,
        'runtime_files': sorted(runtime.ARCHIVE_MEMBERS),
        'runtime_bytes': payload_bytes,
        'required_free_bytes': 2 * payload_bytes + HEADROOM +
            (2 * runtime.FFMPEG_ARCHIVE_BYTES if download else 0),
        'downloads': download, 'engine_settings_changed': False,
        'native_verification': 'required when preparing; NOT_RUN for this plan',
        'rights': 'Private local preparation; this does not authorize redistributing the runtime.',
    }


def _require_windows():
    if os.name != 'nt' or struct.calcsize('P') != 8:
        raise RuntimeError('Actual preparation and verification require Windows x64.')


def prepare_local(reviewed):
    _require_windows()
    if not isinstance(reviewed, dict) or reviewed.get('archive') is None:
        raise ValueError('Select the existing pinned archive before preparing audio.')
    current = plan(reviewed['archive'], reviewed['destination'], library_root=reviewed['library_root'])
    if current != reviewed:
        raise ValueError('The reviewed audio plan or selected archive changed; review a new plan.')
    output = Path(current['destination'])
    ancestor = _existing_parent(output)
    if shutil.disk_usage(ancestor).free < current['required_free_bytes']:
        raise OSError('Not enough free disk space for the staged and completed audio runtime.')
    preparer = _load_preparer()
    with verified_reader(current['archive'], expected_identity=tuple(current['archive_identity'])):
        # Reserve one new external work folder. The unchanged preparer alone
        # owns its runtime child and the exact archive/member/manifest checks.
        output.parent.mkdir(parents=True, exist_ok=True)
        runtime.trusted_directory(output.parent)
        output.mkdir(mode=0o700, exist_ok=False)
        runtime.trusted_directory(output)
        prepared = preparer.prepare(Path(current['archive']), output / 'runtime', verify_native=True)
        verified = runtime.verify_runtime(output / 'runtime')
        if prepared != verified or verified.get('native_validation', {}).get('status') != 'PASS':
            raise ValueError('Prepared runtime lacks the required actual native verification.')
    return {
        'schema': 1, 'status': 'prepared', 'runtime_version': runtime.FFMPEG_VERSION,
        'runtime_directory': str(output / 'runtime'),
        'archive_sha256': runtime.FFMPEG_ARCHIVE_SHA256,
        'native_verification': 'PASS', 'downloads': False, 'engine_settings_changed': False,
        'ffmpeg': str(output / 'runtime' / 'ffmpeg.exe'),
        'ffprobe': str(output / 'runtime' / 'ffprobe.exe'),
        'next_action': 'Choose these verified executables in the current library engine settings.',
        'rights': current['rights'],
    }



def download_local(reviewed):
    """Act only on a reviewed fixed-download plan; reuse manual preparation."""
    _require_windows()
    if (not isinstance(reviewed, dict) or reviewed.get('downloads') is not True
            or reviewed.get('archive') is not None):
        raise ValueError('Review an explicit fixed-download plan before preparing audio.')
    current = plan(destination=reviewed['destination'], library_root=reviewed['library_root'],
                   download=True)
    if current != reviewed:
        raise ValueError('The reviewed audio download plan changed; review a new plan.')
    output = Path(current['destination'])
    ancestor = _existing_parent(output)
    if shutil.disk_usage(ancestor).free < current['required_free_bytes']:
        raise OSError('Not enough free disk space for the fixed download and prepared audio runtime.')
    preparer = _load_preparer()
    # The network input is private temporary setup material. It is removed only
    # after manual preparation/final verification has closed its immutable reader.
    with tempfile.TemporaryDirectory(prefix='.carr-audio-acquire-', dir=ancestor) as temporary:
        directory = Path(temporary)
        acquisition = preparer.acquire_archive(directory)
        if plan(destination=output, library_root=current['library_root'], download=True) != current:
            raise ValueError('The selected audio destination or download plan changed.')
        archive = directory / preparer.DOWNLOAD_FILENAME
        local = plan(archive, output, library_root=current['library_root'])
        result = prepare_local(local)
    # Temporary cleanup is part of completion, including on Windows.
    return {**result, 'downloads': True, 'acquisition': acquisition}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='Existing pinned archive; this manual path stays offline.')
    parser.add_argument('--destination', type=Path, help='New external audio work folder.')
    parser.add_argument('--library-root', type=Path, help='Current library to keep separate from audio files.')
    consent = parser.add_mutually_exclusive_group()
    consent.add_argument('--prepare', action='store_true', help='Explicitly prepare and execute fixed version/filter checks.')
    consent.add_argument('--interactive', action='store_true', help='Ask for YES after displaying the exact local or download plan.')
    consent.add_argument('--download', action='store_true', help='Explicitly acquire the one fixed archive, then prepare and verify it.')
    args = parser.parse_args(argv)
    if args.download and args.archive is not None:
        parser.error('--download cannot be combined with --archive.')
    try:
        downloading = args.download or (args.interactive and args.archive is None)
        reviewed = plan(args.archive, args.destination, library_root=args.library_root,
                        download=downloading)
        print(json.dumps(reviewed, ensure_ascii=False, sort_keys=True, indent=2))
        if not args.prepare and not args.interactive and not args.download:
            return 0
        if reviewed['archive'] is None and not downloading:
            raise ValueError('Select an existing archive with --archive, or explicitly choose --download.')
        prompt = ('Download this exact archive, prepare it and run its fixed checks? Type YES: '
                  if downloading else 'Prepare these local files and run their fixed checks? Type YES: ')
        if args.interactive and input(prompt).strip() != 'YES':
            print('Cancelled. No audio files or engine settings were changed.')
            return 0
        result = download_local(reviewed) if downloading else prepare_local(reviewed)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except EOFError:
        print('Cancelled. No confirmation was received.')
        return 0
    except KeyboardInterrupt:
        print('Stopped. Engine settings remain unchanged; retain any new work folder for inspection.',
              file=sys.stderr)
        return 130
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
