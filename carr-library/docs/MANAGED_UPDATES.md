# Standard managed updates

The existing portable updater continues to reject an `app` directory beside
`managed-install.ini`. A standard installation also owns a cached setup, an
uninstaller, a fixed per-user registration and three Start-menu shortcuts.
Replacing only `app` cannot update or recover that complete installation.

`security.managed_installation.ManagedUpdateTransaction` uses the existing
offline TUF client, complete-library backup, protected Windows vault anchor and
shared owned-process runner. It does not configure a publisher, trust root,
endpoint, renewal policy or signing key. The configured client now checks a signed offline repository or one explicitly
configured HTTPS site, reviews and downloads its exact target, and hands Apply
to an external maintenance runtime after the idle desktop accepts normal Close.
The runtime can be prepared from the independently pinned installed distribution
through the existing offline installer. It stays outside the replaceable program,
research library and recovery directories. It never stops an unrelated process.

## Captured transaction

1. The configured TUF client verifies the independently pinned publisher root,
   metadata, version, expiry, product/channel/schema, target length and digest.
   The target has exactly `ResonanceSetup.exe` and `setup-build.json`.
2. The external host acquires its idle guard and the actual exclusive
   installation lease. A live application holds a shared lease and refuses this
   admission. A PID or pathname alone cannot stand in for that lease.
3. The transaction captures A's original marker, ownership receipt, fixed
   uninstall files, exact registry values and three shortcut byte strings. Its
   data-only proof binds the selected A and B setup/bundle hashes to one root
   and one random transaction. The complete research backup is verified by the
   existing backup implementation before activation.
4. The exact B setup receives a duplicate of the live lease. The fixed helper
   verifies its final path and exclusive sharing mode. A shared application
   handle cannot be relabelled exclusive. A's `app` and maintenance cache move
   to a retained sibling; B bootstraps at its final application path.
5. The marker remains `updating` while Inno installs its wrapper. The external
   owner waits for the exact process family to stop before health checks,
   recovery, stream closure or lease release. No ordinary application may enter
   a pending environment. State and readiness are read under its shared lease.
6. B's complete inventory, source/wheel identity, runtime self-test, package
   version and native wrapper must match. Only then does the protected anchor
   advance. The complete marker is the final application admission step.

Before the anchor advances, a failure restores all of A's app/cache, ownership,
uninstaller, registration and shortcuts. Failed B files remain in a separate
retained sibling. An absent or empty failed registration can be captured as
inert evidence and repaired. Recovery writes and flushes the exact original
`InstallLocation` first, so an interrupted replay can be recognized on retry.
A nonempty registration with another or missing root binding requires review;
it is not treated as permission to overwrite unrelated values.

After the protected anchor advances, recovery checks and completes B. It never
lowers the protected build or pretends that returning to older code preserved
the accepted authority. A and the research backup remain available for reviewed
recovery. The transaction never restores, retargets or deletes the live research
directory as a side effect of replacing program files.

## Source-bound transfer target

`packaging/windows_installer/create_update_target.py` accepts a complete local
`build_setup.py` output and an exact expected source commit. It rechecks the
sealed payload manifest, application wheel, template, helper inventory, compiled
setup and receipt. It calls the existing delivery audio-inventory gate before
creating any transfer target. Both `audio/` and `audio-runtime/` layouts, and
audio flags without those paths, remain refused until their existing rights
requirements are satisfied. Wrapping those bytes in an EXE does not authorize
their transfer. Use an eligible core bundle and configure local audio separately.

```powershell
python packaging/windows_installer/create_update_target.py `
  --prepared C:\Build\ExactSetupB `
  --source-commit <exact-40-character-commit> `
  --output C:\Build\NewUnsignedTarget
```

The new directory contains `managed-update.zip` and `target-build.json`. The ZIP
has exactly the setup and its receipt. Final cancellation, changed compiler
inputs, mutated staging and an occupied output destination refuse publication.
The report is an unsigned build record, not TUF approval, Authenticode, native
acceptance or a production release. The existing independent publisher must
authorize the exact ZIP digest in its own signed repository metadata.

## Validation and native gate

The portable suites use actual official TUF verification, signed synthetic local
repositories, real filesystem backups and actual operating-system leases. The
setup executable, bootstrap and registry in those tests are explicitly synthetic.
They cover A→B, complete-wrapper rollback, missing-app recovery, committed-anchor
recovery, live application exclusion, interrupted registry replay, exact legacy
helper inventories, opaque-setup audio transfer refusal and final publication
races. They do not establish native Inno acceptance.

`scripts/windows_managed_update_smoke.py` requires two distinct real compiled
setups in an otherwise clean isolated Windows account, the exact prepared B
directory, a source-bound installed host wheel and the optional hash-locked
trusted-client dependencies. It installs A, creates an original synthetic
library, verifies actual app-lease exclusion, applies real B with one explicit
post-health refusal to prove full A wrapper recovery, applies B successfully,
verifies/restores its research backup, then uses B's actual Repair and Uninstall.
The temporary Windows vault anchor is removed only after the successful native
flow; an interrupted probe keeps its recovery identity and protected anchor for
review. Synthetic publisher keys stay in memory and are not production keys.

```powershell
python -I scripts/windows_managed_update_smoke.py `
  --setup-a C:\Build\SetupA\compiled\ResonanceSetup.exe `
  --build-a C:\Build\SetupA\setup-build.json `
  --prepared-b C:\Build\ExactSetupB `
  --source-a <exact-A-commit> --source-b <exact-B-commit> `
  --work C:\NativeProof\NewManagedWork `
  --report C:\NativeProof\NewManagedReport
```

The probe must actually finish on Windows before `managed-update.json` can say
PASS. Native setup/update, Authenticode, production publisher configuration and
NVDA are separate evidence. The source implementation and Linux regression
results do not turn those pending acceptance requirements into success.

## Selective canonical integration

This source reuses PR99's managed transaction and exact two-file target format.
Its current process helper inventory consists of the unchanged canonical
`windows_job.py`, `cleanup.py`, `models.py` and `process.py` sources.
Only the fixed previous seven/eight-file wrapper inventories are additionally
admitted for retaining installation A; their helpers are not imported as B's
bootstrap. B's build receipt must contain the exact current helper set.

The new composed source and actual managed A-to-B run are not qualified by
earlier signed-client tests. Each immutable A/B checkout supplies its own
reviewed dependency lock and exact application/native source identity. The
current canonical core has 15 pins; a donor's 17-pin count is not substituted
for it. The final native gate must execute real A installation, actual B
activation, deliberate post-health rollback, successful retry, Repair,
Uninstall, retained library restoration and protected-anchor readback.

## Immutable development baseline

The managed gate pins installation A to
`665acf2c1fdb5a30a05ef31c482eb02dcaf1e141`. Its own application wheel,
native launcher, bundle builder, maintenance helpers and setup are rebuilt from
that exact source; no files from B are substituted into A.
[Run 45985890211, job 140461281613](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45985890211/job/140461281613)
actually installed this A and completed the native update, rollback, Repair and
Uninstall sequence with B `55725629bc0cb79b62f7f3979842a573d5fd868f`.
This is a development acceptance baseline, not a production release or a claim
that the current B has already passed.

The previous test baseline `4c3632fb1baac4705059e344b6c952f7760c4f00`
produces `bundled_launcher` and `bundled_audio_runtime` fields that its own
native installation checker rejects. Its retained native log shows bootstrap
exit 1 followed by setup exit 7; that helper did not retain the child diagnostic
output. The exact source mismatch is proven, while the discarded child message
is not reconstructed as observed evidence. The selected historical correction
accepts and type-checks those fields without filtering the sealed manifest.
The current A-to-B pair still must pass every mandatory native phase with
independently checked source, wheel, launcher, setup and bundle identities.


## Configured Check, Download and Apply

No endpoint, signing key, publisher root or protected anchor is selected by
default. An unconfigured installation retains reading, saving, export, backup
and recovery, and explains why Apply is unavailable. The owner configuration is
a data-only JSON file selected with an independently obtained SHA256. Computing
a checksum from an unknown file does not establish independent publisher trust.

The Help command **Check for updates / Update** opens the existing signed-client
status dialog. The user can select an offline TUF repository or check the one
approved HTTPS site. The review shows product, channel, version, build, source
commit, size, target SHA256 and signed release notes. Download or resume runs
on the existing desktop worker and reports received bytes as unverified. It
returns a package capability only after the complete signed target passes.
Cancel current operation stops the owned transfer. A private partial prefix may
be reused, but never becomes an approved package without full verification.

HTTPS uses a fixed worker and the current owned process/Job mechanism. It
requires certificate verification, ignores proxy environment settings, refuses
redirects and foreign URL authority, bounds response framing and requires exact
Range responses when resuming. A server that ignores Range can restart the
private prefix. The existing TUF bootstrap, root rotation, metadata expiry,
product/channel/schema/build rules, signed length and digest remain authoritative.

Background checks are off by default. An explicit default-No checkbox review
allows at most one metadata check per hour while idle. Consent is bound to the
exact owner configuration digest. The timer borrows the same serial executor,
does not move focus or open dialogs, and never downloads or applies. A new
foreground operation, a changed library, Close or the finite 60-second background
deadline cancels the attempt. License renewal remains a separate setting.

### Prepare the external host

An explicit **carr-managed-update-provision** recipe prepares an eligible standard
installation's maintenance host. A must already contain this implementation,
bundled Python and the existing hash-locked trusted-client dependencies.
Preparation refuses missing dependencies instead of fetching or inventing them.

The recipe has exactly these fields:

| Field | Required value |
| --- | --- |
| schema, kind | Integer 1, string carr-managed-update-provision. |
| runtime | New absolute root and new absolute external manifest file. |
| installation | Existing standard installation root and existing recovery work on the same filesystem. |
| library | Existing selected research-library path. |
| source | Independently approved A bundle_sha256, owner_sha256, setup_sha256; these must match A's complete marker and ownership receipt. |
| publisher | product, channel, base64 bootstrap, bootstrap_sha256, current_build, data_schema, max_package_bytes, max_unpacked_bytes, max_files: the existing UpdatePolicy fields. |
| repository | endpoint (one HTTPS base URL ending in /, or null), exact relative target, tls_ca_pem (null for platform trust, or explicit PEM), timeout (integer 1–3600 seconds). |
| anchor | Existing Windows vault reference, exact binding and profile_id. Provision independently; this path never creates or resets the protected anchor. |
| close_timeout | Integer 1–300 seconds for the exact Close/installation-lease handoff. |
| operation_timeout | Integer 60–7200 seconds. Required native cleanup and rollback keep ownership until settled. |
| configuration | New external output file for the derived ready-host configuration. |

Runtime, current program, work and library cannot overlap. The recipe and new
receipts are outside all four. Existing destinations, symbolic links, reparse
points, hardlinked payloads, changed ancestors and altered source hashes refuse.

Only A's exact manifest-declared distribution and eight fixed maintenance helpers
are copied. The active .venv, research data, private model/reference/voice and Node
directories are not copied. The closed exception for the thirteen observed pip
implementation modules under runtime/Lib/site-packages/pip/_internal/models and
their exact CPython311 bytecode files still requires A's independent bundle and
ownership hashes. It does not admit other files or any private models directory.
The existing offline install_windows.py creates a
new environment at its final external path inside an owned Windows process
family. The complete new runtime is sealed; its manifest and configuration are
published without replacing a competitor. A failed attempt remains at its owned
destination for review and leaves A and the selected library in place.

The configured desktop exposes **Prepare update host** with a default-No review.
The packaged command performs the same operation:

~~~powershell
& 'C:\Apps\Resonance\app\.venv\Scripts\python.exe' -I -B -X utf8 -m carr_library prepare-update-host --recipe 'C:\OwnerPolicy\update-host-recipe.json' --recipe-sha256 '<independently supplied recipe SHA256>'
~~~

It returns the generated configuration path and digest without applying an
update. Subsequent launches explicitly select that ready configuration:

~~~powershell
& 'C:\Apps\Resonance\app\.venv\Scripts\python.exe' -I -B -X utf8 -m carr_library --data-dir 'D:\Research\Library' --update-configuration 'C:\OwnerPolicy\prepared-update-host.json' --update-configuration-sha256 '<digest returned by verified preparation>'
~~~

For first interactive preparation, the same configuration arguments can select
the approved recipe. The current controller switches to the generated
configuration after successful preparation. Future launches use that generated
file; replaying a recipe whose output already exists is refused.

The generated kind is **carr-managed-update-host**. It retains the same
installation/library/publisher/repository/anchor/deadline fields, removes source
and configuration, and replaces runtime with root, external manifest, its exact
sha256, relative python and relative maintenance. Normal preparation selects
app/.venv/Scripts/python.exe and maintenance/maintenance.py.

### Close, Apply and recovery ownership

After a verified download, **Close application and apply this update** is a
separate action with Cancel as default and optional restart unchecked.
Preparation reverifies the exact signed release, copies only authenticated
metadata and its target into a private offline repository, verifies the entire
sealed runtime and starts one fixed supervisor. For a Windows virtual environment,
the launch uses the base python.exe named by its sealed pyvenv.cfg and sets
CPython's internal __PYVENV_LAUNCHER__ binding to the exact configured environment.
The base executable, launcher and configuration must remain inside the same
sealed runtime and retain their captured hashes and directory/file identities
before and immediately after launch. Inherited interpreter overrides cannot
select them. This preserves installed environment imports while making the
retained process ID the actual supervisor ID. READY still requires that exact
live process, request and release. No installer or network child starts before
COMMIT.

READY means only that this exact supervisor, request and release are retained.
The parent registers precommit cleanup on the existing DesktopExecutor. One
ownership lock spans actual frame.Close() and durable COMMIT so queued Close
cleanup cannot abort between them. Unsaved-work refusal or a stale review
cancels and reaps the owned supervisor. No forced destruction substitutes for
normal Close.

The committed host waits for the actual existing exclusive installation lease.
A PID, process name, sleep or apparently closed window does not substitute for
that lease. The unchanged ManagedUpdateTransaction then owns the complete
library backup, writer boundary, full-wrapper activation/health, protected
anchor, final marker and any rollback/recovery.

The UI records HANDED_OFF, not activation success. A possibly visible COMMIT
with failed acknowledgment is HANDOFF_UNCERTAIN: parent cleanup must not kill a
potentially committed owner. An absent decision expires without setup. The
private handoff result.json and existing managed-activation.json report actual
outcomes; a postcommit exception does not imply rollback.

Explicit restart launches only the owned installed Python entrypoint whose
complete inventory and marker identities still equal the completed transaction.
The fixed native launcher remains unchanged and accepts only its diagnostic
switches; configured arguments use the existing module entrypoint with its
actual application lease. It carries the exact library
and ready configuration arguments and reports STARTED_NOT_QUALIFIED until the
new application itself is observed.

Recovery uses the same sealed environment and existing transaction:

~~~powershell
& 'C:\UpdateHost\app\.venv\Scripts\python.exe' -I -B -m carr_library.security.update_host --configuration 'C:\OwnerPolicy\prepared-update-host.json' --configuration-sha256 '<approved configuration SHA256>' --recover
~~~

This is application-update orchestration. Model/language packs, a schema
migration, publisher-key change or owner-anchor change still need their separate
explicit authority; an application target does not grant those permissions.

### Additional regression boundary

Current portable inventory: handoff/transaction/real-lease 53, preparation 40,
actual-handler/shared-executor 15, controller/configuration 14: 122 total.
The added cases exercise real temporary virtual environments, exact PID and
prefix, cancellation/commit ownership, late sealed-input changes, all observed
pip code paths and continued private, unowned, changed or ambiguous path refusal.
A separate 15-case suite uses real local TLS, official TUF and actual transfer
children. It requires the explicit loopback gate and is not admitted through an
all-network-denied runner.

Portable setup/bootstrap/registry are explicitly controlled fixtures. TUF,
filesystem identities, actual backups/rollback, native leases, child lifetime
and shared executor are real where their cases state so. UI cases use the actual
handler AST from the imported source or installed package with controlled native
delivery. They do not establish native keyboard, new-host A-to-B activation,
production TLS, Authenticode, publisher rollout or NVDA.

The added source and isolated-installed suites and actual external-host Windows
acceptance remain separately required. Historical managed-transaction success
does not qualify this newly configured Apply route.

### Compiled configured-update acceptance

The separate `carr-configured-update-native.yml` job builds the immutable baseline
named by CARR_CONFIGURED_SOURCE_A and the exact selected B source. After the
runtime-admission and supervisor-launch corrections, that baseline must itself
contain both fixes; a changed B cannot repair code already installed from A.
The historical A2 source `8f24cae6dbb657854a07dcb801f77296f3b7e4e3` reached the real
preparation review but refused its pip runtime-code paths. It does not qualify the corrected native cycle.
Both distributions use the existing complete 56-wheel trusted-client lock,
original pinned Python, native DLL closure, launcher compiler and standard Inno
setup. Version labels do not replace the source, bundle and installer digests.

The probe runs A's actual installed module entrypoint and existing desktop. It
uses the native preparation question, observes default No, prepares the separate
host after explicit Yes, reviews and downloads one locally signed fixture
release, cancels an Apply review, then explicitly selects restart and applies.
The repository chooser selects one owned synthetic directory; this does not
qualify the operating system's file-picker accessibility.

After normal Close and durable handoff, the original managed transaction must
install B and retain A's complete program, uninstaller, registration snapshot and
cached setup. The original research must remain exact, and the actual backup
must restore it. The restarted installed Python process and its visible desktop
must belong to the same retained Windows Job, expose the configured update
route, hold the actual installation lease and leave that family quiescent after
ordinary Close. A returned process ID alone cannot satisfy this check.

The job requires all 15 native UI checks, all 10 transaction/restart checks and
15 exact installed A module origins, with complete source and isolated B package
proofs before and after the work. The fixed acceptance programs have outbound
firewall rules with checked restoration. This is not a claim that every internal
temporary Inno executable has an independently installed rule.

Only bounded text and JSON reports can be retained. Compiled distributions,
runtime copies, synthetic signing keys, the private recipe, targets and research
fixtures stay in the runner's private temporary tree. The real Windows vault
anchor belongs only to that synthetic run. Production publisher provisioning,
vendor rollout, a real user's installation, Authenticode, NVDA and physical audio
remain separate qualifications. The probe's source review or the portable
122-case and TLS15 results cannot stand in for an actual successful native run.
