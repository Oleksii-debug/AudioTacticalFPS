# R7 selected audio feature port

## Source and validation state

This port is based on frozen R6 commit
`d9db9f676bd7b0d282a2c6e2512b20f02b603b65`.
Selected feature code was reviewed against owner reference
`078d254bd4ab4dbe9de4ea80e1e26731cb8ac9e8`.
The owner head is provenance only: it is not made an ancestor of this selected
port, and none of its prior execution results qualifies this integrated source.

**Status: NOT_RUN.** The local executor was unavailable while this merge was
prepared. File/hash readback and static dependency/guard review are recorded;
source tests, isolated installed tests, native Windows probes and independent
book checkers must execute against the final R7 source before acceptance.
No real-voice, microphone-device, reader-interoperability, manual NVDA, full
DAISY-rule or release-wide qualification is inferred from these fixtures.

## Added behavior

The existing production parser, immutable typed plan, source-preserving Studio
draft, renderer, chunk cache and managed project store now support:

- Version 2 chapter clips with retained PCM inputs, gain/pan, sample-bounded
  trim/duration/fades, track roles, position and minimum chapter duration.
  Add/edit/remove/move/copy/cut/paste/split/join and explicit selected-clip
  preview use the current Studio worker and stale-result guards.
- Version 3 master effects and explicit dry/wet mix. Master processing preserves
  the measured programme frame count; chapter outputs are exact partitions
  of the processed master, and stems remain labelled pre-master.
- Role/master effect editing, ordering, bypass and typed JSON presets.
- Explicit canonical-project recovery into the current managed project format,
  retaining exact original JSON/script provenance and independently copied,
  explicitly selected current sound inputs.
- EPUB 3 and DAISY 2.02 export from an already verified completed render, with
  exact plan/artifact receipts, measured line intervals, explicit untranscribed
  audio intervals, no-overwrite publication and offline verification.

Version 1 serialization and its existing cache identities remain unchanged.
Literal narration does not interpret clip or master directives.
A legacy completed render gains an additive plan digest only after the exact
typed plan, runtime/input identities and completed output are verified.

## Current guards retained

The common files were merged selectively. This port retains:

- The current TrustController and audio.render admission in render_project,
  _render_locked and StudioController, including completed-result replay before
  new-operation admission. New clip-preview DSP also captures this same
  admission once after a verified cache miss; a checked existing preview remains
  readable after expiry. Canonical import forwards the exact controller object
  into its replacement StudioController.
- Progress as an observer: observer start/failure/finish/tick errors do not lose
  a durable result or replace the real worker failure. Existing late-completion,
  stale-controller and exported-filename safeguards remain.
- Current microphone/retake controls, recording monitor, pause/resume model,
  recording_process and Windows process owner. The alternate capture_process,
  capture_types and windows_job families are not introduced.
- wav_info(allow_empty=...) and every current empty-part validation branch used
  by recording pause handling. The only FFmpeg helper edits pass the existing
  cancellation callback through five final no-overwrite publications.
- Existing document_request(original_bytes=...) and large-PDF disk allowances.
  Only production_request changes: declared clip source durations, timeline
  tails, track stems and master intermediates are included in audio reservations.
- Existing audio.backup payload identities and current recording contracts.
  Added inventory support covers clip inputs, canonical project provenance and
  publication receipts. No external saved path is reactivated on restore.

audio.cleanup gains wait_for_cleanup using its existing CleanupOwner,
AudioCleanupPending and quiescence event. It keeps synchronous checker/converter
scopes alive until owned native cleanup is confirmed and delivers a pending
KeyboardInterrupt/SystemExit only after that boundary. It adds no process
supervisor. EPUBCheck uses the current recording_process runner and budget.

## Required selectors and native receipt fields

`validation/AUDIO_R7_SELECTION_20261127.json` is the machine-readable
selection contract. Its counts are statically enumerated expected populations,
not executed passing counts. The 17 changed/added test files total **341**
cases. New audio files add 220 cases; the existing extended-effects file adds
one real DSP order/bypass case. Five new UI files total 54 cases. The existing
signed audio-admission file gains one real clip-preview admission/replay case.

Existing tests/ui/test_audio_studio.py is unchanged at 52 cases, including its
observer and late-writer safeguards. Existing production-integrity and all
recording/pause/monitor/Windows process tests remain in the candidate.
scripts/ci_audio_check.py retains its required native/long-PCM cases and
XFAIL/XPASS/missing-test rejection. Main-workflow selection and population
oracles must include the new files; this port does not modify that workflow.

Native effects retains its original 109 checks and adds 42 chain/master/preset
checks, for **151**. The new clip native probe requires **24** checks across
11 explicit actions; the publication metadata probe requires **11** checks.
These three probes record isolated installed module origins and SHA256 values.
The existing native Studio probe remains unchanged at 16 checks. Their JSON
fields and separate NOT_RUN boundaries are listed in the selection contract.

## Independent checker gate

The separate carr-publication workflow uses exact-source checkout and a
source-bound wheel, a fresh isolated installed environment, the current 53-pin
Windows production lock, and exact wheel-versus-installed Python hashes.
Both source and installed boundary selections require **17** cases, zero
failures/errors/skips: 13 checker-boundary cases and four cleanup-ownership cases.

EPUBCheck remains pinned to 5.3.0. The upstream release API was re-read for this
port and reports the selected 33,071,108-byte archive and SHA256
`6c07e68584b2e2ce2f89fe06e1246dfead3eb36b46b340e7d93524f29dcff6c5`.
The license at the pinned source commit retains the three BSD redistribution
conditions. The gate requires the archive's license and third-party notices;
the checker is an ephemeral CI tool, not a shipped application dependency.

Primary references:

- https://api.github.com/repos/w3c/epubcheck/releases/tags/v5.3.0
- https://github.com/w3c/epubcheck/blob/029831b8f477e4519e9734c984ee24357547a698/LICENSE.md

The five DAISY XHTML/SMIL DTD/entity inputs retain their explicit byte/hash pins.
Validation resolves only the selected local schema bytes. The runner-provided
Java 17 version and executable hash are recorded. No model or private asset is
downloaded or uploaded.

Actual checker runs must cover three newly rendered synthetic publications for
each format, bind them to the installed source/runtime, and reject their invalid
EPUB/XHTML controls. A final receipt rejects missing/partial/failed execution.
Raw textual results upload on failure; only this run's own ephemeral validator,
runtime and synthetic payload directory is cleaned. The job is bounded at
35 minutes; EPUBCheck has a 15-minute step and each child a 180-second deadline.
The offline DAISY step is bounded at 10 minutes. These are execution limits,
not performance or conformance claims.
