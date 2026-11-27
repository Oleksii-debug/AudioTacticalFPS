"""Fail-closed inventory reuse from canonical CARR-10 without another runtime."""
from hashlib import sha256
import json
from pathlib import Path
from threading import Event

import pytest

from carr_library.audio import model_manifest as module


def installed_model(root, *, extra_files=None):
    raw = b'Synthetic asset; never a model inference fixture.'
    (root / 'voice.onnx').write_bytes(raw)
    value = {'schema_version': 1, 'engine': 'piper', 'model_id': 'inventory-fixture',
             'revision': 'a' * 40, 'license': 'synthetic-only', 'license_accepted': True,
             'trusted_weights': True,
             'files': {'voice.onnx': sha256(raw).hexdigest(), **(extra_files or {})}}
    manifest = root / 'manifest.json'
    manifest.write_text(json.dumps(value), encoding='utf-8')
    return module.LocalModelManifest.load(manifest)


@pytest.mark.parametrize('failure', ['open-subtree', 'iterate-subtree'])
def test_complete_inventory_refuses_unreadable_descendants(tmp_path, monkeypatch, failure):
    model = installed_model(tmp_path)
    hidden = tmp_path / 'unreadable'
    hidden.mkdir()
    (hidden / 'unlisted.PY').write_bytes(b'Synthetic unreviewed source, never executed.')
    scandir = module.os.scandir
    observed = []

    class BrokenIteration:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def __iter__(self):
            observed.append('iterate')
            raise PermissionError('Synthetic directory iteration failure.')

    def failed(path):
        if Path(path) == hidden:
            observed.append('open')
            if failure == 'open-subtree':
                raise PermissionError('Synthetic directory open failure.')
            return BrokenIteration()
        return scandir(path)

    monkeypatch.setattr(module.os, 'scandir', failed)
    with pytest.raises(module.ModelManifestError, match='inventory is unreadable or incomplete'):
        model.verify(require_complete=True)
    assert observed == (['open'] if failure == 'open-subtree' else ['open', 'iterate'])


def test_unlisted_uppercase_python_file_is_rejected_with_the_complete_inventory(tmp_path):
    model = installed_model(tmp_path)
    assert model.verify(require_complete=True)['files'] == 1
    (tmp_path / 'injected.PY').write_bytes(b'Synthetic unreviewed source, never executed.')
    with pytest.raises(module.ModelManifestError, match='missing from its reviewed manifest'):
        model.verify(require_complete=True)


def test_every_empty_directory_counts_before_large_inventory_allocation(tmp_path, monkeypatch):
    model = installed_model(tmp_path)
    for number in range(6):
        (tmp_path / f'empty-{number}').mkdir()
    monkeypatch.setattr(module, 'MAX_MODEL_DIRECTORY_ENTRIES', 3)
    with pytest.raises(module.ModelManifestError, match='directory entry budget'):
        model.verify(require_complete=True)


def test_cancel_during_traversal_prevents_asset_hash_or_runtime_admission(tmp_path, monkeypatch):
    model = installed_model(tmp_path)
    cancelled = Event()
    scandir = module.os.scandir
    digest = module._digest_snapshot
    hashed = []

    def observed_digest(path, **kwargs):
        hashed.append(Path(path))
        return digest(path, **kwargs)

    def cancel_on_scan(path):
        cancelled.set()
        return scandir(path)

    monkeypatch.setattr(module, '_digest_snapshot', observed_digest)
    monkeypatch.setattr(module.os, 'scandir', cancel_on_scan)
    with pytest.raises(InterruptedError, match='verification cancelled'):
        model.verify(require_complete=True, cancelled=cancelled.is_set)
    assert hashed == [model.path], 'No model asset may be read after the traversal cancellation.'


def test_complete_inventory_reports_missing_declared_file_without_inference(tmp_path):
    model = installed_model(tmp_path, extra_files={'missing.bin': 'b' * 64})
    with pytest.raises(module.ModelManifestError, match='file inventory differs'):
        model.verify(require_complete=True)


def test_incomplete_inventory_refuses_current_local_profile_admission(tmp_path, monkeypatch):
    from carr_library.runtime_settings import EngineProfileStore

    installation = tmp_path / 'engine'
    installation.mkdir()
    manifest = installed_model(installation)
    # Registration is the real shared authority. A hidden unreadable subtree
    # must not be converted into successful verification or a saved profile.
    hidden = installation / 'unreadable'
    hidden.mkdir()
    (hidden / 'unreviewed.bin').write_bytes(b'Unreviewed synthetic payload.')
    scandir = module.os.scandir
    def failed(path):
        if Path(path) == hidden:
            raise PermissionError('Synthetic directory enumeration failure.')
        return scandir(path)
    monkeypatch.setattr(module.os, 'scandir', failed)
    library = tmp_path / 'library'
    library.mkdir()
    store = EngineProfileStore(library)
    with pytest.raises(module.ModelManifestError, match='inventory is unreadable'):
        store.register(manifest.path, name='Synthetic model')
    assert store.snapshot().profiles == ()
