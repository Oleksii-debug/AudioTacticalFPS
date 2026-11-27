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

That probe verifies the complete bundle seal before and after the real app's
native checks and self-test through the exact EXE. It records the source commit,
application wheel digest and launcher digest. Pass `--source-commit` with the
independently recorded full commit to bind that expectation; otherwise it records
the manifest's source identity. It requires ready state and never installs or
repairs anything. Fresh installation and GUI/NVDA acceptance remain separate checks.

## CI-only actual first installation and desktop lifecycle

Before the existing installed probe, the complete-bundle Windows workflow can run:

```powershell
& $env:CARR_PYTHON -I -B packaging/native-launcher/probe_fresh_windows.py --bundle-root $bundle --source-proof ci-results/source-wheel-before-native.json --source-commit $sourceCommit --report ci-results/fresh-native-launcher.json --timeout 600
```

This invocation **replaces the workflow's direct `install_windows.py` invocation**.
The bundle must be sealed and its `.venv` absent; the staged Python must already
have the same application version installed. Every `carr_library` and `bootstrap`
payload file is compared with the sealed wheel before importing the seed API.
The source proof is the existing `source_wheel.verify_application_wheel` result;
the probe repeats that exact-commit verification and requires the same result.
The existing workflow firewall rules must be active first, including the seed
interpreter and its base executable. The probe is
restricted to native Windows with `GITHUB_ACTIONS=true`; it is not a workstation
installer or a substitute for owner approval.

It requires `--launcher-check` to report `needs-install` with exit 10 without
creating an environment. It then runs the exact `ResearchLibrary.exe` **without
arguments**, allowing that entry to execute the bundled offline installer and
open the actual desktop. Win32 enumeration must observe a visible `Resonance`
window belonging to this launcher's process tree. Its actual process image must
be the bundle's `.venv/Scripts/python.exe` or `runtime/python.exe` redirector.
The probe checks the completed-install marker, rechecks window ownership and
sends `WM_CLOSE` only to that matched window, then requires launcher exit 0.
A second no-argument launch must open and close the same way without changing
the marker's bytes or file identity/timestamps.

A synthetic library is created using the staged installed `LibraryApp`. The
actual `preference_file`/`remember_directory` APIs select it; there is no
`LOCALAPPDATA` redirection assumption. Prior settings bytes are saved and restored
in `finally` only if the probe's own settings bytes and file identity/timestamps
are unchanged. A concurrent preference write is preserved and reported as a
failure. The synthetic source hashes and reopened literal search/quotation must
survive both actual launches. The trusted existing bundle verifier checks every
sealed file before installation and after each launch, retaining the same manifest
and EXE identity. Temporary data is
owned by the probe; it does not inspect existing libraries or remove environments.

Startup waits and logs are bounded. Failure cleanup targets only the still-owned
launcher PID and its descendants; it does not kill processes by executable name.
This proves first-installation and native-window lifecycle **only when that
exact Windows run succeeds**. It does not prove GUI feature workflows, keyboard
focus, NVDA, signing, language quality or first-run behavior on an owner's PC.
Keep `probe_installed_windows.py` and the native feature probes as subsequent
independent gates. Linux AST/pure-helper checks are not native acceptance.

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

## Minimal private bundle hook

The current assembler keeps its existing `--native-launcher` and
`--native-launcher-sha256` inputs. It captures bounded bytes, inspects the staged
unsigned PE, and checks that digest again before the normal checksum seal.
Only `ResearchLibrary.exe` enters the product; no probe executable, compiler,
cache or test report is copied. The installer and manifest format are unchanged.

Before invoking that builder, the integrated gate also runs
`verify_launcher.py --launcher ... --build-evidence ... --native-probe ...
--source-commit ...`. This preflight binds the owner-supplied reports to the
same captured PE and the exact committed C/resource/build inputs. Dirty source,
mixed reports, unsupported schemas/targets and mismatched digests are rejected.
The reports prove consistency with the selected local candidate; they are not
signatures or independent publisher attestations. The check itself never executes
the candidate. Its matching digest is then supplied to the existing builder.

The integrated Windows gate uses installed MSVC and the existing actual native
process probe. It then exercises EXE first installation/window restart and the
installed diagnostics under the workflow's explicit outbound firewall rules.
These combined gates require their own successful run; the earlier standalone
launcher result is component evidence only. Wheel-only bundles still require
`Install.cmd` first; direct EXE first installation needs bundled Python.
A Start-menu shortcut may target the same root entry when the existing installer
gains that separate feature.

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
