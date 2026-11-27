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

The audit also identified independent code work beyond the routes above:
wider typed assistant/audio tool coverage; durable/scalable indexing, ranked
continuation and larger bounded source/import operations beyond the exact-search
paging added here; native Word
citation workflows; conflict-reviewed multi-device sync; and the remaining
recording-to-managed-voice lifecycle. These are not satisfied merely by existing
nearby menus or isolated adapters. Separate current work must reconcile these
routes without duplicating domain services or replacing safe limits with
unbounded operations.

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
