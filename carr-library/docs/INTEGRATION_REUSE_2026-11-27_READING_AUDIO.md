# Reading, audio production and recovery integration — 2026-11-27

## Authority and status

This checkpoint follows root AGENTS.md `080bb9104d3224749e9317c2273d803c0ac37835`,
the current finalization specification `db8adbdc483a03c262885928c7ed214ca70bbaa7`,
and the detailed reading/audio requirements `d0b2761dd22b33233b311d4800230fb47f5d3d8b`.
It extends the existing Resonance application in `carr-library/`.
The canonical integration parent is `a44fac04072389caa40e0d6e799b13797712f797`;
the previous composed product head is `a9da9c4ba591895a78c207eb683499e33c922d63`.

Status: **implemented source, reviewed within the scopes below, composed execution
NOT_RUN at creation of this checkpoint**. This is not whole-platform DONE, a
release, manual NVDA acceptance, speech quality, or evidence that every requested
language/model/device works. Exact workflow head SHAs and results are authoritative;
older component runs do not qualify the newly composed code.

## Implemented routes and reuse

| Route | Existing mechanism reused | Checkpoint/provenance |
| --- | --- | --- |
| Native playback, A/B, skips, sleep, per-handle volume/mute/balance, private speed/pitch copies | One WavePlayer, MCI/owned WinMM, current output identity, shared resource broker and existing native cleanup owners | Core `23099324`, listening correction `ae8ae4ef`, UI `1e1ba182` |
| Retained file queue and passive recovery | Existing clip retention, bounded hashes, one desktop executor/progress owner, explicit saved-state review and Save All | Same player checkpoints; no second playback service |
| Book and multi-book production | Existing source authorization, ordinary Studio projects/ProductionService, measured source ranges and current voice/preset choices | `7216c3e7` |
| M4B chapter output, LRC and measured PCM parts | Existing FFmpeg runtime, verified completed production artifacts and publication receipts | `0854bdcf`, `1d4a050b`, `25d689f7` |
| Batch conversion and ordered effects | Existing DSP operations, current runtime, shared leases, retained stage and native state lock | `aaddceb9`, reviewed cancellation-owner correction `d4c50581` |
| Audio tags and embedded cover | Existing no-clobber publication, current FFmpeg/ffprobe and typed passive state | `468783f9`, native probe `95efe18c`, new-only destination hook |
| Incremental backup chain | Existing complete format-1–5 manifest validation and staged fresh-destination restore | `6c28e165`; bounded parent chains and manifest-last publication |
| Desktop settings, optional tray, explicit global controls/clipboard policy | Current menu/key registry and one wx loop; default-off optional actions and captured native owner identity | Desktop root composition and `3a6eabd5` |
| DAISY 2.02 authoring | Existing completed Studio/primary production, PublicationService and measured timeline; official pinned DTDs used only for explicit CI validation | `d7138f2c` |
| Authored recorded audio in imported EPUB | Existing retained EPUB parser in an owned child, Reading Desk worker/source guards, current audio runtime and WavePlayer | `d7138f2c`; authored fragments, no invented word timing or TTS fallback |
| Current word, logical line, sentence and paragraph speech | Existing ReaderToolSession, native selection offsets, single narrator/player and shared key registry | `59c7062b`; four explicit Read and three passive navigation commands |
| Automatic language/per-region OCR | Existing installed-model chooser, Tesseract path, worker and candidate-review provenance | `10da3735`, runtime-environment correction `47d06262`, exact py3langid packaging `f0331787` |
| Exact/Boolean result continuation | Existing literal/advanced retrieval, scoped source transaction, raw quotation validation and Search panel | Opaque per-app keyset cursors, bounded navigation state; ranked/index capacity remains separate |

These paths preserve immutable originals and the distinction between extracted
text, an editable reading copy, and independently approved quotations. Restoring
state does not authorize old external paths, engines, credentials, providers or
unfinished operations. No private runtime, model, owner reference voice or private
delivery bundle is included in public source or diagnostic artifacts.

Primary/Scene production both feed the existing publication service. The three
measured-PCM methods remain byte-identical to the reviewed `ee1f011c` preimage
after adding DAISY. DAISY production does not acquire a new runtime dependency:
lxml 6.1.3 is already in both the ordinary and semantic Windows locks through the
existing document stack; only the independent CI structural check uses its DTD API.

## Concrete corrections carried into this composition

- Retained clips now recognize only their own exact two-link publication pair
  before removing the stage. The global regular-file rule remains strict.
  Existing actual PCM/clip controls and the additional filesystem race controls
  must execute again; the failed prior Add scenario is not marked fixed by a
  test claim until that rerun succeeds.
- Wrapped cancellation with a pending native cleanup owner propagates through
  the outer batch-conversion boundary. Native state locks, staging and shared
  reservations remain owned until the real cleanup acknowledgement.
- Reader listening preparation acknowledges only its exact completed operation.
  Ordinary window close cannot manufacture a completed worker. The existing
  executor finalizer or successful wait=True shutdown supplies explicit drain
  proof for a task canceled before entering its worker.
- Canonical PR191 stale overwrite approval is reconciled before native/content
  reads while retaining this host's captured-length plus EOF hash bound. A second
  native ChangeTime query detects byte/metadata restoration during the hash.
- Canonical PR196's optional ASR-cache rule is applied to this host's
  backup_compat manifest normalizer. Its 128 layout controls and tampered actual
  restore case are preserved with the existing inventory projection. No donor
  module replaces this host's distinct native backup producer.
- The failed native clipboard assertion was traced to wxTextDataObject newline
  normalization in the pinned wxWidgets source. Windows now copies bounded
  CF_UNICODETEXT under its own OpenClipboard/GlobalLock lifetime, releases both
  owners, and strictly decodes the exact UTF-16 text. Original CRLF, whitespace,
  all nine native assertions, bounded failure receipts, exact source hashes and
  wheel RECORD provenance are retained. The borrowed HGLOBAL is never freed.
- Enabled master effects now publish chapters as exact integer-frame intervals
  of the accepted processed master. A stateful effect is applied once across the
  whole work, and chapter nodes are cached against its digest and frame range.
  Existing speech-cache and master-effect assertions remain unchanged. Added
  real-FFmpeg controls cover continuity, interrupted resume, RF64 and one-frame
  output refusal. Execution of this new composition is still required.
- The clip-admission fixture now creates the library before broker admission.
  The close fixture asserts both native-close phases before editor release and
  final preview unlink; it preserves the real two-phase ownership contract.
- A newly occupied publication destination is rejected without reading the
  competing file. Recorded EPUB shares the actual operation_progress owner.

## Primary ownership and reviewed reading preparation successor

- Primary Director now admits CPU, declared memory, disk and selected MusicGen
  device through the existing ResourceBroker before native discovery or production.
  Completed verified receipts remain passive. Existing compatibility voice, model
  and FFmpeg calls use the same owned native process family; staging, diagnostics,
  required reader cleanup, reservation and render lock remain owned through
  uncertain shutdown. The Windows Job mechanism is unchanged. Linux retains its
  unreaped session leader and verified pidfds until group exit is proved; failure
  to prove or dispose ownership is explicit. This is not a sandbox for engines.
- Book, Book batch and scene production now retain their same existing native
  lock paths while a causal cleanup owner is pending, including masked cancellation
  and failed status writes. Accepted source/cache parts are preserved for explicit
  resume. The exact processed-master chapter frame fix is retained.
- Reading preparation uses the existing ReadingPreview/ReaderToolSession and one
  undoable derivative edit. English, Ukrainian or Slovak is selected explicitly;
  all transformations start off. Finite digit names, valid ISO date field labels,
  selected symbols and existing cleanup rules require preview and acceptance.
  No language, natural grammar, stress, phonemes or voice switch is inferred.
- New strict child/ownership, Director admission and preparation suites are added
  to source and installed inventories. The native reading-preparation probe runs
  from source, installed package and complete bundle, retaining its 180-second
  watchdog and exact module hashes. Existing selectors and native process gates
  are unchanged. Only scheduling budgets accommodate the additional work.

All these successor changes are source-reviewed and **NOT_RUN** at composition.
Earlier successes at d5f6048 or failures at4917 do not qualify this new head.
Preflight46035380537 at a9da9c4 and the six94ecfdd workflows were still queued at
the last check. A successful current full aggregate and all six same-head normal
workflows are still mandatory before an application wheel is eligible.

## Exact evidence available before this checkpoint

The process deadline/ownership diagnosis at
`d5f6048f2ac13b8d7e938985c5db60f8600e5c2b`,
[run 46017828320](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46017828320),
completed on Python 3.11.9 and pinned 3.11.18. Each profile passed 151 installed
strict checks, 50 source checks with two concurrent suites, and 15 actual native
process cases, without failures, skips or expected failures. This run used the
exact e51 application plus diagnostics; it does not execute the new feature trees.

The earlier feature preflight
[run 46016864492](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46016864492)
at 4917e707 failed. The bounded evidence reader
[run 46025168671](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46025168671)
retained every module verdict and recovered the actual failures. Linux passed
85 of 90 modules and the separate 55 provider-policy controls; Windows failed
five of its twelve clip/effect modules. The causes were the owned two-link clip
publication pair, missing library directories in two fixtures, a stale one-close
fixture, and chapters copied before master effects. Both native clipboard runs
failed exact original-whitespace equality. Both native reading-tabs and both
appearance probes passed on that old head. The corrections above require a new
run; these older successes do not qualify the newly composed product.

The capture diagnosis at 905ee6fb showed format-query success but actual
waveInOpen failure for every tested mono/stereo 44.1/48 kHz input. DirectShow also
rejected setting its current and advertised formats. The follow-up
[run 46028171421](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46028171421)
at 05594e86 adds exact-endpoint shared WASAPI HRESULT reporting without Start,
buffer acquisition, device fallback, or policy changes. It is diagnostic evidence,
not a successful capture gate. The required measured capture gate remains intact.

The corrected production/clipboard composition `751c4abc` was submitted to
[preflight 46031798475](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46031798475).
The ordinary evaluation candidate `94ecfdd4` adds only the retained capture
workflow to that product tree. All six expected workflow records exist on that
SHA: main `46031920263`, V3 `46031920252`, compatibility `46031920482`,
transcription `46031920280`, bibliography `46031920357`, and launcher
`46031920446`. At this checkpoint they remain queued; no eligible application
wheel is available. The newer OCR/navigation/pagination composition has not
borrowed their results.

## Qualification carried forward

The main integrated workflow retains every earlier selector and adds the new
source and installed-package suites. Native desktop, incremental backup, Book,
conversion, metadata, player, recorded-EPUB and DAISY controls run from source and
the installed application; the complete bundle runs the corresponding probes.
Independent DAISY DTD validation uses the exact five pinned official resources
and a deliberately invalid XHTML control. Existing EPUBCheck gates remain.
Automatic OCR controls require the actual pinned detector and actual English
Tesseract on a synthetic scanned PDF; missing prerequisites fail the selected
gate. Native source/installed/bundle chooser and result-page controls are added,
plus scoped speech on the existing explicitly identified output endpoint. The
complete bundle keeps its own explicitly selected OCR runtime instead of being
rebound to a source-test runtime. No runtime or private payload is uploaded.

Only job/step scheduling budgets grow to accommodate added checks. Individual
native watchdogs, process deadlines, skip/xfail rejection, source authorization,
publication guards and required device identity are not relaxed. The preflight
retains complete logs in its existing artifacts and prints bounded diagnostic
views while preserving each original test-process exit status.

The exact application wheel is eligible for private final assembly only after
the complete Windows aggregate succeeds and all six ordinary required workflows
are green on that same candidate. A local wheel, a wheel from a different SHA,
or a partial/failed-head artifact cannot substitute. The private deliverable
must still omit the optional audio runtime and offer explicit local setup.

## Remaining specification work, not reclassified as external-only blockers

The successor integration section below advances typed assistant/player/Book
routes, native Word controls and the managed voice/reference lifecycle to source
integration. None has new execution or manual qualification evidence yet.
Durable/scalable indexing, ranked continuation and larger bounded source/import
operations beyond exact-search paging still require work. Conflict-reviewed
portable sync and explicit streamed large-source intake are being reconciled
against the current backup and intake owners. Existing nearby menus and isolated
adapters do not satisfy these requirements. Keep one domain service per route
and retain bounded resource admission.

Real authorized model/reference-voice execution, the per-capability 135-entry
language matrix, native-speaker quality review, Windows 11/manual NVDA,
physical capture/monitoring and listening, real A-to-B updater/installer
qualification, signing and distribution/entitlement rights retain their own
evidence requirements. Optional legacy speech engines, BRF/Liblouis, encrypted
or scheduled backups and watched-folder/plugin execution remain explicitly
unqualified where not implemented and exercised.

The execution host was unavailable during this checkpoint. Work was preserved
through immutable repository objects and branch updates; no private final ZIP
has been assembled or presented as a completed delivery.


## Actual 94ec failures and canonical reconciliation (2026-11-27 17:21 UTC)

The six-workflow evaluation at `94ecfdd48c65d03f0b29abee9000fd4e2a1610ee`
started executing. Both transcription matrix jobs failed the same existing
`test_transcription_backup.py` module: 110 passed, 33 failed, zero skips.
The other nineteen selected modules passed on both platforms. The new narrow
correction validates legacy state filenames in their final `state/` namespace
and applies the existing typed cache-inclusion policy to native state manifests
before restore publication. All original transcription assertions remain.

The V3 run46031920252 exposed actual collection/fixture defects: the DAISY
adapter imported missing shared XML budgets, the Book CLI retained an unavailable
donor entrypoint decorator, one measured-audio test omitted its parametrized
`masked` argument, the local-runtime CLI doubles lacked mandatory service close,
and seven reader cleanup cases omitted their actual playback-session closure.
The corrections preserve current production lifetime owners and every existing
test, strengthening close-order and pending-listening assertions. The shared
XML parser now enforces bounded member length and aggregate metadata size.
Book CLI uses this product's existing main-to-Studio entrypoint contract.

Canonical advanced to `e5c64a5245583f8f8b404a0f042f6ca438c24d7f` (PR183).
Its exact Save All controller `c109f48ccb561b25fc9f348dacc4688f734f2e6b`
and tests `849fcdf2b0ad71d63be4a08ea1b044554f988881` are retained,
including registration identity, late durable completion and usable failure
focus. The governing AGENTS and both updated V3 technical documents are unchanged.

The reader-preparation native fixture now waits for the real reader to become
usable before keyboard input. Its existing native Windows step is placed in
the Windows preflight job; portable preparation service contracts remain in
Linux. No original native watchdog, test selector or qualification rule is
removed. Full source suites for these corrections remain required.

These source-reviewed successor corrections are **NOT_RUN**. Neither the
failing94ec aggregate nor earlier component successes qualify the new source.
No eligible exact-head wheel or private final archive is available.


## V3-03/08/09/21 domain composition (2026-11-27 17:46 UTC)

This checkpoint composes the following source-reviewed adapters on
`d3130096b1f9d09fd9f402a207c55c05d5b76993`, retaining canonical
`e5c64a5245583f8f8b404a0f042f6ca438c24d7f` and its Save All correction.
The technical-document identities remain unchanged. The immutable sparse
trees below identify source selection, not executed or qualified artifacts.

| Requirement | Reused boundary and delivered scenario | Exact reviewed tree | Execution and limitation |
|---|---|---|---|
| V3-03, V3-21 | Existing voice manager, registry, owner worker, broker and export publication: review, revoke, remove and data-only export of one exact installed version | `c451e43b3eb873643adef6d031e22646c8f51116` | INTEGRATED / NOT_RUN; unencrypted local export; real model/voice quality and redistribution rights unqualified |
| V3-21 | Explicit Omnivoice reference preparation through the existing owner API; actual PCM/FFprobe review and a separately approved saved package | `7ff702a01842d33272123d6ddd98b4971a0a87e7` | INTEGRATED / NOT_RUN; no inference, training, registration or quality claim from making a package |
| V3-08 | Current CSL and approved source citations, owned Word fields, bibliography and reviewed note operations through optional live Word COM | `823c81398a8979e979c0907d768bd32f2e3d0bf7` | INTEGRATED / NOT_RUN; native modeled-Word gates and real Office/NVDA acceptance are separate |
| V3-09, V3-13 | Existing typed assistant plan/store/dispatcher: local library review, exact current reader status/pause/stop, and existing reviewed Book/batch plans | `508a9457db80cba53e1e5dbaadd93a1cfa2697d1` | INTEGRATED / NOT_RUN; no new player, job database or direct shell authority |
| V3-10 | Existing package-dialog worker and actual desktop menu/keyboard capture | `e3c4cd1531402e917535cacda6a4650709f3c48b` | INTEGRATED / NOT_RUN; native keyboard controls do not certify manual NVDA speech |

### Voice ownership, exact bytes and explicit decisions

Removal binds the selected profile/version, current registry revision, manifest
and index identities, and complete selected-file inventory. Revocation and
deletion require distinct typed confirmation. The current owner uninstall API
runs under its actual lock and the shared broker's exclusive voice-maintenance
admission. Existing manifest tombstones prevent a revoked profile from silently
reappearing; a new explicit approval is required to reactivate it.

Package export captures the same staged inode and bounded bytes before and after
the owner's inspection. The shared publisher revalidates destination parents,
exact hash and stage identity after fsync immediately before create-only
publication. Post-link reconciliation first proves the exact owned two-link
inode, then checks the content; it never reads a competing destination as if it
were the new package. An uncertain or foreign destination is not deleted.
A proven durable receipt survives later cancellation or cleanup failure, with a
separate cleanup warning. Focused negative tests cover inspection-time mutation,
fsync-time mutation, competing output, changed parent, unexpected link count,
post-link changes and late cancellation. They are new test source, NOT_RUN.

The reference creator borrows the existing dialog worker. It reviews bounded
PCM16 WAV frames and actual selected FFprobe results, exact transcript and
metadata, and independent permission and quality checkboxes. It supports only
the explicitly configured Omnivoice reference API; this is not training for
Holos/OpenVoice. The existing recording dialog can return a verified recording
after the user's explicit choice. Creating a data-only package does not install
or authorize it. Optional parent handoff performs read-only inspection and
leaves rights/language approval empty.

The native reference probe has 20 fixed controls/receipt assertions, source and
installed module hash proof, real FFprobe/PCM and an isolated synthetic owner
process. Its file picker is explicitly synthetic; NVDA, human voice quality and
model inference remain NOT_RUN. The existing parent dialog now revokes work on
direct native destruction, ignores child destruction events, and rechecks the
exact export review after a modal destination selection.

### Existing Word and assistant owners

Word operations bind the active document/window, selection, reviewed source
citation and current preview before mutation. They preserve unrelated fields,
prose and notes. A plan is consumed before writing; a partial operation is not
automatically replayed or undone. A cancelled or overdue mutating COM call keeps
its worker until the call actually returns. The adapter does not launch, open,
save or quit Word. Native CI uses a declared modeled Word object with real
wx controls/current services; real Office use requires an explicitly selected
open document and its own acceptance.

Assistant routes retain typed allowlists and current scope/approval checks.
Player operations refer only to the exact current session and cannot start a
replacement player. Book and batch execution reuses current stored plans,
fingerprints, output reviews, cache/jobs and resource admission. Passive
previews, restored checkpoints and verified completed results do not activate
a local audio runtime. The CLI binds the selected runtime only for pending
media work and closes the real producer before releasing that runtime. A causal
pending native close retains the ordered release owner; later cleanup does not
fall through and unbind the still-owned runtime.

### Gate and actual-failure reconciliation

The main workflow retains all 410 earlier individual test selectors and adds
eleven new domain selectors to both source and installed inventories. The
preflight retains all 185 earlier individual selectors and adds thirteen domain
and lifecycle selectors. The V3 aggregate retains its whole existing selector
set and adds three UI modules. Native Word/current-reader assistant and
reference-package scenarios run from source and installed wheels, with the
same probes also selected in the complete bundle. Scheduling budgets grow;
individual native watchdogs, skip/xfail rejection and required evidence do not
change. Full dependency count is corrected from 51 to the already retained
52 exact pins, including py3langid.

The actual compatibility run46031920482 at `94ecfdd4` failed only the
existing composite-backup schema-type diagnostic assertion; the other38 modules
passed. Boolean Studio-job schema was already rejected, but with a mismatching
diagnostic. The one-path correction
`32378f4073c1ec24b32979f3fda7b10aad20e8b0` restores the explicit schema-type
message before decoding the project. No original test expectation is weakened.
The V3 and transcription failures described above remain evidence for that old
head only. Launcher run46031920446 passed both native compiler profiles.
Main run46031920263 was still executing its final strict inventory, with actual
earlier native/capture/bundle failures; bibliography remained queued.

Every new adapter, fixture and corrected source at this checkpoint remains
**NOT_RUN**. No earlier result is relabeled as evidence for this new composition.
The full same-head Windows aggregate and all six ordinary workflows are still
required. No eligible application wheel or private final archive exists.


## V3-06/08 portable snapshot composition (2026-11-27 18:07 UTC)

The reviewed current-domain Sync selection is
`4837346fe62b7926585d44c26ae8f3891961c8d9` (27 source/test/guide paths),
reconciled from PR99 `5f18481654277425ab8af170575de978e7a2a69c`.
Its narrow backup hooks are separately checkpointed at
`88a72077e2787f440bb175baf1470082f8861345`. The existing backup format 5,
catalog schema 6, original-data validation and passive restore contracts remain
the authority. No live SQLite database is shared between devices.

- Explicit publication uses a separate local transfer directory and immutable
  verified chunks. A separately retained transfer token binds the actual
  manifest. Expected-parent and local-history checks reject divergent history;
  the user must review/restore it separately or select a new transfer directory.
- Source admission measures only the existing backup producer roots. The shared
  ResourceBroker accounts for one CPU slot, declared 256 MiB crypto/archive
  memory and bounded measured staging/final-volume disk copies. Source growth
  while waiting rejects the attempt. These are cooperative declared estimates,
  not a native memory sandbox.
- The existing backup verifier is run on original backup bytes before restore.
  Its internal final-destination projection binds restored clip/Book/pending
  paths to the final new library, even while actual writes remain in private
  preparation. External execution/rights/runtime authority stays revoked.
- One existing broker lease remains held through authenticated reading, fresh
  canonical copy, exclusive final directory publication and staging cleanup.
  Its latched cancellation controls each phase. A proven publication followed
  by cleanup trouble retains a distinct receipt; it is not reported as absent.
- The native UI borrows DesktopExecutor, captures the current application and
  window owner, and revokes callbacks on direct native destruction. Queued
  passwords are released even if their future never starts. The existing main
  entrypoint dispatches `sync`; a nonexistent donor updater decorator is not
  introduced. Only passive, typed `portable-sync-state.json` history joins
  current backups.

### Selected encryption and exact-file correction

The same optional PyCA cryptography 50.0.2 envelope is reused, with its existing
AES-GCM/scrypt/HKDF primitives and unchanged wire format. Encryption is an
explicit choice and cannot fall back to plaintext. Passphrases are not command
arguments, saved history, transfer labels or diagnostics. Python/native memory
cannot promise secure erasure; the guide records that limitation.

The independently reviewed two-path correction
`cedbcf2288339d486bc73b4185f0d62ef5a4b996` changes only the existing crypto
output owner and adds ten actual cipher/filesystem test cases. After fsync, the
output is hashed against the authenticated writer's expected bytes, using the
same captured inode and finite byte count. A newly computed hash cannot approve
a changed stage. Precommit source/destination/cancellation checks remain.
Postcommit verification ignores late Cancel but proves the exact owned output;
uncertain bytes remain for explicit review. Cleanup refuses to delete a staging
name replaced by another file. Cipher primitives, header, input contract and
CryptoReceipt schema are byte-identical to the reviewed donor adaptation.
These new cases are source only, NOT_RUN.

Three exact publisher-verified Windows wheels join the existing locks/SBOM:
cryptography 50.0.2, cffi 2.1.1 and pycparser 3.12. Core/full inventories now
contain 19/55 packages respectively. Existing hashes/pins are retained; the
manual supplemental entries do not claim a new resolver run. Exact wheel
hashes, source links and license declarations are recorded in
[SYNC_DEPENDENCIES.md](SYNC_DEPENDENCIES.md). Actual wheel installation, native
compatibility and qualification still require the current candidate gates.

### Required checks and guide delivery

All twelve Sync/crypto-publication/UI suites join main source and installed
inventories, with the unchanged POSIX cleanup scenario additionally required
in Linux. The POSIX scenario is moved explicitly out of Windows selection;
no platform early return or skip counts as a pass. The V3 aggregate selects
the whole Sync directory. Native encrypted snapshot review/publish/restore
runs from source, installed wheel and full bundle with the existing 180-second
probe watchdog. It verifies actual SQLite sources/citations and native
confirmation ownership, not cloud/WebDAV service behavior or manual NVDA.

The bundle guide list now includes the implemented voice/reference, reading
preparation, Book batch, incremental backup, publishing, search continuation,
Word, assistant and portable-transfer guides, plus the existing explicit local
audio-runtime setup guide. Its packaging fixture gets the same real required
filenames; existing packaging assertions remain.

Two new native probes receive exact distribution identity corrections:
`b44358a8a1dcbeaf01560591c9992d438ffd501f`. The reference probe now filters
the real `universal-research-library` distribution, not the import-directory
name. The assistant probe resolves the exact wheel RECORD owner of its imported
package and matches all nine module files to the current checkout bytes.
Source acceptance requires this exact checkout. This strengthens evidence;
it does not claim either probe has executed.

### New capture diagnosis, not capture qualification

The exact endpoint diagnosis at
[run 46028171421](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46028171421)
completed at `05594e86f12f012fff0ded2b93646eab1658e7d5`.
WASAPI read the exact active capture endpoint, its unchanged identity, device
period and mix format, but shared Initialize returned `0x80070005` (access
denied). It never started a capture stream or requested data buffers. All four
WinMM opens also failed; DirectShow format application returned `0x80004005`.
The successful job conclusion means diagnosis completed, not recording passed.

A separate read-only diagnostic branch
`carr/capture-access-dbbb-20261127`, commit
`f2fe6dcbd54cd5bb0a5a0587993177aa412d1f93`, adds fixed microphone consent,
machine-policy and process-session observations. It neither writes registry
settings nor changes consent, policy, endpoint or device fallback. Main run
46031920263 at `94ecfdd4` completed with failure at 17:58 UTC; its exact final
artifact is retained for a separate bounded evidence read. The selected capture gate
remains required and failing until actual valid capture evidence exists.

All source in this composition is **INTEGRATED / NOT_RUN**. The streamed large
TXT and wider scoped/ranked/federated continuation work is separate and not
claimed by this checkpoint. Protected voice-package transfer is also separate
from this library-snapshot envelope. Full exact-head execution, model/device
quality, Windows 11/manual NVDA and final release evidence remain outstanding.

## Observed Windows failures and bounded corrections (2026-11-27 18:23 UTC)

The completed native evidence from run 46031920263 at `94ecfdd4` was read from
its exact retained pre-source/recording artifacts by diagnostic run
[46040872548](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46040872548).
That read did not execute or qualify any new source.

- Audio batch review passed its preceding native checks, then could not open the
  review dialog. The current UI passed `progress_total`/`progress_unit` to the
  existing DesktopFrame.run_job, which has neither parameter. Both calls now
  use its existing `with_progress=True` contract. The service still reports
  actual completed/total/phase tuples. The existing worker/UI regression fixture
  now has the real explicit job signature and checks both exact progress tuples;
  all cancellation, changed-library and committed-receipt assertions remain.
- Reopening the reader before deferred native Destroy left its old desktop
  command registration attached. Checkpoint `18bdaaf9` retains the returned
  detach token and releases only that token after native/worker ownership cleanup,
  before Destroy. Three focused cases cover normal close and output/worker
  failure followed by immediate reopen. Native appearance assertions remain.
- The incremental-backup probe now obtains its ReadingLocation from the current
  active_page view rather than a removed catalog_edition.active_revision column.
  Actual incremental creation, fresh restore and original-data checks remain.
- The output-routing probe constructs a real LibraryApp before its existing
  OperationProgress and closes progress only if initialization succeeded. Actual
  endpoint selection, pause/resume/default routing and source-integrity checks
  remain required.
- Native multiline text presentation compares against the exact admitted text
  with only CRLF-to-LF conversion required by wx presentation. Separate existing
  assertions still require exact raw clipboard bytes, immutable source_text and
  unchanged retained source/manual draft. This does not normalize source data.
- The player-queue probe `23a6ac7b` only adds bounded actual MCI/status/owner
  diagnostics on its existing failure. Its playing/position condition, route
  and deadline are unchanged. The queue cause remains unproven; this is not a fix.

All corrections are **NOT_RUN**. Earlier d313 corrections cover the actual
DAISY constant import, native Book lease wrapper and transcription cache/root
restoration failures. Current source and installed suites must establish that
these changes resolve the original failures. The failed `94ecfdd4` run retained
no eligible wheel; a final bundle cannot use its wheel.

## Explicit finite streamed TXT intake (2026-11-27 18:23 UTC)

Source checkpoint `4eb3aa9d087104da6c27abb3d173302ea5b6a0d8` reuses the
reviewed PR99 intake/attestation path, reconciled onto current application,
owned interpreter, resource admission and format-5 backup. The separate narrow
CLI/Desktop hooks from `9c4f8100` are composed with the current Word and Sync
hooks; no whole stale Desktop/application file replaces another domain.
The Sync-preserving library_backup seam is `d26c07dd` on `0391f0da`.

Only explicit UTF-8 TXT intake accepts a selected 1–128 MiB allowance, with
strict incremental decoding and at most 8,192 finite logical fragments. Ordinary
64 MiB intake and format limits remain. Original hashes, field/character offsets,
sealed immutable revisions and quotation verification remain authoritative.
The donor's later multi-gigabyte/PDF profiles and capacity-v4 state are excluded.

The existing import worker receives the typed source profile and current resource
budget. Pending ordinary backups retain their original v1 shape; only explicit
streamed requests use the bounded v3 component, whose profile inventory is
validated against actual requests/journal configurations. Outer backup format5
and catalog schema6 are unchanged. Copy/restore preserves original bytes and
revokes execution authority; no live database or worker is transferred.

Opening a retained original now performs complete verification and viewing-copy
work on the existing Desktop worker. Only the fresh selection check and native
opener run on wx. The worker retains both verified readers through that action;
queued cancellation cannot cause a later launch and late cancellation cannot
relabel an acknowledged native open. Existing normal-source assertions remain.

Twelve focused suites join source and installed/full-runtime inventories,
including actual files larger than 64 MiB and real backup/restore. Native probe
`dbb66d72` uses real Import Center controls, the existing worker/child and current
SQLite catalog, verifies quotation and unchanged original, and binds exact
source or unique installed RECORD module bytes. It has an independent 180-second
watchdog through cleanup. Its small native fixture does not claim large-file
capacity, OS file-picker behavior or manual NVDA; those remain separate evidence.
The delivered guide list includes LARGE_SOURCE_PROFILES.md.

All new code remains **INTEGRATED / NOT_RUN**. Scope-ranked and federated search
continuation and protected voice-package transfer are separate pending changes.

### Capture-access diagnostic boundary

Run [46045811596](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46045811596)
at `f2fe6dcb` did read the fixed consent/policy keys without writes. Its device
setup was correctly rejected by the existing exact evaluation-branch guard;
therefore it did not repeat the earlier endpoint failure. The machine microphone
Value was Allow; the queried user/desktop/policy values were not configured at
those exact keys; process session ID was 2. These observations do not establish
the cause of E_ACCESSDENIED on a different runner. The next ordinary evaluation
retains that guard unchanged and runs the read-only observations on the already
authorized `carr/ci-endpoint-evaluation-20261127` branch. No environment spoofing,
consent/policy mutation or device fallback is introduced.

## Protected voice-profile transfer composition (2026-11-27)

The independently reviewed six-path checkpoint
`94886918b6b2da20cf38a7de6faa08241198ad86` extends the current lifecycle and
Reference voice packages window. It reuses the same pinned Sync envelope and
exact package publisher; there is no second crypto format, engine registry,
executor, provider or backup family. Existing plaintext routes remain explicit.

The new `.carr-voice.enc` route is data-only and requires separate export rights,
a confirmed operation-only password and a new destination outside runtime/library.
The original owner export is fully inspected and its exact plaintext identity
rechecked before/after encryption. The existing create-only publisher binds the
actual ciphertext receipt; committed outcomes remain reported after late cleanup.

Import authenticates the entire envelope under one measured broker lease before
the current owner inspector receives plaintext. The temporary plaintext is removed
before a passive review is returned. Applying that review asks for the password
again, reauthenticates/reinspects the same source, plaintext, document, library
inode and runtime/settings identities, then invokes the existing manager.apply
with fresh rights and language decisions. No lease or plaintext stage crosses
a modal review; passwords never enter owner requests, state or backups. Queued
secret holders are cleared on settlement even when their Future never starts or
the actual window is destroyed. Memory erasure is not promised.

Two actual-envelope/owner and UI-lifetime suites join the source and installed
inventories. The native `5fe82576` probe runs from exact source, installed wheel
and full bundle. It uses native masked input/keys, the existing worker and an
isolated synthetic owner API, real authenticated crypto, exact module/RECORD proof
and a 180-second independent cleanup watchdog. OS file-picker interaction, real
reference rights, neural inference, voice quality and manual NVDA are explicitly
not established. PROTECTED_VOICE_PACKAGES.md is included in delivered guides.

This addition is **INTEGRATED / NOT_RUN** until its exact candidate gates finish.

### Actual partial Windows evidence for an earlier composition

The still-running preflight
[46048596823](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46048596823)
uses exactly `142de6b6028cc86f9a5bf70c42e60adc40f96b77` (not this later source).
Its required source+installed native reference-package step completed successfully
at 18:26:54 UTC and its required source+installed encrypted portable-snapshot
review/restore step at 18:27:02 UTC. Dependency installation and these actual
native paths therefore have execution evidence at that earlier identity.
The DAISY-schema/publication, reading-preparation and combined Word/assistant
native steps failed and require their actual retained diagnostics. This does not
make the overall preflight or any later composition PASS. No eligible wheel has
been retained and no manual/model/language qualification is inferred.
