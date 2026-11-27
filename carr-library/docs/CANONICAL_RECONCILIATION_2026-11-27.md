# Canonical behavior and data compatibility reconciliation

## Reviewed identities

- Previously incorporated canonical: 1a0cd095563c9d86d9ca280323ff4449816ad3f7.
- New canonical: 966e5ccf892243ead6942c635af5fbab47e72551, merging that
  parent and ff8034940f7b's qualified library/audio preview lineage.
- Canonical advanced again to 3beebd5aca900b8bf2e86498ae79db4015c96705.
  Its two-path PR176 publication-cancellation delta is already present here:
  the exact check after staged fsync/digest and before link, plus the identical
  real-render cancellation/resume regression. No prior verdict is transferred.
- Latest reviewed canonical: 924ae83316a3cf9482aaf2ec51a6e6a5adb614c3.
  Optional-root lookup remains fail-closed; cancellation now runs before each
  optional sidecar root. The real-child publication barriers already in this
  composite remain stronger than the incoming clock-only fixture.
- Authoritative technical scope blob:
  db8adbdc483a03c262885928c7ed214ca70bbaa7, unchanged by this merge.
- Current native correction baseline:
  77093c8fc1d124bc6bb52c93edce69214098adde.

The new canonical source contains alternate implementations of several
already-composed services. Its passing tests belong to its own source, pack
and installed artifact. They do not qualify this composite. Reconciliation
preserves supported behavior, retained data and reviewed guards; matching
filenames or wholesale replacement is not an acceptance test.

## Required adoption and retained contracts

| Area | Canonical behavior to retain | Composite boundary |
| --- | --- | --- |
| Evidence notes | Per-evidence source/revision-bound recovery, immediate first checkpoint, fresh authorization around review, safe stale-baseline review | Existing evidence panel and typed backup; legacy single-note recovery remains readable |
| Save All | Wait for durable note, reader and Studio acknowledgments; preserve newer edits while saving | Existing menu, domain methods and coordinator; preserve the native timer correction |
| Keyboard | Contextual reader commands and stable saved binding identities | One active registry and explicit preference migration; ordinary typing, NVDA keys, collisions and user overrides remain protected |
| Backup input | Exact canonical flat format-4 sidecars and historical reading-state variants | Keep composite format-5 writer, schema-6 publication receipts, bounded inventory and restore reauthorization; discriminate envelopes structurally |
| Voice lifetime | Close an owned registry only after its last selection and active speech finish; close completed voice inventory probes | Preserve code/model identities, owner adapters and detached restored settings; borrowed registries stay borrowed |
| Progress | Current operation identity, cancellation acknowledgment, quiet audio ownership and explicit query | Retain the existing facade and native controls; no competing second progress stack |
| Export approval | Capture the final output path and existing bytes before overwrite confirmation; reject changes during rendering | Keep current cancellation and atomic create-only guards; capture on the existing worker and report durable completion separately from cleanup |
| Source/Unicode/accessibility | Preserve exact Git tree in wheel proof, use explicit UTF-8 for the isolated pronunciation worker, disambiguate case-equivalent spoken scope labels | Reuse canonical corrections without replacing the active stores or command registry |
| Assistant transcription | Reach the existing transcription service through an approved typed workflow | Keep the supplied worker bridge and durable job identity; no competing single-file runner or source/model-quality claim |

## Executed reconciliation checkpoints

At `07e1e7fcdae89d27b6c8a8a1cd62a95e66e0c363`, Windows job
[140381484849](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45961915464/job/140381484849)
passed all four source suites: 19 evidence-store, 10 Save All coordinator,
18 recovery-panel and 33 progress/owned-voice cases. The fresh installed package
passed the same 80 cases, with zero failures, skips or expected failures.
Installed module paths and exact source bytes were checked. Existing native
SAPI execution is included; human NVDA and acoustic review are not.

Checkpoint `d0a4a30a97c75ec90f545b5213776716dbb9badb` composed canonical
backup compatibility, Save All editor wiring, contextual reader keys and the
small source/Unicode/voice-inventory corrections. Windows job
[140391466569](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45964999701/job/140391466569)
passed all 19 source suites and the same 477 installed-package cases, with zero
failures, skips or expected failures in those contract gates. Actual native
reader key dispatch passed 34 source and 35 installed assertions, including
remap, clear, reset, reopen, persistence and protected editing/NVDA keys.

That job still failed both native Save All cancellation probes: the test read
the event before the queued native Space key had been dispatched. The next
probe uses its existing bounded event pump and also requires the coordinator
to remain busy until the held acknowledgment is released. Production handlers,
deadlines and cancellation assertions are unchanged. That correction required
its own successful native result; the preceding successes did not conceal the failure.

It subsequently passed at ee4f293a70696c1c8d166636ea10b7ad9fecc9c2,
[Windows job 140396813140](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45966651590/job/140396813140).
All six native source/installed gates passed: Save All and evidence recovery
completed 46 source and 47 installed assertions, reader key dispatch completed
both full scenarios, and asynchronous Catalog Tools completed 49 assertions in
each lane. Installed Catalog Tools verified actual purelib origins and exact
checkout-byte equality for the package and exercised export modules. The chooser
destination and confirmation were controlled; real chooser interaction and human
NVDA were not qualified.

That job still failed one of 32 source suites and the matching installed cases:
a headless confirmation fixture supplied wx_module twice. The fixture now consumes
that optional keyword before injecting the same test wx; production code, real
writers, cancellation and race assertions are unchanged. Its 32-case module
reran successfully; all 11 portable export suites passed 333 tests without skips.

The complete subsequent compatibility checkpoint
16a18b2aad8faccdeae30aa27a3e32b434a9c0f0 passed
[Windows job 140400094664](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45967649593/job/140400094664):
all 36 required source suites, 1026 installed cases with zero failure/skip/expected
failure, and all six native source/installed gates. This includes the new typed
transcription composition and its existing bridge/recovery boundaries.

The export composition now captures bounded destination contents on the
existing desktop worker, confirms on the UI thread, and publishes through the
existing checked writer. A committed export reports success with a distinct
cleanup warning when staging cleanup fails. Restored legacy voice-helper
declarations remain inert and cannot clear the revalidation marker or activate
an unreviewed runtime. Source and installed contract/native checks for these
changes are required by carr-reconciliation-checks.yml.

The typed audio.transcribe workflow delegates to the existing supplied-worker
service. It persists source/model/configuration identity and submission intent,
refuses unknown submissions, and journals an exact export receipt before
publication. Lost acknowledgments reconcile those bytes without another model
run; a cancellation after durable publication stops subsequent work without
reclassifying the committed export. Current portable Python 3.12 verification
passed the new 24-case integration, 91 existing bridge, workflow-access,
assistant-service and 11 recovery cases. An existing destination-race negative
control first exposed a changed manifest-writing hook; preserving atomic_json
restored that boundary, and both affected suites reran 35/35 without skips.
These source checks do not qualify the final installed Windows artifact or
real Whisper acoustic inference.

The final review additionally found that audio-plan Preview initialized the
configured voice inventory on the GUI thread. The existing single-worker review
queue now owns that lookup; the native chooser remains on the GUI thread, checks
current scope/cancellation/generation around the modal, and preserves explicit
voice assignments on other steps. Closing queues owned-registry cleanup after
active work. Seventeen new real-thread closure regressions passed; the combined
preview/workflow gate passed 68 cases with no skips. The existing native workflow
probe exercises actual configured inventory, Pause and Close. At
b2c171048e1c775859114d3b0f9808de4e10dd68, the subsequent
[Windows job 140405893761](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45969417950/job/140405893761)
passed all 37 source suites, 1043 installed cases without failure/skip/expected
failure and all seven native lanes. The extended native productivity probe
passed 44 assertions, including real configured-voice inventory off the GUI
thread, current approval, Pause/Close responsiveness and once-only cleanup.

All combined source, installed and native gates must pass on the enclosing
commit. Historical checkpoint PASS never substitutes for that complete
aggregate or qualifies another application wheel.

## Resource-admission composition

The final resource composition adds a validated retained-byte allowance
(default 32 GiB) around the existing production service, including private
state/cache, staging and the current job's published copies. Verified cache is
reused without reserving a second copy. Each new output is bounded by the
remaining allowance, and publication checks the actual destination volume's
free space while preserving the configured runtime reserve. An owned registry
closes once after work; borrowed registries remain caller-owned. Completed
output verification is cancellable without reopening rendering.

Retained-file enumeration checks cancellation for every directory entry and
refuses more than 100,000 discovered entries. It rejects links, reparse points
and special files. The two actual-directory negative controls fail against the
prior helper and pass against the bounded helper without modifying fixture
bytes. The existing post-fsync/digest cancellation check before publication is
preserved.

Portable resource evidence comprises 41 distinct passing cases: real
FFmpeg/PCM admission (3), production cancellation/inventory (8), production
hardening (5), existing real production (11), and state/digest (14), with zero
failures or skips. The two affected modules reran through the strict runner
after the final test-only split. The separate actual Linux small-volume
namespace case remains NOT_RUN; the portable destination-reserve policy case
does not claim actual low-disk qualification.

The backup producer accepts optional exact integer total_disk_bytes in
[1 MiB, 2**63-1]. All 48 canonical backup-compatibility cases passed, including
four genuine composite/flat backup and restore roundtrips at both bounds and
nine invalid-type/range refusals. Restored jobs retain the recorded allowance
but lose old output/runtime authority and require a new reviewed destination.

The complete main workflow retains all 241 previous source-suite selectors and
adds the integrated regressions for 259 unique required source modules. Its
installed lane retains the previous 102 contents selectors and now runs 130,
alongside the existing 11 installed core suites. The portable resource module
is mandatory in both main lanes and the V3 gates. These are required
inventories, not a claim that the final combined Windows run has passed.

## Complete df42 execution and reviewed successor

All following results belong only to
`df42d91b547066e8bc578dd67f8e023502447d09`, tree
`c117a267e771dc0060631214a98833f0e9287b03`:

| Workflow | Result | Evidence |
| --- | --- | --- |
| Main Windows | FAIL | [45972600926](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45972600926): 257/259 source suites passed; native/bundle failures listed below |
| V3 | PASS | [45972601005](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45972601005): 580 installed shared-service, 532 installed recovery and 2064 broad source cases; strict progress and native V3 also passed |
| Canonical reconciliation | PASS | [45972600941](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45972600941): 37 source suites, 1056 installed cases, seven native lanes |
| Supplied transcription bridge | PASS | [45972600943](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45972600943): Linux and Windows contracts plus the native Windows bridge; synthetic model API, not acoustic inference |
| Native launcher | PASS | [45972601074](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45972601074): MSVC and pinned Zig process probes |
| Bibliography | PASS | [45972600917](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45972600917) |

The main failures were examined from the completed job log. Two cancellation
fixtures matched only `sys.executable`, although the Windows supervisor owns
the base interpreter; the PDF fixture also started its cancellation clock
before preflight finished. Their replacement retains the actual bootstrap,
selected interpreter and extraction/rendering, then waits for an atomic child
PID acknowledgement while the staged source is open. Cancellation must reap
that exact child in under five seconds after acknowledgement and leave no
published review/backup. The old defects failed their negative controls; all
31 corrected module cases passed on portable Python, without skips.

The standalone typed WinMM preview had naturally aligned x64 parameter blocks.
The Windows SDK includes MCI structures under byte packing. The correction
preserves native-width fields and compares every field offset, size and
alignment with a C probe compiled against the runner's actual SDK before native
playback. Six layout regressions failed against the old source; the 68-case
playback/reader/keymap gate passes after correction. The separate ReadingDialog
probes now address stable command IDs and still assert accessible names and
actual native event behavior. Their old exact-label lookup failed after the
keyboard registry added the configured shortcut to the visible label.

The successor reconciles PR185 submission recovery and PR188 full completed
artifact/owner verification into the current production service while retaining
its resource, rights and lifecycle guards. A real-render negative control
demonstrated that the old replay accepted a shortened inventory. All 34
production cases pass after composition. PR186's three acquisition negative
controls also fail old source; its 285-case provider/security/workflow/CLI gate
passes after the existing writer transaction covers both reference and
collection mutations and rechecks the exact unambiguous edition identity.

Recording reuses the reviewed PR123 capture implementation through current
runtime, worker, immutable assets and progress contracts. It adds bounded
PCM16 capture, Stop/save, Cancel/discard, retakes and native controls; no second
editor or default microphone is introduced. The backup hooks use the same
recording lock, stream PCM originals, validate typed receipts and retake graphs,
and keep unreceipted finals inert. Composite v5/schema6 and supported flat-v4
roundtrips remain real tests. One composed portable gate passes 275 cases
without skips, including current production and backup regressions. These
overlapping component totals must not be added together.

The successor's main source inventory has 265 unique suites and its installed
contents inventory has 137 selectors, preserving all previous entries and the
11 installed core suites. New native gates exercise capture-process ownership,
source and installed wx recording controls, and independently measured PCM from
an explicitly identified virtual input in the dated evaluation environment.
Reports retain physical-microphone, listening and human NVDA as NOT_RUN.
The complete enclosing Windows aggregate remains required; df42 is not an
eligible final wheel despite its successful independent workflows.

## Stronger behavior retained

The incoming preview removes protections already present here. Reconciliation
must keep:

- osd and equ as special OCR detectors, not selectable reading languages.
- Schema6 request/attempt receipts and historical catalog migrations.
- Bounded backup enumeration, unreadable-root rejection, early unlisted-file
  rejection, case-alias checks and observable cancellation.
- The existing prepare_restored_state barrier before optional external work.
- Sealed audio-runtime identity and explicit local installation consent.
- Exact retained sources, translation ledger, supplied private-worker protocol,
  content-bound exports, EPUB publication, owner profiles and delivery-host APIs.

New module names do not justify replacing an equivalent integrated service.
Private voice/model inputs, Whisper source, keys, deployment policies and user
recordings remain outside public Git. Adapter registration is not feature,
language-quality, distribution or human-NVDA qualification.

## Release evidence

Final qualification must bind the combined source, installed package, native
controls and sealed-bundle gates to the same commit. Delivery must reuse the
exact retained application wheel from that successful aggregate.

The ordinary hosted runner has no audio output; the dated evaluation profile
supplies a verified endpoint for this bounded test. It does not add a driver
to delivery or grant recurring CI deployment rights. The application archive
remains free of the audio runtime and uses explicit local setup on the
destination machine.

Do not build the final application from a locally rebuilt wheel, a failed
aggregate or the independently qualified canonical preview's wheel. Human
NVDA, real owner-engine inference, 135-language acoustic quality, signing and
production update/entitlement authority remain separate requirements from an
integrated library/audio preview.


## Accepted canonical e69 and additive reader reconciliation

The authoritative branch was rechecked at
`e69ba2403515f21509686d66f4bc8b3673756d81` (accepted PR147). The finalization
specification remains the exact blob
`db8adbdc483a03c262885928c7ed214ca70bbaa7`.

The reconciled source adopts the accepted shared resource broker and operations
view, explicit local-file transcription protocol, Advanced v3 translation and
literal-loopback local proposals. Existing receipt-bound transcription remains
in the same public package, with explicit protocol selection and disjoint stores.
The stricter source/usage-ledger, composite backup format5/schema6, owned-process,
publication and output-consent contracts remain authoritative. The obsolete
transcription.py file is removed when its body moves to the package initializer.

The new reader tools extend the existing document, worker, player, keyboard
preferences and typed recovery: output selection, find/replace, pronunciation
rules, installed Windows spelling and font/color/zoom. No alternate document,
player, progress hub or task store is introduced. Native keyboard/selection
checks remain distinct from human NVDA and listening qualification.

Windows resource selectors contain only actual Windows/portable cases.
The unchanged native POSIX fork-ownership test is required in the Linux member
of the transcription protocol workflow, under
tests/operations_posix/test_fork_ownership.py. This preserves that platform
contract without a skipped Windows test.

The 9f predecessor passed all 265 integrated source suites. Its installed
aggregate is ineligible because actual capture rejected FFmpeg input placement,
two setup tests inherited the real bundled-runtime discovery contrary to their
fixture assumptions, and the complete installation gate timed out at 20 minutes.
The corrective source preserves actual input flags, isolates only the setup
fixtures' ambient discovery, and records installed test failures immediately.
Each original source and installed selector remains required in the successor;
the additional platform and reader gates must also pass at that exact head.

No newly composed source is labelled executed merely because its sparse tree
was saved or independently reviewed. The supplementary portable/reader runs
are diagnostic evidence. Final application assembly still requires the retained
application wheel from the complete successful same-head Windows aggregate and
the other required same-head workflows.

## Native reader diagnostic and follow-up admission review

The supplementary Windows run
[45985650997](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45985650997)
belongs to source `31a8acc65f546b7fea77cdb47eb46a87da1b38a1`. Its source
and installed reading-tools and Windows-dictionary probes passed. The same
run did not pass as an aggregate: a 32,768-character automatic pytest parameter
ID exceeded the Windows environment limit, a malformed-state fixture attempted
create-only publication over an existing file, and the appearance probe's
failure cleanup opened an unsaved-copy question before showing its original
exception.

The successor retains the oversized identity input with compact test IDs,
uses explicit replacement only in that malformed-state fixture, and drives
ASCII appearance inputs through wx's byte-input contract. Appearance acceptance
keeps all prior style, preview, selection and source checks. It saves the
synthetic reading copy through actual Ctrl+S and verifies the recovery record
before each Escape. Failure writes its original traceback and stage before
ending the probe's modal loops; production close behavior remains unchanged.

The sealed spelling probe receives its declared site-packages import root.
The sealed platform-tools probe uses the existing native-runtime activation
wrapper before importing wx. These correct launch wiring without removing
any gate. The focused diagnostic job records only source/installed test
evidence; it cannot supply the final delivery wheel.

The latest integration record also identified PR182's early export-approval
path/root guard. Its reviewed narrow correction is reconciled before reading
the destination, with the four original text/binary and path/root regressions.
The existing bounded content hash, native Windows change time, captured-absent,
cancellation and atomic publication checks remain in force. This source review
does not transfer the donor's Windows verdict to the new composition.

The first combined portable preflight at
`c4de280442e714dc53feb5e656b66edd1ddb6ae0`,
[45991270051](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45991270051),
compiled all application modules and passed 61 of its 64 selected suites.
It recorded 1,540 passing, 20 failing and two skipped cases. Nineteen failures
came from running literal-loopback provider tests under the unconditional
offline socket guard; those complete unchanged tests now have their own strict
HTTP/TLS step. Two actual Windows handle cases now run unchanged in a mandatory
Windows-specific module, alongside the nine portable publication cases.
Main source/installed, the Windows bridge and the V3 Windows selection retain
those native cases.

The translation failure was a missing-consent diagnostic: the existing combined
guard rejected absent --allow-remote before evaluating the supplied job digest.
The successor gives that missing flag its own explicit error while preserving
the exact-job-digest guard. The same test now checks both denial paths before
gateway construction, credential access or HTTP; repeated inspection retains
the digest, and a completed approved resume must not bill again. Request
identity and glossary/model binding are unchanged. These corrections require
fresh results; the failed preflight supplies no release verdict.

Native output qualification also carries the reviewed stable endpoint identity
from inventory into each source, installed and sealed probe. A reordered numeric
index must fail before playback instead of silently qualifying another output.
This strengthens the diagnostic boundary without changing the production
player or the user's default audio device.
