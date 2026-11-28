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
waits for its direct child and propagates the child's exit status. Complete child-tree
ownership is supplied only by the explicitly admitted portable profile described below;
a legacy unmarked executable does not acquire that capability.

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

## Sealed default and explicit ownership profiles

The canonical assembler keeps its existing `--native-launcher PATH` option for the
previously qualified pinned PE. That executable remains a supported sealed default;
it has no `bundled_launcher` flag or new ownership receipt. Its bytes still belong to
the bundle checksum inventory. This compatibility does not authorize portable
activation or convert an old launcher into the current ownership implementation.

Use `--launcher-dir DIRECTORY --source-commit FULL_SHA` with a bundled Python
runtime to select a current build. The builder verifies its PE, build receipt and
exact committed C/resource identity, then seals `ResearchLibrary.exe` together with
`native-launcher-build.json`. Do not copy a different executable over that result.
The synthetic child probe, compilers and private tool outputs are never product files.

Standard setup uses this current owned launcher. A persistent installation lease
coordinates application use, Repair and Uninstall. Before Python or site initialization,
the native entry refuses a recorded uncertain bootstrap. Setup writes that record
under its exclusive lease before starting its bootstrap child and removes only its
own unchanged record after the exact owned process tree has stopped. An uncertain
launch or cleanup preserves both program versions and recovery evidence.

The separate `--portable-updates` option additionally seals protocol 2 from
`PORTABLE_UPDATES.md`. Its fixed `-I -S -B` admission bootstrap verifies the exact Job,
shared lease and durable owner receipt before site initialization. Every decorated
public entry retains that ownership, including the fixed local audio setup entry.
Unknown or stale owner receipts remain a refusal; no elapsed-time or missing-PID
rule resets them. Standard setup refuses to wrap this portable profile. Managed
automatic updates still require an authenticated setup handoff.

For current owned bundles, `SetupLocalAudio.cmd` selects the packaged
`carr_library.local_audio_setup` entry. It holds the same application lifetime and
loads inventory-verified snapshots of the canonical source-only audio setup helpers.
The helpers retain their explicit consent, pinned inputs, external runtime output and
license checks. The unmarked canonical bundle keeps its existing source-tool command.

The component's actual Windows gates separately exercise managed bootstrap,
portable pre-admission parent death, descendant cleanup, lease exclusion and
uncertain recovery from both source and installed packages. They do not establish
GUI/NVDA usability, signing, private runtime acceptance or configured service authority.

Do not commit or attach the native binary to the public repository or public CI
artifacts. Current builds are unsigned candidates. Owner-held Authenticode signing,
timestamping, verification and a separately retained signed binary hash remain
release prerequisites.

## Primary toolchain and platform references

- Zig release index: https://ziglang.org/download/index.json
- Zig cross compilation: https://ziglang.org/learn/overview/
- Microsoft `CreateProcessW`: https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw
- Microsoft application manifests: https://learn.microsoft.com/en-us/windows/win32/sbscs/application-manifests
- Microsoft PE format, including `IMAGE_DEBUG_TYPE_REPRO`: https://learn.microsoft.com/en-us/windows/win32/debug/pe-format

The PE probe accepts either a zero timestamp or the documented deterministic-build
debug marker. `/Brepro` can encode a content-derived timestamp; that value is not
treated as a wall-clock build time.
