"""Real bounded files/ZIPs; executable bytes are inert fixtures, never inference."""
from hashlib import sha256
import json
import os
from pathlib import Path
import struct
import threading
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from carr_library.audio import local_runtime as local
from carr_library.audio import runtime, windows_setup as setup
from carr_library.audio import render
from carr_library import runtime_settings as settings
from carr_library.durable_state import atomic_json


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def isolated_activation(monkeypatch):
    monkeypatch.setattr(runtime, '_active', None)
    monkeypatch.setattr(runtime, '_audio_block', None)
    monkeypatch.setattr(runtime, '_binding_owner', None)
    monkeypatch.setenv('PATH', os.environ.get('PATH', ''))
    # These registration/ownership cases describe an independent library, not
    # the interpreter's containing application bundle. The installed lane has
    # a real sealed runtime under sys.prefix; omit only ambient discovery here.
    # Explicit bundle-root verification and the dedicated fallback test below
    # continue through the actual production verifier.
    monkeypatch.setattr(runtime, 'discover_bundled_audio', lambda: None)


@pytest.fixture
def pack(tmp_path, monkeypatch):
    """Only this fixture replaces trust pins; all actual file checks stay real."""
    source = tmp_path / 'synthetic-source-lock.json'
    notice = tmp_path / 'fixture-notice.txt'
    notice.write_text('Synthetic runtime fixture. No native binary or license grant.\n', encoding='utf-8')
    pe = bytearray(134)
    pe[:2] = b'MZ'
    struct.pack_into('<I', pe, 60, 128)
    pe[128:] = b'PE\x00\x00\x64\x86'
    files = {'bin/' + name + '.exe': bytes(pe) + name.encode()
             for name in ('ffmpeg', 'ffprobe', 'ffplay')}
    files['LICENSE'] = b'Synthetic test data only.\n'
    archive = tmp_path / 'synthetic.zip'
    with ZipFile(archive, 'w') as zipped:
        for name, raw in files.items():
            zipped.writestr('fixture/' + name, raw)
    lock = {'version': 1, 'release': 'fixture', 'publisher': 'test', 'license': 'fixture',
        'package_root': 'fixture',
        'archive': {'url': 'https://github.com/example/fixture.zip',
                    'size_bytes': archive.stat().st_size, 'sha256': digest(archive)},
        'original_files': {name: {'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()}
                           for name, raw in files.items()}}
    source.write_text(json.dumps(lock), encoding='utf-8')
    monkeypatch.setattr(setup, 'SOURCE_LOCK', source)
    monkeypatch.setattr(setup, 'SOURCE_LOCK_SHA256', digest(source))
    monkeypatch.setattr(setup, 'NOTICE', notice)
    monkeypatch.setattr(setup, 'sys', SimpleNamespace(platform='fixture'))
    monkeypatch.setattr(runtime, 'SOURCE_LOCK_SHA256', digest(source))
    monkeypatch.setattr(local, 'sys', SimpleNamespace(platform='win32'))
    directory = tmp_path / 'local-pack'
    report = setup.prepare(archive, directory)
    library = tmp_path / 'library'
    library.mkdir()
    versions = []
    def version_probe(argv, **options):
        assert argv[1:] == ['-version']
        assert Path(argv[0]).name in ('ffmpeg.exe', 'ffprobe.exe')
        versions.append(tuple(argv))
        render.check_cancelled(options.get('cancelled'))
        return 'synthetic version fixture', ''
    monkeypatch.setattr(render, 'run_process', version_probe)
    return SimpleNamespace(root=tmp_path, directory=directory, archive=archive, source=source,
                           library=library, report=report, versions=versions)


def register(pack):
    value = settings.load_settings(pack.library / 'state')
    value.update(ffmpeg=str(pack.directory / 'bin' / 'ffmpeg.exe'),
                 ffprobe=str(pack.directory / 'bin' / 'ffprobe.exe'),
                 audio_runtime_manifest_sha256=pack.report['manifest_sha256'])
    atomic_json(settings.settings_path(pack.library / 'state'), value, replace=False)
    return value


def test_installed_setup_resources_are_exact_packaging_resources():
    product = Path(__file__).resolve().parents[2]
    assert setup.SOURCE_LOCK.read_bytes() == (product / 'packaging/audio-runtime/source-lock.json').read_bytes()
    assert setup.NOTICE.read_bytes() == (product / 'packaging/audio-runtime/README.txt').read_bytes()
    information = local.source_summary()
    assert information['archive']['sha256'] == 'f9a83be8f97b8989a223a15e4af234accd0842bacf7c126af5a3aae759785e48'
    assert information['transfer_clearance'] == 'NOT_ESTABLISHED'


def test_existing_selection_publishes_exact_seal_but_does_not_activate(pack):
    before = os.environ['PATH']
    result = local.install_or_select(pack.library, existing=pack.directory)
    assert result['status'] == 'REGISTERED'
    assert result['transfer_clearance'] == 'NOT_ESTABLISHED'
    assert result['settings'][local.SEAL] == digest(pack.directory / runtime.MANIFEST)
    assert settings.load_settings(pack.library / 'state') == result['settings']
    assert os.environ['PATH'] == before and runtime.resolve_active_audio_tool('ffmpeg') is None
    assert len(pack.versions) == 2
    result['binding'].activate()
    try:
        assert runtime.resolve_active_audio_tool('ffplay') == str(pack.directory / 'bin/ffplay.exe')
    finally:
        result['binding'].close()
    assert os.environ['PATH'] == before


@pytest.mark.parametrize('change', ['missing', 'changed', 'binary'])
def test_explicit_selection_never_downgrades_to_loose_executables(pack, monkeypatch, change):
    register(pack)
    manifest = pack.directory / runtime.MANIFEST
    if change == 'missing':
        manifest.unlink()
    elif change == 'changed':
        manifest.write_text('{}', encoding='utf-8')
    else:
        (pack.directory / 'bin/ffmpeg.exe').write_bytes(b'changed')
    monkeypatch.setattr(render.shutil, 'which', lambda *a: pytest.fail('PATH fallback is forbidden'))
    binding = local.prepare_local_audio_runtime(pack.library)
    assert binding.notice
    binding.activate()
    try:
        with pytest.raises(RuntimeError, match='unavailable'):
            render.tool_path('ffmpeg')
    finally:
        binding.close()


def test_restored_barrier_precedes_external_asset_reads(pack, monkeypatch):
    value = register(pack)
    value['restore_requires_revalidation'] = True
    atomic_json(settings.settings_path(pack.library / 'state'), value, replace=True)
    monkeypatch.setattr(local, '_snapshot', lambda *a, **k: pytest.fail('restored external path accessed'))
    monkeypatch.setattr(setup, 'verify_runtime', lambda *a, **k: pytest.fail('restored runtime accessed'))
    prepared = local.prepare_local_audio_runtime(pack.library)
    assert 'revalidation' in prepared.notice
    with pytest.raises(ValueError, match='Review restored'):
        local.install_or_select(pack.library, existing=pack.directory)
    assert settings.load_settings(pack.library / 'state') == value


def test_prepare_is_inert_and_gui_activation_never_rehashes_inventory(pack, monkeypatch):
    register(pack)
    previous_path = os.environ['PATH']
    binding = local.prepare_local_audio_runtime(pack.library)
    assert not binding.notice and runtime._active is None and os.environ['PATH'] == previous_path
    monkeypatch.setattr(setup, 'verify_runtime', lambda *a, **k: pytest.fail('GUI inventory hash'))
    monkeypatch.setattr(setup, 'verify_native_versions', lambda *a, **k: pytest.fail('GUI child process'))
    binding.activate()
    try:
        assert runtime._active is not None
    finally:
        binding.close()


def test_changed_metadata_between_prepare_and_completion_blocks_activation(pack):
    register(pack)
    binding = local.prepare_local_audio_runtime(pack.library)
    (pack.directory / 'bin/ffprobe.exe').write_bytes(b'changed')
    binding.activate()
    try:
        assert binding.notice
        with pytest.raises(RuntimeError):
            runtime.resolve_active_audio_tool('ffprobe')
    finally:
        binding.close()


def test_manifest_growth_after_snapshot_is_bounded_and_rejected(pack, monkeypatch):
    register(pack)
    binding = local.prepare_local_audio_runtime(pack.library)
    snapshot = local._snapshot
    def grow_after_snapshot(*args, **kwargs):
        result = snapshot(*args, **kwargs)
        with (pack.directory / runtime.MANIFEST).open('ab') as stream:
            stream.write(b'oversized-after-snapshot')
        return result
    monkeypatch.setattr(local, '_snapshot', grow_after_snapshot)
    binding.activate()
    try:
        assert 'manifest size changed' in binding.notice
        assert runtime._active is None
    finally:
        binding.close()


def test_invalid_owner_cannot_be_cleared_by_an_unrelated_default_binding(tmp_path):
    first = local.AudioRuntimeBinding(tmp_path / 'first', notice='missing selected pack')
    first.activate()
    other_root = tmp_path / 'second'
    other_root.mkdir()
    second = local.prepare_local_audio_runtime(other_root)
    with pytest.raises(RuntimeError, match='Another library'):
        second.activate()
    second.close()
    with pytest.raises(RuntimeError, match='missing selected pack'):
        runtime.resolve_active_audio_tool('ffmpeg')
    replacement = local.prepare_local_audio_runtime(other_root)
    replacement.activate(previous=first)
    try:
        assert runtime.resolve_active_audio_tool('ffmpeg') is None
    finally:
        first.close()
        replacement.close()


def test_strict_open_failure_closes_the_unreturned_owner(tmp_path, monkeypatch):
    bad = local.AudioRuntimeBinding(tmp_path, notice='bad selection')
    monkeypatch.setattr(local, 'prepare_local_audio_runtime', lambda *a, **k: bad)
    with pytest.raises(RuntimeError, match='bad selection'):
        local.open_local_audio_runtime(tmp_path)
    assert bad._closed and runtime._audio_block is None and runtime._binding_owner is None


def test_unused_prepared_close_never_changes_active_library(pack):
    register(pack)
    active = local.prepare_local_audio_runtime(pack.library).activate()
    pending = local.prepare_local_audio_runtime(pack.library)
    pending.close()
    assert runtime.resolve_active_audio_tool('ffmpeg') == str(pack.directory / 'bin/ffmpeg.exe')
    active.close()


def test_current_settings_change_rejects_atomic_selection(pack, monkeypatch):
    before = settings.load_settings(pack.library / 'state')
    original = settings.verify_configured_audio_runtime
    def reviewed_then_changed(value, **options):
        result = original(value, **options)
        changed = {**before, 'restore_requires_revalidation': False}
        path = settings.settings_path(pack.library / 'state')
        atomic_json(path, changed, replace=os.path.lexists(path))
        return result
    monkeypatch.setattr(settings, 'verify_configured_audio_runtime', reviewed_then_changed)
    with pytest.raises(ValueError, match='settings changed during review'):
        local.install_or_select(pack.library, existing=pack.directory, expected_settings=before)
    assert local.SEAL not in settings.load_settings(pack.library / 'state')
    assert runtime._active is None


def test_late_cancel_after_atomic_registration_is_reported_as_saved(pack, monkeypatch):
    cancel = threading.Event()
    publish = settings.atomic_json
    def save_then_cancel(*args, **kwargs):
        result = publish(*args, **kwargs)
        cancel.set()
        return result
    monkeypatch.setattr(settings, 'atomic_json', save_then_cancel)
    result = local.install_or_select(pack.library, existing=pack.directory, cancelled=cancel.is_set)
    assert cancel.is_set() and result['status'] == 'REGISTERED'
    assert settings.load_settings(pack.library / 'state')[local.SEAL] == result['manifest_sha256']
    assert runtime._active is None
    result['binding'].close()


def test_cancel_during_manifest_last_publication_removes_only_new_output(pack, monkeypatch):
    cancel = threading.Event()
    link = os.link
    def linked_then_cancel(source, destination):
        link(source, destination)
        cancel.set()
    monkeypatch.setattr(setup.os, 'link', linked_then_cancel)
    target = pack.root / 'cancelled-pack'
    with pytest.raises((InterruptedError, render.RenderCancelled)):
        setup.prepare(pack.archive, target, cancelled=cancel.is_set)
    assert not target.exists()
    assert not list(pack.root.glob('.carr-audio-runtime-*'))
    assert runtime.verify_audio_runtime(pack.directory)['manifest_sha256'] == pack.report['manifest_sha256']


def test_download_is_explicit_pinned_and_cancellable_without_registration(pack, monkeypatch):
    cancel = threading.Event()
    calls = []
    data = pack.archive.read_bytes()
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self, size):
            cancel.set()
            return data[:size]
    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, timeout))
            return Response()
    monkeypatch.setattr(setup, 'build_opener', lambda *args: Opener())
    with pytest.raises((InterruptedError, render.RenderCancelled)):
        local.install_or_select(pack.library, download=True, install_parent=pack.root / 'managed',
                                cancelled=cancel.is_set)
    assert calls == [('https://github.com/example/fixture.zip', 10)]
    assert not settings.settings_path(pack.library / 'state').exists()
    assert not list((pack.root / 'managed').iterdir())


@pytest.mark.parametrize('ffmpeg,ffprobe,seal', [
    (None, None, 'a'*64),
    (r'C:\audio\bin\ffmpeg.exe', None, 'a'*64),
    (r'C:\audio\bin\ffmpeg.exe', r'C:\other\bin\ffprobe.exe', 'a'*64),
    (r'C:\audio\bin\ffmpeg.exe', r'C:\audio\bin\ffprobe.exe', 'A'*64),
    (r'C:\audio\bin\ffmpeg.exe', r'C:\audio\bin\wrong.exe', 'a'*64),
])
def test_pure_seal_relationship_validation_rejects_inconsistent_declarations(ffmpeg, ffprobe, seal):
    with pytest.raises(ValueError):
        settings.validate_audio_runtime_binding({'ffmpeg': ffmpeg, 'ffprobe': ffprobe, local.SEAL: seal})


def test_pure_seal_relationship_accepts_foreign_windows_paths_without_access(monkeypatch):
    monkeypatch.setattr(Path, 'lstat', lambda *a: pytest.fail('pure restore validator accessed filesystem'))
    value = {'ffmpeg': r'C:\audio\bin\ffmpeg.exe',
             'ffprobe': r'C:\audio\bin\ffprobe.exe', local.SEAL: 'a'*64}
    assert settings.validate_audio_runtime_binding(value) is value


def test_legacy_unsealed_declarations_are_not_implicitly_converted(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    value = settings.load_settings(root / 'state')
    value['ffmpeg'] = str(tmp_path / 'legacy.exe')
    atomic_json(settings.settings_path(root / 'state'), value, replace=False)
    binding = local.prepare_local_audio_runtime(root)
    assert binding.directory is None and not binding.notice and local.SEAL not in binding.settings


def test_prepare_cancel_is_not_converted_into_a_broken_registration(pack):
    register(pack)
    with pytest.raises(render.RenderCancelled):
        local.prepare_local_audio_runtime(pack.library, cancelled=lambda: True)
    assert runtime._active is None and runtime._audio_block is None


def test_verified_bundle_fallback_uses_the_same_inert_preparation_and_owner(pack, monkeypatch):
    bundle = pack.root / 'bundle'
    bundle.mkdir()
    directory = bundle / runtime.DIRECTORY
    setup.prepare(pack.archive, directory)
    outer = {'schema': 1, 'target': 'Windows x64 / CPython 3.11', 'bundled_audio': True,
             'files': {path.relative_to(bundle).as_posix(): digest(path)
                       for path in directory.rglob('*') if path.is_file()}}
    (bundle / 'bundle-manifest.json').write_text(json.dumps(outer), encoding='utf-8')
    assert runtime.discover_bundled_audio() is None
    # The fixture suppresses only omitted/default discovery. An explicit root
    # must still authenticate this real sealed file inventory and own PATH.
    monkeypatch.setattr(runtime, 'sys', SimpleNamespace(platform='win32'))
    previous_path = os.environ['PATH']
    explicit = runtime.activate_bundled_audio_runtime(bundle_root=bundle)
    try:
        assert explicit is not None
        assert runtime.resolve_active_audio_tool('ffplay') == str(directory / 'bin/ffplay.exe')
    finally:
        explicit.close()
    assert runtime._active is None and os.environ['PATH'] == previous_path
    monkeypatch.setattr(runtime, 'discover_bundled_audio', lambda: bundle)
    binding = local.prepare_local_audio_runtime(pack.library)
    assert not binding.notice and binding.directory == directory and runtime._active is None
    binding.activate()
    try:
        assert runtime.resolve_active_audio_tool('ffplay') == str(directory / 'bin/ffplay.exe')
        # A newly explicit complete-pack selection takes over only with its owner.
        register(pack)
        selected = local.prepare_local_audio_runtime(pack.library)
        selected.activate(previous=binding)
        assert binding._closed
        assert runtime.resolve_active_audio_tool('ffmpeg') == str(pack.directory / 'bin/ffmpeg.exe')
        selected.close()
    finally:
        binding.close()


def test_broken_explicit_selection_does_not_consult_bundle_fallback(pack, monkeypatch):
    register(pack)
    (pack.directory / runtime.MANIFEST).unlink()
    monkeypatch.setattr(runtime, 'discover_bundled_audio',
                        lambda: pytest.fail('explicit broken selection tried bundled fallback'))
    binding = local.prepare_local_audio_runtime(pack.library)
    assert binding.notice and runtime._active is None
