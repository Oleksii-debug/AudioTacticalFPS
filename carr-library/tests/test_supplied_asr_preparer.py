"""Public independent fixtures for a private-source preparer.

No supplied worker, private recordings, model or ASR runtime is included. These
tests exercise the transformation and publication rules, not transcription.
"""
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


SYNTHETIC = '''# Independent protocol fixture, not supplied worker source.
class Worker:
    def transcribe_one(self, item, settings):
        try:
            segments, info = self.model.transcribe(
                item["path"],  # Keep this spelling and comment.
                multilingual=settings["multilingual"],
                language=None if settings["language"] == "auto" else settings["language"],
            )
        except TypeError:
            segments, info = self.model.transcribe(item["path"])
        return "", info.language, [], {"selected_language": settings["language"]}

def _v43_settings_attempts(base):
    return [("changed", {"example": "synthetic"})]
'''


@pytest.fixture
def preparer():
    source = Path(__file__).resolve().parents[1] / "scripts" / "prepare_supplied_asr.py"
    spec = importlib.util.spec_from_file_location("supplied_asr_preparer_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _permit_synthetic_fixture(module, monkeypatch, raw):
    # Test-only identity injection. The actual script keeps the exact owner-
    # reviewed byte count and SHA; it exposes no CLI override for those values.
    monkeypatch.setattr(module, "SOURCE_BYTES", len(raw))
    monkeypatch.setattr(module, "SOURCE_SHA256", hashlib.sha256(raw).hexdigest())


def test_only_exact_reviewed_identity_is_admitted_before_parsing(preparer, monkeypatch):
    assert preparer.SOURCE_BYTES == 37_974
    assert preparer.SOURCE_SHA256 == "1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9"
    monkeypatch.setattr(preparer.ast, "parse", lambda value: pytest.fail("Unreviewed input was parsed"))
    for raw in (b"", b"x" * preparer.SOURCE_BYTES, b"x" * (preparer.SOURCE_BYTES + 1), bytearray(b"x")):
        with pytest.raises(ValueError, match="Unreviewed worker revision"):
            preparer.prepare(raw)


def test_source_read_is_bounded_before_identity_validation(preparer, tmp_path):
    path = tmp_path / "unreviewed.py"
    path.write_bytes(b"x" * (preparer.SOURCE_BYTES + 100))
    raw = preparer._read_source(path)
    assert len(raw) == preparer.SOURCE_BYTES + 1
    with pytest.raises(ValueError, match="Unreviewed"):
        preparer.prepare(raw)
    assert path.stat().st_size == preparer.SOURCE_BYTES + 100


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_transform_keeps_supplied_call_bytes_without_ast_reformatting(preparer, monkeypatch, newline):
    raw = SYNTHETIC.replace("\n", newline).encode("utf-8")
    _permit_synthetic_fixture(preparer, monkeypatch, raw)
    monkeypatch.setattr(preparer.ast, "unparse", lambda value: pytest.fail("Do not reformat the supplied inference call"))
    candidate = preparer.prepare(raw)
    assert candidate != raw
    assert b'item["path"],  # Keep this spelling and comment.' in candidate
    assert b'multilingual=settings["multilingual"],' in candidate
    assert b'language=None if settings["language"] == "auto" else settings["language"],' in candidate
    if newline == "\r\n":
        assert b"\n" not in candidate.replace(b"\r\n", b"")
    namespace = {}
    exec(compile(candidate, "<independent-protocol-fixture>", "exec"), namespace)
    settings = {"language": "auto", "multilingual": True, "beam_size": 5}
    attempts = namespace["_v43_settings_attempts"](settings)
    assert attempts == [("original", settings)]
    assert attempts[0][1] is not settings
    attempts[0][1]["language"] = "other"
    assert settings["language"] == "auto"


def test_requested_auto_and_multilingual_are_not_downgraded(preparer, monkeypatch):
    raw = SYNTHETIC.encode("utf-8")
    _permit_synthetic_fixture(preparer, monkeypatch, raw)
    namespace = {}
    exec(compile(preparer.prepare(raw), "<independent-protocol-fixture>", "exec"), namespace)
    calls = []

    def unsupported(path, **kwargs):
        calls.append((path, kwargs))
        raise TypeError("Synthetic unsupported option")

    worker = namespace["Worker"]()
    worker.model = SimpleNamespace(transcribe=unsupported)
    with pytest.raises(TypeError, match="Synthetic unsupported"):
        worker.transcribe_one({"path": "synthetic.wav"}, {"language": "auto", "multilingual": True})
    assert calls == [("synthetic.wav", {"language": None, "multilingual": True})]


def test_preserved_three_checks_distinguish_original_and_prepared_synthetic_behavior(preparer, monkeypatch, tmp_path):
    raw = SYNTHETIC.encode("utf-8")
    _permit_synthetic_fixture(preparer, monkeypatch, raw)
    baseline = tmp_path / "baseline.py"
    candidate = tmp_path / "candidate.py"
    baseline.write_bytes(raw)
    candidate.write_bytes(preparer.prepare(raw))
    assert list(preparer.checks(baseline).values()) == [False, False, True]
    assert list(preparer.checks(candidate).values()) == [True, True, True]


@pytest.mark.parametrize("change", [
    lambda source: source.replace("except TypeError:", "except ValueError:"),
    lambda source: source.replace('        return "", info.language', '        finally:\n            pass\n        return "", info.language'),
    lambda source: source.replace("self.model.transcribe(", "self.other.transcribe(", 1),
    lambda source: source.replace("def _v43_settings_attempts(base):", "def _v43_settings_attempts(base=None):"),
    lambda source: source + "\nclass Worker:\n    pass\n",
], ids=["wrong-exception", "finally", "wrong-model", "changed-settings-signature", "duplicate-worker"])
def test_structural_mismatch_fails_without_assertion_dependence(preparer, monkeypatch, change):
    raw = change(SYNTHETIC).encode("utf-8")
    _permit_synthetic_fixture(preparer, monkeypatch, raw)
    with pytest.raises(ValueError, match="Unexpected reviewed"):
        preparer.prepare(raw)


def test_private_output_is_exclusive_and_original_is_unchanged(preparer, tmp_path):
    source = tmp_path / "original.py"
    source.write_bytes(b"independent original")
    destination = tmp_path / "prepared.py"
    preparer._publish_private(b"independent candidate", destination)
    assert destination.read_bytes() == b"independent candidate"
    with pytest.raises(FileExistsError):
        preparer._publish_private(b"replacement", destination)
    with pytest.raises(FileExistsError):
        preparer._publish_private(b"replacement", source)
    assert destination.read_bytes() == b"independent candidate"
    assert source.read_bytes() == b"independent original"
    assert {path.name for path in tmp_path.iterdir()} == {"original.py", "prepared.py"}


def test_failed_durable_write_does_not_publish_or_leave_partial_source(preparer, monkeypatch, tmp_path):
    def fail(descriptor):
        raise OSError("Synthetic fsync failure")

    monkeypatch.setattr(preparer.os, "fsync", fail)
    destination = tmp_path / "prepared.py"
    with pytest.raises(OSError, match="Synthetic fsync"):
        preparer._publish_private(b"independent candidate", destination)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_unsupported_exclusive_publication_never_falls_back_to_overwrite(preparer, monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise OSError("Synthetic hard-link rejection")

    monkeypatch.setattr(preparer.os, "link", fail)
    destination = tmp_path / "prepared.py"
    with pytest.raises(OSError, match="hard-link rejection"):
        preparer._publish_private(b"independent candidate", destination)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []
