# Standard per-user Windows installation

This is the compatible port of PR152 commit
934b632f98098d55253354e0c6f7d8d482b73461 into the current application. It
wraps the existing complete, sealed offline bundle with a pinned Inno Setup
installer. It does not add a dependency resolver or a second runtime layout.

## Installation layout

The default program directory is LOCALAPPDATA/Programs/Resonance:

| Path | Purpose |
| --- | --- |
| app/ | Exact sealed bundle and its offline-created .venv |
| maintenance/ | Matching setup and captured maintenance sources |
| managed-install.ini | Installation state and exact receipt identities |
| owned-files.json | Complete admitted file and directory inventory |
| installation.lease | Persistent native coordination file |

The user's libraries remain in their existing, separate data directory.
The wrapper registers per-user launch, repair and uninstall shortcuts and
the corresponding Windows installed-program entry. No elevation or
automatic closing of the application is requested.

Only the exact matching setup can repair this installation. An upgrade
requires a separately reviewed ownership protocol; the portable updater
must not change managed program files behind these receipts.

## Shared process ownership

The standalone maintenance copy captures the current audio/windows_job.py,
audio/cleanup.py, audio/models.py and audio/process.py unchanged. They are loaded
as one private package so relative imports resolve without importing the
not-yet-installed application. The setup receipt covers all four exact sources.

The existing run_process function admits a private Windows Job at creation and
drains both PIPE streams under its fixed diagnostic budget. Bootstrap selects
owned_windows_job=True, a 1,800-second deadline and a 4 MiB diagnostic allowance.
Normal completion, timeout, startup failure and cancellation retain native
ownership until the same owner confirms that the entire family is empty.

When native shutdown temporarily fails, maintenance reports a fixed waiting
status and retains its synchronous helper, diagnostic streams and exclusive
installation lease. It waits on the shared cleanup owner's quiescent_event.
It cannot move the program, restore the previous tree or release the lease
while that tree might still be executing. An interruption received while
waiting is delivered after quiescence. This is intentional retention, not
a successful installation result or an unsafe timeout escape.

Application entrypoints use the shared installation_lease module. The
wrapper holds an exclusive native handle for its whole transaction; its
fixed Python helper duplicates and verifies that exact handle. No process
is found or terminated by a machine-wide PID search.

## Repair and uninstall

Repair verifies the selected bundle and maintenance identities first.
Before moving the current app, it reserves a sibling recovery directory and
copies the old receipts. Every original app file, including changed or
unknown files, moves into that recovery directory together. The new bundle
is installed at its final path through the existing install_windows.py.

A bootstrap or publication failure retains the incomplete new tree and
restores the complete previous tree and receipts after native shutdown.
Disk-full errors before the first move leave the old app in place. A
successful repair also retains the old tree for the user to inspect.

Uninstall removes only the exact unchanged owned inventory. Modified,
extra, linked or redirected paths refuse removal. Research data and sibling
recovery directories are never part of the deletion inventory. The
coordination marker and lease remain after normal uninstall.

This is a cooperating same-user lifecycle boundary. It does not defend
against an administrator or arbitrary hostile code already running as the
same user.

## Preparing and compiling

First build the current full bundle with its current exact dependency lock,
source-bound wheel, SBOM, Python archive, OCR and corresponding source,
model inventory, full FFmpeg runtime, and native --launcher-dir output.

prepare_inno.ps1 downloads only the reviewed toolchain artifact named in
toolchain.lock.json. It checks size, SHA256 and the expected valid publisher
signature before installation. Build and compiler directories must remain
outside Git worktrees.

    python -I -B -X utf8 packaging/windows_installer/build_setup.py --bundle <complete-bundle> --bundle-manifest-sha256 <exact-sha256> --output <private-build> --iscc <verified-ISCC.exe> --toolchain-receipt <toolchain-receipt.json>

For an already installed CI bundle, add --installed-bundle. This excludes
only its existing .venv from capture; every other file must still match the
complete sealed inventory. Extra private files are refused. The installed
environment is rebuilt from the existing wheelhouse at the target path.

The builder verifies the current managed entrypoint/lease sources in the
application wheel. It captures the admitted bundle, fixed helpers and Inno
template; the receipt binds source commit, bundle hash, helper hashes and
compiled setup hash. Compilation alone reports BUILT_NOT_INSTALLED.

## Required Windows acceptance

windows_ci_steps.yml is a fragment for the existing complete-bundle job.
It must follow the source-bound native-launcher build and the existing
full offline installation and native runtime checks. The acceptance script
uses the actual compiled setup and installed wheel to verify:

- fresh per-user registration and exact shortcut targets;
- refusal to overwrite foreign Start-menu content;
- the native launcher and actual installed application diagnostics;
- maintenance refusal while an actual application lease remains held;
- retention of modified and foreign files through repair;
- actual repair and uninstall, with external populated SQLite library
  bytes preserved;
- completion of Inno's second-phase uninstaller before closing its owned
  Job, so the wrapper can remove itself normally.

The native probe reuses the same JobProcess API and checks its owned
_job/_active_processes state. It never closes the first-phase job merely
because the parent process exited.

Portable source regressions cover filesystem receipts, real cooperating
leases, recovery faults and retained native cleanup using explicit doubles:

    python scripts/ci_check.py tests/packaging/test_standard_installer.py
    python scripts/ci_check.py tests/packaging/test_native_launcher_bundle.py
    python scripts/ci_check.py tests/packaging/test_windows_bundle.py

The source doubles do not qualify Inno or native Windows behavior. This new
port is NOT_RUN until the composed candidate's native gate passes. The
native report keeps Authenticode signing, NVDA, separate unprivileged-user
qualification and native egress tracing explicitly unverified.

Keep setup/runtime/model/compiler binaries outside the repository and
outside public artifact uploads. The workflow may upload only its exact
JSON/text evidence allowlist. A final private installer is created only
from the reviewed final source and approved private inputs.

## Canonical composition and delivery

This port preserves the canonical 15-wheel core and 51-wheel semantic profiles.
The optional six-pin trusted-client profile is separate; distribution, wheel
closure and installed qualification must include every extra that is actually
selected. No previous 17/53-wheel donor profile replaces these locks.

The reviewed source-built launcher is selected with the existing builder's new
`--launcher-dir` alternative: its PE/build receipt is checked against the exact
committed C-source inventory and then sealed into the bundle. The old fixed
portable executable remains an explicitly separate supported input. A new
managed setup must contain the source-bound launcher and its declared manifest
inventory. It cannot claim managed launch exclusion from the old portable EXE.

A refreshed owner bundle can receive the already-tested same-source PE/receipt
through `refresh_private_bundle.py --launcher-dir`; it does not rebuild them.
A retained source-built launcher is rechecked against the newly selected source.
The original parent and its independently recorded manifest hash stay required.
No audio-transfer or final source-wheel acceptance guard is removed.

The native installer probe retains the same actual Job handle until Inno's
second-phase uninstaller has completed. Its probe-only readers drain the
canonical PIPE interface to a bounded diagnostic file. This is test adaptation,
not a replacement process supervisor or a production acceptance result.
