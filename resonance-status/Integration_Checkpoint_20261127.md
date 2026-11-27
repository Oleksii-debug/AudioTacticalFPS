# Resonance integration checkpoint — 27 November 2026

This is a development checkpoint, not a qualified release or a claim that the complete platform is finished. Its exact commit and subsequent run results are recorded in [PR #173](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/173).

## Source reconciliation

The first parent is candidate 324b087d0de3abd389ee0056d1c8565a473e3c95. The additional canonical parent is e69ba2403515f21509686d66f4bc8b3673756d81, which merged PR #147. The selected tree retains one current transcription.py service. Canonical's same-named transcription/ package is not installed alongside it: Python would otherwise shadow the existing service and break its current V5, receipt, workspace and backup contracts.

The incompatible canonical local-file-job/1 protocol and historical root-global jobs are being reconciled through internal protocol/state adapters. That union is **not present or qualified in this checkpoint**. Do not package this tree as the final migration from canonical ASR data. Existing original V5 and receipts-v1 source remains intact; there is no conversion of one protocol's receipts into another's evidence.

The application version and all three dependency-inventory root identities follow canonical 0.2.0.dev2. Dependency versions and wheel hashes are unchanged. The optional trusted-client profile still has 56 pinned dependencies.

Reviewed canonical changes retained here include legacy pending-publication metadata normalization, typed state path preflight, queued bibliography/outline source authorization, post-picker import scope validation, committed-export cleanup messages, the actual source-pinned child export fixture and real worker-start cleanup coverage. Stronger current descriptor-bound JSON, source/target publication, pending inventory, Studio managed inputs and reservation lifetime remain in place.

## Added and repaired behavior

- Reader playback uses the existing WavePlayer for A–B repeat, sleep timer and configurable skips. Playback controls are distinct from synthesis rate; no new speed/pitch feature is claimed.
- Recorded EPUB media overlays use the retained original archive, existing source reader, host worker, configured audio runtime, WavePlayer and shared progress/quiet controls. Exact source fragments and measured PCM intervals remain distinct from quotation approval.
- Every Windows DSP codec child uses the existing native Job owner. Render and concat staging follow its CleanupOwner; ordinary process cleanup failures also retain streams until native shutdown is confirmed.
- Portable complete-state recovery exercises the actual ReaderDraftStore, Studio project/revision/draft and assistant contracts. Restored assistant approvals are rejected until reviewed and resubmitted.
- Only two bounded public signed entitlement envelopes join passive backup. Copying them does not import keys, OS trust anchors or executable authorization.
- Current desktop/palette test hooks, the CRT-free MSVC copy loop, immutable launcher-build sibling imports and independent CI prerequisites are corrected.
- The two-import cancellation fixture declares enough capacity for both real supervised imports. The small-filesystem publication fixture still independently enforces the actual AudioBudget reserve.

## Evidence boundary

Candidate324's early syntax gate passed 782 Python files and 31 package declarations. Its Linux Sync run passed 270 cases across seven suites, but the complete-state module failed collection. Trust source gates failed the old desktop fixture and entitlement backup seam. The main desktop lane stopped at the old Tasks probe. The pinned-Zig launcher job passed; MSVC linking failed. V3 recovery showed six competing-import failures and reached its aggregate deadline before detailed summaries; separate citation, translation, planning and loopback selections passed. These results are evidence for 324 only.

All changed source in this checkpoint is SOURCE_REVIEWED / NOT_RUN at publication. Exact blob readbacks and independent reviews are not parser or runtime evidence. The new candidate must independently pass syntax, source, installed, native Windows, transfer-profile, trusted-client, managed update, launcher and restoration gates. The existing source selection is preserved and expanded from 304 to 311 selectors; installed selection expands from 166 to 173. Strict no-skip/no-xfail policy remains. Only the observed aggregate V3 recovery allowance changes from 10 to 20 minutes; its job limit stays 50.

No locally rebuilt wheel may replace an exact post-test retained wheel. Final packaging still requires the tested application wheel, current-source native launcher/receipt, exact optional dependencies, archive review and source/hash identity. Transfer artifacts exclude private engines, models, voice samples, secrets, audio binaries and Node.

## Work still in progress

Canonical ASR protocol/state/runtime-trust union, source-bound book batches and remaining finite reader/workflow features continue in this same integration. Local translation, structured/batch translation, optional Braille export, managed cloning-profile lifecycle and other unqualified requirements remain explicit requirements. Actual owner-engine inference, acoustic quality, manual NVDA, language-specific 135-entry acceptance and production signing/service authority are separate evidence dimensions. No registry count, synthetic test, successful subset or source adapter closes those requirements.
