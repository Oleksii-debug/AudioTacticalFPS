"""Actual Windows acceptance for bounded diagnostic process-tree cleanup.

This uses only a synthetic Python child and grandchild in a temporary directory.
No bundle, research data, native executable payload or external service is used.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from windows_job import _BasicLimits, _ExtendedLimits, _ProcessInfo, _StartupInfo, _StartupInfoEx, run_in_job


def inspect_cleanup():
    if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError("Process-tree acceptance requires native Windows x64.")
    assert [ctypes.sizeof(value) for value in
            (_BasicLimits, _ExtendedLimits, _StartupInfo, _StartupInfoEx, _ProcessInfo)] == [64, 144, 104, 112, 24]
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
    api.OpenProcess.restype = ctypes.c_void_p
    api.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    api.WaitForSingleObject.restype = ctypes.c_uint32
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    api.CloseHandle.restype = ctypes.c_int32
    handles = []
    with tempfile.TemporaryDirectory(prefix="CARR bounded launcher Київ ") as temporary:
        root = Path(temporary)
        script = root / "synthetic_tree.py"
        script.write_text(
            "import os, pathlib, subprocess, sys, time\n"
            "root = pathlib.Path(sys.argv[1])\n"
            "role = sys.argv[2]\n"
            "pending = root / (role + '.pid.part')\n"
            "pending.write_text(str(os.getpid()))\n"
            "pending.replace(root / (role + '.pid'))\n"
            "if role == 'parent':\n"
            "    subprocess.Popen([sys.executable, __file__, str(root), 'child'])\n"
            "time.sleep(60)\n", encoding="utf-8")
        result = run_in_job(Path(sys.executable), ["-I", "-B", "-c", "raise SystemExit(17)"],
                            cwd=root, timeout=15)
        assert result.returncode == 17
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(run_in_job, Path(sys.executable),
                                     ["-I", "-B", str(script), str(root), "parent"],
                                     cwd=root, timeout=10)
                deadline = time.monotonic() + 8
                for role in ("parent", "child"):
                    pid_file = root / (role + ".pid")
                    while True:
                        try:
                            value = pid_file.read_text()
                        except OSError:
                            value = ""
                        if value.isdecimal() and int(value) > 0:
                            pid = int(value)
                            break
                        if time.monotonic() >= deadline:
                            raise RuntimeError("The synthetic process tree did not start in time.")
                        time.sleep(0.02)
                    handle = api.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
                    if not handle:
                        raise ctypes.WinError(ctypes.get_last_error())
                    handles.append(handle)
                try:
                    future.result(timeout=20)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    raise AssertionError("The synthetic sleeping process did not time out.")
                for handle in handles:
                    if api.WaitForSingleObject(handle, 5000) != 0:
                        raise AssertionError("A timed-out acceptance process survived job closure.")
        finally:
            for handle in handles:
                api.CloseHandle(handle)
    return {"schema": 1, "status": "passed", "synthetic_processes": True,
            "checks": ["x64-api-layout", "child-exit-code", "parent-timeout-cleanup", "descendant-timeout-cleanup"],
            "process_job_assigned_at_creation": True, "python_child_output_captured": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = inspect_cleanup()
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
