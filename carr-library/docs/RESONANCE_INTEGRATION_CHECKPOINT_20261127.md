# Verified integration checkpoint — 27 November 2026

This checkpoint composes the already reviewed source changes on top of
`587559f403b0c7a164b7e496bcd6975506bbe881`. It is not a final installer
or a declaration that every V3 platform acceptance gate has passed.

## Changes

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

## Remaining acceptance gates

The complete V3 platform still requires the canonical branch reconciliation,
global completion-aware saving, remaining supplied ASR corrections and actual
model inference, native Windows/NVDA and listening validation, and owner-provided
engine/model/credential/signing material where those features depend on it.
The full pinned Windows audio/dependency input restoration was interrupted
before its final extraction was confirmed. No final private installer or signed
release has been produced by this checkpoint.
