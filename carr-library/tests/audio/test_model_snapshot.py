"""Real file replacement at the local model approval and budget boundaries."""
from hashlib import sha256
import json
from pathlib import Path

import pytest

from carr_library.audio import model_manifest as module


def write_manifest(root, asset):
    value = {'schema_version': 1, 'engine': 'piper', 'model_id': 'synthetic-fixture',
             'revision': 'a' * 40, 'license': 'synthetic-only', 'license_accepted': True,
             'files': {'voice.onnx': sha256(asset).hexdigest()}}
    path = root / 'manifest.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    (root / 'voice.onnx').write_bytes(asset)
    return path


def test_approval_hash_cannot_authenticate_a_different_parsed_snapshot(tmp_path, monkeypatch):
    path = write_manifest(tmp_path, b'synthetic non-model')
    approved = json.loads(path.read_text())
    approved['license_accepted'] = False
    replacement = json.dumps(approved).encode('utf-8')
    expected = sha256(replacement).hexdigest()
    original_open = Path.open
    replaced = False

    class ReplaceAfterRead:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def read(self, size=-1):
            nonlocal replaced
            data = self.stream.read(size)
            if not replaced:
                replaced = True
                with original_open(path, 'wb') as changed:
                    changed.write(replacement)
            return data

    def racing_open(self, mode='r', *args, **kwargs):
        stream = original_open(self, mode, *args, **kwargs)
        return ReplaceAfterRead(stream) if self == path and mode == 'rb' and not replaced else stream

    monkeypatch.setattr(Path, 'open', racing_open)
    with pytest.raises(module.ModelManifestError, match='changed|snapshot'):
        module.LocalModelManifest.load(path, expected_sha256=expected)
    assert replaced


def test_asset_growth_after_budget_stat_cannot_pass_with_pinned_larger_hash(tmp_path, monkeypatch):
    large = b'synthetic non-model' * 400
    path = write_manifest(tmp_path, large)
    model = module.LocalModelManifest.load(path)
    asset = tmp_path / 'voice.onnx'
    asset.write_bytes(b'small')
    original_stat = Path.stat
    replaced = False
    observed = 0

    def racing_stat(self, *args, **kwargs):
        nonlocal replaced, observed
        result = original_stat(self, *args, **kwargs)
        # verify() accounts this size, then digest_file() sees a larger file.
        if self == asset and kwargs.get('follow_symlinks', True):
            observed += 1
            if observed == 2 and not replaced:
                replaced = True
                asset.write_bytes(large)
        return result

    monkeypatch.setattr(Path, 'stat', racing_stat)
    with pytest.raises(module.ModelManifestError, match='budget|changed'):
        model.verify(max_asset_bytes=64)
    assert replaced
