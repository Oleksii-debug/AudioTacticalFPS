# Reading, audio production and recovery integration — 2026-11-27

## Authority and status

This checkpoint follows root AGENTS.md `080bb9104d3224749e9317c2273d803c0ac37835`,
the current finalization specification `db8adbdc483a03c262885928c7ed214ca70bbaa7`,
and the detailed reading/audio requirements `d0b2761dd22b33233b311d4800230fb47f5d3d8b`.
It extends the existing Resonance application in `carr-library/`.
The canonical integration parent is `a44fac04072389caa40e0d6e799b13797712f797`;
the previous composed product head is `e51d7db45ca8eb1f1ef2d77b32648d0bce4bd637`.

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
- Native clipboard diagnostics now persist bounded starting/failure receipts,
  exact loaded-module hashes and wheel RECORD provenance while retaining every
  existing native assertion. No product cause is guessed from a missing report.
- A newly occupied publication destination is rejected without reading the
  competing file. Recorded EPUB shares the actual operation_progress owner.

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
at 4917e707 failed. Retained evidence identified the clip two-link cleanup failure;
the native clipboard/other failed verdicts remain subject to the bounded evidence
reader and the current rerun. Its successful unrelated steps are not borrowed as
qualification for this checkpoint.

The capture diagnosis at 905ee6fb showed format-query success but actual
waveInOpen failure for every tested mono/stereo 44.1/48 kHz input. DirectShow also
rejected setting its current and advertised formats. The follow-up
[run 46028171421](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46028171421)
at 05594e86 adds exact-endpoint shared WASAPI HRESULT reporting without Start,
buffer acquisition, device fallback, or policy changes. It is diagnostic evidence,
not a successful capture gate. The required measured capture gate remains intact.

## Qualification carried forward

The main integrated workflow retains every earlier selector and adds the new
source and installed-package suites. Native desktop, incremental backup, Book,
conversion, metadata, player, recorded-EPUB and DAISY controls run from source and
the installed application; the complete bundle runs the corresponding probes.
Independent DAISY DTD validation uses the exact five pinned official resources
and a deliberately invalid XHTML control. Existing EPUBCheck gates remain.

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
automatic installed-language/per-region OCR; explicit current word, line,
sentence and paragraph speech; wider typed assistant/audio tool coverage and
shared admission for the primary Director; language-aware text preparation;
scalable continuation/indexing beyond the existing bounded search; native Word
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
