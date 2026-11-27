# Standard per-user Windows installation

This source adds the standard Install, Repair and Uninstall shell around the
existing verified offline bundle. It uses Inno Setup 6.7.4 for the Windows wizard,
Start-menu entries and per-user uninstall registration. The captured
`packaging/install_windows.py` still creates the Python environment and installs
the locked wheelhouse; this wrapper has no second package-resolution procedure.

The candidate is unsigned and private. Building or installing it locally does not
clear audio runtimes, models or other bundled components for redistribution. Any
future setup delivery/export must use the existing create_delivery admission and
its reviewed component-rights records; this builder adds no clearance flag and
has no transfer/publish path. In particular, a sealed audio runtime is not itself
proof of redistribution permission. CI may compile/install locally and emit
reviewed text evidence only.

Source/filesystem tests are separate from
the mandatory actual Windows gate. A compiled setup, native installation, NVDA
usability, lower-privilege account run, network trace and product Authenticode
qualification must never be inferred from successful source tests.

## Reuse audit and identities

This canonical adaptation reuses the prepared installer at
`c29cd9886f4c3bd72b6ff7fc3d0c037ec53ae2d4`, including its correction to the
earlier `e8a3675f7c335eba7df344b74e8eada2a7d81395` repair rollback. The host base
is `33930a72be6223334fba20c0d1567e31075c0d2b`. It adopts only the installer,
lease and private maintenance-process helper; existing audio, supplied-worker,
jobs, backup and provider services retain their canonical implementations.
The existing bundle's `bundled_launcher` contract and `audio/bin` runtime path
are preserved. Recovery receipts are copied before moving the old application,
so receipt allocation failures leave its original path and bytes intact.

The reviewed PR122 head `687506ddca74c33fa3ee62c7925ae98b3b1afa3b` and PR149 head
`86691c10c499c1f9201219a0ea2f1425b02efa2f` provide the fixed native launch entry
and existing sealed bundle integration. Neither provides a standard installation
lifecycle. This addition retains those interfaces and the current integrated
bundle verifier. It adds a shared application lease to the same launcher and
Python entrypoint.

The original PR149 Zig-only byte allowlist describes its historical executable.
The lease changes the native source, so the final integration must rebuild the
current launcher and pass its actual Windows acceptance before packaging these
new bytes. An old successful native report is not evidence for the changed
launcher. The current source-bound native bundle contract remains authoritative.

For maintenance subprocesses, the builder copies the reviewed
`src/carr_library/audio/windows_process.py` verbatim. The helper starts the
offline installer suspended, assigns its private Windows Job Object, resumes it,
and closes that job on completion/error/timeout. It does not identify ownership
by a process name or a reused PID.

The Inno compiler is obtained only from the exact official artifact in
`toolchain.lock.json`. Preparation checks its size, SHA256, Authenticode status
and expected publisher before execution. This verifies the toolchain signature;
it does not sign Resonance. Build outputs, original compiler installers and
copied runtimes must remain outside Git worktrees.

## User lifecycle

The default program directory is
`%LOCALAPPDATA%\Programs\Resonance`. The wizard requests the current user's
privileges, adds Resonance, Repair Resonance and Uninstall Resonance to that
user's Start menu, and registers the same repair and uninstall entries under
HKCU. It never stops a running application automatically.

Before creating wrapper files, an existing product registration must identify
the same managed program directory. A preexisting Resonance Start-menu folder
without that registration is refused; its existing shortcuts are not replaced.

Research data remains outside the installation tree, normally under
`%LOCALAPPDATA%\Universal Research Library`. The installer refuses to place the
program inside, above, or exactly at that data directory.

- **Install:** choose a new program folder. The helper checks the complete sealed
  payload, caches the original setup and exact maintenance sources, invokes the
  existing offline installer at its final path, verifies completion and records
  every installed application file and directory.
- **Repair:** close Resonance and its jobs, then run the cached setup or the
  matching original setup. The existing `app` directory is moved intact into a
  sibling `Resonance-recovery-<identity>` directory before rebuilding. Changed
  files and foreign notes remain in that recovery directory. A failed rebuild
  restores the previous application and receipts; partial new output is retained
  in an `-incomplete-` directory.
- **Uninstall:** the helper checks the independent ownership-receipt hash and
  compares the current tree with the exact recorded inventory before deletion.
  Added, changed or missing application files cause refusal before deletion.
  Repair first to preserve an altered tree, then uninstall the fresh application.
  External research data and retained recovery directories are never deleted.

Repair accepts only the identical bundle and original setup identity; this is
not an upgrade protocol. Signed update transport and migration remain the
existing update client's separate responsibility. If ownership metadata or
maintenance files themselves are damaged, keep the folder and install the
verified setup into a new folder instead of guessing ownership.

Recovery directories use additional disk space and are intentionally not
removed automatically. The small `managed-install.ini` and persistent empty
`installation.lease` remain after uninstall so concurrent processes cannot
replace the coordination inode. Unrelated files at the installation root are
also retained. No recursive wildcard deletion is used.

## Coordination and boundaries

A managed app holds a shared file handle for its entire lifetime. The native
launcher takes it before starting Python; the Python entrypoint also covers
direct CLI and installed script entrypoints. Setup holds an exclusive handle
through the full Inno transaction, including its shortcuts and registration.
The fixed Python maintenance helper duplicates that exact live handle and
verifies its resolved path.

One process-scoped hold covers nested entrypoints and concurrent jobs. The last
operation releases it; a nested dispatch cannot release an outer job's hold.
The common guard covers the canonical main/desktop/research/acquisition
dispatcher, OCR model, trusted client, supplied transcription, Studio and
assistant CLI entries. Additional public CLI modules, including the translation
module, must use the same `installed_entrypoint` decorator when integrated.
Imports themselves do not take a filesystem lock. Only the primary entrypoint
may run the exact synthetic bootstrap self-test before installation completes.

Windows sharing rules refuse maintenance while an app/job holds the lease and
refuse a new app during maintenance. POSIX flock exercises the corresponding
source lifecycle tests. This is coordination between cooperating programs, not
a sandbox against malicious code or an administrator running as the same user.

Links, junction/reparse ancestors, hardlinked files, ambiguous/case-colliding
inventory names and Windows device paths are rejected. Metadata publication is
atomic and existing foreign cache files are not overwritten.

The authoritative install operation runs in Inno's `PrepareToInstall` callback.
A nonempty result prevents registration after bootstrap failure. It must not be
moved to `ssPostInstall`: the pinned Inno engine catches exceptions there and
can continue. The uninstall callback runs before Inno removes its own recorded
wrapper files and refuses when our exact-inventory check fails.

## Prepare and compile

Run from `carr-library` on Windows, using fresh output paths outside every Git
worktree. The input must already be the exact verified full bundle with bundled
Python, a currently qualified native launcher and an exact source commit.

```powershell
& .\packaging\windows_installer\prepare_inno.ps1 -OutputDirectory C:\PrivateBuild\Inno674
$bundle = 'C:\PrivateBuild\VerifiedBundle'
$manifest = (Get-FileHash (Join-Path $bundle 'bundle-manifest.json') -Algorithm SHA256).Hash.ToLowerInvariant()
& (Join-Path $bundle '.venv\Scripts\python.exe') -I -B -X utf8 packaging/windows_installer/build_setup.py --bundle $bundle --bundle-manifest-sha256 $manifest --installed-bundle --output C:\PrivateBuild\StandardSetup --iscc C:\PrivateBuild\Inno674\tools\ISCC.exe --toolchain-receipt C:\PrivateBuild\Inno674\toolchain-receipt.json
```

Select the manifest digest from the prior independently verified bundle build
record. The example's file hash is suitable only after that prior verification.
Without `--installed-bundle`, any `.venv` is an undeclared payload and is
rejected. With it, only the sealed manifest files are captured and the existing
ordinary top-level `.venv` is omitted; other undeclared files still cause
refusal. No installed environment, user cache, private note or compiler binary
is silently included.

The builder requires the application wheel's lease and entrypoint source to
match the current checkout. It writes source/setup/bundle/toolchain hashes to
`setup-build.json`, first as `SOURCE_PREPARED` and after compilation as
`BUILT_NOT_INSTALLED`. Those states never imply native installation success.

## Mandatory actual Windows acceptance

The `windows_ci_steps.yml` fragment is intended for `carr-windows.yml` after the
full-bundle gate has restored its firewall changes. It reuses `CARR_CI_BUNDLE`;
it does not build a second runtime or use source imports to impersonate an
installed application. The strict source-suite union must include
`tests/packaging/test_standard_installer.py` and
`tests/packaging/test_installer_process.py`. Preserve the existing full gate
union and add this separate, bounded 20-minute native lifecycle step. There is
no measured setup duration until its actual run, and no cross-job bundle/model
artifact transfer is introduced.

On the canonical adoption, the two real-filesystem receipt-allocation
regressions first failed against the prepared copy-after-move ordering. With
copy-before-move, the two installer suites plus the existing native-launcher
bundle and install-bytecode suites passed 111 cases and six subtests, with no
failures or skips. These source tests use explicit inert PE/runtime fixtures;
they do not qualify the native setup. Run from `carr-library`:

```text
python -B -m pytest -q -o xfail_strict=true tests/packaging/test_standard_installer.py tests/packaging/test_installer_process.py tests/packaging/test_native_launcher_bundle.py tests/packaging/test_install_bytecode.py
```

Run the native probe only in an isolated Windows CI account with no existing
Resonance uninstall key or Start-menu folder:

```powershell
& C:\PrivateBuild\VerifiedBundle\.venv\Scripts\python.exe -I -B -X utf8 scripts/windows_standard_installer_smoke.py --setup C:\PrivateBuild\StandardSetup\compiled\ResonanceSetup.exe --build-evidence C:\PrivateBuild\StandardSetup\setup-build.json --work C:\PrivateBuild\InstallerAcceptance --report C:\PrivateBuild\InstallerReport
```

The probe actually executes the compiled Inno setup in a Unicode path. It checks
HKCU repair/uninstall registration, real Start-menu shortcut targets, native
launcher checks, a populated synthetic SQLite library, refusal while an installed
app holds the lease, unchanged live-app execution, refusal of altered inventory,
repair with retained foreign files, and normal uninstall with byte-identical
external library data. There is no native skip fallback.

The probe waits for the owned Job Object to become empty before closing it.
Inno's first-phase uninstaller deliberately exits before the second phase finishes
self-cleanup, so waiting only for the initial PID would truncate a real uninstall.
The whole wait remains bounded, and timeout cleanup closes only the owned job.

Keep the separate source, synthetic native-launcher, actual installed launcher,
standard setup lifecycle, NVDA, signing and distribution-rights results. Report
native network tracing and unprivileged-account qualification as NOT_RUN until
those checks actually execute. Upload only reviewed JSON/text reports; never
upload the setup EXE, compiler, private bundle, model assets, credentials or keys.

## Primary references

- Pinned Inno release: https://github.com/jrsoftware/issrc/releases/tag/is-6_7_4
- Inno license: https://github.com/jrsoftware/issrc/blob/is-6_7_4/license.txt
- Inno install callback handling: https://github.com/jrsoftware/issrc/blob/is-6_7_4/Projects/Src/Setup.MainForm.pas
- Inno two-phase uninstall: https://github.com/jrsoftware/issrc/blob/is-6_7_4/Projects/Src/Setup.Uninstall.pas
- Inno temporary payload extraction: https://github.com/jrsoftware/issrc/blob/is-6_7_4/Projects/Src/Setup.ExtractFileFunc.pas
- Windows sharing rules: https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
- Job accounting: https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information
- Querying an existing owned job: https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject


## Confirmed bootstrap cleanup

The setup helper holds the existing exclusive installation lease and records
its own bounded bootstrap receipt before starting the offline installer. It
uses the same private Windows Job launcher for the selected installer and every
pip or ensurepip descendant. A successful direct-child exit is insufficient:
the helper retains exact process handles until every observed Job member is
signaled, then closes the owned handles before removing its own unchanged
receipt.

If native launch returns no handle, the cleanup deadline expires, the receipt
changes, or the helper dies, the receipt remains a conservative veto. The
current and retained application directories and their original recovery
receipts stay in place. No rollback, repair or uninstall is inferred safe from
a PID, elapsed time or disappearance of the setup window. Both the native
launcher before Python startup and ordinary managed application entries refuse
work while that veto exists. Preserve the folder and use the matching verified
setup in a new program folder; there is no automatic receipt reset.

Ordinary confirmed bootstrap failures still restore the original application
and original receipt bytes. The source filesystem suite retains its existing
ENOSPC, foreign-file, user-data and rollback assertions. Separate Windows cases
exercise direct-child exit with a live descendant, uncertain cleanup, actual
helper death, post-create constructor failure, and early native-launch refusal
before site initialization. These cases supplement the existing real Inno
install/repair/uninstall qualification and do not replace it.
