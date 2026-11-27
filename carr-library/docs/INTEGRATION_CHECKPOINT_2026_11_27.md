# Integrated development checkpoint — 2026-11-27

This is a development checkpoint, not acceptance of the whole V3 specification.
The requirements remain those in [Scope V3](UNIVERSAL_SCOPE_V3_AUDIO_AGENT.md)
and its [finalization addendum](UNIVERSAL_SCOPE_V3_FINALIZATION.md).
Keep the integration PR in draft until its exact-source gates and the outstanding
product acceptance work are complete.

## Published predecessor and observed failures

The published predecessor is
`5a70f5ea9c04d5bdc488bf896ce99929369558e2`.

- [Integrated Windows run 45915596274](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45915596274)
  printed 3,764 passing cases over 163 distinct required source-suite paths.
  Repeated invocations of the same suite are counted once. It subsequently
  failed in the native document-format probe: a caption lookup no longer matched
  a reader button whose current shortcut is included in its visible label.
  The actual imports, immutable source bytes, Unicode reader text, unverified
  source status and keyboard focus checks before that lookup passed.
- [Portable snapshot run 45915596223](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45915596223)
  passed 246 required source cases (151 sync, 36 portable UI, 59 desktop).
  Its native confirmation helper failed to find a unique child answer button;
  it did not finish the native publish/restore acceptance or the subsequent
  installed-package checks.
- [Audio run 45915596179](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45915596179)
  reported 249 passing and three failing audio cases, with no skips. The three
  failures shared the same MP3 decoded-duration discrepancy. Separate audio UI,
  provider-boundary, CLI and packaging suites in that job passed.

These are partial results, not three successful workflows.

## Corrections in this checkpoint

### Encoded MP3 delivery and completed-render reuse

The actual pinned Windows FFmpeg 9.1 diagnostic measured 529 extra decoded PCM
frames at source rates 8, 16 and 24 kHz for both requested bitrates. At 32, 44.1 and
48 kHz the measured decoded frame counts matched the input. This is evidence
about that tested runtime, not a claim about every encoder build.

MP3 export now selects a MPEG-1 delivery rate of 32, 44.1 or 48 kHz and writes the
Xing header explicitly. The original PCM and project timeline rate remain
unchanged. Resampling does not claim to restore missing source detail.

The verifier still checks the actual container, codec, channel count, sample
rate, full decoded duration and stable file identity. It does not tolerate the
529-frame discrepancy. Only the existing one-frame resampling allowance remains.
Six additional low-rate cases independently inspect the encoded stream and
count actual decoded PCM outside the production verifier.

Completed MP3 render identity advances to pipeline version 4. Other encoders
retain version 3; reusable PCM chunks retain version 1. A historical, internally
consistent completion receipt cannot bypass the current MP3 policy. The
regression seeds an old completion, changes its artifact and matching
self-described hash, and requires a newly verified output while reusing the
accepted speech chunk.

### Native controls and required-suite execution

The document reader probe resolves its Next action through the stable
`reader.next` command ID. It retains real button-event dispatch, second-segment
loading and unverified-source assertions.

The Main keyboard acceptance probe reuses the reviewed upstream correction
from PR123: it does not overwrite a successful modal result during cleanup,
tests unassigned native text-key fallthrough in a read-only control, and chooses
an unused chord for its cancellation case.

The portable confirmation helper retains process, owner-chain, dialog-class and
title scoping and supports the native Windows dialog button mechanisms. It does
not replace production confirmation dialogs or weaken receipt/restore checks.

The required-suite runner reuses the PR119 implementation. Up to three required
suites run concurrently in the integrated Windows job, each in its own Python
process, temporary directory and strict JUnit gate. Missing, failed, skipped,
XFAIL and XPASS outcomes remain failures. Every future is consumed.

### Managed OCR on Unicode Windows paths

The three reviewed PR128 patches are composed with the current model-ID,
language and preset imports. The bounded OCR process helper preserves the
verified engine and chosen model bytes while accommodating native Unicode path
limitations. The helper participates in the extraction fingerprint.

An explicit Windows gate exercises actual managed OCR with Unicode paths.
The separate runtime-identity gate keeps the same actual engine on PATH and
removes the explicit portable override only while testing PATH discovery and
TESSDATA_PREFIX changes.

## Recovered integrated source and current evidence

The disconnected executor recovered with local commit
`cccae8c8f480537551a7034d62bf3dae51f02558` intact. Its complete source was composed
with the published correction checkpoint, preserving the MP3 policy identity.
The integrated additions now include:

- Pinned CSL and BibTeX implementations, custom-style workbench and reviewed
  academic export destinations.
- A local allowlisted assistant with explicit approval, restart reapproval and
  an exact-boolean generated-audio backup choice bound into the reviewed plan.
- Owned Studio projects with explicit bounded adoption of referenced PCM inputs,
  history/recovery inventories, cancellation and no-clobber publication.
- Streamed common backups; optional generated derivatives in Main and portable
  GUI/CLI workflows; mode-bound snapshot and legacy-resume identities.
- Shared Desktop/Studio/Sync/Assistant progress with bounded callbacks, playback
  notification quiescence, root/window lifetime guards and persisted settings.
- Installed native assistant and extended Studio/progress acceptance probes.

The composed local verification after these merges completed **653 cases across
21 required suites, with zero failures or skips**. It exercised the actual
assistant, adoption and backup/recovery services, sync/encryption, progress/UI
protocols and packaging policy. Earlier CSL/bibliography gates passed 519 cases
across ten suites. These overlapping component totals are not a complete Windows
release total.

### Exact published Windows checkpoint c28015d1

- [Audio 45919953061](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45919953061)
  succeeded: all 259 audio cases passed, including the new independent MP3
  decoded-duration cases and historical-completion regression; separate provider,
  UI, CLI and packaging suites passed. The actual native smoke synthesized two
  installed SAPI voices, rendered stereo/procedural music, measured duration,
  clipping and loudness, retained source text, and exercised Credential Manager.
  Human listening, neural music and NVDA were not run.
- [Portable snapshots 45919953063](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45919953063)
  succeeded: 246 source cases, real installed native plain/encrypted publish and
  restore, followed by 151 installed-only cases outside the source checkout.
- [Integrated Windows 45919953126](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45919953126)
  passed 3,776 cases over 164 required suites, managed Unicode OCR and actual
  runtime fingerprints, native Main/Reader/draft controls, and the corrected
  27-check document-format navigation. It then failed on an obsolete total menu
  count in the academic comparison probe. The source now checks exact-once
  presence of its four required actions; later native/full-bundle acceptance
  still requires a new exact-source run.

Counts from these overlapping jobs must not be added into a unique total.

### Decoder qualification boundary

The supported pinned Windows FFmpeg 9.1 gate passed every new MP3 case. The
local Debian FFmpeg 7.1.5 decoder has an additional observed small-tail boundary:
for 4,321-frame inputs at 8/12/24 kHz, its 32-kHz MP3 decode was respectively
43/44/46 frames longer than the expected resampled duration. The strict gate
rejected these artifacts; it was not relaxed and no output was trimmed to hide
the discrepancy. That older runtime is not qualified for general MP3 export by
this checkpoint. Passing longer local fixtures is not universal qualification.

The complete application wheel and installed package must be checked again
for the final composed source. Component commits and a passing predecessor do
not establish acceptance for later feature composition.

## Current specification and reuse

The requirements now follow canonical
`33dcc42edd6781b9aeb02dcaaf016b386bcf37b6`: Resonance by Sceneria Innovative
Technologies, with the supplied Whisper Transcribe review included. Existing
internal filenames remain compatible pending a tested migration. This branch is
reconciling the newer canonical and PR99 reader/player, language, conversion and
translation modules through the existing tested services; it does not claim
those modules complete merely because another source branch contains them.

## Remaining product acceptance

A final V3 release is not established by a draft PR or by the partial results
above. Remaining work includes full exact-source Windows installed-package
acceptance, genuine Windows 11/NVDA keyboard and listening assessment, the
complete behavioral-parity inventory, actual optional neural music/extended
generation qualification, owner-supplied cloning-runtime execution, and the
canonical 135-language capability and acoustic matrix.

The native launcher is not a signed production installer with all required
Start-menu, repair, uninstall and update behavior. Production signing,
entitlement and update integration require the existing owner-controlled
private authority; no issuer secrets or invented production endpoint may enter
the public repository. These requirements are separate from preserving access
to user-owned data, exports and recovery.

Transcription is now approved platform scope and the owner's implementation
has been supplied for review. Its exact private source, runtime, model rights
and corrected integration still require acceptance; no competing ASR stack is
introduced. A bounded library/audio preview is not the complete platform.

Public changes contain source and synthetic test evidence only. No installers,
model weights, private voices, user books, credentials or generated recordings
are included. There is no public-release or final-delivery claim.
