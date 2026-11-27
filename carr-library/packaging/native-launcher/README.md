# Native Windows launcher

This directory reuses the native entrypoint from PR152, commit
934b632f98098d55253354e0c6f7d8d482b73461, for the current application bundle.
It adds ResearchLibrary.exe alongside the existing command launchers. The
Windows installer remains the current hash-locked offline Python installer.

## Current bundle contract

The launcher locates its own local program directory. It starts only the
bundled runtime or the installed .venv interpreter, with fixed -I -B -X utf8
arguments and an explicit working directory. It does not search PATH for
Python, accept arbitrary commands, download dependencies, elevate, or delete
an incomplete environment. Diagnostic options are --launcher-check,
--self-test and --check-installation.

The complete bundle still contains the existing OCR runtime and models,
the MiniLM inventory when selected, and the full sealed FFmpeg runtime at
audio-runtime. The application resolves FFmpeg through audio.runtime; the
launcher leaves PATH unchanged. The current citation engine is QuickJS.
This port does not introduce the donor's separate Node runtime or audio/bin
layout.

The launch environment preserves the existing offline/telemetry settings
and supplies CARR_PROGRAM_DIR and the bundled OCR location. A managed
installation under app also holds the shared installation lease until the
Python child exits. Application entrypoints and operations retain their own
cooperating lease while active. A portable bundle without a managed marker
retains its existing behavior.

## Build inputs and receipts

toolchain.lock.json pins the reviewed Zig archive by size and SHA256.
build_launcher.py verifies that archive and the compiler executable selected
from it before compiling. Use a private output directory outside every Git
worktree:

    python packaging/native-launcher/build_launcher.py --zig <verified-zig> --toolchain-archive <pinned-archive> --output-dir <private-output> --build-probe

The build emits ResearchLibrary.exe and build-evidence.json, with source and
PE hashes. --build-probe also creates the explicitly synthetic child used
only by native CI. build_msvc.ps1 is an alternative for an operator-selected
MSVC development environment; its receipt records the actual toolchain
hashes and versions. These are unsigned private candidates.

Pass the native output to the existing bundle builder as --launcher-dir.
The builder requires bundled Python and a full source commit, compares the
launcher source receipt with that exact Git commit, validates PE structure
and hashes, and seals only ResearchLibrary.exe plus the exact build receipt.
Synthetic children, compiler files and caches never enter the bundle.
All previous wheel/source, dependency, SBOM, model, OCR and FFmpeg checks
continue to apply.

## Acceptance and evidence

The Python suite tests receipt/source mismatches, missing inventory,
changed binaries and unannounced launchers with inert synthetic PE bytes:

    python scripts/ci_check.py tests/packaging/test_native_launcher_bundle.py

Native Windows acceptance must compile this exact source, run
probe_windows.py against the actual launcher and synthetic child, and run
probe_installed_windows.py against the actual installed complete bundle.
The standard-installer acceptance additionally exercises this launcher
during real install, repair and uninstall.

windows_ci_steps.yml is an integration fragment for the existing Windows
workflow. Keep binaries, models, compiler payloads and caches under
RUNNER_TEMP. Upload only the selected JSON/text reports, never a bundle or
executable. Source tests, a cross build and a synthetic process probe do
not establish signing, NVDA behavior, model quality, an unprivileged-account
installation, or native network isolation. New port execution remains
NOT_RUN until the exact composed Windows CI candidate passes.
