"""Actual synthetic two-archive checks for the CI-only owner assembly wrapper."""
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


existing = load("owner_existing_delivery_tests", ROOT / "tests/packaging/test_delivery_archive.py")
owner = load("owner_candidate_assembly_tests", ROOT / "scripts/assemble_owner_candidate_archives.py")
bundle = existing.bundle
COMMIT = existing.COMMIT
TREE = "b" * 40


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save_inputs(case):
    (case.inputs / owner.INPUT_REPORT).write_bytes(
        (json.dumps(case.receipt, sort_keys=True) + "\n").encode("utf-8"))


def seal_manifest(case):
    raw = (json.dumps(case.manifest, sort_keys=True) + "\n").encode("utf-8")
    (case.bundle / "bundle-manifest.json").write_bytes(raw)
    retained = case.inputs / "metadata/no-audio-bundle-manifest.json"
    retained.write_bytes(raw)
    case.receipt["no_audio_bundle_manifest_sha256"] = digest(raw)
    case.receipt["files"]["metadata/no-audio-bundle-manifest.json"] = {
        "sha256": digest(raw), "bytes": len(raw)}
    save_inputs(case)


@pytest.fixture
def accepted(bundle, tmp_path, monkeypatch):
    # Only source/native acceptance metadata is synthetic. The existing
    # trusted installer and archive writer/verifier run on actual files.
    delivery = existing.delivery
    monkeypatch.setattr(delivery, "_tree", lambda commit: TREE)
    inputs = tmp_path / "accepted public inputs"
    (inputs / "metadata").mkdir(parents=True)
    manifest = json.loads((bundle / "bundle-manifest.json").read_bytes())
    manifest["source_tree"] = TREE
    manifest.update(bundled_python=True, bundled_native_runtime=True,
                    bundled_trusted_client=True, bundled_launcher=True)
    payloads = {
        "runtime/python.exe": b"synthetic runtime, never executed",
        "native-runtime/native-manifest.json": b'{"synthetic":true}\n',
        "native-runtime/bin/msvcp140.dll": b"synthetic native file",
        "native-runtime/bin/msvcp140_1.dll": b"synthetic native file 1",
        "application-source.json": b'{"synthetic_source_acceptance":true}\n',
        "ResearchLibrary.exe": b"synthetic launcher, never executed",
        "native-launcher-build.json": b'{"synthetic_build_acceptance":true}\n',
        "runtime-sbom.cdx.json": b'{"synthetic_sbom":true}\n',
        "trusted-client-dependencies.json": b'{"synthetic_dependency_review":true}\n',
    }
    for name, raw in payloads.items():
        path = bundle / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        manifest["files"][name] = digest(raw)
    extra = ("cffi", "cryptography", "pycparser", "securesystemslib", "tuf")
    wheels = [
        {"name": name, "version": "1", "filename": name + "-1-py3-none-any.whl",
         "bytes": 1, "sha256": "0" * 64, "retained": index < 5}
        for index, name in enumerate((*extra, *(f"fixture{n}" for n in range(51))))
    ]
    for row in wheels:
        raw = ("synthetic wheel " + row["name"]).encode("ascii")
        row["bytes"], row["sha256"] = len(raw), digest(raw)
        name = "wheelhouse/" + row["filename"]
        (bundle / name).write_bytes(raw)
        manifest["files"][name] = row["sha256"]
    receipt = {
        "schema": "resonance-owner-candidate-inputs/1",
        "status": "OWNER_CANDIDATE_INPUTS_READY",
        "source_commit": COMMIT, "source_tree": TREE,
        "complete_bundle_outcome": "failure",
        "full_qualification": "NOT_ESTABLISHED", "release_eligibility": "NOT_APPROVED",
        "assembled_owner_bundle": False, "private_parent_included": False,
        "no_audio_installation_and_native_library_checks": "PASS",
        "dependency_count": 56, "retained_optional_count": 5, "wheels": wheels,
        "application_wheel": "universal_research_library-1.0-py3-none-any.whl",
        "files": {},
    }
    for key in ("application_wheel_sha256", "application_source_sha256", "launcher_sha256",
                "native_build_receipt_sha256", "dependency_lock_sha256",
                "semantic_parent_lock_sha256", "sbom_sha256", "dependency_review_sha256"):
        receipt[key] = "0" * 64
    bindings = {
        "application-source.json": "application_source_sha256",
        "ResearchLibrary.exe": "launcher_sha256",
        "native-launcher-build.json": "native_build_receipt_sha256",
        "runtime-sbom.cdx.json": "sbom_sha256",
        "trusted-client-dependencies.json": "dependency_review_sha256",
        "wheelhouse/" + receipt["application_wheel"]: "application_wheel_sha256",
    }
    for name, field in bindings.items():
        receipt[field] = manifest["files"][name]
    case = SimpleNamespace(bundle=bundle, inputs=inputs, manifest=manifest, receipt=receipt,
        output=tmp_path / "ephemeral archives", evidence=tmp_path / "public receipts",
        delivery=delivery)
    seal_manifest(case)
    return case


def assemble(case, **kwargs):
    return owner.assemble(case.bundle, case.inputs, case.output, case.evidence,
        kwargs.get("source_commit", COMMIT), TREE,
        full_outcome=kwargs.get("full_outcome", "failure"), delivery=case.delivery)


def test_actual_two_archive_assembly_keeps_public_evidence_binary_free_and_unqualified(accepted):
    before = (accepted.bundle / "bundle-manifest.json").read_bytes()
    result = assemble(accepted)
    assert result["status"] == "OWNER_CANDIDATE_UNQUALIFIED"
    assert result["archive_assembly"] == result["independent_two_archive_verification"] == "PASS"
    assert result["complete_bundle_outcome"] == "failure"
    assert result["full_qualification"] == "NOT_ESTABLISHED"
    assert result["release_eligibility"] == "NOT_APPROVED"
    assert result["assembled_owner_bundle"] is True
    assert result["complete_binary_archive_uploaded"] is False
    assert result["binary_handoff"] == "NOT_PERFORMED"
    assert set(path.name for path in accepted.evidence.iterdir()) == owner.PUBLIC_FILES
    assert all(path.suffix == ".json" for path in accepted.evidence.iterdir())
    assert len(list(accepted.output.glob("*.zip"))) == 2
    app = accepted.output / result["archives"]["application"]["path"]
    sources = accepted.output / result["archives"]["sources"]["path"]
    verified = accepted.delivery.verify_archives(app, sources, before, COMMIT)
    assert verified["application"]["sha256"] == result["archives"]["application"]["sha256"]
    assert verified["sources"]["sha256"] == result["archives"]["sources"]["sha256"]
    with zipfile.ZipFile(sources) as archive:
        assert archive.namelist() == [accepted.delivery.PREFIX + accepted.delivery.SOURCES]
    with zipfile.ZipFile(app) as archive:
        assert archive.read(accepted.delivery.PREFIX + "bundle-manifest.json") == before
        assert accepted.delivery.PREFIX + accepted.delivery.SOURCES not in archive.namelist()
    assert (accepted.bundle / "bundle-manifest.json").read_bytes() == before
    assert json.loads((accepted.evidence / "owner-candidate-assembly.json").read_bytes()) == result


@pytest.mark.parametrize("fault", [
    "input-status", "source-head", "full-outcome", "changed-retained-bytes",
    "undeclared-input", "node-payload", "audio-payload",
])
def test_unaccepted_or_changed_inputs_refuse_before_archive_creation(accepted, monkeypatch, fault):
    options = {}
    if fault == "input-status":
        accepted.receipt["status"] = "PASS"
        save_inputs(accepted)
    elif fault == "source-head":
        options["source_commit"] = "c" * 40
    elif fault == "full-outcome":
        options["full_outcome"] = "success"
    elif fault == "changed-retained-bytes":
        path = accepted.inputs / "metadata/no-audio-bundle-manifest.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif fault == "undeclared-input":
        (accepted.inputs / "undeclared.exe").write_bytes(b"synthetic, never executable")
    elif fault == "node-payload":
        payload = b"synthetic, never executable"
        (accepted.bundle / "node.exe").write_bytes(payload)
        accepted.manifest["files"]["node.exe"] = digest(payload)
        seal_manifest(accepted)
    else:
        accepted.manifest["bundled_audio"] = True
        seal_manifest(accepted)
    def forbidden(*args, **kwargs):
        pytest.fail("Unaccepted owner inputs must not reach the archive builder.")
    monkeypatch.setattr(accepted.delivery, "create_delivery", forbidden)
    with pytest.raises(ValueError):
        assemble(accepted, **options)
    assert not accepted.output.exists()
    assert not accepted.evidence.exists()


@pytest.mark.parametrize("kind", ["application", "sources"])
def test_second_actual_archive_verification_rejects_late_modified_output(accepted, monkeypatch, kind):
    original = accepted.delivery.create_delivery
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        path = Path(result["output"]) / result["archives"][kind]["path"]
        with path.open("ab") as stream:
            stream.write(b"unreceipted trailing payload")
        return result
    monkeypatch.setattr(accepted.delivery, "create_delivery", changed)
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        assemble(accepted)
    assert len(list(accepted.output.glob("*.zip"))) == 2
    assert not accepted.evidence.exists()


def test_changed_original_seal_after_archive_commit_never_publishes_acceptance(accepted, monkeypatch):
    original = accepted.delivery.create_delivery
    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        path = accepted.bundle / "bundle-manifest.json"
        path.write_bytes(path.read_bytes() + b" ")
        return result
    monkeypatch.setattr(accepted.delivery, "create_delivery", changed)
    with pytest.raises(ValueError, match="manifest changed"):
        assemble(accepted)
    assert len(list(accepted.output.glob("*.zip"))) == 2
    assert not accepted.evidence.exists()


def test_public_evidence_cannot_be_nested_in_complete_archive_output(accepted):
    accepted.evidence = accepted.output / "public"
    with pytest.raises(ValueError, match="disjoint"):
        assemble(accepted)
    assert not accepted.output.exists()

def test_missing_application_source_cannot_be_resealed_as_null_acceptance(accepted, monkeypatch):
    (accepted.bundle / "application-source.json").unlink()
    del accepted.manifest["files"]["application-source.json"]
    accepted.receipt["application_source_sha256"] = None
    seal_manifest(accepted)
    # This resealed payload still passes the unchanged installer's generic
    # inventory check. The owner-input source binding must reject it itself.
    accepted.delivery._trusted_verify(accepted.bundle)
    def forbidden(*args, **kwargs):
        pytest.fail("A missing application-source member must not reach archive assembly.")
    monkeypatch.setattr(accepted.delivery, "create_delivery", forbidden)
    with pytest.raises(ValueError, match="required application/native/dependency payload"):
        assemble(accepted)
    assert not accepted.output.exists()
    assert not accepted.evidence.exists()


@pytest.mark.parametrize("field,digest_value", [
    ("application_source_sha256", None),
    ("launcher_sha256", "A" * 64),
    ("native_build_receipt_sha256", "0" * 63),
    ("sbom_sha256", "z" * 64),
    ("dependency_review_sha256", 0),
    ("application_wheel_sha256", ""),
], ids=["source-null", "launcher-uppercase", "build-short", "sbom-nonhex", "review-number", "wheel-empty"])
def test_noncanonical_required_digest_refuses_before_archive_creation(accepted, monkeypatch, field, digest_value):
    accepted.receipt[field] = digest_value
    save_inputs(accepted)
    def forbidden(*args, **kwargs):
        pytest.fail("An invalid required payload digest must not reach archive assembly.")
    monkeypatch.setattr(accepted.delivery, "create_delivery", forbidden)
    with pytest.raises(ValueError, match="required application/native/dependency payload"):
        assemble(accepted)
    assert not accepted.output.exists()
    assert not accepted.evidence.exists()


@pytest.mark.parametrize("filename", ["../application.whl", r"nested\application.whl", "application.zip"],
                         ids=["parent", "windows-child", "not-wheel"])
def test_application_wheel_must_be_a_plain_basename_before_archive_creation(accepted, monkeypatch, filename):
    accepted.receipt["application_wheel"] = filename
    save_inputs(accepted)
    def forbidden(*args, **kwargs):
        pytest.fail("An invalid application wheel name must not reach archive assembly.")
    monkeypatch.setattr(accepted.delivery, "create_delivery", forbidden)
    with pytest.raises(ValueError, match="plain wheel basename"):
        assemble(accepted)
    assert not accepted.output.exists()
    assert not accepted.evidence.exists()
