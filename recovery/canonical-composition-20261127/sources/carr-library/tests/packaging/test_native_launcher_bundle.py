"""Native-entry admission and bundle integrity with explicit synthetic fixtures.

No executable fixture is run. Actual Windows process acceptance is performed by
the separate native probes against the privately built, hash-pinned executable.
"""
import ctypes
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import runpy
import subprocess
import tarfile
from types import SimpleNamespace
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "packaging/native-launcher"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def identity():
    return load(NATIVE / "bundle_identity.py", "launcher_identity_test")


@pytest.fixture
def synthetic_launcher(tmp_path, monkeypatch, identity):
    payload = b"Explicitly synthetic test data; this is not a PE executable."
    path = tmp_path / "private launcher" / "ResearchLibrary.exe"
    path.parent.mkdir()
    path.write_bytes(payload)
    monkeypatch.setattr(identity, "LAUNCHER_BYTES", len(payload))
    monkeypatch.setattr(identity, "LAUNCHER_SHA256", hashlib.sha256(payload).hexdigest())
    return path, payload


def test_source_hashes_remain_exactly_the_native_tested_sources(identity):
    for name, expected in identity.SOURCE_SHA256.items():
        assert hashlib.sha256((NATIVE / name).read_bytes()).hexdigest() == expected


def test_snapshot_returns_immutable_bytes_and_explicit_unsigned_scope(identity, synthetic_launcher):
    path, payload = synthetic_launcher
    frozen, evidence = identity.snapshot_launcher(path)
    assert frozen == payload and isinstance(frozen, bytes)
    assert evidence["release_status"] == "unsigned-private-candidate"
    assert evidence["authenticode_signed"] is False
    assert evidence["actual_installed_application_verified"] is False
    assert evidence["windows_process_acceptance"]["synthetic_child"] is True
    evidence["source_sha256"].clear()
    assert len(identity.SOURCE_SHA256) == 5


@pytest.mark.parametrize("damage", ["empty", "short", "large", "changed"])
def test_size_and_independent_checksum_reject_substitution(identity, synthetic_launcher, damage):
    path, payload = synthetic_launcher
    changed = {"empty": b"", "short": payload[:-1], "large": payload + b"!",
               "changed": b"X" + payload[1:]}[damage]
    path.write_bytes(changed)
    with pytest.raises(ValueError, match="bounded size|exact Windows-tested"):
        identity.snapshot_launcher(path)


def test_changed_source_cannot_reuse_an_older_binary_approval(identity, synthetic_launcher, tmp_path, monkeypatch):
    source = tmp_path / "changed sources"
    source.mkdir()
    for name in identity.SOURCE_SHA256:
        shutil.copyfile(NATIVE / name, source / name)
    (source / "launcher.c").write_bytes(b"changed source")
    monkeypatch.setattr(identity, "HERE", source)
    with pytest.raises(ValueError, match="source differs"):
        identity.snapshot_launcher(synthetic_launcher[0])


def test_linked_input_is_not_admitted(identity, synthetic_launcher, tmp_path):
    path, _payload = synthetic_launcher
    link = tmp_path / "linked.exe"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="ordinary local"):
        identity.snapshot_launcher(link)


def test_same_bytes_replaced_between_stat_and_open_are_rejected(identity, synthetic_launcher, monkeypatch):
    path, payload = synthetic_launcher
    original = identity.os.open
    replaced = []

    def change_before_open(candidate, *args, **kwargs):
        if Path(candidate) == path and not replaced:
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(payload)
            replacement.replace(path)
            replaced.append(True)
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(identity.os, "open", change_before_open)
    with pytest.raises(ValueError, match="changed before reading"):
        identity.snapshot_launcher(path)
    assert replaced == [True]


def _wheel(folder, name, requires="", *, application=False):
    path = folder / (name + "-1.0-py3-none-any.whl")
    with ZipFile(path, "w") as archive:
        archive.writestr(name + "-1.0.dist-info/METADATA",
                         "Metadata-Version: 2.3\nName: " + name + "\nVersion: 1.0\n" + requires)
        if application:
            archive.writestr("carr_library/__main__.py", "# synthetic wheel; never executed\n")
    return path


@pytest.fixture
def bundle_inputs(tmp_path, monkeypatch, identity, synthetic_launcher):
    builder = load(ROOT / "packaging/build_windows_bundle.py", "launcher_bundle_builder")
    installer = load(ROOT / "packaging/install_windows.py", "launcher_bundle_installer")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    dependency = _wheel(wheels, "fixture")
    application = _wheel(tmp_path, "universal_research_library", "Requires-Dist: fixture==1.0\n", application=True)
    lock = tmp_path / "requirements.lock"
    lock.write_text("fixture==1.0 --hash=sha256:" + builder.sha256(dependency) + "\n")
    runtime = tmp_path / "synthetic-python.tar.gz"
    with tarfile.open(runtime, "w:gz") as archive:
        payload = b"Synthetic runtime; never executed."
        member = tarfile.TarInfo("python/python.exe")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    expected_runtime = json.loads((ROOT / "packaging/windows-python.json").read_text())["download"]["sha256"]
    actual_sha = builder.sha256
    monkeypatch.setattr(builder, "sha256", lambda path: expected_runtime if Path(path) == runtime else actual_sha(path))
    monkeypatch.setattr(builder, "verify_application_source", lambda path, commit: {"source_commit": commit, "source_tree": "b" * 40, "wheel_sha256": actual_sha(path)})
    # The current builder takes an independently supplied exact digest and
    # inspects real PE metadata. Reuse its inert PE fixture, never bypass the
    # parser or reinterpret the historical Zig receipt as current acceptance.
    fixtures = runpy.run_path(str(ROOT / "tests/packaging/test_windows_bundle.py"))
    executable = fixtures["WindowsBundleTests"].native_launcher(SimpleNamespace(root=tmp_path))
    frozen = executable.read_bytes()
    inputs = dict(wheel=application, wheelhouse=wheels, lock=lock, output=tmp_path / "bundle",
                  python_archive=runtime, source_commit="a" * 40, native_launcher=executable,
                  native_launcher_sha256=hashlib.sha256(frozen).hexdigest())
    return builder, installer, inputs, dependency, frozen


def test_bundle_seals_exact_frozen_launcher_and_rejects_later_tamper(bundle_inputs):
    builder, installer, inputs, _dependency, payload = bundle_inputs
    (inputs["native_launcher"].parent / "windows_child_probe.exe").write_bytes(b"never ship")
    builder.build_bundle(**inputs)
    output = inputs["output"]
    manifest = json.loads((output / "bundle-manifest.json").read_text())
    assert (output / "ResearchLibrary.exe").read_bytes() == payload
    assert manifest["files"]["ResearchLibrary.exe"] == hashlib.sha256(payload).hexdigest()
    from carr_library.native_launcher import verify_payload
    identity = verify_payload(output, manifest)
    assert identity["sha256"] == manifest["files"]["ResearchLibrary.exe"]
    assert identity["identity_scope"] == "source-bound sealed bytes"
    assert identity["authenticode_signed"] is False
    assert identity["historical_native_acceptance"] == "NOT_CLAIMED"
    assert not (output / "windows_child_probe.exe").exists()
    installer.verify(output)
    (output / "ResearchLibrary.exe").write_bytes(b"modified after sealing")
    with pytest.raises(ValueError, match="checksum"):
        installer.verify(output)


def test_bundle_does_not_reopen_mutated_launcher_after_admission(bundle_inputs, monkeypatch):
    builder, installer, inputs, dependency, payload = bundle_inputs
    original = builder.shutil.copyfile
    replaced = []

    def replace_after_admission(source, destination, *args, **kwargs):
        if Path(source) == dependency and not replaced:
            inputs["native_launcher"].write_bytes(b"later unapproved content")
            replaced.append(True)
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(builder.shutil, "copyfile", replace_after_admission)
    builder.build_bundle(**inputs)
    assert replaced == [True]
    assert inputs["native_launcher"].read_bytes() != payload
    assert (inputs["output"] / "ResearchLibrary.exe").read_bytes() == payload
    installer.verify(inputs["output"])


def test_built_native_manifest_is_accepted_by_installed_runtime_checker(bundle_inputs, monkeypatch):
    """The builder and installed checker must agree on optional manifest fields."""
    from carr_library import native_checks, native_launcher
    builder, _installer, inputs, _dependency, payload = bundle_inputs
    builder.build_bundle(**inputs)
    manifest = native_checks._manifest(inputs['output'])
    assert 'native_launcher' not in manifest  # no borrowed historical Windows receipt
    identity = native_launcher.verify_payload(inputs['output'], manifest)
    assert identity['sha256'] == hashlib.sha256(payload).hexdigest()
    assert identity['identity_scope'] == 'source-bound sealed bytes'
    assert identity['status'] == 'PASS'
    assert manifest['source_commit'] == inputs['source_commit']
    (inputs['output'] / 'ResearchLibrary.exe').write_bytes(b'X' + payload[1:])
    with pytest.raises(ValueError, match='checksum'):
        native_launcher.verify_payload(inputs['output'], manifest)


@pytest.mark.parametrize("missing", ["python_archive", "source_commit"])
def test_native_entry_requires_bundled_python_and_source_binding(bundle_inputs, missing):
    builder, _installer, inputs, _dependency, _payload = bundle_inputs
    inputs[missing] = None
    with pytest.raises(ValueError, match="bundled Python and an exact source"):
        builder.build_bundle(**inputs)
    assert not inputs["output"].exists()


class FakeKernel:
    """Controlled API failures prove Python cleanup; this is not Windows evidence."""
    def __init__(self, module, outcome=0, failure=None):
        self.module, self.outcome, self.failure = module, outcome, failure
        self.closed, self.deleted, self.waits = [], 0, 0

    def CreateJobObjectW(self, *_args):
        return 100

    def SetInformationJobObject(self, _job, _kind, data, _length):
        assert ctypes.cast(data, ctypes.POINTER(self.module._ExtendedLimits)).contents.BasicLimitInformation.LimitFlags == 0x2000
        return 1

    def InitializeProcThreadAttributeList(self, pointer, _count, _flags, size):
        ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t)).contents.value = 64
        return int(pointer is not None)

    def UpdateProcThreadAttribute(self, _attributes, _flags, attribute, jobs, *_args):
        assert attribute == 0x2000D and jobs[0] == 100
        return int(self.failure != "attribute")

    def DeleteProcThreadAttributeList(self, _attributes):
        self.deleted += 1

    def CreateProcessW(self, _executable, _command, _process_attributes, _thread_attributes,
                       inherit, flags, _environment, _cwd, startup, result):
        assert inherit is False and flags == 0x08080000
        assert ctypes.cast(startup, ctypes.POINTER(self.module._StartupInfoEx)).contents.lpAttributeList
        if self.failure == "process":
            return 0
        process = ctypes.cast(result, ctypes.POINTER(self.module._ProcessInfo)).contents
        process.hProcess, process.hThread = 101, 102
        return 1

    def WaitForSingleObject(self, _handle, _timeout):
        self.waits += 1
        return self.outcome if self.waits == 1 else 0

    def GetExitCodeProcess(self, _handle, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint32)).contents.value = 17
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


@pytest.mark.parametrize("scenario", ["success", "timeout", "attribute", "process"])
def test_job_handles_are_closed_on_success_timeout_and_creation_failure(tmp_path, monkeypatch, scenario):
    job = load(NATIVE / "windows_job.py", "launcher_job_test")
    fake = FakeKernel(job, 0x102 if scenario == "timeout" else 0,
                      scenario if scenario in ("attribute", "process") else None)
    monkeypatch.setattr(job, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(job, "_kernel", lambda: fake)
    monkeypatch.setattr(ctypes, "WinError", lambda _error: OSError("synthetic API failure"), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    if scenario == "success":
        assert job.run_in_job(tmp_path / "program.exe", [], cwd=tmp_path, timeout=1).returncode == 17
    else:
        error = subprocess.TimeoutExpired if scenario == "timeout" else OSError
        with pytest.raises(error):
            job.run_in_job(tmp_path / "program.exe", [], cwd=tmp_path, timeout=1)
    assert fake.deleted == 1
    assert fake.closed == ([100, 101, 102] if scenario in ("success", "timeout") else [100])
