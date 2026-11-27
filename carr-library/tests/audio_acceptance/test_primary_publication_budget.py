"""Real PR103 destination reserve and recovery, using a private small filesystem."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

if __name__ != "__main__":
    import pytest

# Test helper only: bind the independent decoder to its exact sibling file.
# Do not alter carr_library resolution in an installed-package test.
_oracle_spec = importlib.util.spec_from_file_location(
    "_carr_primary_publication_oracle", Path(__file__).resolve().with_name("oracle.py"))
_oracle = importlib.util.module_from_spec(_oracle_spec)
sys.modules[_oracle_spec.name] = _oracle
_oracle_spec.loader.exec_module(_oracle)
decode_pcm, file_sha256, probe_pcm = _oracle.decode_pcm, _oracle.file_sha256, _oracle.probe_pcm


MIB = 1024**2


def snapshot(directory):
    return {p.relative_to(directory).as_posix(): (file_sha256(p), p.stat().st_mtime_ns)
            for p in directory.rglob("*") if p.is_file()}


def worker(args):
    if args.package_root is None and args.source_root is None:
        raise ValueError("Select the exact package under test.")
    package = (args.package_root.resolve(strict=True) if args.package_root is not None
               else (args.source_root / "src" / "carr_library").resolve(strict=True))
    if args.source_root is not None:
        # Explicit source-only lane. The installed lane supplies no source root
        # and must import its installed package under -I without path injection.
        source = args.source_root.resolve(strict=True)
        assert package == (source / "src" / "carr_library").resolve(strict=True)
        sys.path.insert(0, str(package.parent))
    audio = importlib.import_module("carr_library.audio")
    speech = importlib.import_module("carr_library.speech")
    production = importlib.import_module("carr_library.studio.render")
    assert Path(production.__file__).resolve() == package / "studio/render.py"
    assert Path(audio.__file__).resolve() == package / "audio/__init__.py"
    assert Path(speech.__file__).resolve() == package / "speech/__init__.py"
    names = ("studio/render.py", "audio/dsp.py", "audio/process.py", "audio/models.py", "durable_state.py")
    identity = {name: file_sha256(package / name) for name in names}
    if args.case == "total":
        return total_budget_case(args, audio, speech, production, identity)
    if args.case == "lifecycle":
        return lifecycle_case(args, production, identity)
    if args.case == "reserve-policy":
        return reserve_policy_case(args, audio, speech, production, identity)
    evidence = {"source_sha256": identity, "status": "RUNNING", "statistics_mocked": False,
                "scope": "Actual 30-second FFmpeg silence project; no speech or neural inference claim."}
    with tempfile.TemporaryDirectory(prefix="carr-primary-reserve-", dir=args.work_parent) as temporary:
        root = Path(temporary)
        output = root / "outputs"
        output.mkdir()
        subprocess.run(["mount", "-t", "tmpfs", "-o", "size=8m,mode=700", "none", str(output)],
                       capture_output=True, check=True)
        try:
            budget = audio.AudioBudget(max_output_bytes=4 * MIB, reserve_disk_bytes=4 * MIB, timeout=30)

            def factory(path, **options):
                return audio.AudioRuntime(path, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe, budget=budget, **options)

            def service():
                return production.ProductionService(root / "state", voices=speech.VoiceRegistry([]), runtime_factory=factory)

            engine = service()
            plan = engine.create("[Scene: Actual disk reserve]\n[Pause: 30]", mode="authored")
            plan = replace(plan, sample_rate=16000, revision=plan.revision + 1)
            job = engine.submit(plan, output, stems=True)
            try:
                engine.run(job)
            except audio.AudioError as error:
                evidence["refusal"] = str(error)
            else:
                evidence.update(status="FAIL", actual_free=shutil.disk_usage(output).free,
                                configured_reserve=budget.reserve_disk_bytes,
                                result="Production ignored the configured reserve on its output filesystem.")
                raise AssertionError(evidence["result"])
            state = engine.get(job)
            assert state["status"] == "failed"
            assert shutil.disk_usage(output).free >= budget.reserve_disk_bytes
            assert not list(output.rglob(".pending-*"))
            accepted = snapshot(output)
            cached = snapshot(engine.cache)
            assert sum(name.endswith(".wav") for name in accepted) >= 1
            assert cached
            evidence.update(free_after_refusal=shutil.disk_usage(output).free,
                            configured_reserve=budget.reserve_disk_bytes, accepted_files=len(accepted),
                            accepted_cache_files=len(cached))
            subprocess.run(["mount", "-t", "tmpfs", "-o", "remount,size=16m", "none", str(output)], check=True, capture_output=True)
            resumed = service().run(job)
            assert resumed["status"] == "complete"
            assert all(node["reused"] for node in resumed["nodes"].values())
            assert snapshot(engine.cache) == cached
            assert all(snapshot(output).get(name) == value for name, value in accepted.items())
            assert shutil.disk_usage(output).free >= budget.reserve_disk_bytes
            master = Path(resumed["destination"]) / "master.wav"
            probe_pcm(master, args.ffprobe, rate=16000, frames=480000)
            decoded = decode_pcm(master, args.ffmpeg, expected_frames=480000)
            expected = hashlib.sha256()
            for _ in range(30):
                expected.update(bytes(16000 * 4))
            assert decoded.sha256 == expected.hexdigest()
            complete = snapshot(output)
            # Verified completed results need no new audio allocation. A real
            # smaller filesystem must not cause false disk-space refusal.
            subprocess.run(["mount", "-t", "tmpfs", "-o", "remount,size=7m", "none", str(output)], check=True, capture_output=True)
            assert shutil.disk_usage(output).free < budget.reserve_disk_bytes
            reopened = service().run(job)
            assert reopened == resumed and snapshot(output) == complete
            assert {name: file_sha256(package / name) for name in names} == identity
            evidence.update(status="PASS", frames=decoded.frames, pcm_sha256=decoded.sha256,
                            resumed_node_count=len(resumed["nodes"]),
                            free_before_completed_reopen=shutil.disk_usage(output).free,
                            complete_files_unchanged=True)
        finally:
            args.evidence.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
            subprocess.run(["umount", str(output)], check=True, capture_output=True)


def total_budget_case(args, audio, speech, production, identity):
    with tempfile.TemporaryDirectory(prefix="carr-primary-total-", dir=args.work_parent) as temporary:
        root = Path(temporary)
        outputs = root / "outputs"
        outputs.mkdir()
        budget = audio.AudioBudget(max_output_bytes=4 * MIB, reserve_disk_bytes=0, timeout=30)

        def factory(path, **options):
            return audio.AudioRuntime(path, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe, budget=budget, **options)

        def service(limit):
            return production.ProductionService(root / "state", voices=speech.VoiceRegistry([]),
                                                runtime_factory=factory, total_disk_bytes=limit)

        engine = service(4 * MIB)
        plan = engine.create("[Scene: Total retained budget]\n[Pause: 30]", mode="authored")
        plan = replace(plan, sample_rate=16000, revision=plan.revision + 1)
        job = engine.submit(plan, outputs, stems=True, format="flac")
        try:
            engine.run(job)
        except audio.AudioError as error:
            assert "budget" in str(error)
        else:
            raise AssertionError("The complete cache/staging/publication budget was not enforced.")
        failed = engine.get(job)
        assert failed["status"] == "failed" and failed["nodes"]
        used = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
        assert used <= 4 * MIB
        cached = snapshot(engine.cache)
        assert cached
        complete = service(32 * MIB).run(job)
        assert complete["status"] == "complete" and complete["total_disk_bytes"] == 32 * MIB
        assert all(snapshot(engine.cache).get(name) == value for name, value in cached.items())
        assert all(complete["nodes"][key]["reused"] for key in failed["nodes"])
        total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
        assert 4 * MIB < total <= 32 * MIB
        publication = Path(complete["destination"])
        before = snapshot(publication)
        master = publication / "master.wav"
        probe_pcm(master, args.ffprobe, rate=16000, frames=480000)
        decoded = decode_pcm(master, args.ffmpeg, expected_frames=480000)
        assert decode_pcm(publication / "audiobook.flac", args.ffmpeg, expected_frames=480000,
                          expected_sha256=decoded.sha256).frames == decoded.frames
        # A read-only completed verification has no incremental audio cost.
        assert service(MIB).run(job) == complete
        try:
            service(MIB).run(job, cancelled=lambda: True)
        except InterruptedError:
            pass
        else:
            raise AssertionError("Cancellation before completed verification was ignored.")
        polls = 0

        def interrupt_hash():
            nonlocal polls
            polls += 1
            return polls >= 3

        try:
            service(MIB).run(job, cancelled=interrupt_hash)
        except InterruptedError:
            pass
        else:
            raise AssertionError("Cancellation while hashing the actual completed master was ignored.")
        assert polls == 3 and snapshot(publication) == before
        assert service(MIB).get(job)["status"] == "complete"
        args.evidence.write_text(json.dumps({"status": "PASS", "source_sha256": identity,
            "scope": "Actual 30-second FFmpeg PCM; no speech/model claim.",
            "small_total_budget": 4 * MIB, "retained_after_refusal": used,
            "increased_total_budget": 32 * MIB, "retained_after_completion": total,
            "per_operation_budget_unchanged": budget.max_output_bytes,
            "verified_existing_nodes_reused": len(failed["nodes"]), "decoded_frames": decoded.frames,
            "completed_reopen_with_smaller_budget": "PASS", "cancel_before_verify": "PASS",
            "cancel_during_actual_hash": "PASS", "publication_unchanged": True}, indent=2), encoding="utf-8")



def reserve_policy_case(args, audio, speech, production, identity):
    """Portable real-I/O refusal and resume; no fabricated free-space counter."""
    with tempfile.TemporaryDirectory(prefix="carr-primary-policy-", dir=args.work_parent) as temporary:
        root = Path(temporary)
        outputs = root / "outputs"
        outputs.mkdir()
        base_budget = audio.AudioBudget(max_output_bytes=4 * MIB, reserve_disk_bytes=0, timeout=30)

        def service(reserve=0):
            def factory(path, **options):
                return audio.AudioRuntime(path, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe,
                    budget=replace(base_budget, reserve_disk_bytes=reserve), **options)
            return production.ProductionService(root / "state", voices=speech.VoiceRegistry([]),
                                                runtime_factory=factory, total_disk_bytes=32 * MIB)

        first = service()
        plan = first.create("[Scene: Destination reserve admission]\n[Pause: 30]", mode="authored")
        plan = replace(plan, sample_rate=16000, revision=plan.revision + 1)
        first_job = first.submit(plan, outputs, stems=True)
        first_result = first.run(first_job)
        assert first_result["status"] == "complete"
        accepted = snapshot(Path(first_result["destination"]))
        cached = snapshot(first.cache)
        assert cached and accepted

        # Reuse actual accepted cache under a stricter explicit reserve. All
        # physical statistics stay real. Requiring the entire volume capacity
        # is deliberately unattainable while these owned files exist.
        actual_space = shutil.disk_usage(outputs)
        stricter = service(actual_space.total)
        job = stricter.submit(plan, outputs, stems=True)
        try:
            stricter.run(job)
        except audio.AudioError as error:
            refusal = str(error)
            assert "output disk space" in refusal
        else:
            raise AssertionError("Publication ignored the actual destination reserve.")
        failed = stricter.get(job)
        destination = Path(failed["destination"])
        assert failed["status"] == "failed" and failed["nodes"]
        assert all(row["reused"] for row in failed["nodes"].values())
        assert not (destination / "master.wav").exists()
        assert not list(destination.rglob(".pending-*"))
        assert snapshot(first.cache) == cached
        assert snapshot(Path(first_result["destination"])) == accepted

        resumed = service().run(job)
        assert resumed["status"] == "complete"
        assert all(row["reused"] for row in resumed["nodes"].values())
        assert snapshot(first.cache) == cached
        assert snapshot(Path(first_result["destination"])) == accepted
        master = destination / "master.wav"
        probe_pcm(master, args.ffprobe, rate=16000, frames=480000)
        decoded = decode_pcm(master, args.ffmpeg, expected_frames=480000)
        assert decoded.nonzero_bytes == 0
        args.evidence.write_text(json.dumps({
            "status": "PASS", "source_sha256": identity, "statistics_mocked": False,
            "scope": "Actual 30-second PCM and explicit unattainable reserve; no low-disk or acoustic-quality claim.",
            "observed_volume_total": actual_space.total, "observed_volume_free": actual_space.free,
            "requested_reserve": actual_space.total, "refusal": refusal,
            "retained_cache_unchanged": True, "earlier_publication_unchanged": True,
            "resumed_node_count": len(resumed["nodes"]), "decoded_frames": decoded.frames,
            "pcm_sha256": decoded.sha256}, indent=2), encoding="utf-8")


def lifecycle_case(args, production, identity):
    class Registry:
        def __init__(self):
            self.closes = 0

        def close(self):
            self.closes += 1

    with tempfile.TemporaryDirectory(prefix="carr-primary-close-", dir=args.work_parent) as temporary:
        root = Path(temporary)
        owned = Registry()
        service = production.ProductionService(root / "owned")
        service._voices = owned  # Inject the result of lazy owned discovery.
        service.close()
        service.close()
        assert owned.closes == 1
        for action in (lambda: service.voices, lambda: service.run("unused"), lambda: service.create("text")):
            try:
                action()
            except RuntimeError as error:
                assert "closed" in str(error)
            else:
                raise AssertionError("A closed service was usable again.")
        external = Registry()
        borrowed = production.ProductionService(root / "borrowed", voices=external)
        borrowed.close()
        borrowed.close()
        assert external.closes == 0
        unopened = production.ProductionService(root / "unopened")
        unopened.close()
        assert unopened._voices is None
        args.evidence.write_text(json.dumps({"status": "PASS", "source_sha256": identity,
            "scope": "Registry ownership/lifecycle unit test with injected resources, not synthesis.",
            "owned_registry_closed_once": True, "external_registry_untouched": True,
            "unopened_registry_not_created": True, "later_use_rejected": True}, indent=2), encoding="utf-8")


def package_arguments():
    import carr_library
    package = Path(carr_library.__file__).resolve().parent
    checkout = Path(__file__).resolve().parents[2]
    source = checkout if (checkout / "src" / "carr_library").resolve() == package else None
    declared = os.environ.get("CARR_PRIMARY_STUDIO_SOURCE")
    if declared and (source is None or Path(declared).resolve() != source):
        raise AssertionError("The selected source differs from the actual imported package.")
    arguments = ["--package-root", str(package)]
    if source is not None:
        arguments += ["--source-root", str(source)]
    return arguments


def run_plain_case(tmp_path, request, case):
    ffmpeg, ffprobe = os.environ.get("CARR_FFMPEG") or shutil.which("ffmpeg"), os.environ.get("CARR_FFPROBE") or shutil.which("ffprobe")
    assert ffmpeg and ffprobe, "This required audio gate needs actual FFmpeg and ffprobe."
    evidence = tmp_path / (case + "-evidence.json")
    command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--case", case,
               *package_arguments(), "--work-parent", str(tmp_path), "--evidence", str(evidence),
               "--ffmpeg", ffmpeg, "--ffprobe", ffprobe]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    if evidence.is_file():
        request.node.user_properties.append(("actual_primary_" + case + "_evidence", evidence.read_text(encoding="utf-8")))
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(evidence.read_text(encoding="utf-8"))["status"] == "PASS"


def test_actual_total_budget_retains_verified_nodes_and_completed_verification_is_cancellable(tmp_path, request):
    run_plain_case(tmp_path, request, "total")


def test_service_closes_only_owned_registry_once_and_rejects_reuse(tmp_path, request):
    run_plain_case(tmp_path, request, "lifecycle")


def test_actual_destination_reserve_refuses_cached_copy_and_resumes_without_rewriting(tmp_path, request):
    run_plain_case(tmp_path, request, "reserve-policy")



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, help="Explicit checkout source override; omitted for installed qualification.")
    parser.add_argument("--package-root", type=Path, help="Exact current imported package identity.")
    parser.add_argument("--work-parent", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument("--case", choices=("reserve", "total", "lifecycle", "reserve-policy"), default="reserve")
    worker(parser.parse_args())
