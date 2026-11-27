"""Offline archive and installed-runtime checks using inert synthetic PE fixtures."""
from hashlib import sha256
import io
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import sys
from types import SimpleNamespace
from zipfile import ZipFile, ZipInfo

import pytest

from carr_library.audio import runtime
from carr_library.audio.runtime import tool_path


PRODUCT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('prepare_windows_audio_tested', PRODUCT/'packaging/prepare_windows_audio.py')
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def record(path):
    return dict(size_bytes=path.stat().st_size, sha256=digest(path))


@pytest.fixture
def package(tmp_path, monkeypatch):
    """The fixture changes trust pins locally; no synthetic executable is started."""
    setup = tmp_path/'setup'
    (setup/'audio-runtime').mkdir(parents=True)
    (setup/'audio-runtime/README.txt').write_text('Synthetic private setup notice.\n', encoding='utf-8')
    binary = bytearray(134)
    binary[:2] = b'MZ'
    struct.pack_into('<I', binary, 60, 128)
    binary[128:] = b'PE\0\0\x64\x86'
    files = {f'bin/{name}.exe': bytes(binary) + name.encode('ascii') for name in ('ffmpeg', 'ffprobe', 'ffplay')}
    files['LICENSE'] = b'Synthetic license fixture; no publisher binary.\n'
    archive = tmp_path/'fixture.zip'
    with ZipFile(archive, 'w') as zipped:
        for name, data in files.items():
            zipped.writestr('ffmpeg-fixture/' + name, data)
    lock = dict(version=1, release='fixture', package_root='ffmpeg-fixture',
                archive=dict(url='https://github.com/example/inert.zip', **record(archive)),
                original_files={name: dict(size_bytes=len(data), sha256=sha256(data).hexdigest())
                                for name, data in files.items()})
    source = setup/'audio-runtime/source-lock.json'
    source.write_text(json.dumps(lock), encoding='utf-8')
    monkeypatch.setattr(prepare, 'HERE', setup)
    monkeypatch.setattr(prepare, 'SOURCE_LOCK', source)
    monkeypatch.setattr(prepare, 'SOURCE_LOCK_SHA256', digest(source))
    monkeypatch.setattr(runtime, 'SOURCE_LOCK_SHA256', digest(source))
    # Windows host tests still use inert bytes: native execution is separately
    # required against the actual reviewed archive by the CI setup and probes.
    monkeypatch.setattr(prepare, 'sys', SimpleNamespace(platform='fixture'))
    return SimpleNamespace(root=tmp_path, archive=archive, lock=lock, source=source)


def make_bundle(package):
    root = package.root/'bundle'
    root.mkdir()
    report = prepare.prepare(package.archive, root/runtime.DIRECTORY)
    outer = dict(schema=1, target='Windows x64 / CPython 3.11', bundled_audio=True,
                 files={p.relative_to(root).as_posix(): digest(p)
                        for p in (root/runtime.DIRECTORY).rglob('*') if p.is_file()})
    (root/'bundle-manifest.json').write_text(json.dumps(outer), encoding='utf-8')
    return root, report, outer


def rewrite_inner(root):
    directory = root/runtime.DIRECTORY
    path = directory/runtime.MANIFEST
    manifest = json.loads(path.read_text(encoding='utf-8'))
    manifest['files'] = {p.relative_to(directory).as_posix(): record(p)
                         for p in directory.rglob('*') if p.is_file() and p != path}
    path.write_text(json.dumps(manifest), encoding='utf-8')
    return manifest


def test_checked_publisher_lock_pins_every_original_executable():
    lock = prepare.load_lock()
    assert lock['archive']['sha256'] == 'f9a83be8f97b8989a223a15e4af234accd0842bacf7c126af5a3aae759785e48'
    assert lock['archive']['size_bytes'] == 264268459
    assert lock['original_files']['bin/ffmpeg.exe']['sha256'] == '3ab4a9c404504ebff6662d6a48661db952f88ee7fb7d5949a711cad7a763b2cf'
    assert len(lock['original_files']) == 45
    assert lock['release'] == '9.1'


def test_prepare_and_shared_verifier_bind_exact_original_bytes(package):
    root, report, outer = make_bundle(package)
    assert report['status'] == 'VERIFIED' and report['windows_execution'] == 'NOT_RUN'
    assert runtime._bundle(root) == root/runtime.DIRECTORY
    assert report == runtime.verify_audio_runtime(root/runtime.DIRECTORY)
    with pytest.raises(FileExistsError):
        prepare.prepare(package.archive, root/runtime.DIRECTORY)


def test_wrong_archive_hash_never_publishes(package):
    package.archive.write_bytes(package.archive.read_bytes() + b'changed')
    output = package.root/'never-published'
    with pytest.raises(ValueError, match='reviewed publisher bytes'):
        prepare.prepare(package.archive, output)
    assert not output.exists()


@pytest.mark.parametrize('name', ['ffmpeg-fixture/../escape', '/absolute', 'ffmpeg-fixture/C:/escape',
    'ffmpeg-fixture/bin\\escape', 'ffmpeg-fixture/CON.txt', 'ffmpeg-fixture/trailing.'])
def test_unsafe_archive_member_rejected(package, name):
    malicious = package.root/'unsafe.zip'
    with ZipFile(malicious, 'w') as archive:
        archive.writestr(name, b'never extracted')
    with ZipFile(malicious) as archive, pytest.raises(ValueError):
        prepare._members(archive, package.lock)


def test_archive_link_and_case_collision_rejected(package):
    malicious = package.root/'link.zip'
    link = ZipInfo('ffmpeg-fixture/bin/link')
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(malicious, 'w') as archive:
        archive.writestr(link, b'../../outside')
    with ZipFile(malicious) as archive, pytest.raises(ValueError, match='links'):
        prepare._members(archive, package.lock)
    with ZipFile(malicious, 'w') as archive:
        archive.writestr('ffmpeg-fixture/bin/ffmpeg.exe', b'one')
        archive.writestr('ffmpeg-fixture/bin/FFMPEG.exe', b'two')
    with ZipFile(malicious) as archive, pytest.raises(ValueError, match='colliding'):
        prepare._members(archive, package.lock)


def test_prepare_has_no_implicit_network_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(prepare, 'download_archive', lambda *_: pytest.fail('unexpected network acquisition'))
    with pytest.raises(SystemExit) as error:
        prepare.main(['--output', str(tmp_path/'output')])
    assert error.value.code == 2


def test_runtime_tamper_rejected_even_after_resealing_inner_inventory(package):
    root, _, outer = make_bundle(package)
    path = root/runtime.DIRECTORY/'bin/ffmpeg.exe'
    path.write_bytes(path.read_bytes() + b'changed')
    rewrite_inner(root)
    with pytest.raises(ValueError, match='publisher archive'):
        runtime.verify_audio_runtime(root/runtime.DIRECTORY)


@pytest.mark.parametrize('change', ['missing', 'unknown', 'checksum', 'source-lock'])
def test_actual_files_must_match_manifest(package, change):
    root, _, _ = make_bundle(package)
    directory = root/runtime.DIRECTORY
    if change == 'missing':
        (directory/'bin/ffmpeg.exe').unlink()
    elif change == 'unknown':
        (directory/'bin/injected.dll').write_bytes(b'extra')
    elif change == 'checksum':
        (directory/'bin/ffmpeg.exe').write_bytes(b'different')
    else:
        (directory/'carr-provenance/source-lock.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError):
        runtime.verify_audio_runtime(directory)


def test_outer_manifest_binds_inner_and_every_runtime_file(package):
    root, _, outer = make_bundle(package)
    outer['files'][runtime.DIRECTORY+'/'+runtime.MANIFEST] = '0'*64
    (root/'bundle-manifest.json').write_text(json.dumps(outer), encoding='utf-8')
    with pytest.raises(ValueError, match='sealed application'):
        runtime.activate_bundled_audio_runtime(root)


@pytest.mark.parametrize('change', ['missing', 'false-flag', 'no-manifest', 'inventory-case'])
def test_declared_or_unannounced_runtime_cannot_fall_back(package, change):
    root, _, outer = make_bundle(package)
    if change == 'missing':
        shutil.rmtree(root/runtime.DIRECTORY)
    elif change == 'false-flag':
        outer['bundled_audio'] = False
    elif change == 'no-manifest':
        (root/'bundle-manifest.json').unlink()
    else:
        name = runtime.DIRECTORY+'/bin/ffmpeg.exe'
        outer['files']['Audio-runtime/bin/ffmpeg.exe'] = outer['files'].pop(name)
    if change != 'no-manifest':
        (root/'bundle-manifest.json').write_text(json.dumps(outer), encoding='utf-8')
    with pytest.raises(ValueError):
        runtime.activate_bundled_audio_runtime(root)


def test_runtime_reparse_file_is_rejected_before_hashing(package, monkeypatch):
    root, _, _ = make_bundle(package)
    file = root/runtime.DIRECTORY/'bin/ffmpeg.exe'
    original = Path.lstat
    def lstat(path):
        value = original(path)
        if path == file:
            return SimpleNamespace(st_mode=value.st_mode, st_size=value.st_size,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return value
    monkeypatch.setattr(Path, 'lstat', lstat)
    with pytest.raises(ValueError, match='ordinary'):
        runtime.verify_audio_runtime(root/runtime.DIRECTORY)


def test_source_mode_ignores_working_directory_and_environment(package, monkeypatch):
    make_bundle(package)
    prefix = package.root/'development-python'
    prefix.mkdir()
    monkeypatch.setattr(sys, 'prefix', str(prefix))
    monkeypatch.setattr(sys, 'base_prefix', str(prefix))
    monkeypatch.setenv('CARR_AUDIO_RUNTIME', str(package.root/'bundle'))
    monkeypatch.setattr(os, 'getcwd', lambda: pytest.fail('runtime must not discover through cwd'))
    assert runtime.discover_bundled_audio() is None
    assert runtime.activate_bundled_audio_runtime() is None


def test_discovery_follows_virtual_environment_and_base_interpreter(package, monkeypatch):
    root, _, _ = make_bundle(package)
    monkeypatch.setattr(sys, 'prefix', str(root/'.venv'))
    monkeypatch.setattr(sys, 'base_prefix', str(root/'runtime'))
    assert runtime.discover_bundled_audio() == root


def test_nested_activation_preserves_runtime_until_last_handle(package, monkeypatch):
    root, _, _ = make_bundle(package)
    monkeypatch.setattr(runtime, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setenv('PATH', 'original-search-path')
    first = runtime.activate_bundled_audio_runtime(root)
    second = runtime.activate_bundled_audio_runtime(root)
    try:
        assert os.environ['PATH'].count(str(root/runtime.DIRECTORY/'bin')) == 1
        first.close()
        first.close()
        assert tool_path('ffmpeg') == str(root/runtime.DIRECTORY/'bin/ffmpeg.exe')
        second.close()
        assert os.environ['PATH'] == 'original-search-path'
        assert runtime.resolve_active_audio_tool('ffmpeg') is None
    finally:
        first.close()
        second.close()


def test_activated_runtime_tamper_or_override_cannot_select_path_tool(package, monkeypatch):
    root, _, _ = make_bundle(package)
    monkeypatch.setattr(runtime, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(shutil, 'which', lambda *_: pytest.fail('must not fall back to PATH'))
    handle = runtime.activate_bundled_audio_runtime(root)
    try:
        with pytest.raises(ValueError, match='overridden'):
            tool_path('ffmpeg', package.root/'different.exe')
        (root/runtime.DIRECTORY/'bin/ffmpeg.exe').write_bytes(b'modified')
        with pytest.raises(ValueError, match='reviewed bytes'):
            tool_path('ffmpeg')
        (root/runtime.DIRECTORY/'bin/ffprobe.exe').unlink()
        with pytest.raises(FileNotFoundError):
            tool_path('ffprobe')
    finally:
        handle.close()


def test_handle_preserves_unrelated_path_changes(package, monkeypatch):
    root, _, _ = make_bundle(package)
    monkeypatch.setattr(runtime, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setenv('PATH', 'first')
    handle = runtime.activate_bundled_audio_runtime(root)
    try:
        os.environ['PATH'] += os.pathsep+'another-entry'
    finally:
        handle.close()
    assert os.environ['PATH'] == 'first'+os.pathsep+'another-entry'


def bundle_tools_and_inputs(package):
    modules = []
    for name in ('build_windows_bundle', 'install_windows'):
        loader = importlib.util.spec_from_file_location(name+'_audio_test', PRODUCT/'packaging'/f'{name}.py')
        module = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(module)
        modules.append(module)
    wheels = package.root/'wheels'
    wheels.mkdir()
    dependency = wheels/'fixture-1.0-py3-none-any.whl'
    application = package.root/'universal_research_library-1.0-py3-none-any.whl'
    with ZipFile(dependency, 'w') as archive:
        archive.writestr('fixture-1.0.dist-info/METADATA', 'Metadata-Version: 2.3\nName: fixture\nVersion: 1.0\n')
    with ZipFile(application, 'w') as archive:
        archive.writestr('universal_research_library-1.0.dist-info/METADATA',
                         'Metadata-Version: 2.3\nName: universal-research-library\nVersion: 1.0\nRequires-Dist: fixture==1.0\n')
        archive.writestr('carr_library/__main__.py', '# Inert synthetic package\n')
    lock = package.root/'wheels.lock'
    lock.write_text(f'fixture==1.0 --hash=sha256:{digest(dependency)}\n', encoding='utf-8')
    return (*modules, application, wheels, lock)


def test_actual_bundle_copy_and_installer_preserve_checked_audio(package):
    root, original_report, _ = make_bundle(package)
    builder, installer, application, wheels, lock = bundle_tools_and_inputs(package)
    output = package.root/'complete'
    builder.build_bundle(application, wheels, lock, output, audio_dir=root/runtime.DIRECTORY)
    installer.verify(output)
    assert runtime.verify_audio_runtime(output/runtime.DIRECTORY) == original_report
    manifest_path = output/'bundle-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    assert manifest['bundled_audio'] is True
    (output/runtime.DIRECTORY/'bin/ffplay.exe').unlink()
    del manifest['files']['audio-runtime/bin/ffplay.exe']
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='missing audio runtime'):
        installer.verify(output)


def test_changed_audio_inventory_between_verification_and_copy_is_rejected(package, monkeypatch):
    root, _, _ = make_bundle(package)
    builder, _, application, wheels, lock = bundle_tools_and_inputs(package)
    original = runtime.verify_audio_runtime
    def verify_then_replace(directory, expected_manifest_sha256=None):
        result = original(directory, expected_manifest_sha256)
        (Path(directory)/runtime.MANIFEST).write_text('{"files":{"../escape":{}}}', encoding='utf-8')
        return result
    monkeypatch.setattr(runtime, 'verify_audio_runtime', verify_then_replace)
    output = package.root/'never-published'
    with pytest.raises(ValueError, match='inventory changed'):
        builder.build_bundle(application, wheels, lock, output, audio_dir=root/runtime.DIRECTORY)
    assert not output.exists()
    assert not (package.root/'escape').exists()


def test_explicit_download_retries_transport_failure_and_publishes_checked_bytes(package, monkeypatch):
    payload = package.archive.read_bytes()
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, timeout))
            if len(calls) == 1:
                raise TimeoutError('Synthetic transport timeout')
            return io.BytesIO(payload)
    monkeypatch.setattr(prepare, 'build_opener', lambda *_: Opener())
    monkeypatch.setattr(prepare.time, 'sleep', lambda *_: None)
    target = package.root / 'downloaded.zip'
    assert prepare.download_archive(target) == target
    assert target.read_bytes() == payload
    assert len(calls) == 2 and all(0 < timeout <= 30 for _, timeout in calls)
    assert not list(package.root.glob('.carr-audio-download-*'))


def test_explicit_download_stops_after_three_transport_failures(package, monkeypatch):
    calls = []
    class Opener:
        def open(self, *args, **kwargs):
            calls.append(True)
            raise TimeoutError('Synthetic transport timeout')
    monkeypatch.setattr(prepare, 'build_opener', lambda *_: Opener())
    monkeypatch.setattr(prepare.time, 'sleep', lambda *_: None)
    target = package.root / 'unpublished.zip'
    with pytest.raises(TimeoutError):
        prepare.download_archive(target)
    assert len(calls) == 3 and not target.exists()
    assert not list(package.root.glob('.carr-audio-download-*'))


def test_explicit_download_never_retries_corruption_or_replaces_existing_file(package, monkeypatch):
    payload = package.archive.read_bytes()
    calls = []
    class Opener:
        def open(self, *args, **kwargs):
            calls.append(True)
            return io.BytesIO(bytes([payload[0] ^ 1]) + payload[1:])
    monkeypatch.setattr(prepare, 'build_opener', lambda *_: Opener())
    target = package.root / 'unpublished.zip'
    with pytest.raises(ValueError, match='checksum'):
        prepare.download_archive(target)
    assert len(calls) == 1 and not target.exists()
    target.write_bytes(b'Existing private archive')
    with pytest.raises(FileExistsError):
        prepare.download_archive(target)
    assert len(calls) == 1 and target.read_bytes() == b'Existing private archive'
    assert not list(package.root.glob('.carr-audio-download-*'))
