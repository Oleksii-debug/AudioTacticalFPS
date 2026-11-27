"""Assemble a private Windows successor without expanding its large runtimes.

Freeze an exact local Git commit; build and verify its wheel with production
tools; stream unchanged payloads from a pinned application ZIP; verify the exact
two-archive union with production create_delivery.verify_archives. No downloads,
uploads, native execution, shared-input deletion, or input-repository edits.
Copy this helper into the private assembly directory before using its defaults.
Failed staging and all source/wheel inputs are retained for diagnosis.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import ExitStack
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
import zlib


HERE = Path(__file__).resolve().parent
CHUNK = 1024 * 1024
PREFIX = 'Universal-Research-Library/'
PARENT_NAME = 'Universal-Research-Library-Application-1b939d6f21c3.zip'
PARENT_SHA = '3c2f9deae07651125381e878d33ad14920fc15f2aee4f8c90991fc30adde61b3'
PARENT_SIZE = 469973448
PARENT_COMMIT = '1b939d6f21c31940eecf45192193b9843b2f331f'
PARENT_MANIFEST_SHA = 'e3ec7f6410e5ec101181f57dbeb0f53b1e0724e688e4aeccff7852c495c9e206'
COMPANION_SHA = '97365b6f2ee80667f86c524ed18460f867acc0fee63860e1f0e5bf1e8adc5c48'
HISTORICAL_SHA = '48c52527f49c4e574043a8873676daeaf867d5c0a79067bada5d632773b4cec5'
RUNTIME_PREFIXES = ('runtime/', 'models/', 'ocr/', 'native-runtime/',
                    'audio/', 'citation-runtime/')
HISTORICAL_PREFIXES = RUNTIME_PREFIXES[:4]
RUNTIME_FLAGS = ('bundled_python', 'bundled_model', 'bundled_ocr',
                 'bundled_native_runtime', 'bundled_audio', 'bundled_citation_runtime')


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def write_new(path, raw):
    with path.open('xb') as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def run(command, *, cwd, log, commands):
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
        HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', DO_NOT_TRACK='1',
        HF_HUB_DISABLE_TELEMETRY='1', ORT_DISABLE_TELEMETRY='1',
        GIT_TERMINAL_PROMPT='0', PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1')
    completed = subprocess.run(command, cwd=cwd, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=240)
    record = {'command': command, 'cwd': str(cwd), 'exit_code': completed.returncode}
    commands.append(record)
    with log.open('a', encoding='utf-8') as output:
        output.write(json.dumps(record) + '\n' + completed.stdout + '\n')
    if completed.returncode:
        raise RuntimeError('Build command failed; inspect ' + str(log))
    return completed.stdout.strip()


def constant(path, name):
    """Read a committed literal pin without importing optional runtime engines."""
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise ValueError('Expected literal source pin is missing: ' + name)


def read_member(archive, name, files, *, limit=8 * CHUNK):
    info = archive.getinfo(PREFIX + name)
    if info.file_size > limit:
        raise ValueError('Metadata exceeds its read bound: ' + name)
    raw = archive.read(info)
    if len(raw) != info.file_size or hashlib.sha256(raw).hexdigest() != files[name]:
        raise ValueError('Pinned archive metadata changed: ' + name)
    return raw


def member_json(archive, name, files, delivery):
    return json.loads(read_member(archive, name, files), object_pairs_hook=delivery._unique)


def pe64_member(archive, name):
    """Inspect bounded PE headers; do not load or execute the binary."""
    with archive.open(PREFIX + name) as stream:
        head = stream.read(64)
        if len(head) != 64 or head[:2] != b'MZ':
            raise ValueError('Expected a Windows PE file: ' + name)
        offset, = struct.unpack_from('<I', head, 60)
        if not 64 <= offset <= CHUNK:
            raise ValueError('PE header exceeds its inspection bound: ' + name)
        stream.seek(offset)
        pe = stream.read(26)
        if (len(pe) != 26 or pe[:6] != b'PE\0\0\x64\x86'
                or struct.unpack_from('<H', pe, 24)[0] != 0x20b):
            raise ValueError('Expected Windows AMD64 PE32+: ' + name)


def runtime_contracts(archive, manifest, packaging, historical, historical_path, delivery, source_payload_size):
    """Bind unchanged bytes to current pins and historical verifier evidence.

    Production archive verification checks every payload SHA/CRC/size before and
    after assembly. No expanded-runtime verification or execution is claimed.
    """
    files = manifest['files']
    infos = {info.filename[len(PREFIX):]: info for info in archive.infolist()}
    if not all(manifest.get(flag) is True for flag in RUNTIME_FLAGS):
        raise ValueError('The parent does not contain the complete V3 runtime set.')
    if (historical.get('status') != 'PASS' or historical.get('previous_sha256') != HISTORICAL_SHA
            or historical.get('all_entry_sha256_crc32_and_sizes') != 'PASS'
            or historical.get('installer_manifest_verifier') != 'PASS'):
        raise ValueError('The independently retained historical verification is missing.')
    old = {record['name']: record for record in historical['files']}
    if len(old) != len(historical['files']):
        raise ValueError('Historical file inventory contains duplicates.')
    history_counts = {}
    for prefix in HISTORICAL_PREFIXES:
        selected = {name for name in files if name.startswith(prefix)}
        if not selected or selected != {name for name in old if name.startswith(prefix)}:
            raise ValueError('Historical runtime inventory changed: ' + prefix)
        for name in selected:
            record, info = old[name], infos[name]
            if (record['sha256'] != files[name] or record['size'] != info.file_size
                    or record['crc32'] != info.CRC):
                raise ValueError('Historical runtime payload changed: ' + name)
        history_counts[prefix] = len(selected)

    source = packaging.parent / 'src' / 'carr_library'
    approved = constant(source / 'semantic.py', 'APPROVED_MANIFEST')
    model = member_json(archive, 'models/minilm-manifest.json', files, delivery)
    model_files = {name.removeprefix('models/minilm/'): value for name, value in files.items()
                   if name.startswith('models/minilm/')}
    if model != approved or model_files != approved['hashes']:
        raise ValueError('Preserved MiniLM does not match the final source approved manifest.')
    if read_member(archive, 'python-provenance.json', files) != (packaging / 'windows-python.json').read_bytes():
        raise ValueError('Preserved Python differs from the final source pin.')
    native_source = source / 'native_runtime.py'
    native = member_json(archive, 'native-runtime/native-manifest.json', files, delivery)
    native_files = constant(native_source, 'FILES')
    source_lock = constant(native_source, 'SOURCE_LOCK_SHA256')
    if (set(native) != {'schema', 'target', 'use_scope', 'source_lock_sha256', 'files'}
            or type(native['schema']) is not int or native['schema'] != 1
            or native['target'] != 'Windows x64'
            or native['use_scope'] != 'private-local-installation'
            or native['source_lock_sha256'] != source_lock or set(native['files']) != native_files):
        raise ValueError('Preserved native supplement differs from the current contract.')
    expected_native = {'native-runtime/' + name: value for name, value in native['files'].items()}
    expected_native['native-runtime/native-manifest.json'] = files['native-runtime/native-manifest.json']
    if expected_native != {name: value for name, value in files.items() if name.startswith('native-runtime/')}:
        raise ValueError('Native inner/outer inventories differ.')
    lock_name = 'native-runtime/provenance/source-lock.json'
    if (files[lock_name] != source_lock or
            read_member(archive, lock_name, files) != (packaging / 'native-runtime/source-lock.json').read_bytes()):
        raise ValueError('Native provenance differs from the final source.')
    for name, (size, expected) in constant(native_source, 'DLLS').items():
        name = 'native-runtime/bin/' + name
        if files[name] != expected or infos[name].file_size != size:
            raise ValueError('Native DLL differs from its reviewed identity.')
        pe64_member(archive, name)
    for name, expected in constant(native_source, 'HOST_DLLS').items():
        if files['runtime/' + name] != expected:
            raise ValueError('Python host DLL compatibility changed.')

    ocr = member_json(archive, 'ocr/ocr-manifest.json', files, delivery)
    if (ocr.get('schema') != 1 or ocr.get('target') != 'win_amd64'
            or ocr.get('executable') != 'tesseract.exe' or ocr.get('tessdata') != 'tessdata'
            or not isinstance(ocr.get('files'), dict)):
        raise ValueError('Unexpected portable OCR contract.')
    expected_ocr = {'ocr/' + name: record['sha256'] for name, record in ocr['files'].items()}
    expected_ocr['ocr/ocr-manifest.json'] = files['ocr/ocr-manifest.json']
    if expected_ocr != {name: value for name, value in files.items() if name.startswith('ocr/')}:
        raise ValueError('OCR inner/outer inventories differ.')
    for name, record in ocr['files'].items():
        delivery._name(name)
        if set(record) != {'size', 'sha256'} or infos['ocr/' + name].file_size != record['size']:
            raise ValueError('OCR payload size or identity changed.')
    corresponding = ocr.get('provenance', {}).get('corresponding_source', {})
    if (corresponding.get('filename') != delivery.SOURCES
            or corresponding.get('sha256') != files[delivery.SOURCES]
            or corresponding.get('size') != old[delivery.SOURCES]['size']
            or corresponding.get('size') != source_payload_size):
        raise ValueError('OCR corresponding-source binding changed.')

    audio_helper = load_module('stream_audio_pins', packaging / 'prepare_windows_audio.py')
    audio, audio_hash = audio_helper.load_lock(packaging / 'windows-audio.lock.json')
    if read_member(archive, 'audio/provenance.json', files) != (packaging / 'windows-audio.lock.json').read_bytes():
        raise ValueError('FFmpeg provenance differs from the final source lock.')
    expected_audio = {'audio/' + name: record['sha256'] for name, record in audio['output_files'].items()}
    expected_audio['audio/provenance.json'] = audio_hash
    if expected_audio != {name: value for name, value in files.items() if name.startswith('audio/')}:
        raise ValueError('FFmpeg inventory differs from the final source lock.')
    for name, record in audio['output_files'].items():
        if infos['audio/' + name].file_size != record['size']:
            raise ValueError('FFmpeg payload size changed.')
        if name.endswith('.exe'):
            pe64_member(archive, 'audio/' + name)
    node_raw = (packaging / 'windows-citations.lock.json').read_bytes()
    node = json.loads(node_raw, object_pairs_hook=delivery._unique)
    if read_member(archive, 'citation-runtime/provenance.json', files) != node_raw:
        raise ValueError('Node provenance differs from the final source lock.')
    expected_node = {'citation-runtime/' + name: value for name, value in node['files'].items()}
    expected_node['citation-runtime/provenance.json'] = hashlib.sha256(node_raw).hexdigest()
    if expected_node != {name: value for name, value in files.items() if name.startswith('citation-runtime/')}:
        raise ValueError('Node inventory differs from the final source lock.')
    pe64_member(archive, 'citation-runtime/node.exe')
    return {'status': 'PASS', 'method': 'Current source pins and preserved sealed archive inventories',
        'historical_report_sha256': digest(historical_path),
        'historical_source_commit': historical['previous_source_commit'],
        'historical_archive_sha256': historical['previous_sha256'],
        'historical_payload_counts': history_counts, 'model_revision': model['revision'],
        'model_files': len(model_files), 'ocr_manifest_sha256': files['ocr/ocr-manifest.json'],
        'ocr_files': len(ocr['files']), 'ocr_languages': sorted(name.split('/')[-1][:-12]
            for name in ocr['files'] if name.startswith('tessdata/') and name.endswith('.traineddata')),
        'native_source_lock_sha256': source_lock, 'python_provenance_sha256': files['python-provenance.json'],
        'audio_lock_sha256': audio_hash, 'citation_lock_sha256': hashlib.sha256(node_raw).hexdigest(),
        'native_execution': 'NOT_RUN', 'model_inference': 'NOT_RUN'}


def copy_member(archive, name, target, expected):
    target.parent.mkdir(parents=True, exist_ok=True)
    value = hashlib.sha256()
    with archive.open(PREFIX + name) as incoming, target.open('xb') as output:
        while block := incoming.read(CHUNK):
            value.update(block)
            output.write(block)
    if value.hexdigest() != expected:
        raise ValueError('Extracted build input changed: ' + name)


def wheel_licenses(stream):
    with ZipFile(stream) as wheel:
        return {name.split('.dist-info/licenses/', 1)[1]: hashlib.sha256(wheel.read(name)).hexdigest()
                for name in wheel.namelist() if '.dist-info/licenses/' in name}


def stream_application(parent, destination, files, replacements, manifest_raw, delivery):
    """Write a fresh seekable ZIP, never append over or expand the old bundle."""
    expected = dict(files)
    expected.pop(delivery.SOURCES)
    expected[delivery.MANIFEST] = hashlib.sha256(manifest_raw).hexdigest()
    records = {}
    with delivery._file(parent) as parent_stream, ZipFile(parent_stream) as old:
        with destination.open('xb') as target:
            with ZipFile(target, 'w', compression=ZIP_DEFLATED, compresslevel=6) as new:
                for index, (name, expected_hash) in enumerate(sorted(expected.items()), 1):
                    info = ZipInfo(PREFIX + name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    info.compress_type = ZIP_DEFLATED
                    with ExitStack() as contexts:
                        if name == delivery.MANIFEST:
                            incoming = io.BytesIO(manifest_raw)
                            expected_size, origin = len(manifest_raw), 'new-manifest'
                        elif name in replacements:
                            incoming = contexts.enter_context(delivery._file(replacements[name]))
                            expected_size, origin = os.fstat(incoming.fileno()).st_size, 'current-source-overlay'
                        else:
                            old_info = old.getinfo(PREFIX + name)
                            incoming = contexts.enter_context(old.open(old_info))
                            expected_size, origin = old_info.file_size, 'verified-parent'
                        if expected_size > delivery.MAX_FILE:
                            raise ValueError('Output file exceeds the production size limit.')
                        value, crc, size = hashlib.sha256(), 0, 0
                        with new.open(info, 'w', force_zip64=True) as output:
                            while block := incoming.read(CHUNK):
                                size += len(block)
                                if size > expected_size:
                                    raise ValueError('Build input grew during streaming: ' + name)
                                value.update(block)
                                crc = zlib.crc32(block, crc)
                                output.write(block)
                        if value.hexdigest() != expected_hash or size != expected_size or info.CRC != crc:
                            raise ValueError('Streaming SHA256/CRC32/size mismatch: ' + name)
                        records[name] = {'sha256': expected_hash, 'crc32': crc, 'size': size, 'origin': origin}
                    if index % 512 == 0:
                        print(f'Streamed {index} of {len(expected)} entries.', flush=True)
            target.flush()
            os.fsync(target.fileno())
    return records


def build(args):
    commit = args.published_commit
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Use the full exact published source Git SHA.')
    repository = args.repository.absolute()
    if not repository.is_dir() or repository.is_symlink() or '..' in repository.parts:
        raise ValueError('Use a local source repository directory without links or traversal.')
    output = args.output.absolute()
    if os.path.lexists(output) or not output.parent.is_dir() or '..' in output.parts:
        raise ValueError('Choose a new output directory under an existing local parent.')
    filename = args.filename or 'Universal-Research-Library-Application-' + commit[:12] + '.zip'
    if Path(filename).name != filename or not filename.endswith('.zip'):
        raise ValueError('Use a simple application ZIP basename.')
    work = (args.work or HERE / ('stream-work-' + commit[:12])).absolute()
    if os.path.lexists(work) or output.is_relative_to(work) or work.is_relative_to(output):
        raise ValueError('Use fresh, separate work and output directories.')
    work.mkdir(mode=0o700)
    log, commands = work / 'build.log', []
    source = work / 'source'
    git = ['git', '--no-replace-objects']
    run(git + ['clone', '--shared', '--no-checkout', str(repository), str(source)],
        cwd=work, log=log, commands=commands)
    run(git + ['checkout', '--detach', commit], cwd=source, log=log, commands=commands)
    if run(git + ['rev-parse', 'HEAD'], cwd=source, log=log, commands=commands) != commit:
        raise ValueError('The frozen source differs from the published commit.')
    tree = run(git + ['rev-parse', 'HEAD^{tree}'], cwd=source, log=log, commands=commands)
    project, packaging = source / 'carr-library', source / 'carr-library' / 'packaging'
    delivery = load_module('stream_production_delivery', packaging / 'create_delivery.py')
    builder = load_module('stream_production_builder', packaging / 'build_windows_bundle.py')
    installer = load_module('stream_production_installer', packaging / 'install_windows.py')
    wheel_verifier = load_module('stream_production_wheel', packaging / 'source_wheel.py')
    delivery._name(filename)
    delivery._directories(output.parent)
    delivery._directories(work)
    parent, companion = delivery._absolute(args.parent_application), delivery._absolute(args.source_companion)
    for value in (parent, companion):
        if output.is_relative_to(value) or value.is_relative_to(output):
            raise ValueError('Inputs must remain outside the new delivery directory.')
    print('Verifying the pinned parent application and source companion.', flush=True)
    # Authenticate the outer bytes before ZipFile allocates its central directory.
    with delivery._file(parent) as stream:
        outer_sha, outer_size = delivery._stream(stream, lambda: False)
    if outer_sha != args.parent_application_sha256 or outer_size != args.parent_application_size:
        raise ValueError('The parent application differs from its independently recorded bytes.')
    with delivery._file(parent) as stream, ZipFile(stream) as archive:
        manifest_info = archive.getinfo(PREFIX + delivery.MANIFEST)
        if manifest_info.file_size > delivery.MAX_MANIFEST:
            raise ValueError('Parent manifest is too large.')
        parent_raw = archive.read(manifest_info)
    if hashlib.sha256(parent_raw).hexdigest() != args.parent_manifest_sha256:
        raise ValueError('Parent manifest does not match its independently recorded SHA256.')
    previous = delivery._manifest(parent_raw, args.parent_source_commit)
    initial = delivery.verify_archives(parent, companion, parent_raw, args.parent_source_commit)
    if (initial['application']['sha256'] != args.parent_application_sha256
            or initial['application']['bytes'] != args.parent_application_size
            or initial['sources']['sha256'] != args.source_companion_sha256):
        raise ValueError('A parent archive does not match its independently recorded identity.')
    historical_path = delivery._absolute(args.runtime_report)
    with delivery._file(historical_path) as stream:
        historical = json.load(stream, object_pairs_hook=delivery._unique)
    pins = builder.read_lock(packaging / 'windows-semantic-cp311.lock')
    dependency_dir, wheel_dir, minimal = work / 'dependency-wheels', work / 'application-wheel', work / 'minimal-bundle'
    dependency_dir.mkdir()
    wheel_dir.mkdir()
    dependency_records, old_application, old_licenses = [], None, None
    with delivery._file(parent) as stream, ZipFile(stream) as archive:
        contract = runtime_contracts(archive, previous, packaging, historical, historical_path,
                                     delivery, initial['sources']['payload_bytes'])
        for name in sorted(n for n in previous['files'] if n.startswith('wheelhouse/')):
            if name.count('/') != 1 or not name.endswith('.whl'):
                raise ValueError('Unexpected parent wheelhouse path.')
            from packaging.utils import parse_wheel_filename
            distribution, version, _build, _tags = parse_wheel_filename(Path(name).name)
            if distribution == 'universal-research-library':
                if old_application is not None:
                    raise ValueError('The parent contains more than one application wheel.')
                old_application = name
                old_licenses = wheel_licenses(io.BytesIO(read_member(archive, name, previous['files'], limit=64 * CHUNK)))
                continue
            target = dependency_dir / Path(name).name
            copy_member(archive, name, target, previous['files'][name])
            distribution, version, _metadata = builder.wheel_metadata(target)
            if (distribution not in pins or str(version) != pins[distribution][0]
                    or previous['files'][name] not in pins[distribution][1]):
                raise ValueError('Parent dependency is not admitted by the current exact lock.')
            dependency_records.append({'name': distribution, 'version': str(version),
                'file': target.name, 'sha256': previous['files'][name], 'bytes': target.stat().st_size})
    if (old_application is None or len(dependency_records) != len(pins)
            or {record['name'] for record in dependency_records} != set(pins)):
        raise ValueError('The parent lacks the exact current dependency closure.')

    print('Building and verifying the exact-source application wheel and minimal production bundle.', flush=True)
    run([sys.executable, '-m', 'pip', '--isolated', '--disable-pip-version-check', 'wheel',
         '--no-index', '--no-deps', '--no-build-isolation', '--wheel-dir', str(wheel_dir), str(project)],
        cwd=source, log=log, commands=commands)
    wheels = list(wheel_dir.glob('*.whl'))
    if len(wheels) != 1:
        raise ValueError('Expected exactly one fresh application wheel.')
    application = wheels[0]
    source_identity = wheel_verifier.verify_application_wheel(application, source, commit)
    if not old_licenses.items() <= wheel_licenses(application).items():
        raise ValueError('An application license changed or disappeared.')
    sbom = packaging / 'windows-semantic-sbom.cdx.json'
    if json.loads(sbom.read_bytes()).get('metadata', {}).get('component', {}).get('version') != source_identity['project_version']:
        raise ValueError('Runtime SBOM root version differs from the application.')
    produced = builder.build_bundle(application, dependency_dir, packaging / 'windows-semantic-cp311.lock',
                                   minimal, sbom=sbom, source_commit=commit)
    installer.verify(minimal)
    current = json.loads((minimal / 'bundle-manifest.json').read_bytes(), object_pairs_hook=delivery._unique)
    if current['source_commit'] != commit or current['target'] != previous['target']:
        raise ValueError('The production builder changed the source/target identity.')
    files = dict(previous['files'])
    files.pop(old_application)
    files.update(current['files'])
    replacements = {name: minimal / name for name in current['files']}
    removed_bytes = 0
    for record in dependency_records:
        name = 'wheelhouse/' + record['file']
        if current['files'].get(name) != previous['files'].get(name):
            raise ValueError('Production builder changed an unchanged dependency.')
        # Remove only our own verified duplicate minimal copies. All extracted
        # inputs, shared archives and existing verification reports remain.
        removed_bytes += replacements.pop(name).stat().st_size
        (minimal / name).unlink()
    overlay = work / 'overlay'
    overlay.mkdir()
    write_new(overlay / 'application-source.json', json_bytes(source_identity))
    replacements['application-source.json'] = overlay / 'application-source.json'
    tracked = run(git + ['ls-tree', '-r', '--name-only', commit, '--', 'carr-library/docs'],
                  cwd=source, log=log, commands=commands).splitlines()
    for name in tracked:
        path = source / name
        relative = path.relative_to(project / 'docs').as_posix()
        if path.suffix.casefold() in {'.md', '.txt'}:
            replacements['docs/' + relative] = path
    replacements['START_V3_UK.txt'] = project / 'docs' / 'START_V3_UK.txt'
    for item in args.guide:
        name, separator, supplied = item.partition('=')
        if (not separator or '/' in name or Path(name).suffix.casefold() not in {'.md', '.txt'}
                or name in replacements or name in files):
            raise ValueError('An additional guide needs a unique top-level .md/.txt NAME=PATH.')
        delivery._name(name)
        path = delivery._absolute(supplied)
        with delivery._file(path) as stream:
            raw = stream.read(8 * CHUNK + 1)
        if len(raw) > 8 * CHUNK:
            raise ValueError('The additional guide exceeds its size limit.')
        snapshot = overlay / name
        write_new(snapshot, raw)
        replacements[name] = snapshot
    for name, path in replacements.items():
        delivery._plain(path)
        files[name] = digest(path)
    runtime_names = {name for name in previous['files'] if name.startswith(RUNTIME_PREFIXES)}
    if any(files.get(name) != previous['files'][name] for name in runtime_names):
        raise ValueError('Streaming overlay attempted to alter a preserved runtime.')
    legal_names = {name for name in previous['files'] if
        ('license' in name.casefold() or name.startswith('notices/')) and not name.endswith('/SOURCES.json')}
    if any(files.get(name) != previous['files'][name] for name in legal_names):
        raise ValueError('A retained runtime/dependency license or notice changed.')
    manifest = dict(previous, source_commit=commit, parent_source_commit=previous['source_commit'],
        parent_manifest_sha256=args.parent_manifest_sha256, use_scope='private-personal-installation',
        files=dict(sorted(files.items())))
    manifest_raw = json_bytes(manifest)
    delivery._manifest(manifest_raw, commit)
    install_pins = builder.read_lock(minimal / 'install.lock')
    if (set(install_pins) != set(pins) | {'universal-research-library'}
            or install_pins['universal-research-library'] !=
               (source_identity['project_version'], {source_identity['wheel_sha256']})):
        raise ValueError('The installation lock is not bound to the new application.')
    if run(git + ['diff', '--name-only', 'HEAD'], cwd=source, log=log, commands=commands):
        raise ValueError('Tracked source changed while building the delivery.')
    estimate = parent.stat().st_size + sum(path.stat().st_size for path in replacements.values()) + len(manifest_raw)
    required_free = estimate + args.reserve_bytes
    free = shutil.disk_usage(output.parent).free
    if free < required_free:
        raise OSError(f'Insufficient free space for streamed output: {free} available; '
                      f'{required_free} estimated including reserve. Build inputs are retained in {work}.')
    print(f'Streaming final ZIP; {free} bytes free, {required_free} estimated with reserve.', flush=True)
    stage = Path(tempfile.mkdtemp(prefix='.stream-delivery-', dir=output.parent))
    final_zip = stage / filename
    records = stream_application(parent, final_zip, files, replacements, manifest_raw, delivery)
    print('Verifying the final application and unchanged source companion with production verify_archives.', flush=True)
    verified = delivery.verify_archives(final_zip, companion, manifest_raw, commit)
    if verified['sources'] != initial['sources']:
        raise ValueError('The source companion changed during assembly.')
    with ZipFile(final_zip) as archive:
        if len(archive.infolist()) != len(records):
            raise ValueError('The final ZIP file count changed.')
        for info in archive.infolist():
            record = records[info.filename[len(PREFIX):]]
            if info.CRC != record['crc32'] or info.file_size != record['size']:
                raise ValueError('The final ZIP CRC or size differs from the streamed inventory.')
    final_source_identity = wheel_verifier.verify_application_wheel(application, source, commit)
    if source_identity != final_source_identity:
        raise ValueError('The source-verified wheel changed after assembly.')
    for value, record in ((final_zip, verified['application']), (companion, verified['sources'])):
        if delivery._stamp(delivery._plain(value)) != record['_verified_stamp']:
            raise ValueError('A verified archive changed before publication.')
    for record in verified.values():
        record.pop('_verified_stamp')
    verified['application'].update(path=filename, reused=False)
    verified['sources'].update(path=str(companion), reused=True)
    report = {'schema': 1, 'kind': 'private-streamed-successor-delivery', 'status': 'PASS',
        'source_commit': commit, 'source_tree': tree, 'source_checkout': str(source),
        'output': str(output), 'manifest_sha256': hashlib.sha256(manifest_raw).hexdigest(),
        'manifest_payload_files': len(files), 'archive_prefix': PREFIX, 'archives': verified,
        'application_wheel': source_identity, 'dependency_wheels': dependency_records,
        'dependency_wheel_count': len(dependency_records), 'production_builder': produced,
        'production_minimal_installer_verifier': 'PASS (before removing only duplicate dependency copies)',
        'production_two_archive_verifier': 'PASS', 'streamed_inventory_sha_crc_size': 'PASS',
        'preserved_runtime_files': len(runtime_names), 'preserved_runtime_payload_bytes':
            sum(records[name]['size'] for name in runtime_names),
        'preserved_legal_payloads': len(legal_names), 'runtime_contracts': contract,
        'parent_source_commit': previous['source_commit'], 'parent_manifest_sha256': args.parent_manifest_sha256,
        'parent_application_sha256': initial['application']['sha256'],
        'parent_application_bytes': initial['application']['bytes'],
        'dependency_lock_sha256': digest(packaging / 'windows-semantic-cp311.lock'),
        'sbom_sha256': digest(sbom), 'application_source_record_regenerated': True,
        'assembly': {'mode': 'ZIP member streaming; no full runtime expansion',
            'buffer_bytes': CHUNK, 'duplicate_minimal_dependency_bytes_removed': removed_bytes,
            'extracted_dependency_bytes_retained': sum(item['bytes'] for item in dependency_records),
            'estimated_output_and_reserve_bytes': required_free, 'free_bytes_before_output': free},
        'instructions': delivery.INSTRUCTIONS,
        'boundaries': {'both_archives_required': True, 'native_windows_execution': 'NOT_RUN',
            'model_inference': 'NOT_RUN', 'manual_nvda': 'NOT_RUN',
            'whole_expanded_bundle_installer_verifier': 'NOT_RUN; production minimal builder and exact archive union verified',
            'historical_native_evidence_relabelled': False, 'network_downloads': False,
            'public_binary_distribution': False}}
    write_new(stage / 'bundle-manifest.json', manifest_raw)
    write_new(stage / 'archive-inventory.json', json_bytes(records))
    write_new(stage / 'delivery-verification.json', json_bytes(report))
    write_new(stage / 'START_HERE_UK.txt', delivery._start_here(filename, companion.name, True).encode('utf-8'))
    write_new(work / 'build-commands.json', json_bytes(commands))
    parent_fd = (os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                 if os.name != 'nt' else None)
    try:
        delivery._publish(stage, output, parent_fd)
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True, type=Path)
    parser.add_argument('--published-commit', required=True)
    parser.add_argument('--output', required=True, type=Path, help='New delivery directory, atomically published after verification')
    parser.add_argument('--source-companion', required=True, type=Path)
    parser.add_argument('--filename', help='Optional simple application ZIP filename')
    parser.add_argument('--work', type=Path, help='Fresh small source/wheel build directory, retained')
    parser.add_argument('--parent-application', type=Path, default=HERE / 'reuse-v3-core' / PARENT_NAME)
    parser.add_argument('--parent-application-sha256', default=PARENT_SHA)
    parser.add_argument('--parent-application-size', type=int, default=PARENT_SIZE)
    parser.add_argument('--parent-source-commit', default=PARENT_COMMIT)
    parser.add_argument('--parent-manifest-sha256', default=PARENT_MANIFEST_SHA)
    parser.add_argument('--source-companion-sha256', default=COMPANION_SHA)
    parser.add_argument('--runtime-report', type=Path, default=HERE / 'previous-payload-verification.json')
    parser.add_argument('--guide', action='append', default=[], help='Additional unique top-level .txt/.md guide as NAME=PATH')
    parser.add_argument('--reserve-bytes', type=int, default=32 * CHUNK)
    args = parser.parse_args(argv)
    if args.reserve_bytes < 8 * CHUNK:
        parser.error('--reserve-bytes must preserve at least 8 MiB.')
    for field in ('parent_application_sha256', 'parent_manifest_sha256', 'source_companion_sha256'):
        if not re.fullmatch('[0-9a-f]{64}', getattr(args, field)):
            parser.error(field + ' must be a full lowercase SHA256.')
    report = build(args)
    print(json.dumps({key: report[key] for key in ('status', 'source_commit', 'source_tree', 'output',
          'manifest_sha256', 'archives', 'dependency_wheel_count', 'preserved_runtime_files')}, indent=2))


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    main()
