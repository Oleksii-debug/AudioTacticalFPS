"""Actual fixed Windows PowerShell classification before expensive native setup.

This uses the current owned launcher and exact Invoke-Logged function. It does
not install anything, change firewall state, load models, or qualify library50.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import sys
import tempfile
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
CHECKS = (
    "absent-pathext-refuses-before-python",
    "fixed-exe-runs-absolute-isolated-python-in-pipeline",
    "ambient-extension-poison-cannot-change-fixed-environment",
)


def digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def write_bytes(path, data):
    with Path(path).open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if os.name != "nt" or not sys.flags.isolated or not sys.flags.dont_write_bytecode:
        raise RuntimeError("Use native installed Windows Python with -I -B.")
    for value in (args.source_commit, args.source_tree):
        if re.fullmatch("[0-9a-f]{40}", value) is None:
            raise ValueError("Exact source commit and tree are required.")
    report = args.output.absolute()
    report.parent.mkdir(parents=True, exist_ok=True)
    if report.exists():
        raise FileExistsError("Use a fresh shell-classification report.")
    import carr_library
    import carr_library.security.standard_windows as native
    if not Path(carr_library.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()):
        raise AssertionError("The smoke must use the installed product owner.")
    source = PROJECT / "packaging" / "probe_standard_update_windows.py"
    qualifier = PROJECT / "scripts" / "qualify_standard_library.ps1"
    definitions = runpy.run_path(str(source))
    environment = definitions["_qualification_environment"]
    text = qualifier.read_text(encoding="utf-8")
    start = "function Invoke-Logged {\n"
    stop = "\nfunction Assert-OfflineRules {"
    if text.count(start) != 1 or text.count(stop) != 1:
        raise AssertionError("The exact qualification logging function is ambiguous.")
    invoke = text[text.index(start):text.index(stop, text.index(start))]
    if len(invoke.encode("utf-8")) > 1024:
        raise AssertionError("The qualification logging function exceeds its fixed bound.")
    host = native.WindowsStandardHost
    shell = host._powershell()
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("Use the same explicitly resolved CI Git executable.")
    original_environment = dict(os.environ)
    original_host = host.environment()
    fixed = environment(host, shell, git)
    absent = dict(fixed)
    del absent["PATHEXT"]
    with patch.dict(os.environ, {"PATHEXT": ".CPL;.BAT;.CMD;.PS1",
                                "PYTHONPATH": "untrusted-not-an-import-root"}):
        poisoned = environment(host, shell, git)
    if (fixed != poisoned or fixed["PATHEXT"] != ".EXE" or
            "PYTHONPATH" in fixed or host.environment() != original_host or
            dict(os.environ) != original_environment or
            any(key.upper() == "PATHEXT" for key in original_host)):
        raise AssertionError("The fixed CI environment widened or changed ambient product policy.")
    stage = Path(tempfile.mkdtemp(prefix="carr-qualification-shell-"))
    child = stage / "selected-python.py"
    child.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "value = {'status':'PASS','os':os.name,'pid':os.getpid(),"
        "'python':sys.executable,'isolated':bool(sys.flags.isolated),"
        "'no_bytecode':bool(sys.flags.dont_write_bytecode)}\n"
        "with Path(sys.argv[1]).open('x', encoding='utf-8') as stream:\n"
        "    json.dump(value, stream, sort_keys=True)\n"
        "print('CARR_PIPELINE_CHILD ' + json.dumps(value, sort_keys=True), flush=True)\n",
        encoding="utf-8")
    powershell = stage / "exact-invoke-logged.ps1"
    powershell.write_text(
        "[CmdletBinding()]\n"
        "param([string]$Python, [string]$Child, [string]$Marker, [string]$Log)\n"
        "Set-StrictMode -Version Latest\n"
        "$ErrorActionPreference = 'Stop'\n"
        "if ($PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1) "
        "{ throw 'Require actual fixed Windows PowerShell5.1.' }\n"
        "$identity = [ordered]@{version=$PSVersionTable.PSVersion.ToString(); pathext=$env:PATHEXT}\n"
        "[Console]::Out.WriteLine('CARR_SHELL ' + ($identity | ConvertTo-Json -Compress))\n"
        + invoke + "\n"
        "Invoke-Logged -Arguments @('-I', '-B', '-X', 'utf8', $Child, $Marker) -Log $Log\n",
        encoding="utf-8")
    observations = []
    # Retain the private fixture directory on any unexpected exception. The
    # existing run_owned settles its actual process family before returning.
    for mode, selected_environment in (("absent", absent), ("fixed", fixed), ("poisoned", poisoned)):
        marker = stage / (mode + "-python.json")
        tee = stage / (mode + "-tee.txt")
        code, output = native.run_owned(
            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", powershell, "-Python", sys.executable, "-Child", child,
             "-Marker", marker, "-Log", tee],
            cwd=stage, environment=selected_environment, timeout=60)
        if not isinstance(output, bytes) or len(output) > 2 * 1024**2:
            raise AssertionError("Owned shell output exceeded its existing bound.")
        output_path = report.with_name(report.stem + "-" + mode + ".log")
        write_bytes(output_path, output)
        decoded = output.decode("utf-8", errors="replace")
        shells = [json.loads(line[len("CARR_SHELL "):]) for line in decoded.splitlines()
                  if line.startswith("CARR_SHELL ")]
        if len(shells) != 1 or not shells[0]["version"].startswith("5.1."):
            raise AssertionError("The fixed system shell identity is missing.")
        observed = {"mode": mode, "returncode": code, "shell": shells[0],
                    "output_bytes": len(output), "output_sha256": sha256(output).hexdigest(),
                    "output_file": output_path.name}
        if mode == "absent":
            if (code == 0 or shells[0]["pathext"] != ".CPL" or
                    b"CantActivateDocumentInPipeline" not in output or marker.exists()):
                raise AssertionError("The exact old classification failure was not reproduced before Python.")
            observed["python_started"] = False
        else:
            # WinPS adds .CPL itself; the supplied child environment remains .EXE only.
            if code != 0 or shells[0]["pathext"] != ".EXE;.CPL" or not marker.is_file() or not tee.is_file():
                raise AssertionError("The fixed executable classification did not complete its real pipeline.")
            value = json.loads(marker.read_text(encoding="utf-8"))
            if (value.get("status") != "PASS" or value.get("os") != "nt" or
                    value.get("isolated") is not True or value.get("no_bytecode") is not True or
                    not isinstance(value.get("pid"), int) or value["pid"] <= 0 or
                    not Path(value["python"]).samefile(sys.executable)):
                raise AssertionError("The actual Python marker differs from the selected isolated executable.")
            observed.update(python_started=True, child=value, tee_sha256=digest(tee))
        observations.append(observed)
    shutil.rmtree(stage)
    if stage.exists():
        raise AssertionError("The settled owned fixture directory was not removed.")
    result = {
        "schema": "carr-standard-qualification-shell/1", "status": "PASS",
        "source_commit": args.source_commit, "source_tree": args.source_tree,
        "checks": list(CHECKS), "passed": 3, "failed": 0, "skipped": 0,
        "fixed_pathext": ".EXE", "observed_fixed_pathext": ".EXE;.CPL",
        "host_environment_unchanged": True,
        "fixed_shell": str(shell), "fixed_shell_sha256": digest(shell),
        "python": sys.executable, "python_sha256": digest(sys.executable),
        "source_sha256": {
            "packaging/probe_standard_update_windows.py": digest(source),
            "scripts/qualify_standard_library.ps1": digest(qualifier),
            "scripts/standard_qualification_shell_acceptance.py": digest(__file__),
            "installed/security/standard_windows.py": digest(native.__file__),
        },
        "invoke_logged_sha256": sha256(invoke.encode("utf-8")).hexdigest(),
        "observations": observations, "owned_process_family": True,
        "fixture_cleanup": "PASS", "installed_library_50": "NOT_RUN_BY_THIS_SMOKE",
        "managed_upgrade_15": "NOT_RUN_BY_THIS_SMOKE",
    }
    write_bytes(report, (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
