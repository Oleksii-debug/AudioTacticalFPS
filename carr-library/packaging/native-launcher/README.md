# Native Windows launch entry

This CARR-owned, small Win32 launcher is the source for the private bundle's single
`ResearchLibrary.exe` entry. It uses the current user's privileges and only fixed
bundled Python/application paths. It does not invoke a shell, search PATH for Python,
download anything, update software, delete an environment, or move research data.

## Exact launch contract

Place the compiled executable at the bundle root beside `install_windows.py`.
If `.venv` is absent, a normal launch runs exactly the bundled
`runtime\python.exe -I -B -X utf8 install_windows.py`, waits for success, and checks
the existing installer's `.venv\.installation-complete` marker. A partially existing
environment is preserved and reported as incomplete. An already complete package
runs `.venv\Scripts\python.exe -I -B -X utf8 -m carr_library`.
Available bundled model paths are passed as fixed arguments, and the same optional
OCR, FFmpeg and citation runtime paths are configured locally. All commands use
`CreateProcessW` with an explicit application filename and bounded Unicode quoting.

The only accepted diagnostic options are:

| Option | Behavior |
| --- | --- |
| `--launcher-check` | Read-only JSON state: ready (0), incomplete (3), missing-runtime (4), invalid-path (5), needs-install (10). |
| `--self-test` | Existing installed application's self-test; never initiates installation. |
| `--check-installation` | Existing `carr_library.native_checks --bundle-root` check; never initiates installation. |

Extra or unknown arguments return 64. Native dialogs explain normal launch errors;
diagnostic failures use stderr without displaying a blocking dialog. The launcher
waits for its child and propagates the child's exit status. No process stays behind
after the application exits.

## Windows build and actual native process acceptance

Use an **x64 Visual Studio Developer Command Prompt** with the installed Microsoft
Windows SDK. From the repository root, choose a fresh output directory outside Git:

```powershell
powershell.exe -NoProfile -File carr-library\packaging\native-launcher\build_msvc.ps1 -OutputDirectory C:\PrivateBuild\NativeLauncher
python carr-library\packaging\native-launcher\probe_windows.py --launcher C:\PrivateBuild\NativeLauncher\ResearchLibrary.exe --child C:\PrivateBuild\NativeLauncher\windows_child_probe.exe --report C:\PrivateBuild\NativeLauncher\windows-probe.json
```

The build uses installed `cl.exe`, `link.exe` and `rc.exe`. It records their versions
and SHA256 hashes, source hashes, and the exact PE hash in `build-evidence.json`.
Python 3.11 or newer is only needed for the stdlib-only build/probe helpers. The
launcher itself imports only Windows system DLLs and adds no Python dependency.
The source-only Windows 2022 workflow in `.github/workflows/carr-native-launcher.yml`
loads `VsDevCmd.bat`, compiles the actual executable, and executes it. It emits text
evidence only and has no binary artifact upload step.

The child probe is explicitly synthetic. It verifies Windows process creation,
Unicode/space-safe arguments, fixed module selection, optional local runtime paths,
read-only inspection, child exit status, rejection of extra arguments, incomplete
environment preservation, the fresh installer handoff, and a research-data sentinel.
Its success does **not** establish actual Python installation, GUI/NVDA usability,
voice quality, code-signing, or complete product acceptance.

For an actual **already offline-installed** private bundle, run separately:

```powershell
python carr-library\packaging\native-launcher\probe_installed_windows.py --bundle-root C:\PrivateBuild\ResearchLibrary --report C:\PrivateBuild\installed-launcher-probe.json
```

That probe runs the real app's native checks and self-test through the exact EXE.
It requires ready state and never installs or repairs anything. Fresh installation
and GUI/NVDA acceptance remain separate release checks.

## Actual fresh EXE installation and first GUI boot

Use a fresh extracted private bundle on an interactive native Windows desktop.
The bundle must have no `.venv` at all. Run this source-checkout helper
**before any Python-led installation**, with a new report path outside the bundle:

```powershell
python -I -B carr-library\packaging\native-launcher\probe_fresh_windows.py --bundle-root C:\PrivateBuild\ResearchLibrary --report C:\PrivateBuild\fresh-launcher-probe.json --timeout 600
```

The helper verifies the sealed manifest using the trusted source checkout's
installer verifier, then launches the actual bundled `ResearchLibrary.exe`
with no arguments. The normal native launcher must perform offline installation
and reach the real wx desktop. The helper requires the completion marker, installed
Python and a database inside its newly created private data directory. It accepts
only a visible, enabled, responsive application window whose live Python process
belongs to the exact job created for that launch.

Each launcher starts suspended, joins a non-breakaway job with kill-on-close, and
only then resumes. An assignment failure terminates the exact still-suspended
process handle. The accepted GUI process handle remains open through shutdown;
a matching title or reused PID alone cannot establish ownership. The helper
rechecks membership and responsiveness, posts normal `WM_CLOSE`, and requires
the GUI process, launcher and all owned descendants to exit. It then runs
`--launcher-check`, `--check-installation` and `--self-test` through
the same real EXE. A nonzero exit, missing GUI or remaining child fails the probe.
Timeouts terminate only its owned job; cleanup waits are bounded.

Temporary child environment settings include the pinned platformdirs
`WIN_PD_OVERRIDE_*` Known Folder overrides. Changing APPDATA alone does not
redirect that library's Windows data lookup. The probe changes no host environment
or registry settings and removes only its own temporary test data. It leaves the
newly installed bundle environment in place. Existing, incomplete or linked
`.venv` paths are refused and preserved; a failed run must use another fresh
extraction for its next installation attempt.

Success also requires the original manifest bytes and every sealed file hash to
remain unchanged. The report distinguishes automated first-launch GUI boot/close
from full interface acceptance: `first_launch_gui_boot_close_verified` can be
true while `gui_verified`, `nvda_verified` and
`authenticode_verified` remain false. Manual NVDA and signing are always
reported `NOT_RUN`; window responsiveness is not an accessibility or signing
assessment.

Focused portable contracts are separate from actual native execution:

```powershell
python -m pytest -q carr-library\tests\packaging\test_native_fresh_launcher.py carr-library\tests\packaging\test_native_launcher_bundle.py
```

These tests use process/API doubles only for failure and ownership contracts.
This source addition was prepared while the local executor was unavailable:
portable execution and actual fresh-bundle native acceptance were `NOT_RUN`
at preparation. A later exact-source native report must establish those results.
The helper does not change the launcher C source, executable or build provenance.

Platform contracts used here:

- [Job objects and descendant membership](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- [AssignProcessToJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject)
- [IsProcessInJob](https://learn.microsoft.com/en-us/windows/win32/api/jobapi/nf-jobapi-isprocessinjob)
- [SendMessageTimeoutW return and timeout rules](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendmessagetimeoutw)
- [Pinned platformdirs 4.10.0 Windows path overrides](https://github.com/tox-dev/platformdirs/blob/4.10.0/src/platformdirs/windows.py)

## Optional pinned cross build

`toolchain.lock.json` pins the official Zig 0.14.1 archive URLs, lengths and SHA256
digests. The helper verifies the archive and selected compiler binary before use;
it does not download or silently replace a compiler. Example from `carr-library`:

```sh
python packaging/native-launcher/build_launcher.py --zig /private/zig-x86_64-linux-0.14.1/zig --toolchain-archive /private/zig-x86_64-linux-0.14.1.tar.xz --output-dir /private/native-launcher --build-probe
```

The output directory must be outside every Git worktree. A successful PE structure
probe proves its architecture, imports, manifest and mitigation flags; it does not
prove Windows execution. The portable core test can also be compiled with a local
C compiler using `-std=c11 -Wall -Wextra -Werror test_launcher_core.c`.

## Source-bound private bundle integration

Pass the verified executable and its corresponding build report to the existing
`build_windows_bundle.py --launcher ... --launcher-evidence ...` options. The
assembler checks bounded plain files, the actual PE properties, exact launcher
source hashes and executable identity before copying the entry. It rejects a
replacement during assembly and seals the original evidence snapshot alongside
`ResearchLibrary.exe` in the ordinary bundle checksum inventory. The installer
requires both files and bundled Python whenever the native-launcher flag is set,
and rejects undeclared launcher files. Build evidence is not a publisher signature.

Keep the existing offline installer, runtime, wheelhouse and data locations. The
application selects `audio-runtime/ffmpeg.exe` through its existing sealed runtime
verifier; the launcher's optional legacy PATH entries do not replace that verifier.
Never include `windows_child_probe.exe`, test outputs, compiler files or caches in
the product. A Start-menu shortcut may target the same fixed root entry when the
existing installer gains that separate feature.

Do not commit or attach the native binary to the public repository or public CI
artifacts. The current build is an **unsigned private candidate**. A real owner-held
Authenticode certificate, signing/timestamping process, subsequent signature
verification and separately retained signed binary hash remain release prerequisites.
This addition alone does not complete all V3-16 installation/update requirements.

## Primary toolchain and platform references

- Zig release index: https://ziglang.org/download/index.json
- Zig cross compilation: https://ziglang.org/learn/overview/
- Microsoft `CreateProcessW`: https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw
- Microsoft application manifests: https://learn.microsoft.com/en-us/windows/win32/sbscs/application-manifests
- Microsoft PE format, including `IMAGE_DEBUG_TYPE_REPRO`: https://learn.microsoft.com/en-us/windows/win32/debug/pe-format

The PE probe accepts either a zero timestamp or the documented deterministic-build
debug marker. `/Brepro` can encode a content-derived timestamp; that value is not
treated as a wall-clock build time.
