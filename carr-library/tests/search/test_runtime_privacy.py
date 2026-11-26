"""Order and fail-closed privacy checks without loading native model libraries."""
import sys
import builtins
from types import SimpleNamespace

import pytest

from carr_library.search import offline_runtime


class EngineConstructionReached(Exception):
    pass


FLAGS = ('ORT_DISABLE_TELEMETRY', 'HF_HUB_OFFLINE',
         'HF_HUB_DISABLE_TELEMETRY', 'TRANSFORMERS_OFFLINE')


def prepare(monkeypatch, tmp_path, disable):
    events = []
    monkeypatch.setattr(offline_runtime, 'verify_model_bundle', lambda *_: (tmp_path, {}))
    monkeypatch.setattr(offline_runtime, 'version', lambda *_: '0.9.1')
    def disable_checked():
        events.append('disabled')
        return disable()

    monkeypatch.setitem(sys.modules, 'onnxruntime', SimpleNamespace(disable_telemetry_events=disable_checked))
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == 'onnxruntime':
            assert all(offline_runtime.os.environ.get(flag) == '1' for flag in FLAGS)
            events.append('onnx_import')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guarded_import)

    def engine(**options):
        events.append('engine')
        assert options['local_files_only'] is True
        raise EngineConstructionReached

    monkeypatch.setitem(sys.modules, 'fastembed', SimpleNamespace(TextEmbedding=engine))
    monkeypatch.setitem(sys.modules, 'tokenizers', SimpleNamespace(Tokenizer=object))
    for name in FLAGS:
        monkeypatch.setenv(name, '0')
    return events


def test_telemetry_disabled_before_model_construction(monkeypatch, tmp_path):
    order = []
    events = prepare(monkeypatch, tmp_path, lambda: order.append('disabled'))
    with pytest.raises(EngineConstructionReached):
        offline_runtime.OfflineRuntime(tmp_path, tmp_path / 'manifest.json')
    assert order == ['disabled'] and events == ['onnx_import', 'disabled', 'engine']
    for name in FLAGS:
        assert offline_runtime.os.environ[name] == '1'


def test_telemetry_disable_failure_prevents_model_construction(monkeypatch, tmp_path):
    def failure():
        raise RuntimeError('privacy control failed')
    events = prepare(monkeypatch, tmp_path, failure)
    with pytest.raises(RuntimeError, match='privacy control failed'):
        offline_runtime.OfflineRuntime(tmp_path, tmp_path / 'manifest.json')
    assert events == ['onnx_import', 'disabled']
