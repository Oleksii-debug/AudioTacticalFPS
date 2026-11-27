"""Real child imports must leave the sealed Python runtime unchanged.

The workers run with their production interpreter arguments. A small local import
sentinel stands in for a hash-sealed stdlib module; the SAPI child replaces COM
only, so this suite does not claim speech synthesis on a non-Windows host.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from carr_library.speech import reading, runtime


@pytest.fixture
def sealed_runtime(tmp_path, monkeypatch):
    root = tmp_path / "sealed runtime Україна"
    root.mkdir()
    sentinel = root / "sealed_runtime_probe.py"
    sentinel.write_text("VALUE = 'immutable runtime'\n", encoding="utf-8")
    original = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    # -I ignores inherited Python policy. Only the worker's startup arguments
    # can protect this runtime, even when the parent itself was started with -B.
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    prefix = ("import pathlib, sys\n"
              "sys.path.insert(0, str(pathlib.Path(__file__).parent))\n"
              "import sealed_runtime_probe\n")

    def worker(name, body):
        target = root / name
        target.write_text(prefix + body, encoding="utf-8")
        return target

    def verify():
        assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == original
        assert not list(root.rglob("*.pyc")), "A child changed the sealed runtime bytecode inventory."

    return root, worker, verify


def test_regex_pronunciation_child_preserves_runtime(sealed_runtime, monkeypatch):
    root, worker, verify = sealed_runtime
    original = Path(reading.__file__).with_name("regex_worker.py").read_text(encoding="utf-8")
    worker("regex_worker.py", original)
    monkeypatch.setattr(reading, "__file__", str(root / "reading.py"))
    document = reading.ReadingDocument("12 kg")
    assert document.apply_pronunciations([
        reading.PronunciationRule(r"(\d+) kg", r"\1 kilograms", regex=True)])
    assert document.text == "12 kilograms"
    assert document.source_text == "12 kg"
    verify()


def test_native_sapi_child_preserves_runtime(sealed_runtime, monkeypatch):
    root, worker, verify = sealed_runtime
    worker("sapi_native_worker.py", """import json, struct
request = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))
assert request['operation'] == 'voices'
pathlib.Path(sys.argv[2]).write_text(json.dumps({
    'version': 1, 'status': 'listed', 'voices': [],
    'architecture': str(struct.calcsize('P') * 8), 'adapter': 'native-comtypes',
    'dont_write_bytecode': sys.dont_write_bytecode,
}), encoding='utf-8')
""")
    monkeypatch.setattr(runtime, "__file__", str(root / "runtime.py"))
    monkeypatch.setattr(runtime, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    original_find = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **kw: object() if name == "comtypes"
                        else original_find(name, *a, **kw))
    reply = runtime.NativeSapiProvider()._request({"operation": "voices"})
    assert reply["status"] == "listed" and reply["voices"] == []
    assert reply["dont_write_bytecode"] is True
    verify()
