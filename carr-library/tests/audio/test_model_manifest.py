import hashlib
import json
from pathlib import Path

import pytest

from carr_library.audio.model_manifest import LocalModelManifest, ModelManifestError, digest_file, local_path, read_json, offline_environment


def manifest(tmp_path, **updates):
    asset = tmp_path / 'voice.onnx'
    asset.write_bytes(b'synthetic non-model test fixture')
    value = {'schema_version': 1, 'engine': 'piper', 'model_id': 'synthetic-fixture',
             'revision': 'a' * 40, 'license': 'synthetic-only', 'license_accepted': True,
             'files': {'voice.onnx': hashlib.sha256(asset.read_bytes()).hexdigest()}}
    value.update(updates)
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def test_exact_manifest_hashes_and_mutated_asset(tmp_path):
    model = LocalModelManifest.load(manifest(tmp_path))
    assert model.verify()['files'] == 1
    assert model.cache_identity()['revision'] == 'a' * 40
    (tmp_path / 'voice.onnx').write_bytes(b'modified')
    with pytest.raises(ModelManifestError, match='Checksum'):
        model.verify()


def test_moving_revision_and_wrong_engine_fail_closed(tmp_path):
    with pytest.raises(ModelManifestError, match='exact model revision'):
        LocalModelManifest.load(manifest(tmp_path, revision='main'))
    with pytest.raises(ModelManifestError, match='another engine'):
        LocalModelManifest.load(manifest(tmp_path), engine='musicgen')


@pytest.mark.parametrize('name', ['../outside.onnx', '/absolute.onnx', 'C:/model.onnx',
                                 'parent\\file.onnx', 'https://example.test/model', 'a/./b', 'a//b'])
def test_asset_paths_cannot_escape_manifest_root(tmp_path, name):
    with pytest.raises(ModelManifestError):
        LocalModelManifest.load(manifest(tmp_path, files={name: 'a' * 64}))


def test_manifest_duplicate_keys_and_nonfinite_values_rejected(tmp_path):
    path = tmp_path / 'manifest.json'
    path.write_text('{"schema_version":1,"schema_version":2}', encoding='utf-8')
    with pytest.raises(ModelManifestError, match='duplicate'):
        read_json(path)
    path.write_text('{"gain": NaN}', encoding='utf-8')
    with pytest.raises(ModelManifestError, match='Nonfinite'):
        read_json(path)


def test_manifest_changed_between_validation_and_use(tmp_path):
    path = manifest(tmp_path)
    model = LocalModelManifest.load(path)
    path.write_text(path.read_text() + ' ', encoding='utf-8')
    with pytest.raises(ModelManifestError, match='changed'):
        model.verify()


def test_license_and_execution_consent_not_inferred(tmp_path):
    model = LocalModelManifest.load(manifest(tmp_path, license_accepted=False))
    with pytest.raises(ModelManifestError, match='accept'):
        model.verify()
    model = LocalModelManifest.load(manifest(tmp_path, engine='musicgen', license='CC-BY-NC-4.0',
                                           personal_noncommercial=True))
    with pytest.raises(ModelManifestError, match='trusted MusicGen checkpoint'):
        model.verify()


def test_musicgen_requires_both_pinned_checkpoints_and_noncommercial_profile(tmp_path):
    model = LocalModelManifest.load(manifest(tmp_path, engine='musicgen', license='CC-BY-NC-4.0',
                                           trusted_weights=True, personal_noncommercial=False))
    with pytest.raises(ModelManifestError, match='personal noncommercial'):
        model.verify()
    model = LocalModelManifest.load(manifest(tmp_path, engine='musicgen', license='CC-BY-NC-4.0',
                                           trusted_weights=True, personal_noncommercial=True))
    with pytest.raises(ModelManifestError, match='Both MusicGen checkpoints'):
        model.verify()


def test_linked_asset_is_rejected(tmp_path):
    external = tmp_path / 'external'
    external.write_bytes(b'external')
    linked = tmp_path / 'linked'
    try:
        linked.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip('This filesystem cannot create a test symlink.')
    with pytest.raises(ModelManifestError, match='Linked paths'):
        local_path(linked)


def test_local_path_checks_each_component_with_one_lstat(tmp_path, monkeypatch):
    from collections import Counter

    asset = tmp_path / 'runtime' / 'package' / 'model.bin'
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b'synthetic asset')
    expected = asset.resolve()
    calls = Counter()
    original_lstat = Path.lstat

    def counted_lstat(path, *args, **kwargs):
        calls[path] += 1
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', counted_lstat)
    assert local_path(asset) == expected
    assert calls == Counter({part: 1 for part in (asset, *asset.parents)})


@pytest.mark.parametrize('component', ['asset', 'parent'])
@pytest.mark.parametrize('kind', ['symlink', 'reparse'])
def test_local_path_rejects_links_from_the_component_stat(tmp_path, monkeypatch, component, kind):
    import stat
    from types import SimpleNamespace

    asset = tmp_path / 'runtime' / 'model.bin'
    asset.parent.mkdir()
    asset.write_bytes(b'synthetic asset')
    selected = asset if component == 'asset' else asset.parent
    original_lstat = Path.lstat

    def linked_lstat(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if path == selected:
            return SimpleNamespace(
                st_mode=stat.S_IFLNK if kind == 'symlink' else info.st_mode,
                st_file_attributes=(0 if kind == 'symlink' else
                                    getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)),
            )
        return info

    monkeypatch.setattr(Path, 'lstat', linked_lstat)
    message = 'Linked paths' if kind == 'symlink' else 'Reparse points'
    with pytest.raises(ModelManifestError, match=message):
        local_path(asset)


def test_local_path_still_requires_existence_after_component_checks(tmp_path, monkeypatch):
    asset = tmp_path / 'model.bin'
    asset.write_bytes(b'synthetic asset')
    original_lstat = Path.lstat
    removed = False

    def remove_after_stat(path, *args, **kwargs):
        nonlocal removed
        info = original_lstat(path, *args, **kwargs)
        if path == asset and not removed:
            removed = True
            asset.unlink()
        return info

    monkeypatch.setattr(Path, 'lstat', remove_after_stat)
    with pytest.raises(FileNotFoundError):
        local_path(asset)
    assert removed


def test_manifest_byte_budget_and_cancellation(tmp_path):
    model = LocalModelManifest.load(manifest(tmp_path))
    with pytest.raises(ModelManifestError, match='budget'):
        model.verify(max_asset_bytes=1)
    with pytest.raises(InterruptedError):
        digest_file(model.path, cancelled=lambda: True)


def test_boolean_schema_and_acceptance_strings_are_rejected(tmp_path):
    with pytest.raises(ModelManifestError, match='schema version'):
        LocalModelManifest.load(manifest(tmp_path, schema_version=True))
    with pytest.raises(ModelManifestError, match='booleans'):
        LocalModelManifest.load(manifest(tmp_path, license_accepted='yes'))


def test_complete_runtime_inventory_rejects_unlisted_python_file(tmp_path):
    model = LocalModelManifest.load(manifest(tmp_path))
    assert model.verify(require_complete=True)['files'] == 1
    (tmp_path/'unexpected_import.py').write_text('raise RuntimeError()', encoding='utf-8')
    with pytest.raises(ModelManifestError, match='missing from its reviewed manifest'):
        model.verify(require_complete=True)


def test_worker_environment_excludes_host_credentials(monkeypatch):
    for name in ('PROVIDER_API_KEY', 'HF_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'HTTP_PROXY'):
        monkeypatch.setenv(name, 'synthetic-secret')
    environment = offline_environment()
    assert not any(name in environment for name in ('PROVIDER_API_KEY', 'HF_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'HTTP_PROXY'))
    assert environment['HF_HUB_OFFLINE'] == '1'


def test_complete_torch_sized_runtime_inventory_and_mutation(tmp_path, record_property):
    """Cover the measured 32,881-file runtime using only synthetic regular files."""
    import time

    started = time.perf_counter()
    empty_digest = hashlib.sha256(b'').hexdigest()
    names = ['state_dict.bin', 'compression_state_dict.bin']
    prefix = 'runtime/lib/python3.11/site-packages/synthetic_dependency_fixture/modules'
    names.extend(f'{prefix}/module_{number:05}.py' for number in range(32879))
    for name in names:
        asset = tmp_path / name
        asset.parent.mkdir(parents=True, exist_ok=True)
        asset.touch()
    raw = {'schema_version': 1, 'engine': 'musicgen',
           'model_id': 'synthetic-inventory-only-not-a-model',
           'revision': 'a' * 40, 'license': 'CC-BY-NC-4.0',
           'license_accepted': True, 'trusted_weights': True,
           'personal_noncommercial': True,
           'files': {name: empty_digest for name in names}}
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(raw, separators=(',', ':')), encoding='utf-8')
    assert path.stat().st_size > 1024 * 1024
    model = LocalModelManifest.load(path, engine='musicgen')
    before_verify = time.perf_counter()
    report = model.verify(require_complete=True)
    verify_seconds = time.perf_counter() - before_verify
    assert report['files'] == 32881 and report['bytes'] == 0
    assert model.asset(names[-1]) == tmp_path / names[-1]
    with pytest.raises(ModelManifestError, match='relative paths'):
        model.asset('../outside.py')
    with pytest.raises(ModelManifestError, match='not in the verified manifest'):
        model.asset('runtime/unlisted.py')
    # The first sorted checkpoint is checked again rather than trusted from
    # the preceding inventory pass; no model loader executes these fixtures.
    (tmp_path / 'compression_state_dict.bin').write_bytes(b'mutated fixture')
    with pytest.raises(ModelManifestError, match='Checksum mismatch'):
        model.verify()
    record_property('synthetic_inventory_files', len(names))
    record_property('serialized_manifest_bytes', path.stat().st_size)
    record_property('verification_seconds', round(verify_seconds, 6))
    record_property('total_fixture_seconds', round(time.perf_counter() - started, 6))
    record_property('model_inference', 'NOT_RUN')


def test_model_inventory_file_count_remains_bounded(tmp_path):
    from carr_library.audio.model_manifest import MAX_MODEL_FILES
    files = {f'file_{number}.bin': 'a' * 64 for number in range(MAX_MODEL_FILES + 1)}
    with pytest.raises(ModelManifestError, match='List every required local asset'):
        LocalModelManifest.load(manifest(tmp_path, files=files))


def test_model_manifest_bytes_remain_bounded_before_json_parse(tmp_path):
    from carr_library.audio.model_manifest import MAX_MODEL_MANIFEST_BYTES
    path = tmp_path / 'oversized.json'
    with path.open('wb') as stream:
        stream.seek(MAX_MODEL_MANIFEST_BYTES)
        stream.write(b' ')
    with pytest.raises(ModelManifestError, match='bounded regular file'):
        LocalModelManifest.load(path)


def test_complete_inventory_directory_budget_remains_enforced(tmp_path, monkeypatch):
    import carr_library.audio.model_manifest as module
    model = LocalModelManifest.load(manifest(tmp_path))
    for index in range(5):
        (tmp_path / f'empty_{index}').mkdir()
    monkeypatch.setattr(module, 'MAX_MODEL_DIRECTORY_ENTRIES', 3)
    with pytest.raises(ModelManifestError, match='directory entry budget'):
        model.verify(require_complete=True)


def test_complete_inventory_can_be_cancelled_before_hashing_assets(tmp_path):
    model = LocalModelManifest.load(manifest(tmp_path))
    for index in range(5):
        (tmp_path / f'empty_{index}').mkdir()
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls == 4
    with pytest.raises(InterruptedError, match='verification cancelled'):
        model.verify(require_complete=True, cancelled=cancelled)
    assert calls == 4


@pytest.mark.parametrize('phase', ['open', 'iteration'])
def test_complete_inventory_refuses_unreadable_subtree_without_hashing_assets(tmp_path, monkeypatch, phase):
    from carr_library.audio import model_manifest as module

    model = LocalModelManifest.load(manifest(tmp_path))
    hidden = tmp_path / 'private-subtree'
    hidden.mkdir()
    (hidden / 'unreviewed.PY').write_bytes(b'# synthetic unlisted code')
    native_scandir = module.os.scandir
    closed = []
    visited = []

    class FailingEntries:
        def __init__(self, path):
            self.entries = native_scandir(path)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.entries.close()
            closed.append(True)
        def __iter__(self):
            return self
        def __next__(self):
            raise PermissionError('synthetic-private-enumeration-detail')

    def scan(path):
        visited.append(Path(path))
        if Path(path) == hidden:
            if phase == 'open':
                raise PermissionError('synthetic-private-enumeration-detail')
            return FailingEntries(path)
        return native_scandir(path)

    monkeypatch.setattr(module.os, 'scandir', scan)
    actual_digest = module._digest_snapshot
    digests = []
    def digest(path, **kwargs):
        digests.append(Path(path))
        return actual_digest(path, **kwargs)
    monkeypatch.setattr(module, '_digest_snapshot', digest)
    with pytest.raises(ModelManifestError, match='inventory is unreadable or incomplete') as failed:
        model.verify(require_complete=True)
    assert hidden in visited
    assert digests == [model.path]
    assert closed == ([True] if phase == 'iteration' else [])
    assert 'synthetic-private-enumeration-detail' not in str(failed.value)
    assert (tmp_path / 'voice.onnx').read_bytes() == b'synthetic non-model test fixture'


def test_complete_inventory_rejects_nonregular_entry_from_filesystem_metadata(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import stat
    from carr_library.audio import model_manifest as module

    model = LocalModelManifest.load(manifest(tmp_path))
    entry = tmp_path / 'special-entry'
    entry.write_bytes(b'controlled filesystem metadata fixture')
    original_stat = Path.stat

    def special(path, *args, **kwargs):
        if path == entry:
            return SimpleNamespace(st_mode=stat.S_IFIFO, st_size=0)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', special)
    with pytest.raises(ModelManifestError, match='regular files and directories'):
        model.verify(require_complete=True)


def test_inventory_cancellation_remains_interrupted_and_closes_the_iterator(tmp_path, monkeypatch):
    from carr_library.audio import model_manifest as module

    for number in range(3):
        (tmp_path / f'entry-{number}').mkdir()
    native_scandir = module.os.scandir
    closed = []
    class Entries:
        def __init__(self, path):
            self.entries = native_scandir(path)
        def __enter__(self):
            return self.entries
        def __exit__(self, *args):
            self.entries.close()
            closed.append(True)
    monkeypatch.setattr(module.os, 'scandir', Entries)
    iterator = module._local_inventory(tmp_path, cancelled=lambda: bool(closed))
    first, info = next(iterator)
    assert first.parent == tmp_path and first.is_dir()
    iterator.close()
    assert closed == [True]
    with pytest.raises(InterruptedError, match='verification cancelled'):
        list(module._local_inventory(tmp_path, cancelled=lambda: True))
