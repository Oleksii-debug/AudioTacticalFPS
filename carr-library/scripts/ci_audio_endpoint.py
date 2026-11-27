"""One-off hosted-CI endpoint evaluation; never an application installer.

Run only from the exact experiment branch. No vendor executable, trust-store
change, signing-policy change, forced update, restart, or uninstall is used.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile

BRANCH = "carr/ci-endpoint-evaluation-20261127"
REPOSITORY = "Oleksii-debug/AudioTacticalFPS"
URL = "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack46.zip"
ARCHIVE_SIZE = 1324680
ARCHIVE_SHA256 = "bc44e8974191a5a2427d6c979e2dab08dbb09925f3f435dfa2f0ba3e45b6eb90"
HWID = "VBAudioVACWDM"
CLASS_GUID = "4d36e96c-e325-11ce-bfc1-08002be10318"
INF_NAME = "vbMmeCable64_win10.inf"
SYS_NAME = "vbaudio_cable64_win10.sys"
ARM_SYS_NAME = "vbaudio_cable64arm_win10.sys"
CAT_NAME = "vbaudio_cable64_win10.cat"
STAGED = (INF_NAME, SYS_NAME, ARM_SYS_NAME, CAT_NAME)
SIGNED = (SYS_NAME, ARM_SYS_NAME, CAT_NAME)
FORMAT = {"sample_rate": 16000, "bits": 16, "channels": 1,
          "block_align": 2, "average_bytes_per_second": 32000}
INSTALLFLAG_NONINTERACTIVE = 0x00000004
DIF_REGISTERDEVICE = 0x00000019
DI_NEEDRESTART = 0x00000080
DI_NEEDREBOOT = 0x00000100
MAX_ARCHIVE_MEMBERS = 64
MAX_UNCOMPRESSED = 8 * 1024 * 1024


class EvaluationError(RuntimeError):
    def __init__(self, message, *, operation=None, code=None):
        super().__init__(message)
        self.operation = operation
        self.code = code


class Receipt:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.terminal = False
        self.value = {
            "schema": "carr-ci-endpoint-evaluation/1", "status": "RUNNING",
            "phase": "starting", "evaluation_only": True,
            "vendor_setup_exe_executed": False, "trust_store_changed": False,
            "security_policy_changed": False, "service_startup_policy_changed": False,
            "device_state": "not_attempted", "package_state": "not_attempted",
            "handles_closed": True, "input_handles_closed": True,
            "uninstall_attempted": False, "reboot_attempted": False,
            "format": FORMAT, "events": [],
        }
        self._write()

    def _write(self):
        encoded = (json.dumps(self.value, ensure_ascii=True, indent=2,
                              allow_nan=False) + "\n").encode("utf-8")
        temporary = self.path.with_name(self.path.name + ".tmp")
        with temporary.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def update(self, phase=None, **fields):
        with self.lock:
            if self.terminal:
                return
            if phase:
                self.value["phase"] = phase
                self.value["events"].append({"phase": phase})
            self.value.update(fields)
            self._write()

    def finish(self, status, **fields):
        with self.lock:
            if self.terminal:
                return False
            self.value.update(fields)
            self.value["status"] = status
            self.terminal = True
            self._write()
            return True

    def timeout(self):
        if self.finish(
            "FAILED", phase="deadline",
            error={"type": "TimeoutError",
                   "message": "Evaluation exceeded 180 seconds; retained device/package state may be partial."},
        ):
            os._exit(124)


def check_environment(environ=None, *, os_name=None, system=None, machine=None,
                      pointer_bytes=None):
    env = os.environ if environ is None else environ
    required = {
        "GITHUB_ACTIONS": "true", "RUNNER_OS": "Windows",
        "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_ARCH": "X64",
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REF": "refs/heads/" + BRANCH, "GITHUB_REF_NAME": BRANCH,
    }
    for key, expected in required.items():
        if env.get(key) != expected:
            raise EvaluationError("One-off evaluation environment guard rejected " + key)
    if env.get("GITHUB_EVENT_NAME") != "push":
        raise EvaluationError("Only the exact one-off branch push is allowed")
    if (os.name if os_name is None else os_name) != "nt":
        raise EvaluationError("Windows is required")
    if (platform.system() if system is None else system) != "Windows":
        raise EvaluationError("Windows platform identity is required")
    arch = platform.machine() if machine is None else machine
    if arch.casefold() not in {"amd64", "x86_64"}:
        raise EvaluationError("Native x64 Windows is required")
    if (struct.calcsize("P") if pointer_bytes is None else pointer_bytes) != 8:
        raise EvaluationError("WOW64/32-bit helper is prohibited")


def _unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise EvaluationError("Duplicate JSON key")
        result[key] = value
    return result


def load_pins():
    path = Path(__file__).with_name("ci_audio_endpoint_pins.json")
    raw = path.read_bytes()
    if len(raw) > 32768:
        raise EvaluationError("Pins document exceeds its bound")
    pins = json.loads(raw, object_pairs_hook=_unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(
                          EvaluationError("Nonfinite JSON value")))
    if (pins.get("schema") != "carr-ci-vbcable-pins/1"
            or pins.get("url") != URL
            or type(pins.get("archive_size")) is not int
            or pins["archive_size"] != ARCHIVE_SIZE
            or pins.get("archive_sha256") != ARCHIVE_SHA256
            or set(pins.get("stage_files", {})) != set(STAGED)):
        raise EvaluationError("Unexpected reviewed pins contract")
    for name, item in pins["stage_files"].items():
        if (set(item) != {"size", "sha256"} or type(item["size"]) is not int
                or not 0 < item["size"] < 1024 * 1024
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise EvaluationError("Invalid reviewed file pin: " + name)
    signers = pins.get("signers")
    expected_names = {
        SYS_NAME: {"BUREL VINCENT Entrepreneur individuel",
                   "Microsoft Windows Hardware Compatibility Publisher"},
        ARM_SYS_NAME: {"BUREL VINCENT Entrepreneur individuel",
                       "Microsoft Windows Hardware Compatibility Publisher"},
        CAT_NAME: {"Microsoft Windows Hardware Compatibility Publisher"},
    }
    if not isinstance(signers, dict) or set(signers) != set(expected_names):
        raise EvaluationError("Invalid reviewed signer targets")
    for name, allowed in signers.items():
        if (not isinstance(allowed, dict) or len(allowed) != len(expected_names[name])
                or any(not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key)
                       or not isinstance(value, str) for key, value in allowed.items())
                or set(allowed.values()) != expected_names[name]):
            raise EvaluationError("Invalid reviewed signer certificate pin: " + name)
    return pins, hashlib.sha256(raw).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EvaluationError("Official archive redirect is not allowed")


def download_archive(target):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    deadline = time.monotonic() + 60
    digest = hashlib.sha256()
    count = 0
    with opener.open(URL, timeout=15) as response, Path(target).open("xb") as stream:
        if response.status != 200 or response.geturl() != URL:
            raise EvaluationError("Unexpected official archive response")
        length = response.headers.get("Content-Length")
        if length is not None and length != str(ARCHIVE_SIZE):
            raise EvaluationError("Official archive Content-Length changed")
        while True:
            if time.monotonic() >= deadline:
                raise EvaluationError("Official archive download deadline exceeded")
            block = response.read(min(65536, ARCHIVE_SIZE + 1 - count))
            if not block:
                break
            count += len(block)
            if count > ARCHIVE_SIZE:
                raise EvaluationError("Official archive exceeds its exact bound")
            digest.update(block)
            stream.write(block)
        stream.flush()
        os.fsync(stream.fileno())
    if count != ARCHIVE_SIZE or digest.hexdigest() != ARCHIVE_SHA256:
        raise EvaluationError("Official archive size or SHA-256 mismatch")


def stage_archive(archive, directory, pins):
    archive = Path(archive)
    if archive.stat().st_size != pins["archive_size"]:
        raise EvaluationError("Archive size mismatch before ZIP parsing")
    data = archive.read_bytes()
    if hashlib.sha256(data).hexdigest() != pins["archive_sha256"]:
        raise EvaluationError("Archive SHA-256 mismatch before ZIP parsing")
    directory = Path(directory)
    directory.mkdir()
    with zipfile.ZipFile(archive) as package:
        infos = package.infolist()
        if not 0 < len(infos) <= MAX_ARCHIVE_MEMBERS:
            raise EvaluationError("Archive member count exceeds its bound")
        expected = pins["archive_members"]
        seen = set()
        total = 0
        for info in infos:
            name = info.filename
            folded = name.casefold()
            if (info.orig_filename != name or info.is_dir()
                    or name not in expected or "/" in name or "\\" in name
                    or ":" in name or "\x00" in name or folded in seen
                    or info.flag_bits & 1
                    or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or ((info.external_attr >> 16) & 0o170000) == 0o120000
                    or info.file_size != expected[name]):
                raise EvaluationError("Unexpected or unsafe archive member")
            seen.add(folded)
            total += info.file_size
        if total > MAX_UNCOMPRESSED or len(infos) != len(expected):
            raise EvaluationError("Archive inventory or total-size mismatch")
        # Original license stays unmodified in the archive; retain its identity.
        notice = package.read("readme.txt")
        if hashlib.sha256(notice).hexdigest() != pins["notice_sha256"]:
            raise EvaluationError("Original vendor notice changed")
        for name in STAGED:
            content = package.read(name)
            item = pins["stage_files"][name]
            if len(content) != item["size"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
                raise EvaluationError("Pinned driver member mismatch: " + name)
            with (directory / name).open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
    return {name: directory / name for name in STAGED}


def verify_staged(paths, pins):
    for name in STAGED:
        data = paths[name].read_bytes()
        item = pins["stage_files"][name]
        if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise EvaluationError("Staged input changed: " + name)


def validate_signature_reports(reports, pins):
    if not isinstance(reports, list) or len(reports) != len(SIGNED):
        raise EvaluationError("Incomplete Windows signature verification")
    by_name = {}
    for item in reports:
        if not isinstance(item, dict) or item.get("file") in by_name:
            raise EvaluationError("Invalid signature report")
        by_name[item.get("file")] = item
    if set(by_name) != set(SIGNED):
        raise EvaluationError("Unexpected signature report targets")
    for name, report in by_name.items():
        allowed = pins["signers"][name]
        fingerprint = report.get("certificate_sha256")
        if (report.get("status") != "Valid" or not isinstance(fingerprint, str)
                or fingerprint.casefold() not in allowed
                or report.get("simple_name") != allowed[fingerprint.casefold()]):
            raise EvaluationError("Windows signature/publisher verification failed: " + name)
    return reports


def verify_signatures(paths, pins, native, work):
    request = Path(work) / "signature-input.json"
    request.write_text(json.dumps(
        [{"file": name, "path": str(paths[name])} for name in SIGNED]
    ), encoding="utf-8")
    script = Path(__file__).with_name("ci_audio_endpoint_authenticode.ps1")
    powershell = native.system_directory / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    # A pwsh -> Python -> Windows PowerShell launch otherwise inherits PS7
    # modules that Windows PowerShell cannot load. Reconstruct its own normal
    # module path, as documented by Microsoft; change no machine policy.
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() != "PSMODULEPATH"}
    result = subprocess.run(
        [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive",
         "-File", str(script), "-InputPath", str(request)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=45, check=False, env=environment,
    )
    if len(result.stdout) > 65536 or len(result.stderr) > 65536:
        raise EvaluationError("Windows signature verifier exceeded output bound")
    if result.returncode:
        raise EvaluationError("Windows signature verifier failed: "
                              + result.stderr.decode("utf-8", "replace")[:2000])
    reports = json.loads(result.stdout.decode("utf-8-sig"),
                         object_pairs_hook=_unique_object)
    return validate_signature_reports(reports, pins)


DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p


class GUID(ctypes.Structure):
    _fields_ = [("Data1", DWORD), ("Data2", WORD), ("Data3", WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("ClassGuid", GUID), ("DevInst", DWORD),
                ("Reserved", ctypes.c_size_t)]

    def __init__(self):
        super().__init__()
        self.cbSize = ctypes.sizeof(self)


class SP_DEVINSTALL_PARAMS_W(ctypes.Structure):
    _fields_ = [
        ("cbSize", DWORD), ("Flags", DWORD), ("FlagsEx", DWORD),
        ("hwndParent", HANDLE), ("InstallMsgHandler", HANDLE),
        ("InstallMsgHandlerContext", HANDLE), ("FileQueue", HANDLE),
        ("ClassInstallReserved", ctypes.c_size_t), ("Reserved", DWORD),
        ("DriverPath", ctypes.c_wchar * 260),
    ]

    def __init__(self):
        super().__init__()
        self.cbSize = ctypes.sizeof(self)


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", WORD), ("nChannels", WORD),
                ("nSamplesPerSec", DWORD), ("nAvgBytesPerSec", DWORD),
                ("nBlockAlign", WORD), ("wBitsPerSample", WORD), ("cbSize", WORD)]


class WAVEOUTCAPSW(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wMid", WORD), ("wPid", WORD), ("vDriverVersion", DWORD),
                ("szPname", ctypes.c_wchar * 32), ("dwFormats", DWORD),
                ("wChannels", WORD), ("wReserved1", WORD), ("dwSupport", DWORD)]


class NativeWindows:
    def __init__(self):
        if os.name != "nt":
            raise EvaluationError("Windows native calls are unavailable")
        def dll(name):
            return ctypes.WinDLL(name, use_last_error=True, winmode=0x00000800)
        self.setup = dll("setupapi.dll")
        self.newdev = dll("newdev.dll")
        self.cfg = dll("cfgmgr32.dll")
        self.winmm = dll("winmm.dll")
        self.kernel = dll("kernel32.dll")
        self.shell = dll("shell32.dll")
        self.input_handles = []
        self._bind()
        if not self.shell.IsUserAnAdmin():
            raise EvaluationError("An already elevated runner is required; no elevation is attempted")
        text = ctypes.create_unicode_buffer(32768)
        n = self.kernel.GetSystemDirectoryW(text, len(text))
        if not 0 < n < len(text):
            self._error("GetSystemDirectoryW")
        self.system_directory = Path(text.value)
        self.class_guid = GUID.from_buffer_copy(uuid.UUID(CLASS_GUID).bytes_le)

    def _bind(self):
        def bind(dll, name, result, *args):
            function = getattr(dll, name)
            function.restype = result
            function.argtypes = list(args)
        pinfo = ctypes.POINTER(SP_DEVINFO_DATA)
        pguid = ctypes.POINTER(GUID)
        pword = ctypes.POINTER(DWORD)
        string = ctypes.c_wchar_p
        bind(self.shell, "IsUserAnAdmin", BOOL)
        bind(self.kernel, "GetSystemDirectoryW", DWORD, string, DWORD)
        bind(self.kernel, "CreateFileW", HANDLE, string, DWORD, DWORD,
             HANDLE, DWORD, DWORD, HANDLE)
        bind(self.kernel, "CloseHandle", BOOL, HANDLE)
        bind(self.setup, "SetupDiGetClassDevsW", HANDLE, pguid, string, HANDLE, DWORD)
        bind(self.setup, "SetupDiEnumDeviceInfo", BOOL, HANDLE, DWORD, pinfo)
        bind(self.setup, "SetupDiDestroyDeviceInfoList", BOOL, HANDLE)
        bind(self.setup, "SetupDiGetDeviceRegistryPropertyW", BOOL, HANDLE, pinfo,
             DWORD, pword, ctypes.c_void_p, DWORD, pword)
        bind(self.setup, "SetupDiGetDeviceInstanceIdW", BOOL, HANDLE, pinfo,
             string, DWORD, pword)
        bind(self.setup, "SetupDiGetINFClassW", BOOL, string, pguid, string, DWORD, pword)
        bind(self.setup, "SetupDiCreateDeviceInfoList", HANDLE, pguid, HANDLE)
        bind(self.setup, "SetupDiCreateDeviceInfoW", BOOL, HANDLE, string, pguid,
             string, HANDLE, DWORD, pinfo)
        bind(self.setup, "SetupDiSetDeviceRegistryPropertyW", BOOL, HANDLE, pinfo,
             DWORD, ctypes.c_void_p, DWORD)
        bind(self.setup, "SetupDiCallClassInstaller", BOOL, DWORD, HANDLE, pinfo)
        bind(self.setup, "SetupDiGetDeviceInstallParamsW", BOOL, HANDLE, pinfo,
             ctypes.POINTER(SP_DEVINSTALL_PARAMS_W))
        bind(self.setup, "SetupSetNonInteractiveMode", BOOL, BOOL)
        bind(self.newdev, "UpdateDriverForPlugAndPlayDevicesW", BOOL, HANDLE,
             string, string, DWORD, ctypes.POINTER(BOOL))
        bind(self.cfg, "CM_Get_DevNode_Status", DWORD, pword, pword, DWORD, DWORD)
        bind(self.winmm, "waveOutGetNumDevs", DWORD)
        bind(self.winmm, "waveOutGetDevCapsW", DWORD, ctypes.c_size_t,
             ctypes.POINTER(WAVEOUTCAPSW), DWORD)
        bind(self.winmm, "waveOutOpen", DWORD, ctypes.POINTER(HANDLE), DWORD,
             ctypes.POINTER(WAVEFORMATEX), ctypes.c_size_t, ctypes.c_size_t, DWORD)

    @staticmethod
    def _error(operation):
        code = ctypes.get_last_error()
        raise EvaluationError(operation + " failed: " + ctypes.FormatError(code).strip(),
                              operation=operation, code=code)

    def _handle(self, value, operation):
        if value in (None, ctypes.c_void_p(-1).value):
            self._error(operation)
        return value

    def _destroy(self, handle):
        if not self.setup.SetupDiDestroyDeviceInfoList(handle):
            self._error("SetupDiDestroyDeviceInfoList")

    def _property(self, handle, info, number, *, optional=False):
        buffer = (ctypes.c_ubyte * 65536)()
        kind, required = DWORD(), DWORD()
        if not self.setup.SetupDiGetDeviceRegistryPropertyW(
            handle, ctypes.byref(info), number, ctypes.byref(kind), buffer,
            len(buffer), ctypes.byref(required),
        ):
            code = ctypes.get_last_error()
            if optional and code in (13, 1168):
                return None
            self._error("SetupDiGetDeviceRegistryPropertyW")
        if kind.value not in (1, 7) or required.value > len(buffer) or required.value % 2:
            raise EvaluationError("Unexpected device property type/size")
        return bytes(buffer[:required.value]).decode("utf-16le").rstrip("\x00")

    def _instance(self, handle, info):
        buffer = ctypes.create_unicode_buffer(512)
        needed = DWORD()
        if not self.setup.SetupDiGetDeviceInstanceIdW(
            handle, ctypes.byref(info), buffer, len(buffer), ctypes.byref(needed)
        ):
            self._error("SetupDiGetDeviceInstanceIdW")
        return buffer.value

    def matching_devices(self):
        handle = self._handle(
            self.setup.SetupDiGetClassDevsW(None, None, None, 4),
            "SetupDiGetClassDevsW")
        found = []
        try:
            for index in range(10000):
                info = SP_DEVINFO_DATA()
                if not self.setup.SetupDiEnumDeviceInfo(handle, index, ctypes.byref(info)):
                    if ctypes.get_last_error() == 259:
                        return found
                    self._error("SetupDiEnumDeviceInfo")
                identifiers = self._property(handle, info, 1, optional=True)
                if identifiers and HWID.casefold() in [
                    item.casefold() for item in identifiers.split("\x00")
                ]:
                    found.append(self._instance(handle, info))
            raise EvaluationError("Device enumeration bound exceeded")
        finally:
            self._destroy(handle)

    def lock_inputs(self, paths):
        try:
            for name in STAGED:
                # Read sharing only: keep the exact staged bytes immutable while
                # Authenticode and SetupAPI read/copy them.
                value = self.kernel.CreateFileW(
                    str(paths[name]), 0x80000000, 1, None, 3, 0x80, None)
                self.input_handles.append(self._handle(value, "CreateFileW(input lock)"))
        except BaseException:
            self.close_inputs()
            raise

    def close_inputs(self):
        failed = False
        while self.input_handles:
            if not self.kernel.CloseHandle(self.input_handles.pop()):
                failed = True
        if failed:
            self._error("CloseHandle(input lock)")

    def wave_devices(self):
        count = self.winmm.waveOutGetNumDevs()
        if count > 64:
            raise EvaluationError("Unexpected WinMM device count")
        devices = []
        for index in range(count):
            caps = WAVEOUTCAPSW()
            code = self.winmm.waveOutGetDevCapsW(index, ctypes.byref(caps), ctypes.sizeof(caps))
            if code:
                raise EvaluationError("waveOutGetDevCapsW failed", operation="waveOutGetDevCapsW",
                                      code=int(code))
            devices.append({"id": index, "name": caps.szPname, "channels": int(caps.wChannels)})
        return devices

    def query_format(self, device_id):
        form = WAVEFORMATEX(1, 1, 16000, 32000, 2, 16, 0)
        return int(self.winmm.waveOutOpen(
            None, device_id, ctypes.byref(form), 0, 0, 0x00000001))

    def _restart_flags(self, handle, info):
        params = SP_DEVINSTALL_PARAMS_W()
        if not self.setup.SetupDiGetDeviceInstallParamsW(
            handle, ctypes.byref(info), ctypes.byref(params)
        ):
            self._error("SetupDiGetDeviceInstallParamsW")
        return int(params.Flags & (DI_NEEDRESTART | DI_NEEDREBOOT))

    def _installed_identity(self, handle, info, pins):
        import winreg
        if self._property(handle, info, 4) != "VBAudioVACMME":
            raise EvaluationError("Installed service differs from reviewed INF")
        driver_key = self._property(handle, info, 9)
        if not re.fullmatch(r"\{" + re.escape(CLASS_GUID) + r"\}\\[0-9]{4}",
                            driver_key, flags=re.IGNORECASE):
            raise EvaluationError("Unexpected installed driver registry key")
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Class" + "\\" + driver_key) as key:
            inf_name = winreg.QueryValueEx(key, "InfPath")[0]
            version = winreg.QueryValueEx(key, "DriverVersion")[0]
            provider = winreg.QueryValueEx(key, "ProviderName")[0]
        if (not isinstance(inf_name, str) or not re.fullmatch(r"oem[0-9]+\.inf", inf_name, re.I)
                or version != "3.5.2.0" or provider != "VB-Audio Software"):
            raise EvaluationError("Installed INF/version/provider is not the reviewed driver")
        installed_inf = self.system_directory.parent / "INF" / inf_name
        with installed_inf.open("rb") as stream:
            content = stream.read(pins["stage_files"][INF_NAME]["size"] + 1)
        if hashlib.sha256(content).hexdigest() != pins["stage_files"][INF_NAME]["sha256"]:
            raise EvaluationError("Installed INF SHA-256 differs from reviewed bytes")
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Services\VBAudioVACMME") as key:
            image = winreg.QueryValueEx(key, "ImagePath")[0]
            start = winreg.QueryValueEx(key, "Start")[0]
            service_type = winreg.QueryValueEx(key, "Type")[0]
        if not isinstance(image, str) or start != 3 or service_type != 1:
            raise EvaluationError("Installed service differs from its normal INF contract")
        for prefix in ("\\SystemRoot\\", "%SystemRoot%\\"):
            if image.casefold().startswith(prefix.casefold()):
                image = str(self.system_directory.parent / image[len(prefix):])
                break
        installed_sys = Path(image).resolve(strict=True)
        store = (self.system_directory / "DriverStore" / "FileRepository").resolve(strict=True)
        if (not installed_sys.is_relative_to(store)
                or installed_sys.name.casefold() != SYS_NAME.casefold()):
            raise EvaluationError("Installed driver is outside the normal DriverStore")
        with installed_sys.open("rb") as stream:
            driver_bytes = stream.read(pins["stage_files"][SYS_NAME]["size"] + 1)
        if hashlib.sha256(driver_bytes).hexdigest() != pins["stage_files"][SYS_NAME]["sha256"]:
            raise EvaluationError("Installed SYS SHA-256 differs from reviewed bytes")
        return {"published_inf": inf_name, "driver_version": version,
                "provider": provider, "service": "VBAudioVACMME",
                "inf_sha256": hashlib.sha256(content).hexdigest(),
                "sys_sha256": hashlib.sha256(driver_bytes).hexdigest()}

    def install(self, paths, pins, receipt):
        guid, class_name, required = GUID(), ctypes.create_unicode_buffer(256), DWORD()
        if not self.setup.SetupDiGetINFClassW(
            str(paths[INF_NAME]), ctypes.byref(guid), class_name, len(class_name),
            ctypes.byref(required),
        ):
            self._error("SetupDiGetINFClassW")
        if bytes(guid) != bytes(self.class_guid) or class_name.value.casefold() != "media":
            raise EvaluationError("INF class differs from the reviewed MEDIA class")
        handle = self._handle(
            self.setup.SetupDiCreateDeviceInfoList(ctypes.byref(guid), None),
            "SetupDiCreateDeviceInfoList")
        receipt.update(handles_closed=False)
        previous_mode = self.setup.SetupSetNonInteractiveMode(True)
        try:
            info = SP_DEVINFO_DATA()
            if not self.setup.SetupDiCreateDeviceInfoW(
                handle, class_name.value, ctypes.byref(guid), "VB-Audio Virtual Cable",
                None, 1, ctypes.byref(info),
            ):
                self._error("SetupDiCreateDeviceInfoW")
            data = (HWID + "\x00\x00").encode("utf-16le")
            buffer = ctypes.create_string_buffer(data, len(data))
            if not self.setup.SetupDiSetDeviceRegistryPropertyW(
                handle, ctypes.byref(info), 1, buffer, len(data)
            ):
                self._error("SetupDiSetDeviceRegistryPropertyW")
            receipt.update("registering-device",
                           device_state="registration_attempted_retained_until_VM_teardown")
            if not self.setup.SetupDiCallClassInstaller(
                DIF_REGISTERDEVICE, handle, ctypes.byref(info)
            ):
                self._error("SetupDiCallClassInstaller(DIF_REGISTERDEVICE)")
            instance = self._instance(handle, info)
            flags = self._restart_flags(handle, info)
            receipt.update("registered", device_instance=instance, registration_restart_flags=flags,
                           device_state="registered_retained_until_VM_teardown")
            if not instance.upper().startswith("ROOT\\") or flags:
                raise EvaluationError("Root registration needs restart or returned unexpected instance")
            verify_staged(paths, pins)
            reboot = BOOL(False)
            receipt.update("installing-driver", install_flags=INSTALLFLAG_NONINTERACTIVE,
                           package_state="installation_attempted_retained_until_VM_teardown")
            ok = self.newdev.UpdateDriverForPlugAndPlayDevicesW(
                None, HWID, str(paths[INF_NAME]), INSTALLFLAG_NONINTERACTIVE,
                ctypes.byref(reboot))
            error = ctypes.get_last_error() if not ok else None
            receipt.update(update_returned=bool(ok), update_reboot_required=bool(reboot.value),
                           update_error=error)
            if not ok:
                raise EvaluationError("UpdateDriverForPlugAndPlayDevicesW failed: "
                                      + ctypes.FormatError(error).strip(),
                                      operation="UpdateDriverForPlugAndPlayDevicesW", code=error)
            flags = self._restart_flags(handle, info)
            receipt.update(update_restart_flags=flags,
                           package_state="installed_retained_until_VM_teardown")
            if reboot.value or flags:
                raise EvaluationError("Windows requires reboot; this job cannot qualify the endpoint")
            deadline = time.monotonic() + 20
            while True:
                status, problem = DWORD(), DWORD()
                code = self.cfg.CM_Get_DevNode_Status(
                    ctypes.byref(status), ctypes.byref(problem), info.DevInst, 0)
                current = {"configret": int(code), "status": int(status.value),
                           "problem": int(problem.value)}
                if code == 0 and status.value & 8 and not status.value & 0x400 and problem.value == 0:
                    break
                if time.monotonic() >= deadline:
                    receipt.update(devnode=current)
                    raise EvaluationError("Installed devnode did not become started/problem-free")
                time.sleep(0.25)
            identity = self._installed_identity(handle, info, pins)
            receipt.update("driver-started", devnode=current, installed_identity=identity,
                           device_state="started_retained_until_VM_teardown")
        finally:
            self.setup.SetupSetNonInteractiveMode(previous_mode)
            self._destroy(handle)
            receipt.update(handles_closed=True)


def evaluate(receipt):
    check_environment()
    pins, pins_hash = load_pins()
    receipt.update("guard-passed", branch=BRANCH, pins_sha256=pins_hash,
                   source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   archive={"url": URL, "size": ARCHIVE_SIZE, "sha256": ARCHIVE_SHA256},
                   license_url=pins["license_url"], vendor_notice_sha256=pins["notice_sha256"])
    native = NativeWindows()
    matching = native.matching_devices()
    receipt.update("fresh-device-check", matching_hardware_ids=matching)
    if matching:
        raise EvaluationError("Matching hardware ID already exists; no existing device is changed")
    before = native.wave_devices()
    receipt.update(winmm_before=before)
    if before:
        raise EvaluationError("Existing audio output found; a new endpoint is not needed for this experiment")
    with tempfile.TemporaryDirectory(prefix="carr-endpoint-evaluation-") as work:
        archive = Path(work) / "VBCABLE_Driver_Pack46.zip"
        receipt.update("downloading")
        download_archive(archive)
        paths = stage_archive(archive, Path(work) / "driver", pins)
        receipt.update("archive-verified", staged_files=pins["stage_files"])
        native.lock_inputs(paths)
        receipt.update(input_handles_closed=False)
        try:
            verify_staged(paths, pins)
            reports = verify_signatures(paths, pins, native, work)
            receipt.update("signatures-verified", signatures=reports)
            # A second fresh-device check closes the long download/verification window.
            if native.matching_devices():
                raise EvaluationError("Matching device appeared before registration")
            native.install(paths, pins, receipt)
            deadline = time.monotonic() + 20
            while True:
                after = native.wave_devices()
                candidates = [d for d in after if d["name"].casefold().startswith("cable input")]
                if candidates:
                    break
                if time.monotonic() >= deadline:
                    receipt.update(winmm_after=after)
                    raise EvaluationError("No actual CABLE Input WinMM endpoint appeared")
                time.sleep(0.25)
            queries = [{"device_id": d["id"], "result": native.query_format(d["id"])}
                       for d in candidates]
            mapper = native.query_format(0xffffffff)
            receipt.update("format-query", winmm_after=after, format_queries=queries,
                           wave_mapper_result=mapper)
            if mapper or any(query["result"] for query in queries):
                raise EvaluationError("Exact16000Hz PCM16 mono format query failed")
        finally:
            native.close_inputs()
            receipt.update(input_handles_closed=True)
    receipt.update("complete", temporary_download_removed=True)
    return {"qualification": "actual_WinMM_endpoint_and_format_only",
            "actual_MCI_playback": "requires_unchanged_reader_probe",
            "reboot_required": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args(argv)
    receipt = Receipt(args.receipt)
    watchdog = threading.Timer(180, receipt.timeout)
    watchdog.daemon = True
    watchdog.start()
    try:
        result = evaluate(receipt)
        receipt.finish("PASS", **result)
        return 0
    except BaseException as error:
        receipt.finish("FAILED", error={
            "type": type(error).__name__, "message": str(error)[:4000],
            "operation": getattr(error, "operation", None), "code": getattr(error, "code", None),
        })
        print(json.dumps(receipt.value["error"]), file=sys.stderr, flush=True)
        return 1
    finally:
        watchdog.cancel()


if __name__ == "__main__":
    raise SystemExit(main())

