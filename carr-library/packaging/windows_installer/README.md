# Standard private Windows installation

This component adapts the standard installer from PR99 commit
fd0cca2405f33c813c4cf43c9661edf1f2e93a55 to the current application.
It wraps the existing sealed offline bundle with the pinned Inno Setup
toolchain. The existing offline installer, source-wheel verifier, runtime
layout, dependency locks and transfer eligibility checks remain authoritative.

## Installation and maintenance

The per-user installation owns only its selected program directory:

| Path | Purpose |
| --- | --- |
| app/ | Exact sealed bundle and its offline-created .venv |
| maintenance/ | Matching setup and captured maintenance sources |
| managed-install.ini | Installation state and receipt identities |
| owned-files.json | Exact admitted files and directories |
| installation.lease | Persistent native coordination file |

Libraries remain in their separate research-data directory. The wrapper
registers the application, Repair and Uninstall in the Start menu and Windows
installed programs. It does not request elevation or stop unrelated processes.

Use the matching setup for Repair. It retains the entire previous application,
including changed or unknown files, in a sibling recovery directory before
rebuilding at the final path. A failed bootstrap restores the previous tree and
receipts after all owned child processes have stopped. Uninstall refuses
modified, extra, linked or redirected files; it removes only the unchanged
recorded program inventory. Research data and sibling recovery directories are
never deletion targets.

## Source and process ownership

The builder reuses the current complete source-wheel verifier against the
exact manifest commit before creating output. It checks the complete sealed
file inventory and compares the lease, passive identity contract and four
native-owner helper copies against the same captured, source-verified wheel.
The generated template, bundle and compiled setup retain their recorded
hashes. These hashes
do not establish publisher signing or authorization.

The fixed helper copies the current audio/process.py, windows_process.py,
models.py and cleanup.py unchanged and imports them as one private package.
It uses the existing owned_process boundary; no second process supervisor is
introduced. A regular binary log stream and the exclusive installation lease
remain held through constructor failures, timeouts and every causal pending
cleanup owner. File replacement, rollback and release wait for actual family
quiescence. A successful child followed by a cleanup retry is accepted only
after that barrier; the original operation failure remains a failure.

Ordinary managed application entrypoints hold a shared native lease for their
whole operation and through registered or causal pending native cleanup.
The standard launcher also holds its prelaunch shared handle until the child
exits. Maintenance needs the exclusive lease. Before normal readiness, the
unchanged offline installer may run only its two fixed checks: the primary
--self-test and native_checks --bundle-root <this exact program directory>.
Each entrypoint opts into its own argument shape; extra arguments, another
bundle root and ordinary launches remain refused. Neither diagnostic opens
the desktop or a user library. Portable installations do not create this
managed metadata.

## Preparing a setup

Prepare a current, source-bound CORE bundle with the existing pinned runtime,
wheelhouse, SBOM and native launcher. Keep setup/runtime/compiler binaries and
build output outside Git and public artifact uploads.

prepare_inno.ps1 accepts a new toolchain directory. It admits only the exact
official artifact in toolchain.lock.json after size, SHA-256 and expected
publisher-signature checks.

    python -I -B -X utf8 packaging/windows_installer/build_setup.py --bundle <sealed-bundle> --bundle-manifest-sha256 <exact-sha256> --output <new-private-build> --iscc <verified-ISCC.exe> --toolchain-receipt <toolchain-receipt.json>

For a previously installed CI bundle, --installed-bundle captures only its
sealed payload, excluding the existing .venv. Every other extra file is
refused. The environment is rebuilt from the existing offline wheelhouse at
the final program path.

Compilation reports BUILT_NOT_INSTALLED. The actual Windows probe must
complete before the composed installation is described as validated:

    python -I -B scripts/windows_standard_installer_smoke.py --setup <compiled/ResonanceSetup.exe> --build-evidence <setup-build.json> --work <fresh-private-work> --report <new-evidence-directory>

The probe uses real Inno, the real offline runtime, the native launcher,
actual per-user registration and shortcuts, a real populated SQLite library,
live lease exclusion, Repair and Uninstall. Source tests use clearly labelled
synthetic bootstrap/PE bytes for filesystem faults; they do not substitute
for that native gate.

## Scope and remaining evidence

The donor's managed CORE A-to-B workflow succeeded on its own exact source.
This adaptation and its new regression/native gates are NOT_RUN until the
receiving CI executes this composed source. No donor result qualifies the
current wheel or installer.

This slice enables explicit local Install, Repair and Uninstall. It does not
add an entitlement authority, TUF client, protected update anchor or automatic
update activation. The passive managed-upgrade identity format remains data;
authenticated A-to-B installation must use the existing separately supplied
private provider and its external maintenance host.

Packaging as an EXE does not change audio/model transfer eligibility.
Publisher configuration, Authenticode for the delivered setup, private
authority qualification, NVDA, separate unprivileged-account acceptance and
native network tracing remain separate evidence.
