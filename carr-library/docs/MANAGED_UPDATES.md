# Standard managed updates

The existing portable updater continues to reject an `app` directory beside
`managed-install.ini`. A standard installation also owns a cached setup, an
uninstaller, a fixed per-user registration and three Start-menu shortcuts.
Replacing only `app` cannot update or recover that complete installation.

`security.managed_installation.ManagedUpdateTransaction` uses the existing
offline TUF client, complete-library backup, protected Windows vault anchor and
shared owned-process runner. It does not configure a publisher, trust root,
endpoint, renewal policy or signing key. The normal client review dialog still
prepares a verified package; applying it requires an external idle maintenance
host outside the program directory. That host must be supplied by the explicit
installation integration and must not stop an unrelated application or job.

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
