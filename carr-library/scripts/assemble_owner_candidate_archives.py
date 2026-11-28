"""Assemble an unqualified owner candidate; retain only public-safe receipts.

The preceding required owner-candidate-inputs step owns installed/native
qualification. This helper reuses the unchanged delivery builder and verifies
its two archives again. Complete archives stay in RUNNER_TEMP and are never
placed in the public artifact directory.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
INPUT_REPORT = "owner-candidate-inputs.json"
MAX_REPORT = 1024 * 1024
PUBLIC_FILES = {"delivery-verification.json", "owner-candidate-assembly.json"}
PACKAGING_PINS = {
    "packaging/create_delivery.py": "b9041dd6a09156458d4991a7a8635954f516d21f",
    "packaging/install_windows.py": "e4c30a7600c7afdb6771d9ae58b1d16558df0eba",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_delivery():
    path = ROOT / "packaging/create_delivery.py"
    spec = importlib.util.spec_from_file_location("owner_candidate_existing_delivery", path)
    raw = path.read_bytes()
    actual = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    require(actual == PACKAGING_PINS["packaging/create_delivery.py"],
            "The unchanged reviewed delivery builder is required before import.")
    module = importlib.util.module_from_spec(spec)
    exec(compile(raw, str(path), "exec"), module.__dict__)
    return module


def git(*arguments):
    return subprocess.run(
        ["git", "--no-replace-objects", "-C", str(ROOT.parent), *arguments],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()


def source_bindings(commit, delivery):
    require(git("rev-parse", "HEAD") == commit, "Owner assembly checkout differs from the event source.")
    result = {}
    for relative in (*PACKAGING_PINS, "scripts/assemble_owner_candidate_archives.py",
                     "scripts/retain_owner_candidate_inputs.py"):
        path = ROOT / relative
        raw = read_bounded(path, delivery, MAX_REPORT)
        actual = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
        expected = git("rev-parse", commit + ":carr-library/" + relative)
        require(actual == expected, "Owner assembly helper differs from its committed source.")
        if relative in PACKAGING_PINS:
            require(actual == PACKAGING_PINS[relative], "The reviewed delivery implementation changed.")
        result[relative] = {"git_blob": actual, "sha256": hashlib.sha256(raw).hexdigest()}
    return result


def read_bounded(path, delivery, maximum=MAX_REPORT):
    with delivery._file(path) as stream:
        raw = stream.read(maximum + 1)
    require(len(raw) <= maximum, "Owner assembly metadata exceeds its finite bound.")
    return raw


def decode(raw, delivery):
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=delivery._unique)
    require(isinstance(value, dict), "Owner assembly metadata must be an object.")
    return value


def file_identity(path, delivery):
    with delivery._file(path) as stream:
        digest, size = delivery._stream(stream, lambda: False)
    return {"sha256": digest, "bytes": size}


def validate_inputs(inputs, delivery, source_commit, source_tree, full_outcome):
    require(full_outcome == "failure", "This fallback must preserve the failed full-bundle verdict.")
    raw = read_bounded(inputs / INPUT_REPORT, delivery)
    report = decode(raw, delivery)
    fixed = {
        "schema": "resonance-owner-candidate-inputs/1",
        "status": "OWNER_CANDIDATE_INPUTS_READY",
        "source_commit": source_commit,
        "source_tree": source_tree,
        "complete_bundle_outcome": "failure",
        "full_qualification": "NOT_ESTABLISHED",
        "release_eligibility": "NOT_APPROVED",
        "assembled_owner_bundle": False,
        "private_parent_included": False,
        "no_audio_installation_and_native_library_checks": "PASS",
    }
    require(all(type(report.get(key)) is type(value) and report[key] == value
                for key, value in fixed.items()), "Required owner-input acceptance is absent or inconsistent.")
    require(type(report.get("dependency_count")) is int and report["dependency_count"] == 56
            and type(report.get("retained_optional_count")) is int
            and report["retained_optional_count"] == 5, "Owner input dependency closure is incomplete.")
    wheels = report.get("wheels")
    require(isinstance(wheels, list) and len(wheels) == 56
            and all(isinstance(row, dict) for row in wheels), "All 56 installed wheel records are required.")
    names = set()
    for row in wheels:
        require(isinstance(row.get("name"), str) and row["name"] not in names,
                "Owner input wheel names must be distinct.")
        names.add(row["name"])
        require(isinstance(row.get("filename"), str)
                and PurePosixPath(row["filename"]).name == row["filename"]
                and row["filename"].endswith(".whl")
                and isinstance(row.get("version"), str)
                and re.fullmatch("[0-9a-f]{64}", row.get("sha256", "")) is not None
                and type(row.get("bytes")) is int and row["bytes"] > 0
                and type(row.get("retained")) is bool, "Invalid owner input wheel record.")
    require({row["name"] for row in wheels if row["retained"]} ==
            {"cffi", "cryptography", "pycparser", "securesystemslib", "tuf"},
            "The five optional wheels differ from the reviewed closure.")
    files = report.get("files")
    require(isinstance(files, dict) and 1 <= len(files) <= 128,
            "Owner inputs require a bounded exact file inventory.")
    seen = set()
    for name, expected in files.items():
        folded = delivery._name(name)
        require(folded not in seen and name != INPUT_REPORT, "Ambiguous owner input inventory.")
        seen.add(folded)
        require(isinstance(expected, dict) and set(expected) == {"sha256", "bytes"}
                and re.fullmatch("[0-9a-f]{64}", expected.get("sha256", "")) is not None
                and type(expected.get("bytes")) is int and expected["bytes"] >= 0,
                "Invalid owner input file receipt.")
        require(file_identity(inputs / name, delivery) == expected, "Retained owner input bytes changed.")
    actual = set()
    for path in inputs.rglob("*"):
        info = delivery._plain(path, directory=path.is_dir())
        if path.is_file():
            require(info.st_nlink == 1, "Owner input aliases are not admitted.")
            actual.add(path.relative_to(inputs).as_posix())
    require(actual == set(files) | {INPUT_REPORT}, "Owner input directory has an undeclared file.")
    return raw, report


def no_audio_or_node(manifest, delivery):
    delivery._require_transferable_audio_inventory(manifest)
    require(manifest.get("bundled_citation_runtime", False) is False,
            "The owner candidate cannot include a citation Node runtime.")
    forbidden_names = {"node", "node.exe", "node.dll", "ffmpeg", "ffmpeg.exe",
                       "ffprobe", "ffprobe.exe", "ffplay", "ffplay.exe"}
    for name in manifest["files"]:
        parts = PurePosixPath(name).parts
        require(parts[0].casefold() not in {"audio", "audio-runtime", "citation-runtime"}
                and parts[-1].casefold() not in forbidden_names,
                "The owner candidate cannot include audio or Node binaries.")


def bind_accepted_payload(bundle, manifest, receipt):
    require(all(manifest.get(name) is True for name in (
        "bundled_python", "bundled_native_runtime", "bundled_ocr",
        "bundled_trusted_client", "bundled_launcher")),
        "The accepted runnable owner runtime inventory is incomplete.")
    application_wheel = receipt.get("application_wheel")
    require(isinstance(application_wheel, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\.whl", application_wheel) is not None,
            "The accepted application wheel must be a plain wheel basename.")
    bindings = {
        "application-source.json": "application_source_sha256",
        "ResearchLibrary.exe": "launcher_sha256",
        "native-launcher-build.json": "native_build_receipt_sha256",
        "runtime-sbom.cdx.json": "sbom_sha256",
        "trusted-client-dependencies.json": "dependency_review_sha256",
        "wheelhouse/" + application_wheel: "application_wheel_sha256",
    }
    for name, field in bindings.items():
        digest = receipt.get(field)
        require(name in manifest["files"] and isinstance(digest, str)
                and re.fullmatch("[0-9a-f]{64}", digest) is not None,
                "A required application/native/dependency payload or accepted digest is absent or invalid.")
        require(manifest["files"][name] == digest,
                "A sealed application/native/dependency payload differs from its accepted receipt.")
    expected_wheels = {"wheelhouse/" + application_wheel}
    for row in receipt["wheels"]:
        name = "wheelhouse/" + row["filename"]
        require(manifest["files"].get(name) == row["sha256"]
                and (bundle / name).stat().st_size == row["bytes"],
                "The complete accepted wheel closure differs from the sealed payload.")
        expected_wheels.add(name)
    require({name for name in manifest["files"] if name.startswith("wheelhouse/")} == expected_wheels,
            "The complete owner archive requires exactly the application and all 56 pinned wheels.")


def write_new_json(path, value):
    raw = (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")
    require(len(raw) <= MAX_REPORT, "Public owner evidence exceeds its finite bound.")
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def assemble(bundle, inputs, output, evidence, source_commit, source_tree, *,
             full_outcome, delivery, source_records=None):
    """Reuse the accepted CI input receipt; grant no release or execution authority."""
    require(re.fullmatch("[0-9a-f]{40}", source_commit) is not None
            and re.fullmatch("[0-9a-f]{40}", source_tree) is not None, "Exact source and tree are required.")
    paths = [delivery._absolute(path) for path in (bundle, inputs, output, evidence)]
    bundle, inputs, output, evidence = paths
    require(all(not left.is_relative_to(right) and not right.is_relative_to(left)
                for index, left in enumerate(paths) for right in paths[index + 1:]),
            "Assembly, accepted inputs and public evidence must use disjoint directories.")
    require(not os.path.lexists(output) and not os.path.lexists(evidence),
            "Use new owner archive and public-evidence directories.")
    scopes = [delivery._directories(path) for path in (bundle, inputs, output.parent, evidence.parent)]
    raw_inputs, inputs_report = validate_inputs(inputs, delivery, source_commit, source_tree, full_outcome)
    raw_manifest = delivery._read_manifest(bundle, lambda: False)
    manifest = delivery._manifest(raw_manifest, source_commit)
    no_audio_or_node(manifest, delivery)
    manifest_hash = hashlib.sha256(raw_manifest).hexdigest()
    require(manifest_hash == inputs_report["no_audio_bundle_manifest_sha256"],
            "The accepted no-audio seal changed before assembly.")
    require(read_bounded(inputs / "metadata/no-audio-bundle-manifest.json", delivery,
                         delivery.MAX_MANIFEST) == raw_manifest,
            "The retained source/native acceptance belongs to a different bundle.")
    require(manifest.get("source_tree") == source_tree, "The no-audio bundle tree differs.")
    bind_accepted_payload(bundle, manifest, inputs_report)
    delivery._trusted_verify(bundle, manifest_bytes=raw_manifest)
    for scope in scopes:
        delivery._unchanged_directories(scope)
    built = delivery.create_delivery(bundle, source_commit, output)
    app_name = "Universal-Research-Library-Application-" + source_commit[:12] + ".zip"
    sources_name = "Universal-Research-Library-Sources-" + source_commit[:12] + ".zip"
    require({path.name for path in output.iterdir()} ==
            {app_name, sources_name, "delivery-verification.json", "START_HERE_UK.txt"},
            "The existing delivery builder returned an unexpected file inventory.")
    raw_built = read_bounded(output / "delivery-verification.json", delivery)
    saved = decode(raw_built, delivery)
    require(saved == {key: value for key, value in built.items() if key != "output"}
            and saved["source_commit"] == source_commit and saved["source_tree"] == source_tree
            and saved["manifest_sha256"] == manifest_hash,
            "The committed archive receipt differs from the exact source and seal.")
    # Independent second verification reads every member, compressed frame,
    # central directory and archive hash after the builder has returned.
    checked = delivery.verify_archives(output / app_name, output / sources_name,
                                       raw_manifest, source_commit)
    for kind, name in (("application", app_name), ("sources", sources_name)):
        current = {key: value for key, value in checked[kind].items() if key != "_verified_stamp"}
        require(current == {key: value for key, value in saved["archives"][kind].items()
                            if key not in {"path", "reused"}}
                and saved["archives"][kind]["path"] == name
                and saved["archives"][kind]["reused"] is False,
                "Independent archive verification differs from the builder receipt.")
    require(read_bounded(inputs / INPUT_REPORT, delivery) == raw_inputs,
            "Owner input acceptance changed during archive assembly.")
    validate_inputs(inputs, delivery, source_commit, source_tree, full_outcome)
    require(delivery._read_manifest(bundle, lambda: False) == raw_manifest,
            "The original no-audio manifest changed during archive assembly.")
    delivery._trusted_verify(bundle, manifest_bytes=raw_manifest)
    for scope in scopes:
        delivery._unchanged_directories(scope)
    report = {
        "schema": "resonance-owner-candidate-assembly/1",
        "status": "OWNER_CANDIDATE_UNQUALIFIED",
        "archive_assembly": "PASS",
        "independent_two_archive_verification": "PASS",
        "original_no_audio_seal_unchanged": "PASS",
        "source_commit": source_commit,
        "source_tree": source_tree,
        "complete_bundle_outcome": "failure",
        "full_qualification": "NOT_ESTABLISHED",
        "release_eligibility": "NOT_APPROVED",
        "assembled_owner_bundle": True,
        "private_parent_required": False,
        "private_parent_used": False,
        "complete_binary_archive_uploaded": False,
        "binary_handoff": "NOT_PERFORMED",
        "binary_retention": "Ephemeral CI-owned RUNNER_TEMP only; no public upload.",
        "owner_input_receipt_sha256": hashlib.sha256(raw_inputs).hexdigest(),
        "bundle_manifest_sha256": manifest_hash,
        "both_archives_required": True,
        "archives": saved["archives"],
        "delivery_receipt_sha256": hashlib.sha256(raw_built).hexdigest(),
        "corresponding_source": {
            "member": delivery.SOURCES, "sha256": manifest["files"][delivery.SOURCES],
            "archive": sources_name,
        },
        "sealed_rights_files": sorted(name for name in manifest["files"]
            if name.startswith("notices/") or "LICENSE" in PurePosixPath(name).name
            or "COPYING" in PurePosixPath(name).name),
        "application_wheel": inputs_report["application_wheel"],
        "application_wheel_sha256": inputs_report["application_wheel_sha256"],
        "application_source_sha256": inputs_report["application_source_sha256"],
        "launcher_sha256": inputs_report["launcher_sha256"],
        "native_build_receipt_sha256": inputs_report["native_build_receipt_sha256"],
        "dependency_count": 56,
        "retained_optional_count": 5,
        "wheels": inputs_report["wheels"],
        "dependency_lock_sha256": inputs_report["dependency_lock_sha256"],
        "semantic_parent_lock_sha256": inputs_report["semantic_parent_lock_sha256"],
        "sbom_sha256": inputs_report["sbom_sha256"],
        "dependency_review_sha256": inputs_report["dependency_review_sha256"],
        "source_helpers": source_records or {},
        "no_audio_installation_and_native_library_checks": "PASS",
        "audio_runtime_payload": False,
        "node_payload": False,
        "private_engine_or_user_content": "NOT_INCLUDED",
        "actual_audio_output": "NOT_QUALIFIED",
        "acoustic_listening": "NOT_RUN",
        "private_model_inference": "NOT_RUN",
        "production_issuer": "UNCONFIGURED",
        "authenticode": "NOT_RUN",
        "manual_nvda": "NOT_RUN",
        "qualification": "Assembly and seal checks only. All failed full gates and all remaining hardware, manual and release gates stay required.",
    }
    if source_records is not None:
        require(source_bindings(source_commit, delivery) == source_records,
                "Owner archive helper source changed before the public receipt.")
    # The public directory is created only after all archive and seal checks.
    # It receives JSON receipts, never archives, executable payload or models.
    evidence.mkdir(mode=0o700)
    write_new_json(evidence / "delivery-verification.json", saved)
    write_new_json(evidence / "owner-candidate-assembly.json", report)
    require({path.name for path in evidence.iterdir()} == PUBLIC_FILES,
            "Public owner assembly evidence must contain exactly two JSON receipts.")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--full-outcome", required=True)
    args = parser.parse_args(argv)
    require(__debug__ and sys.flags.isolated and sys.flags.dont_write_bytecode,
            "Owner assembly requires isolated Python with assertions and bytecode writes disabled.")
    require(args.source_commit == os.environ["CARR_CANDIDATE_SOURCE_SHA"]
            and args.full_outcome == os.environ["CARR_COMPLETE_BUNDLE_OUTCOME"] == "failure",
            "Owner assembly must retain the exact event and failed full-gate context.")
    temp = Path(os.environ["RUNNER_TEMP"]).absolute()
    require(args.bundle.absolute() == temp / "carr-bundle-ci/native bundle"
            and args.inputs.absolute() == temp / "carr-owner-candidate-inputs"
            and args.output.absolute() == temp / "carr-owner-candidate-archives"
            and args.evidence.absolute() == ROOT / "ci-results/owner-candidate-assembly",
            "Owner archive assembly paths differ from the closed CI contract.")
    delivery = load_delivery()
    source_records = source_bindings(args.source_commit, delivery)
    require(git("rev-parse", "HEAD^{tree}") == args.source_tree, "Owner assembly tree differs from HEAD.")
    _, inputs_report = validate_inputs(args.inputs, delivery, args.source_commit, args.source_tree, args.full_outcome)
    require(all(inputs_report.get(key) == os.environ[environment] for key, environment in (
        ("repository", "GITHUB_REPOSITORY"), ("run_id", "GITHUB_RUN_ID"),
        ("run_attempt", "GITHUB_RUN_ATTEMPT"))), "Owner input acceptance belongs to another run.")
    report = assemble(args.bundle, args.inputs, args.output, args.evidence,
                      args.source_commit, args.source_tree, full_outcome=args.full_outcome,
                      delivery=delivery, source_records=source_records)
    require(source_bindings(args.source_commit, delivery) == source_records,
            "Owner archive helper source changed during execution.")
    print(json.dumps({key: report[key] for key in (
        "status", "source_commit", "source_tree", "archive_assembly",
        "independent_two_archive_verification", "complete_bundle_outcome",
        "full_qualification", "assembled_owner_bundle", "complete_binary_archive_uploaded",
    )}, ensure_ascii=True, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
