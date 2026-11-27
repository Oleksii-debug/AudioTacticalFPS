# Resonance integration checkpoint — 27 November 2026

## Composed acceptance candidate r6 — execution in progress

The current technical contract is `UNIVERSAL_SCOPE_V3_FINALIZATION.md` read from
canonical `e69ba2403515f21509686d66f4bc8b3673756d81`. Its contents are unchanged
from the previously reviewed `4b6f8169986c4a58b43e4a23bc081fbefadbbdcb`.
The implementation remains scoped to `carr-library/**` and its CARR workflows.
This checkpoint is not a declaration that every V3 release gate has passed.

### Current composition and intended behavior

The r6 candidate composes reviewed work on top of `498bf205`: bounded PDF
streaming (`b12e63ca`), scan/EPUB continuation (`18f4249c` and the Windows
fixture correction `3f95e47c`), exact final audio publication (`ed5d4333`),
explicit local audio setup and installed-library qualification (`b8ca78ef`),
protected standard recovery (`5760cc69`), scalable catalog/search queries
(`51e55b37`), the native About probe (`683f315d`), and the native Trust-window
lifecycle correction (`e5d03cca`). Durable product composition is
`2f062a429054bc0f971accc7af6811151deb21d0`; native recovery composition is
`e71ced734624320f2ef881158d0f8e590d319a79`.

- Optional backup roots use strict metadata inspection: only confirmed absence
  is omitted. Access errors, invalid parent types and staged traversal failures
  stop completion. The 39-case regression module is required in both source and
  installed execution; this final port has not yet been executed.
- PDF source identity, copying, parser access, retained readback and backup
  paths use bounded streams. Scan continuation carries cancellation and its
  deadline into PDF hashing. Existing originals remain immutable.
- Scan pages retain a stable cursor across more than 5,000 sources. EPUB pages
  retain edition identity and fresh catalog validation. Pagination has explicit
  limits; a partial page is never mislabeled as complete import.
- Audio publication checks cancellation after the last flush and before the
  atomic move, while a completed move remains completed after late cancellation.
- `SetupLocalAudio.cmd` and its two helpers are sealed into the bundle. Planning
  is read-only; preparation requires an explicit local archive, destination and
  confirmation. The setup path reuses the existing pinned native verifier.
- Standard A/B updates retain the previous generation, verified research backup,
  exact native activation and the existing A process. Same-release replacement
  is refused. A failed B remains available for diagnostics and retry uses a new
  generation.
- Windows Vault recovery binds the transaction, prior native activation, exact
  observed candidate generation, policy and backup. A fresh process must obtain
  fresh TUF verification and the same protected authority before recovery.
  Missing, changed or foreign authority fails closed. Generic memory-only
  anchors do not authorize a fresh-process rollback.
- The native probe now requires a real owned-process exit followed by recovery
  in a separate process, two failed B generations, successful B activation and
  a 50-case installed, offline, audio-free library qualification.
- Catalog predicates use the existing workspace/id index. Search avoids
  repeatedly constructing a revision index while preserving fresh full-source
  fingerprints and stable continuation semantics.
- The Trust owner-destroy handler uses the captured native owner ID, allowing
  teardown to finish before queued workers can access destroyed wx objects.

### Executed evidence retained from this run

Results below belong to their named source snapshots; they are not a substitute
for executing the complete composed candidate. Overlapping test groups are not
added together.

| Area | Actual result | Scope |
| --- | --- | --- |
| Scan, EPUB, resources, notices, About and legacy state | 262 passed locally | Linux; frozen 498 source |
| Large PDF and scan composition | 25 passed locally | Actual 72/144 MiB parser/render/retained-readback cases |
| Word integration boundaries | 49 passed locally; 36 passed on Windows | Real CSL with controlled COM; actual Office not run |
| Standard, audio, engine and publication guards | 196 passed locally | Reviewed component composition |
| Bundle helpers and local audio setup | 37 passed locally | Exact helper bytes, seal checks and tamper rejection |
| Protected standard restart predecessor | 90 passed locally | 54d source; the later 5760 cleanup successor is separate |
| Final protected cleanup successor | 33 focused passed, 61 deselected | 5760; whole 94-case gate still required on composed source |
| Query performance composition | 124 passed locally | 66 new query cases, existing pagination and continuation |
| Exact installed-library qualifier | 26 passed locally | Qualifier source; native 50-case execution still required |
| Windows import and EPUB continuation | 174 source and 26 native checks passed | Run 45975256450, exact 3f95 source |
| Windows About dialog | 19 native checks per profile passed | Run 45967011963, core 17 and integrated 56 dependencies |
| Windows Trust lifecycle | 15 native checks passed | Run 45978658434, exact e5 source |
| Private supplied ASR compatibility | 68 passed | Private checkpoint retained separately; synthetic decoder |

The r5 complete integrated lane failed and remains recorded as failed. Its
successful independent audio, keyboard, bibliography and sync lanes are retained
in `docs/evidence/windows-r5-completed-lanes-20261127.json`; they do not turn the
failed r5 integrated lane green. The original Windows CRLF fixture failure and
Trust access violation were diagnosed and corrected in the named successor
executions above.

### Measured capacity and remaining release qualification

Synthetic catalog runs fully paged 1,000 and 10,000 results. On the retained
50,000-book fixture, the reviewed query change reduced the median first-page
time from 8.0648 seconds to 1.3442 seconds (20 queries each). The last observed
candidate checkpoint covered 25 pages / 2,500 exact unique results. The executor
disconnected before the final 500-page receipt could be read. Full 50,000-result
completion is therefore **NOT ESTABLISHED**; the original timeout and the
interrupted baseline are retained in evidence rather than replaced by a PASS.

The complete r6 Windows workflow retains all prior source, installed, native,
firewall, provenance, ASR, Advanced, recording, Word and installer gates. It now
declares 296 source selectors, 161 additive installed selectors plus the existing
external installed list, and 94 exact standard-recovery source cases. A separate
standard lane runs the same native A/B probe sooner; its success cannot replace
the full integrated lane. A separate 2,049 MiB PDF measurement requires actual
Windows parsing/rendering/readback and installed-source hashes.

The latest owner source also contains new live-reader, clip/mastering, audio
publication, explicit selected conversion and streamed UTF-8 features. Their
compatible integration is a separate r7 workline; this r6 snapshot does not
claim those newly identified implementation gaps are closed.

The following remain distinct qualifications until actual evidence is obtained:
the full composed Windows run, the new native A/B/restart/50-case sequence, the
multi-gigabyte measurement, full 50,000-result traversal, actual Word/Office,
physical microphone and output, NVDA/manual accessibility, real acoustic model
quality and the real user book corpus. Production entitlement issuance and
signing are not synthesized from CI fixture authority. Private models, worker
code, recordings, transcripts and credentials are not included in public source
or artifacts.

## Historical checkpoints

The following sections describe earlier source snapshots and are retained for
traceability. Their statuses are superseded only by the specific evidence above.

## Composed acceptance candidate r5 — current source, execution required

The technical contract remains `UNIVERSAL_SCOPE_V3_FINALIZATION.md` at canonical
`4b6f8169986c4a58b43e4a23bc081fbefadbbdcb`. The r5 composition includes the
reviewed base `08aaa4a85cc9d85b01f81cdc3bbce775d462f8ba`, the final admission,
pagination and About changes below, and one shared acceptance workflow.
This document does **not** declare the complete V3 product or release qualified.
A source review, an expected case count and a successful earlier commit are
distinct from passing execution on the present composed commit.

### Included changes

| Source | Resulting behavior | Evidence at composition |
| --- | --- | --- |
| `7a656d6281e92c7656725d55008a75321966d10f` | Canonical Windows ledger paths and actual publication fault seams | Reviewed corrections to the r4 failures; current broad replay pending |
| `ad87fad496213ca6eb2dce683ff36c7611be84fb` and `32fac9fa3493f2639414632ba88faf6e152e5fb2` | Actual Windows state-replacement seam, binary child readiness, enabled fake controls, typed PDF backup compatibility, owned pronunciation child and bounded import publication fixtures | Independent source reviews; existing assertions retained, PDF current/historical coverage adds one case |
| `7ac6cec1e08bf544a50f17ebe4afdf0c13a1f440` | Recording Pause/Resume through the existing owned capture, verified PCM parts, one reservation and original/retake backup; stale-host modal cleanup | **115 strict source cases and 50 installed native keyboard checks PASS** on this exact commit |
| `5c357542de0000be0998241fe19301cf5941c916` | Current vault-backed signed entitlement, real TUF package authority, bounded explicit renewal/polling, managed-layout guard, shared installation lifetime lease and active-app uninstall refusal | Independent source reviews; 174 security and 39 lease cases expected; current execution pending |
| `9a192f25ea46acc0f17badd03e9d2c7ed9c1fe94` | One explicitly injected controller admits only fresh mapped audio/ASR/planning work; retained operation leases and saved data remain usable; foreign nested controllers and injected service rebinding refused | Two independent source reviews; 46 source-derived cases, execution pending |
| `bd2b5edc8b4ecf9b3e3589ad098753f123cceabc` | Explicit Word working copy, current CSL snapshot and stable citation IDs, native controls/endnotes, worker-owned COM apartment and proxy release | 36 backend, 9 UI and 4 profile cases expected; actual Word qualification remains separate |
| `41c2abd5875ffec2da6ff71a06a0c43ca442ae0c` | Native About component catalogue and bounded read-only actual installed notices using current background host | 27 catalogue and 14 UI cases expected; execution pending |
| `afdd5ece72cfad789db9c221efaf23d682a47a28` | Bounded search continuation across more than 500 editions and 20 libraries, literal keyset past 10,000 matches, native Next/Previous and CLI/assistant cursor parity | Independent source review; 49 search, 11 UI and 9 end-to-end cases plus 21 native checks expected |

No parallel capture, process supervisor, credential vault, publication mechanism,
installer layout, model transport or document store is introduced. Existing
protected user-data operations remain independent of optional entitlement
availability. The default private installation has no configured production
licensing authority or automatic remote endpoint.

Pause acknowledges only after the actual owned capture has closed. Resume
rechecks the selected local executable and microphone and keeps the original
reservation and media quieting. Final audio streams verified PCM parts without
inserting pause silence or decoding/re-encoding; the final typed receipt binds
both parts and the whole PCM. Pending cleanup cannot release the reservation or
claim a paused state. The exact r5 composition must replay these assertions.

Pagination retains existing source adapters and selected-library order. Literal
enumeration is complete under the unchanged snapshot; ranked pages explicitly
retain within-batch order and do not promise a global score. Page cursors bind the
current query, filters, catalogue revision, library/workspace and selected library
order. Current-page handles are bounded and a new page retires only that query's
prior handles. Native source actions still reverify retained originals.
The synthetic 503-edition, 25-library and 10,003-match tests are not a claim about
1k/10k/50k real-corpus performance.

The optional Word profile uses the reviewed official pywin32 312 platform wheel.
COM is initialized and all wrappers are released inside the same actual worker
apartment. The native probe also releases its own proxies before uninitializing.
The user's original document is never saved or replaced; the explicit new copy
remains the mutation target. Installed pywin32 ABI/origin/STA checks are mandatory,
while an unavailable Word application is recorded as **NOT_RUN**, not a passed
Office workflow.

### Shared dependency profile and exact current gates

The integrated Windows profile preserves every version, accepted wheel hash,
component identity and original dependency edge in the 53-package semantic
profile. It adds only python-tuf 7.0.1, securesystemslib 1.6.0 and pywin32 312,
giving 56 packages and 56 SBOM components. Prior resolver provenance is retained
only for the prior inventory; this additive composition is not presented as a new
uv resolution. Actual component notices retain Apache/MIT alternatives, vendored
CC0 terms and the upstream mixed Word/Win32 notices.

The prepared r5 workflow selects **285 strict source suites** and the separate
**14-case numeric-loopback local-planning fixture**. Installed execution selects
**146 content selectors plus the existing 14 explicit workflows**. Additive exact
oracles require **441 cases in 25 source modules** and **395 cases in 22 installed
modules**. These overlap other gates and must not be added as unique test totals.
Source counts are derived from the unchanged test functions/parameters; installed
JUnit gates additionally require each exact function count and zero errors/skips.

Existing exact ASR466, Advanced/planning135 and bounded-inventory10 gates remain.
Recording/process/monitor/playback/UI/pause expectations now total157. Native
checks retain the former gates and add recording50, trust15 and pagination21.
Source, initial wheel, full sealed bundle and native probes retain installed-origin
and immutable-source checks. Only the separate synthetic local HTTP fixture can
use numeric loopback TCP; the sealed bundle still executes under outbound
Windows Firewall blocks.

The standard installer driver uses the same integrated56 profile and fresh,
independently sealed audio-free distribution. Its six exact native checks include
refusing uninstall while the actual managed application holds its lease, followed
by repair and preservation checks. A genuine different-release standard A-to-B
update transaction is a separate workline; this repair probe is not relabelled as
an A-to-B upgrade.

### Completed r4 evidence — exact old head only

The completed r4 head was `28a4e5428a035af8d2f9edd89ebd4ae3802d1f5b`.
Its results are recorded here to explain the required corrections, not transferred
to r5.

| Windows lane | Exact result | Run / job |
| --- | --- | --- |
| Integrated source | **FAIL**:260 strict suites,252 suite PASS/8 suite FAIL;6616 passed/35 failed/0 skipped cases | [45954388978 /140357389698](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45954388978/job/140357389698) |
| Audio | **FAIL**:879 passed/5 failed/7 explicit device/platform skips;22 subtests | [45954388910 /140357389183](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45954388910/job/140357389183) |
| Native commands/drafts | **PASS**:270 strict cases, actual native Main35/Reader23/drafts15/SaveAll27/Operations35 | [45954388978 /140357389487](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45954388978/job/140357389487) |
| Portable sync | **PASS**:320 source,154 installed and44 actual native checks | [45954388929 /140357389225](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45954388929/job/140357389225) |
| Bibliography | **PASS**:1018 strict cases across28 suites,108 installed native checks | [45954389218 /140357390587](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45954389218/job/140357390587) |

The detailed 260-row source evidence is retained in
`evidence/windows-r4-main-20261127.json`. Main artifact15180380896 has SHA256
`72e3ea40428a84333717b60feda0bfc3a9dfb6604ad92e766fbdec18de92f50f`.
Later r4 main sealed-installed/native/loopback/Inno stages were **NOT_RUN** after
the strict source failure. All eight failed groups have reviewed corrections in
the r5 source; that statement is not an execution verdict.

### Exact recording Pause/Resume acceptance

[Run45960506050 / job140376949230](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45960506050/job/140376949230)
completed successfully on `7ac6cec1e08bf544a50f17ebe4afdf0c13a1f440`,
tree `85d8ed4187db79656192580012c74515b014717a`.
It passed115 strict source cases with zero failures/skips and50 real installed
native keyboard checks. Artifact15180398342 SHA256 is
`ba9f4f2624c32192d0b580796f8a6e35bed0980349c206c2b9885813929c0d2b`.
The full receipt is `evidence/recording-pause-7ac6-20261127.json`.
This qualifies the synthetic actual-FFmpeg/native-control scenario; physical
microphone, physical monitor output, listening quality and NVDA remain **NOT_RUN**.

### Remaining qualification boundaries

Real private-worker interoperability, actual Whisper/model/voice/music quality,
production provider/account credentials, actual Office availability, physical
audio devices/listening, NVDA, real large-corpus benchmarks, signing and final
distribution rights are not inferred from synthetic or source-only tests.
No private worker, model, recording, transcript, credential or private path is
published. The current execution-host outage does not justify weakening these
requirements or fabricating a release qualification.

## Historical r4 composition record

The following is the earlier preparation record. Its "pending" statements and
expected counts describe r4 before its completed results above; they are not
the status or selected counts for r5.

### Earlier r4 source-review record

The technical entry point is `UNIVERSAL_SCOPE_V3_FINALIZATION.md`, rechecked
against canonical commit `4b6f8169986c4a58b43e4a23bc081fbefadbbdcb` (unchanged specification bytes).
This candidate preserves the reviewed f4 composition and integrates the exact
backend, UI, backup and standard-installer sources below. These additions are
**SOURCE_REVIEWED / INTEGRATED / NOT_RUN** until their own current-commit gates
finish. Earlier successful jobs are evidence only for the exact source they ran.

| Source | Included behavior | Evidence at composition |
| --- | --- | --- |
| `f4f48f11b4777cec89fe8d2769e644f690794689` | Shared operations facade/resource admission, settings, retained reading histories, typed backup, recording/monitor host and native launcher assembly | Reviewed earlier composition; superseded by this candidate |
| `d16e4023fcee86855e7d70b6b6fa6757826fd61e` | Advanced v3 translation, bounded local LLM proposals and CLI, decoded receipt/ledger proof, canonical library paths, shared coherent state, portable policy migration and exact loopback test harness | Independent source reviews; execution pending |
| `835a7d6892e256e8ef5db705e851fbd696503f9e` | Recording/retake controls, reusable playback, Advanced approval and settings, asynchronous assistant preview/run, committed results preserved through advisory progress failure | Independent source reviews; execution pending |
| `5afb615cdb9e3efb57b81c6e02a8471a32a1eb7c` | Passive zero-byte inert recording preservation, bounded recording manifests and the existing native legacy-reading numeric/list report contract | Independent source review; execution pending |
| `c32992318882faebf27d0b80d0dedfe8be7a4acd` + `9a709f3fc83f271c6f4efe888198884b166c764f` | Reviewed standard per-user Inno preparation/probe, side-by-side repair and preservation of failed environments and unknown files | 37 source-derived expected cases; independent driver source review; native acceptance pending |
| `7e4ce3d7f3da56fe8cc5bee7c2b0f187c1a5d047` | Native launcher, bounded source/PE admission, typed bundle manifest and exact actual process probe | Its dedicated Windows run passed; current installed-package gate remains separate |

The backend uses current provider profiles, vault, budgets, immutable source and
accepted receipts. Basic v2 remains distinct from Advanced v3. Advanced approval
pins project/location/model/glossary and directional language coverage. A decoded
result has independent live-ledger proof; historical wire-only evidence and
passively restored archived-unverified results retain their explicit limits.
Local model output can propose only the existing typed, allowlisted plan.
Review and execution remain separate actions. No external provider credentials,
account or actual model quality are certified by synthetic protocol fixtures.

The existing publication/resource/process boundaries remain authoritative.
A fresh queued import has no premature launch attempt; replay invalidates its old
attempt. Heavy reservations span the actual owned worker and cleanup. Shared
state replacement preserves coherent snapshots under native Windows sharing,
without weakening immutable source or audio read guards. The translation path
fix uses the existing safe canonical-path admission and still rejects a foreign
library, links and arbitrary ledger/profile copies.

Microphone capture, saved originals/retakes and live monitoring use the current
FFmpeg host, Job Object, resource broker, PCM player and managed sound assets.
No second capture/process/cleanup family is introduced. Monitoring starts only
after explicit device selection. A committed recording remains available if
advisory progress fails; repeated preview uses Stop rather than permanently
closing the player's native backend. Typed complete backup preserves all three
ASR profiles, policy, reading history and recording namespaces, existing budgets,
source/staged revalidation and final source hashes. An unreceipted final recording
is an inert retained original, never a completed verified recording.

The latest canonical bounded-inventory changes were compared with this
owner implementation. Its OCR scanner already preserves those exact guards;
all three canonical inventory regressions are now mandatory in installed replay.
The current music API differs from that older canonical module, so its renderer
is preserved. Commit `6704be362336c4750daa9d504b886ca6078d0c81` instead applies
the same fail-closed scandir rule through the existing local model manifest,
music cache and generated-stage checks. Existing file/entry/byte bounds and
hashes remain. Seven new source cases and all ten selected installed inventory
cases must pass; the canonical module's31 historical tests are not transferred.

### Exact gates prepared for this composition

The workflow selects 260 strict source suites and separately the exact
14-case local HTTP fixture. Installed execution selects 124 content selectors plus
14 explicit workflows, retaining all prior 232 source selectors. Named installed
oracles require 466 ASR/profile cases, 128 recording/process/monitor/playback/UI
cases and 135 Advanced/local-planning/UI cases. These are source-derived expected
counts within the selected suites, not executed results or additive unique totals.

The local HTTP fixture is staged without application source and uses only the
installed Python/package. It explicitly admits numeric loopback TCP; DNS,
external hosts, datagrams and inherited environment opt-ins remain rejected.
It runs separately after the sealed offline/native test phase; it does not weaken
that phase's Windows Firewall or installed-origin checks. Existing direct
`PYTEST_CHILD` users keep their offline behavior and argument order.

Native gates retain the supplied ASR/profile/progress checks and require Tasks,
legacy-reading28, state-sharing25, native launcher9 and recording41 invariants.
The Advanced translation probe uses actual native controls and a disposable
Windows credential-vault entry with a synthetic provider; its field-based report
must prove the exact reviewed pair and immutable derivative. Actual Google API,
physical microphone/output, acoustic model quality and NVDA remain NOT_RUN.

Standard installation accepts only a fresh independently sealed **audio-free**
bundle reusing the acquired pinned Python, application wheel, model, OCR,
corresponding sources and native runtime. The current audio-transfer rights gate
is unchanged. Native acceptance requires an already-installed explicitly pinned
Inno compiler, a disposable CI account, the actual installed application Python,
both successful and failed-bootstrap installers, and exact install/repair/
uninstall preservation checks. No compiler or private model is acquired by this
driver. Setup and runtime binaries remain outside public artifacts.

### Executed Windows evidence for ecf only

Source `ecf3b5511a0df34642317c55b96da3f604d0c67b`,
tree `e82b567de83d481c780d8174ff784d814b3a0efd`. These results are not
transferred to the present composition, and independent case executions are
not a count of unique platform requirements.

- Main PR [run45942148665](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45942148665):
  232 required source suites and 5983 case executions passed, with zero failures
  or skips. Initial wheel installation/self-test and early native bibliography108
  passed. The next native translation probe failed on short/relative versus
  canonical library paths. The narrow product fix is included above. The later
  full sealed bundle, offline-egress replay, installed ASR and progress gates
  were **NOT_RUN** in that attempt. Artifact15174853726 SHA256
  `cbbe339c1fd51db45288c6f84f435f0611c64611e61a66aeb37aad19d2f02634`.
- Audio [run45942148646](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45942148646):
  691 cases passed, 7 explicit platform/device/FluidSynth skips and22 subtests.
  Native16, effects109, all15 Windows ownership cases and actual1/3/10-hour
  streamed PCM passed. Physical playback was unavailable; NVDA was NOT_RUN.
  Artifact15173591906 SHA256
  `bf9386c311b50070ce7962e7a733780f1498811d55782ed4031b82fcfebc5740`.
- Bibliography [run45942148640](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45942148640):
  all28 strict suites /1018 cases passed with zero failures/skips; installed-wheel
  native108 passed. Artifact15173701798 SHA256
  `0cea30d473cb112d6a4e3ce798d13a59a7141d11f7b25637b33a0064e09a8287`.
- Sync [run45942148745](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45942148745):
  320 source,154 isolated installed cases and44 native checks passed, including
  actual publish/restore TaskDialog closure. Artifact15172718236 SHA256
  `bf2442078fe19ff0d33d6f6d209398ae0c55693d961083fbe1a81204eae17ecc`.
- Frozen r3 keyboard job140318636358:10 strict suites /270 cases passed without
  failure/skip; native main35, reader23, drafts15 and SaveAll27 passed.
  Artifact15174351370 SHA256
  `b124fe8e2b179f4df90adc8de213ef0d859a80914f8da6a4b272bd2d5910e9fe`.
- Frozen r3 main run45942146599 stopped earlier at the forty-line orientation
  fixture's30-second deadline:44 passed/1 failed in that suite. The candidate
  retains a90-second limit only for this multipass fixture and bounded failure
  diagnostics; production remains30 seconds, with native script inventory checks
  intact. Artifact15172889621 SHA256
  `f63b1fd53d29cdc6846f267db6db9099747157c37b95b46f02643e8236e51513`.

### Executed native launcher evidence for7e4 only

[Run45946238424](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45946238424),
job140331479383, used Windows Server2022 x64 /CPython3.11 /installed MSVC.
All18 new launcher-admission/bundle cases,18 existing Windows-bundle cases,
the portable C core and9 actual native process checks passed. The unsigned EXE
was13824 bytes, SHA256
`d4d0870e8e8135a84019fc79101ef5e6a431a47a5dfd9dfdb5b79cfa4c4202ff`.
The adjacent machine-readable evidence binds exact source/toolchain/PE identities.
That separate job used a synthetic child; real installed application, fresh
Python installation, physical audio, NVDA and Authenticode were not qualified
there. The EXE was not published.

### Private integration and final acceptance boundaries

The unchanged supplied receipt adapter previously passed22 private contract
cases; the unchanged hardened V43 checkpoint passed its original38 cases.
These controlled decoder fixtures are not acoustic model qualification.
Current-host execution verified three-input/twelve-output bindings, but passive
reopening rejected an output-owner record. The actual private owner constructor
must be inspected and reconciled without weakening scope/owner checks.
Local execution/private-source access is temporarily unavailable; the attempted
new private checkpoint save failed, while the original private inputs remain
durable and outside Git.

Live Word automation, inactive entitlement/update integration and their present
source work remain separately tracked. Actual owner engines/models and voice
cloning, authorized listening samples, physical devices, NVDA, declared
hardware/corpus capacity, signing authority, private service configuration,
per-artifact rights decisions and the final135-language quality matrix remain
distinct acceptance dependencies. Neither this candidate nor its prepared gates
certify a complete qualified or distributable Resonance platform.

## Prior ecf composition and historical evidence


This continuation reconciles the owner branch through
`8a8249342e5b48f67d83cd957b3c33bf752a7ce3` with the supplied-worker profiles,
passive backup validation, request-bound import publication recovery and the
single shared native progress controller. Component source review is complete;
the composed Windows and installed-bundle gates must run against the published
head. Historical results below are not transferred to that new head.

The selected implementation includes:

- Explicit `receipts-v1` and `whisper-v5-manifest-v1` profiles through the current
  transcription service, isolated process host, immutable receipts, edit
  revisions and bounded file/folder intake.
- Typed passive backup validation for both profiles, including saved partial
  edits after resume/Stop, without reading or executing external models.
- The same native entrypoint and CLI now explicitly select the library-wide
  supplied V43 protocol, preserving its seventeen settings, immutable definitions,
  pause/resume/stop, verified reading copies and separate typed backup namespace.
  Existing workspace jobs keep their original identities and scope.
- New receipt-profile runs hash the complete selected model before launch, pass
  the exact frozen identity to the supplied adapter and require that identity in
  every accepted receipt. Historical saved results remain passively readable.
- One native `OperationProgress` controller and notification worker. The
  `progress_view` compatibility name aliases that same object and the shared
  hub remains stable when the library root changes.
- Actual worker/Future results determine completion. Late Stop and presentation
  failures cannot undo or hide a committed result in Desktop or Audio Studio.
- Existing completion-aware Save All, extended Studio effects and captured
  export approvals. Transcript edits use that same coordinator: only a durable
  revision matching the current draft completes saving or a pending close.
- Import recovery requires an immutable database publication receipt bound to
  the exact request and attempt. A newer watermark from another import is not
  sufficient. Database schema V5 is distinct from retained backup formats 1–5.
- Native snapshot confirmation retains real owned-window/UIA/receipt checks
  through the successor password dialog; OCR keeps strict model identifiers
  while accepting the native Windows report's script separators.
- A bounded native TaskDialog answer handles the verified production confirmation
  when Windows exposes no UIA button. It still requires exact ownership and
  observes actual closure before allowing the successor dialog.
- Portable OCR inventory enumeration now fails on unreadable directories and
  bounds both files and empty directories, with a cancellation/deadline check
  before every entry. Verified model packs and native OCR behavior are retained.
- Windows audio input hashing/copying holds a read-only sharing handle so an
  in-place writer cannot evade detection through timestamp coincidence. All
  existing regular-file/reparse/identity/resource guards remain.
- Oversized malformed discovery input keeps its original payload and uses short
  semantic test IDs, avoiding the Windows environment-value limit.
- Transcription preserves the selected virtual environment, rechecks edit
  publication authorization under the existing writer lock, and uses a bounded
  live-status rename fallback only inside the authorized worker job.
- Evidence recovery stops/unbinds its exact native timer before owner disposal.
  Queued callbacks do not touch destroyed controls or restart recovery work.
- Isolated test replays load the exact trusted sibling watchdog. The separate
  bibliography workflow uses its existing two-worker runner within unchanged
  deadlines.

The final workflow preserves the preceding selectors and currently requires
232 unique source suites, 84 installed contents selectors plus the original
14 positional installed suites, the original native gates, the 13-check
workspace transcription probe, the 39-check supplied-profile probe and the
44-check shared-progress probe. These
are required gates, not counts of completed acceptance tests.

Recovered local execution completed 173 focused cases plus 25 subtests for
the progress/Studio/CI-helper/OCR-identity composition. The subsequent
protocol/recovery/OCR selection completed 471 passing cases and exposed 25
failures/errors with one cause: the ported V5 word-subtitle formatter referenced
an absent alternative Studio package. Rebinding it to the selected host's
existing audio formatter, with exact actual-child SRT/VTT byte assertions,
passed all 79 V5-profile and typed-backup cases. The completed transcript
Save All/current-draft selection passed 55 cases, including all 35 current-host
cases. These selections overlap and are not the composed Windows or installed
acceptance verdict. The exact audio-boundary component also passed 68 focused
Linux cases. The final PR159 current-host port was independently reviewed;
its new virtual-environment, publication-race and held-status-reader cases
require the new composed Windows run.

## Historical ecf portable verification

The new supplied-profile backend passed 223 cases across receipt validation,
supervision and both backup namespaces. After the shared Windows status startup
and explicit UI/CLI routing were composed, all 91 supervisor/UI/CLI cases passed.
The current native snapshot confirmation policy suite passed 102 cases.

The receipt-model binding passed its 83-case bridge suite, all 35 current-host
cases and all 42 workspace backup cases. Two cross-profile fixtures initially
lacked the newly required vocabulary file; adding that nonempty inert fixture
left every assertion intact, and the complete 39-case V5 suite then passed.
The actual supplied private receipt adapter passed all 22 of its own checks
against this host, using its controlled decoder and synthetic model bytes.
The original private source was preserved and was not included in this repository.

The observed backup/audio/database fixture corrections passed all 46 cases.
Portable OCR, including the new enumeration checks and real local Tesseract
fixtures, passed all 45 cases. These component selections overlap and must not
be summed into a unique acceptance count. Model accuracy, real weights, Windows
execution of the private worker and NVDA listening remain separate evidence.

## Exact 2274270 Windows checkpoint

The immutable acceptance source was
`2274270c790b2e9574026be5115b282babe51d51`, tree
`8bca8a7dd1ef562aca639eb739f78b9d180fe224`. Its four push workflows ran on
`carr/v3-acceptance-d3a-r2-20261127`; their findings prompted the corrections in
the current composition.

| Workflow | Observed result | Current correction or limit |
| --- | --- | --- |
| Main integration, run 45938490473, job 140307099075 | All 227 suites completed: 224 suites passed, three failed; 5,724 passing cases and six failures, zero skips | Four failures were the already corrected backup-phase and bounded diagnostic fixtures; two more preference fixtures now require the exact typed-field rejection |
| Keyboard/recovery, run 45938490473, job 140307099400 | 270 strict source cases; installed Main 35, Reader 23, evidence draft 15 and Save All 27 checks passed; native measured progress passed | Exact checkpoint PASS; includes the disposed-owner timer regression |
| Audio, run 45938490502 | 687 passing cases, four failures, seven explicit platform/runtime skips and 22 subtests; native 16 checks and 1/3/10-hour synthetic PCM processing passed | Two backup guard fixtures now cover both copy and final verification; two mutation fixtures read WAV metadata before opening the writer |
| Bibliography, run 45938490505 | All 28 suites completed within the deadline; 1,016 passing and two failing cases, zero skips; native citation 108 checks passed | The two real replay-race fixtures now open the current database schema instead of requesting historical schema 4 |
| Snapshot, run 45938490533 | 301 source and 154 isolated installed cases passed; native confirmation exposed no UIA buttons and failed | The bounded production TaskDialog fallback requires a new Windows result |

The main job also passed the 42-case native OCR runtime suite, all 108 language
cases, all 39 V5-profile and 35 current-host cases, and all 42 workspace backup
cases. The held-status tests exercised actual child publication on Windows.
The six strict-source failures prevented its downstream installed acceptance.
The new composition requires its own exact-head runs; preceding results are
retained as defect evidence, not transferred acceptance.

## Exact preceding CBA Windows result

Source head `cba0b1404a817c05a59f7b5eef8deb000491a87f`, tree
`1dd59df44c4c3c915e0b2003e43a3d030e34dd47`. The main run's generated merge
commit `f65fec60812fc21e57fdcc46c393f5a60676c593` has the same exact tree.

| Workflow | Observed result | Consequence |
| --- | --- | --- |
| Main, run 45931122504 | 214 of 217 strict source suites passed; 5,341 passing and four failing cases, no skips in that selection | Copied watchdog fixture, explicit PATH OCR fixture isolation and portable orientation fixture require the corrected run; downstream installed gates did not run |
| Keyboard/recovery, same run | 231 source cases passed; installed native Main 35, Reader 23 and draft 14 checks passed | Evidence applies to the CBA tree |
| Audio, run 45931122529 | 609 passing cases, one failed case, two fixture errors and seven explicitly skipped cases; job later cancelled at its deadline | Oversized parametrized test IDs and the same-size mutation check require correction; installed replay did not run |
| Bibliography, run 45931122516 | 23 completed suites / 959 passing cases and 108 native citation checks, then the unchanged aggregate step deadline | Entire job did not pass; the bounded two-worker continuation requires its own result |
| Snapshot, run 45931122513 | 284 source cases passed; isolated installed replay failed to load its sibling watchdog | Native confirmation acceptance did not run |

The CBA audio job also completed the real native Studio/SAPI/FFmpeg/vault
16-check probe, all 15 process-ownership cases, and 1/3/10-hour synthetic PCM
processing. The physical player reported UNAVAILABLE because the runner had no
output device; this is not listening or audible playback acceptance.

## Historical remaining gaps at the CBA/ecf checkpoints

At that historical checkpoint, the source did not implement microphone capture/retakes/live
monitoring, live Word field insertion/editing, or the complete signed
end-user installer, repair, uninstall, update and entitlement lifecycle.
Those are implementation gaps and must not be relabeled as completed or merely
manual tests. Signing certificates, authorized distribution/service inputs
and owner runtime/model rights are separate required inputs.

Actual private ASR and local model inference, owner-engine compatibility,
language and long-form quality, physical listening, and manual Windows/NVDA
acceptance remain separate qualification work. Public synthetic workers prove
protocol/recovery behavior and do not qualify neural recognition or voices.
No final installer or complete V3 release is claimed.

## Historical owner checkpoint

The following record was retained from the owner composition. Its earlier
component counts overlap; they must not be added to the current counts or used
as acceptance of a different source tree.

This checkpoint composes the already reviewed source changes on top of
`587559f403b0c7a164b7e496bcd6975506bbe881`. It is not a final installer
or a declaration that every V3 platform acceptance gate has passed.

## Continuation after the 6c7afd Windows checkpoint

The next source composition adds completion-aware Save All/Close for the
existing note, reading and Studio stores; captured Studio Save As/export
approval; final streaming source-byte revalidation in backups; and the 21
typed role-effect controls through the current FFmpeg pipeline. Existing
seven-effect defaults remain unchanged. WMA explicitly encodes at 48 kHz,
including when the master/timeline uses 96 kHz, and the plan explains that
constraint. New native effect and Save All probes are required gates.

Actual recovered-executor verification of this composition before another
transport outage:

- Focused combined selection: 285 passed and one new Save As fixture assertion
  failed because JSON lists were compared to in-memory tuples. Reopening the
  actual saved controller corrected the assertion; its complete Studio suite
  then passed 43/43 with no skips.
- Existing production, signal, FFmpeg and productivity regressions: 66 passed
  plus 7 subtests, no skips.
- Independent Save All/current-store/backup-byte selection: 202 passed across
  nine strict suites, no skips.
- Native snapshot probe policy selection: 78 passed, no skips.
- The new effect tests actually ran synthetic PCM through local FFmpeg,
  including independent pitch/tempo, notch, stereo width, loudness/limiting,
  all added default filters and encode/decode of Ogg/AAC/WMA including a
  96 kHz WMA input. This is signal/runtime evidence, not listening/model quality.
- Source compilation and workflow YAML parsing passed before the final OCR
  and native-effect-probe additions; those last additions require the new
  Windows compile/runtime run.

Exact 6c7afd Windows results: bibliography and keyboard/recovery jobs passed.
The main job completed 209 required suites: 208 passed; only the portable OCR
runtime suite failed (38 passed, 2 failed, 1 missing-font skip). The two failures
revealed native Windows script-pack separators in Tesseract's inventory.
The continuation normalizes those separators only at the native report boundary,
keeps user selectors/path guards strict, and uses the pinned Pillow embedded
font for the orientation fixture instead of skipping on a missing host font.
Native snapshot source/installed suites passed 270/151 cases; the real modal
probe failed. Its continuation preserves the pending owned dialog across
asynchronous Invoke/password succession and adds bounded non-sensitive diagnostics.
Neither corrected native flow is claimed passed before its new run.

These selections overlap and must not be added as unique test counts.
The restored large-input extraction was interrupted again without a completion
verdict. No new private installer, actual ASR inference, NVDA or full-platform
acceptance is claimed.

## Changes in the preceding 6c7afd checkpoint

- Initialize the assistant Tools menu before translation and document conversion.
- Reconstruct citation DOCX documents through the existing QuickJS CSL processor,
  retaining stable source/cluster identities and requiring the exact original
  processor/style or an explicitly selected original custom style. Existing Word
  edits remain in the original document. XML expansion is bounded.
- Carry one captured export approval from the native file picker through the
  queued work and final atomic publication. Parent/destination replacement races
  are refused. Existing files require an explicit application confirmation.
- Convert supported documents using the existing extraction/supervisor pipeline,
  with immutable source identity, bounded resources, precise position receipts
  and cancellation. If the first atomic publication succeeds before fsync fails,
  rollback removes only the exact unchanged file owned by that attempt.
- Recover a completed isolated import only from its own publication watermark;
  a stale or foreign result cannot become a successful cancelled operation.
- Update backup fixture fault injection to exercise the current schema while
  retaining historical backup coverage.
- Prepare the supplied Whisper worker privately with its exact input size/hash.
  The public preparer changes only the verified ASR03 settings fallback and
  contains no supplied private source, model or recording.

## Executed verification before the executor disconnected

These groups overlap; their case counts must not be added as unique coverage.

| Scope | Actual result |
| --- | --- |
| Main citations/assistant/translation/regeneration/UI/delivery/preparer selection | 788 passed, 1 assertion failure: Chicago title casing in a test |
| Corrected complete DOCX document suite, including bounded escaped XML metadata | 9 passed, 0 skipped |
| Composed desktop/export/citation/translation/conversion handler contracts | 116 passed, 0 skipped |
| Conversion with real local reader workers and publication fault injection | 32 passed, 0 skipped |
| Backup current/historical/pending-input suites (17 strict suites) | 343 passed, 0 skipped |
| Isolated import owner publication and recovery (6 strict suites) | 133 passed, 0 skipped |
| Export approval UI suites (12 suites) | 375 passed plus 7 subtests, 0 skipped |
| Reader/publication service selection | 170 passed; 1 Windows wx module unavailable on Linux |
| Main topic export handler selection after applying both fixture changes | 35 passed, 0 skipped |

The supplied-worker ASR03 checks were actually executed against the verified
private source with model doubles: baseline 2 failed/1 passed, candidate 3 passed.
All 14 public preparer cases passed. This is not speech recognition inference
and does not qualify a model, language, voice or recording.

The earlier published `0ccb0bd` Windows main/audio runs stopped on the Tools
menu initialization order. The sync source selection had two stale test-fixture
installer namespaces. This checkpoint contains the corresponding fixes; fresh
Windows native and installed-artifact results are still required. No final
verdict is recorded for the root combined publication/conversion command that
was interrupted by the executor outage.

## Historical remaining acceptance gates

The complete V3 platform still requires the canonical branch reconciliation,
global completion-aware saving, remaining supplied ASR corrections and actual
model inference, native Windows/NVDA and listening validation, and owner-provided
engine/model/credential/signing material where those features depend on it.
The full pinned Windows audio/dependency input restoration was interrupted
before its final extraction was confirmed. No final private installer or signed
release has been produced by this checkpoint.
