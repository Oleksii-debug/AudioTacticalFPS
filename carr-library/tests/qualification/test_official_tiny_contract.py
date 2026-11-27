"""Input, dependency and accuracy gates; these tests perform no inference."""
import email.parser
import importlib.util
from pathlib import Path
import sys

import pytest

_spec = importlib.util.spec_from_file_location(
    "official_tiny_proof",
    Path(__file__).resolve().parents[2] / "scripts/qualify_official_tiny.py")
proof = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proof)


def test_import_has_no_model_or_download_side_effect():
    assert "faster_whisper" not in sys.modules
    assert len(proof.PINS) == 31
    assert proof.WINDOWS_PINS == {"colorama": "0.4.6"}
    assert set(proof.MODEL_FILES) == {
        "model.bin", "config.json", "tokenizer.json", "vocabulary.txt", "README.md"}
    assert all(len(digest) == 64 for size, digest in proof.MODEL_FILES.values())


def test_accuracy_gate_measures_edits_and_rejects_unbounded_output():
    assert proof.word_error_rate("one two three", "One, two three.") == 0
    assert proof.word_error_rate("one two three", "one wrong three") == pytest.approx(1 / 3)
    assert proof.word_error_rate("one two", "") == 1
    with pytest.raises(ValueError):
        proof.word_error_rate("one", "word " * 2049)


def test_complete_model_pack_refuses_missing_and_changed_bytes(tmp_path, monkeypatch):
    import hashlib
    files = {"model.bin": b"synthetic",
             "README.md": b"---\nlicense: mit\n---\n"}
    monkeypatch.setattr(proof, "MODEL_FILES", {
        name: (len(raw), hashlib.sha256(raw).hexdigest()) for name, raw in files.items()})
    for name, raw in files.items():
        (tmp_path / name).write_bytes(raw)
    assert set(proof.model_inventory(tmp_path)) == set(files)
    (tmp_path / "model.bin").write_bytes(b"tampered!")
    with pytest.raises(ValueError, match="pinned identity"):
        proof.model_inventory(tmp_path)
    (tmp_path / "model.bin").unlink()
    with pytest.raises(ValueError, match="complete"):
        proof.model_inventory(tmp_path)


def test_exact_closure_rejects_missing_incompatible_and_extra_packages(monkeypatch):
    from pip._vendor.packaging.markers import default_environment
    windows = default_environment()["sys_platform"] == "win32"
    monkeypatch.setattr(proof, "ROOTS", ("root",))
    def meta(requirement="child>=1,<2"):
        return email.parser.Parser().parsestr(
            "Name: root\nVersion: 1\nRequires-Python: >=3.11\nRequires-Dist: "
            + requirement + "\n")
    child = email.parser.Parser().parsestr("Name: child\nVersion: 1\n")
    data = {"root": meta(), "child": child}
    assert proof.dependency_closure(data, {"root": "1", "child": "1"}, windows=windows) == ["child", "root"]
    for versions in ({"root": "1"}, {"root": "1", "child": "2"},
                     {"root": "1", "child": "1", "extra": "1"}):
        with pytest.raises(ValueError):
            proof.dependency_closure(data, versions, windows=windows)
    data["root"] = meta("child @ https://example.invalid/arbitrary.whl")
    with pytest.raises(ValueError, match="Direct-URL"):
        proof.dependency_closure(data, {"root": "1", "child": "1"}, windows=windows)


def test_marker_extras_are_required_and_not_guessed(monkeypatch):
    from pip._vendor.packaging.markers import default_environment
    monkeypatch.setattr(proof, "ROOTS", ("root",))
    def meta(text):
        return email.parser.Parser().parsestr(text)
    data = {
        "root": meta("Name: root\nVersion: 1\nRequires-Dist: child[chosen]>=1\n"),
        "child": meta("Name: child\nVersion: 1\nRequires-Dist: leaf; extra == 'chosen'\n"),
        "leaf": meta("Name: leaf\nVersion: 1\n"),
    }
    windows = default_environment()["sys_platform"] == "win32"
    assert proof.dependency_closure(
        data, {"root": "1", "child": "1", "leaf": "1"}, windows=windows) == ["child", "leaf", "root"]
    with pytest.raises(ValueError):
        proof.dependency_closure(data, {"root": "1", "child": "1"}, windows=windows)


def test_evidence_never_upgrades_engine_only_to_private_worker_acceptance(tmp_path):
    root = tmp_path / "owned"
    root.mkdir()
    proof.write_json(root / "acquisition.json", {"status": "verified"})
    proof.write_json(root / "fixture.json", {"kind": "public-sapi-fixture"})
    proof.write_json(root / "inference.json", {"status": "passed"})
    proof.write_json(root / "firewall.json", {"status": "enforced", "cleanup_succeeded": True})
    output = tmp_path / "proof.json"
    assert proof.evidence(root, output) == 0
    value = proof.load_json(output)
    assert value["private_worker_interoperability"] == "NOT_RUN"
    with pytest.raises(ValueError, match="byte budget"):
        proof.write_json(tmp_path / "large.json", {"text": "x" * proof.MAX_JSON})


def test_native_denial_requires_access_denied_not_an_unrelated_timeout(tmp_path, monkeypatch):
    proof.write_json(tmp_path / "network-probe.json", {
        "ipv4": "192.0.2.1", "port": 443, "connected_before_rule": True})
    class FakeSocket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): pass
        def connect(self, target): raise TimeoutError("no route does not prove firewall denial")
    monkeypatch.setattr(proof.socket, "socket", lambda *args: FakeSocket())
    with pytest.raises(RuntimeError, match="WSAEACCES"):
        proof.probe_network(tmp_path, True)


def test_real_same_size_mutation_during_read_is_still_rejected(tmp_path, monkeypatch):
    target = tmp_path / "changing.bin"
    target.write_bytes(b"old-content")
    original_open = Path.open
    class ChangingReader:
        def __init__(self):
            self.stream = original_open(target, "rb")
            self.changed = False
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def fileno(self): return self.stream.fileno()
        def read(self, size):
            raw = self.stream.read(size)
            if raw and not self.changed:
                self.changed = True
                with original_open(target, "r+b") as writer:
                    writer.write(b"new-content")
                    writer.flush()
                    proof.os.fsync(writer.fileno())
                # Set a visible, different modification time without relying
                # on the filesystem's minimum timestamp resolution.
                before = target.stat()
                proof.os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
            return raw
    def selected_open(path, *args, **kwargs):
        if path == target and args == ("rb",):
            return ChangingReader()
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", selected_open)
    with pytest.raises(ValueError, match="changed during verification"):
        proof.checked_file(target, 1024)
