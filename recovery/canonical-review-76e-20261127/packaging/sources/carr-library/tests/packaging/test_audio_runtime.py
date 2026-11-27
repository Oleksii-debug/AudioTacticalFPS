"""The FFmpeg payload is bound to reviewed bytes, not a self-signed manifest."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import zipfile

import pytest

from carr_library import audio_runtime as runtime


PREPARE = Path(__file__).resolve().parents[2] / 'packaging/prepare_audio_runtime.py'
spec = importlib.util.spec_from_file_location('carr_prepare_audio_runtime', PREPARE)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def binary(machine=0x8664):
    raw = bytearray(256)
    raw[:2] = b'MZ'
    struct.pack_into('<I', raw, 60, 128)
    raw[128:132] = b'PE\0\0'
    struct.pack_into('<H', raw, 132, machine)
    return bytes(raw)


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    sources = tmp_path / 'supplied sources'
    sources.mkdir()
    packaging = tmp_path / 'packaging'
    templates = packaging / 'audio-runtime'
    templates.mkdir(parents=True)
    (templates / 'README.txt').write_bytes(b'Untouched synthetic notice.\n')
    payload = {path: binary() for path in runtime.TOOLS.values()}
    payload.update({'bin/avutil.dll': binary(), 'LICENSE.txt': b'Synthetic fixture, not a licensed runtime.\n'})
    archive = tmp_path / 'synthetic.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        for name, raw in payload.items():
            z.writestr('upstream/' + name, raw)
    lock = {'schema': 1, 'package_id': runtime.PACKAGE_ID, 'target': 'Windows x64',
            'version': 'synthetic-only', 'executables': runtime.TOOLS,
            'archive': {'root': 'upstream', 'size': archive.stat().st_size,
                        'sha256': digest(archive.read_bytes())},
            'archive_files': {name: {'size': len(raw), 'sha256': digest(raw)} for name, raw in payload.items()},
            'extra_files': {'README.txt': {'size': 28, 'sha256': digest((templates / 'README.txt').read_bytes())}}}
    lock['extra_files']['README.txt']['size'] = (templates / 'README.txt').stat().st_size
    lock_raw = encode(lock)
    (templates / 'source-lock.json').write_bytes(lock_raw)
    monkeypatch.setattr(prepare, 'HERE', packaging)
    monkeypatch.setattr(runtime, 'SOURCE_LOCK_SHA256', digest(lock_raw))
    root = tmp_path / 'program with spaces ž Україна'
    root.mkdir()
    output = root / runtime.DIRECTORY
    report = prepare.prepare(archive, sources, output)
    inner = json.loads((output / runtime.MANIFEST).read_text())
    outer = {'schema': 1, 'target': 'Windows x64 / CPython 3.11', 'bundled_audio_runtime': True,
             'files': {runtime.DIRECTORY + '/' + n: h for n, h in inner['files'].items()}}
    outer['files'][runtime.DIRECTORY + '/' + runtime.MANIFEST] = report['manifest_sha256']
    (root / 'bundle-manifest.json').write_bytes(encode(outer))
    return root, output, report, archive, sources, templates, lock


def test_prepared_complete_payload_has_exact_tools_and_unchanged_notice(bundle):
    root, output, report, *_ = bundle
    assert runtime.verify_audio_runtime(output) == report
    assert runtime.bundled_audio_tools(root) == report['tools']
    assert (output / 'README.txt').read_bytes() == b'Untouched synthetic notice.\n'
    assert report['native_execution'] == 'NOT_RUN'
    assert set(report['tools']) == {'ffmpeg', 'ffprobe', 'ffplay'}


def test_coordinated_binary_and_inner_outer_manifest_tamper_cannot_change_reviewed_identity(bundle):
    root, output, *_ = bundle
    target = output / 'bin/avutil.dll'
    target.write_bytes(binary() + b'tampered')
    inner_path = output / runtime.MANIFEST
    inner = json.loads(inner_path.read_text())
    inner['files']['bin/avutil.dll'] = digest(target.read_bytes())
    inner_path.write_bytes(encode(inner))
    outer_path = root / 'bundle-manifest.json'
    outer = json.loads(outer_path.read_text())
    outer['files']['audio-runtime/bin/avutil.dll'] = digest(target.read_bytes())
    outer['files']['audio-runtime/' + runtime.MANIFEST] = digest(inner_path.read_bytes())
    outer_path.write_bytes(encode(outer))
    with pytest.raises(ValueError, match='reviewed source inventory'):
        runtime.bundled_audio_tools(root)


@pytest.mark.parametrize('change', ['missing', 'extra', 'bytes', 'outer', 'lock'])
def test_missing_extra_changed_or_rebound_payload_is_rejected(bundle, change):
    root, output, *_ = bundle
    if change == 'missing':
        (output / 'bin/ffprobe.exe').unlink()
    elif change == 'extra':
        (output / 'bin/unreviewed.dll').write_bytes(binary())
    elif change == 'bytes':
        (output / 'bin/ffprobe.exe').write_bytes(b'XX' + binary()[2:])
    elif change == 'outer':
        p = root / 'bundle-manifest.json'
        value = json.loads(p.read_text())
        value['files']['audio-runtime/bin/ffprobe.exe'] = 'a' * 64
        p.write_bytes(encode(value))
    else:
        (output / 'provenance/source-lock.json').write_bytes(b'{}\n')
    with pytest.raises(ValueError):
        runtime.bundled_audio_tools(root)


@pytest.mark.parametrize('flag', [False, 1, None, 'true'])
def test_unannounced_or_non_boolean_runtime_flag_is_rejected(bundle, flag):
    root, *_ = bundle
    p = root / 'bundle-manifest.json'
    value = json.loads(p.read_text())
    value['bundled_audio_runtime'] = flag
    p.write_bytes(encode(value))
    with pytest.raises(ValueError):
        runtime.bundled_audio_tools(root)


def test_duplicate_manifest_keys_fail_closed(bundle):
    root, output, *_ = bundle
    p = output / runtime.MANIFEST
    p.write_text('{"schema":1,' + p.read_text()[1:])
    with pytest.raises(ValueError, match='Duplicate'):
        runtime.bundled_audio_tools(root)


def test_source_checkout_and_non_windows_inventory_check_do_not_change_path(tmp_path, bundle, monkeypatch):
    root, *_ = bundle
    monkeypatch.setenv('PATH', 'user-tool-setting')
    monkeypatch.setattr(runtime.sys, 'platform', 'linux')
    assert runtime.activate_bundled_audio_runtime(root) is None
    assert os.environ['PATH'] == 'user-tool-setting'
    assert runtime.bundled_audio_tools(tmp_path) is None


def test_windows_activation_prepends_verified_directory_once_and_keeps_user_paths(bundle, monkeypatch):
    root, output, report, *_ = bundle
    monkeypatch.setenv('PATH', os.pathsep.join(['user-tools', 'system-tools']))
    monkeypatch.setattr(runtime.sys, 'platform', 'win32')
    assert runtime.activate_bundled_audio_runtime(root) == report['tools']
    assert runtime.activate_bundled_audio_runtime(root) == report['tools']
    assert os.environ['PATH'].split(os.pathsep) == [str(output / 'bin'), 'user-tools', 'system-tools']


def test_tampered_payload_never_mutates_path(bundle, monkeypatch):
    root, output, *_ = bundle
    monkeypatch.setenv('PATH', 'untouched')
    monkeypatch.setattr(runtime.sys, 'platform', 'win32')
    (output / 'bin/ffmpeg.exe').unlink()
    with pytest.raises(ValueError):
        runtime.activate_bundled_audio_runtime(root)
    assert os.environ['PATH'] == 'untouched'


def test_discovery_uses_only_near_interpreter_prefixes(bundle, monkeypatch):
    root, *_ = bundle
    monkeypatch.setattr(runtime.sys, 'prefix', str(root / '.venv'))
    monkeypatch.setattr(runtime.sys, 'base_prefix', str(root / 'runtime'))
    assert runtime._discover() == root
    monkeypatch.setattr(runtime.sys, 'prefix', str(root / 'unrelated/deeper/python'))
    monkeypatch.setattr(runtime.sys, 'base_prefix', str(root / 'unrelated/deeper/python'))
    assert runtime._discover() is None


def test_sibling_source_checkout_name_is_not_an_installed_runtime(tmp_path, monkeypatch):
    (tmp_path / 'audio-runtime/carr-library').mkdir(parents=True)
    monkeypatch.setattr(runtime.sys, 'prefix', str(tmp_path / 'test-venv'))
    monkeypatch.setattr(runtime.sys, 'base_prefix', str(tmp_path / 'test-venv'))
    assert runtime._discover() is None
    assert runtime.activate_bundled_audio_runtime() is None


def test_unsealed_runtime_bin_is_still_rejected_by_implicit_discovery(tmp_path, monkeypatch):
    (tmp_path / 'audio-runtime/bin').mkdir(parents=True)
    monkeypatch.setattr(runtime.sys, 'prefix', str(tmp_path / 'test-venv'))
    monkeypatch.setattr(runtime.sys, 'base_prefix', str(tmp_path / 'test-venv'))
    with pytest.raises(ValueError, match='no enclosing bundle manifest'):
        runtime.activate_bundled_audio_runtime()


def test_preparation_never_overwrites_existing_directory(bundle):
    _, output, _, archive, sources, *_ = bundle
    original = (output / runtime.MANIFEST).read_bytes()
    with pytest.raises(ValueError, match='new directory'):
        prepare.prepare(archive, sources, output)
    assert (output / runtime.MANIFEST).read_bytes() == original


def test_wrong_download_or_sources_fail_before_output_publication(bundle):
    root, _, _, archive, sources, templates, _ = bundle
    archive.write_bytes(b'wrong archive')
    output = root / 'new-runtime'
    with pytest.raises(ValueError, match='pinned upstream archive'):
        prepare.prepare(archive, sources, output)
    assert not output.exists()


def test_parent_traversal_and_relative_runtime_paths_are_rejected(bundle):
    root, output, *_ = bundle
    for path in ('audio-runtime', root / 'audio-runtime/../audio-runtime'):
        with pytest.raises(ValueError, match='absolute'):
            runtime.verify_audio_runtime(path)


def test_wrong_pe_architecture_fails_even_for_internally_consistent_inventory(bundle, monkeypatch):
    _, output, _, _, _, _, _ = bundle
    target = output / 'bin/ffmpeg.exe'
    target.write_bytes(binary(0x14c))
    lock_path = output / 'provenance/source-lock.json'
    lock = json.loads(lock_path.read_text())
    lock['archive_files']['bin/ffmpeg.exe']['sha256'] = digest(target.read_bytes())
    lock_path.write_bytes(encode(lock))
    monkeypatch.setattr(runtime, 'SOURCE_LOCK_SHA256', digest(lock_path.read_bytes()))
    inner_path = output / runtime.MANIFEST
    inner = json.loads(inner_path.read_text())
    inner['source_lock_sha256'] = runtime.SOURCE_LOCK_SHA256
    inner['files']['provenance/source-lock.json'] = runtime.SOURCE_LOCK_SHA256
    inner['files']['bin/ffmpeg.exe'] = digest(target.read_bytes())
    inner_path.write_bytes(encode(inner))
    with pytest.raises(ValueError, match='Windows x64'):
        runtime.verify_audio_runtime(output)


def test_source_lock_replaced_after_snapshot_read_cannot_rebind_verification(bundle, monkeypatch):
    _, output, *_ = bundle
    lock_path = output / 'provenance/source-lock.json'
    replacement = output / 'provenance/replacement'
    replacement.write_bytes(lock_path.read_bytes().replace(b'synthetic-only', b'synthetic-evil'))
    original_open = Path.open
    swapped = False

    class SnapshotReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, length):
            nonlocal swapped
            raw = self.stream.read(length)
            # The original handle can be closed before the Windows rename;
            # verification must continue using the bytes already captured.
            self.stream.close()
            replacement.replace(lock_path)
            swapped = True
            return raw

    def open_file(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path == lock_path and args and args[0] == 'rb' and not swapped:
            return SnapshotReader(stream)
        return stream

    monkeypatch.setattr(Path, 'open', open_file)
    with pytest.raises(ValueError, match='checksum.*source-lock'):
        runtime.verify_audio_runtime(output)
    assert swapped


def test_preparation_uses_verified_archive_snapshot_after_original_path_replacement(bundle, monkeypatch):
    root, _, _, archive, sources, *_ = bundle
    original_copy = prepare._copy_file

    def copy_then_replace(source, target, record):
        original_copy(source, target, record)
        if source == archive:
            archive.write_bytes(b'replaced after verified copy')

    monkeypatch.setattr(prepare, '_copy_file', copy_then_replace)
    output = root / 'second-runtime'
    report = prepare.prepare(archive, sources, output)
    assert archive.read_bytes() == b'replaced after verified copy'
    assert runtime.verify_audio_runtime(output) == report


def test_untrusted_lock_is_hash_rejected_before_json_is_parsed(bundle):
    _, output, *_ = bundle
    lock_path = output / 'provenance/source-lock.json'
    lock_path.write_bytes(b'this is not JSON')
    with pytest.raises(ValueError, match='reviewed package'):
        runtime._source_lock(lock_path)


def test_unreadable_extra_directory_is_not_silently_omitted(bundle, monkeypatch):
    _, output, *_ = bundle
    hidden = output / 'unreadable-extra'
    hidden.mkdir()
    scan = os.scandir

    def denied(path):
        if Path(path) == hidden:
            raise PermissionError('Synthetic directory enumeration failure')
        return scan(path)

    monkeypatch.setattr(runtime.os, 'scandir', denied)
    with pytest.raises(PermissionError, match='enumeration failure'):
        runtime.verify_audio_runtime(output)


def test_inventory_checks_entry_limit_before_consuming_large_directory(bundle, monkeypatch):
    _, output, *_ = bundle
    empty = output / 'empty-directory'
    empty.mkdir()
    scan = os.scandir
    consumed = []
    monkeypatch.setattr(runtime, 'MAX_FILES', 10)

    class Entries:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def __iter__(self):
            return self

        def __next__(self):
            consumed.append(True)
            if len(consumed) > runtime.MAX_FILES * 2 + 1:
                pytest.fail('Inventory consumed the unbounded directory before checking its limit.')
            with scan(output) as rows:
                return next(row for row in rows if row.name == empty.name)

    def many(path):
        return Entries() if Path(path) == output else scan(path)

    monkeypatch.setattr(runtime.os, 'scandir', many)
    with pytest.raises(ValueError, match='too many filesystem entries'):
        runtime.verify_audio_runtime(output)
    assert len(consumed) == runtime.MAX_FILES * 2 + 1
