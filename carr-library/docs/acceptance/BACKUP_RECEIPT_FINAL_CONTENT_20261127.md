# Final backup receipt content attestation — 2026-11-27

## Observed required failure

The exact `18a61e7ec6601119f9a4ffa7228274e836856ca4`
[Windows Audio job 140647933992](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46040854423/job/140647933992)
completed 80 strict suites with 1,588 passes, no failures and no skips, plus
31 installed translation-loopback cases. Its later broad gate reported
1,603 passed, **2 failed**, 6 explicit platform/device/optional-resource skips
and 22 passing subtests. The final installed self-test was skipped after the
failure. These overlapping stage counts are not a unique aggregate.

The two failures were the existing
`test_final_quiet_native_pins_reject_mutations_after_second_content_pass`
cases `earlier-payload-later-file` and `earlier-payload-last-callback`.
Both failed because the expected changed-during-verification ValueError was
not raised. The other 26 cases in that receipt suite passed.

The log records that verdict, not a dump of native timestamps or the fixture's
post-failure mutation flags. The diagnosis below follows the source boundary
and its real-byte negative controls; it is not a direct timestamp measurement.

## Source boundary and correction

The first walk records each admitted file's size, SHA-256, ordinary stamp and
native change pin. A second bounded pass re-attests content while honoring the
caller's cancellation/deadline callback. A later callback can alter a file
already read by that second pass. Previously, the final quiet pass checked
only stamps/native pins and directory names. Equal metadata cannot authenticate
the recorded payload digest.

Reviewed candidate `b741052dd58f5786774fe0144294d33ccbbe73ef` adds content
attestation to that existing final quiet pass. For each file it:

1. Checks the opened file's recorded ordinary/native identity.
2. Reads exactly the previously admitted size in chunks of at most 1 MiB.
3. Checks one additional EOF byte, the recorded SHA-256 and final handle metadata.
4. Retains the existing final native path pin and bounded directory-name checks.

The helper neither writes nor restores the backup, changes no receipt schema
or digest format, and adds no accumulating open-handle set. A short read, extra
byte, changed digest, changed identity or changed namespace remains a refusal.
All existing admission limits and cancellation/deadline checks before this
phase remain intact.

The final phase deliberately invokes no external callback. It now costs one
additional read of every previously admitted byte, plus at most one EOF byte
per file. Its work is bounded by the already approved byte/file inventory;
cancellation through the external callback is observed before this phase and
during the two earlier passes. This is a finite verification boundary, not a
claim of instantaneous cancellation during the final read. No arbitrary new
time limit or capacity profile has been introduced.

## Regression and required execution

The original 28 cases, including all six late-mutation IDs and their refusal
assertions, remain present. Two added cases perform actual same-size byte
corruption while controlling only ordinary/native metadata to repeat on either
platform. They verify the changed SHA after refusal; hashing and reads are
not mocked. There are **30 expected receipt cases**.

The existing Audio strict source list now includes this suite first (80 to
81 suites). A separate exact-installed offline run uses the existing strict
runner for the same receipt suite. All former suites, the installed loopback
policy, native workflows and final broad gate remain required.

At preparation time the new product helper, two deterministic controls and
their composed source/installed gates are **NOT_RUN**. Earlier native Audio
success does not qualify this later receipt correction. Actual platform
execution must replace that preparation status in the PR's execution matrix.
