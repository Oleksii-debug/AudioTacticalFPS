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

## Larger local integration awaiting recovery and publication

The development executor disconnected after the clean integrated local commit
`cccae8c8f480537551a7034d62bf3dae51f02558`
(tree `d21516b372108e526a4e3d79dd74a6c7743eff6d`) had been prepared for
publication. That larger tree was **not published** before the disconnect.

Its implemented additions include:

- Actual pinned CSL rendering and BibTeX parsing; citation style and custom-style
  workbench; structured academic metadata; reviewed export destinations.
- A local, allowlisted assistant with read-only plan preview, explicit approval,
  durable restart reapproval, bounded operations and verified runtime reuse.
- One owned Audio Studio project namespace, typed input/history inventories,
  streamed common-format backups and publication-crash recovery.
- Main backup GUI/CLI options with generated derivatives excluded by default.
- Shared operation progress, source-preserving producer callbacks and focus
  handling, plus the corresponding integrated packaging and test registration.

Component gates were run locally before the disconnect, including 519 CSL and
bibliography cases, 79 assistant core cases, 197 managed-audio/common-backup
cases in the independent integration worktree, and focused backup GUI/CLI gates.
These overlapping component results are not an exact-head complete Windows
acceptance total. A prior 22-case actual-codec gate tested the MP3 delivery
mapping locally; the new historical-completion regression in this checkpoint
still needs its own execution.

Two additional source commits also await composition with that larger
tree:

- `306686c06f1d80c08bcfddf33aeb739ea23c94b7`: explicit generated-audio scope in
  portable snapshots and the sync CLI, mode-bound resume/cache identity and
  actual plain/encrypted generated-audio roundtrips.
- `94d4ffeeb86920a038839b1cd516ef71dd748183`: installed native assistant probe.
  Compilation and review passed; its local execution correctly returned
  NOT_RUN because there was no native display.

The progress/media follow-up is verified but uncommitted in its isolated
worktree. The explicit external-input adoption follow-up is uncommitted and
untested. The assistant's optional generated-audio backup argument was designed
but not implemented before the disconnect. Do not silently treat any of these
as part of the published package.

After executor recovery, recover the exact local commits/diffs, compose this
published correction checkpoint without losing its MP3 cache-policy fix,
finish the outstanding targeted work, and rerun the composed required gates and
source-wheel/installed-package checks.

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

Standalone transcription remains the explicitly deferred integration from the
owner's separate project; it is not replaced here with another ASR stack.

Public changes contain source and synthetic test evidence only. No installers,
model weights, private voices, user books, credentials or generated recordings
are included. There is no public-release or final-delivery claim.
