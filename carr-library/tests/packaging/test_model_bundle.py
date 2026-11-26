import hashlib
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
from huggingface_hub.hf_api import HfApi, RepoFile
spec = importlib.util.spec_from_file_location(
    "model_bundle", Path(__file__).resolve().parents[2] / "packaging" / "model_bundle.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


@pytest.fixture
def staged(tmp_path, monkeypatch):
    # Synthetic remote metadata; actual upstream checksum implementation runs.
    entries = []
    published = {}
    for name in sorted(m.FILES):
        data = ('fixture:' + name).encode()
        (tmp_path / name).write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        oid = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        fields = dict(path=name, size=len(data), oid=oid)
        if name in m.PUBLISHED:
            published[name] = sha
            fields['lfs'] = dict(size=len(data), oid=sha, pointerSize=128)
        entries.append(RepoFile(**fields))
    monkeypatch.setattr(m, 'PUBLISHED', published)
    return tmp_path, entries


def test_native_verifier_and_manifest(staged):
    root, entries = staged
    with patch.object(HfApi, 'list_repo_tree', return_value=entries) as tree:
        result = m.seal(root)
    assert tree.call_args.kwargs['revision'] == m.REVISION
    assert tree.call_args.kwargs['token'] is False
    assert result['hashes'] == m.hashes(root)
    assert len(result['hashes']) == 9


@pytest.mark.parametrize('name', ['model_optimized.onnx', 'config.json'])
def test_corruption(staged, name):
    root, entries = staged
    (root / name).write_bytes(b'corrupt')
    with patch.object(HfApi, 'list_repo_tree', return_value=entries), pytest.raises(ValueError):
        m.seal(root)


@pytest.mark.parametrize('change', ['missing', 'extra', 'remote_missing', 'remote_extra'])
def test_file_set(staged, change):
    root, entries = staged
    if change == 'missing':
        (root / 'config.json').unlink()
    elif change == 'extra':
        (root / 'unexpected').write_bytes(b'bad')
    elif change == 'remote_missing':
        entries.pop()
    else:
        entries.append(RepoFile(path='unknown', size=0, oid='0' * 40))
    with patch.object(HfApi, 'list_repo_tree', return_value=entries), pytest.raises(ValueError):
        m.seal(root)


def test_link(staged):
    root, _ = staged
    (root / 'config.json').unlink()
    (root / 'config.json').symlink_to(root / 'README.md')
    with pytest.raises(ValueError):
        m.hashes(root)


def test_drift(staged):
    root, entries = staged
    original = HfApi.verify_repo_checksums
    def changed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        (root / 'config.json').write_bytes(b'changed after verification')
        return result
    with patch.object(HfApi, 'list_repo_tree', return_value=entries), patch.object(HfApi, 'verify_repo_checksums', changed), pytest.raises(ValueError, match='changed during'):
        m.seal(root)
