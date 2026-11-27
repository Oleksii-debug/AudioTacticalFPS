# Shared integration and execution checkpoint — 2026-11-27

This is an engineering checkpoint, not a distribution or whole-platform
qualification. The current technical specification remains authoritative.

## Executed source snapshot

Exact source: `5355942a230b23458d96c195a8b0d85253d7b32a`.

- Windows bibliography run 45943589850, job 140323191322: SUCCESS. All 26
  explicitly required suites passed (992 cases, zero skips/failures); 108
  actual installed native control/clipboard/export checks passed.
- Portable shared-state run 45943589969, job 140323133592: FAILURE. 22 of
  26 required suites passed; four suites had failures. Complete log/artifact
  remains the evidence. Partial suite passes do not turn the job green.
- Windows keyboard job 140323133160: FAILURE after the existing keyboard,
  draft recovery, progress and new reading-history controls passed. The
  native Tasks destruction observation failed; subsequent native checks
  in that job did not run.
- No result above establishes real NVDA speech, acoustic model quality,
  a Google account request, or the canonical 135-language qualification.

## Changes after that snapshot

The source checkpoint composes the current provider, assistant, reader,
resource coordinator and process owner instead of adding replacement stacks.

- A document releases its parser allowance after native completion and before
  receipt reconciliation. Another request may then commit its own immutable
  receipt. A cancelled queued retry has a fresh attempt identity and cleared
  publication hints.
- Application resource-policy identity is relative to the selected library.
  Old absolute application bindings migrate only when this coordinator has no
  held native reservation. Arbitrary/custom policy rebinding remains refused.
  Typed backups continue to exclude runtime leases.
- Reading-history archive retry repeats directory durability synchronization
  after a previous post-publication failure.
- Advanced Google v3 uses explicit project/location/model/glossary authority,
  expiring OAuth credentials in the existing OS vault, the shared request
  ledger and durable derivative jobs. Existing Basic v2 hashes and records
  retain their contract.
- Local model proposals revalidate the saved profile, root and request ledger.
  Preview and Run validate plans on the existing worker; stale inputs never
  authorize a late result. Default assistant rendering uses shared resource
  admission and existing local music profiles.
- The shared Windows Job Object owner assigns children at creation, limits
  inherited handles, and retains uncertain cleanup until native quiescence.
  This source is reused for capture and later installer composition; adding a
  helper alone does not claim every older process call already uses it.
- Real HTTP planning tests use only numeric loopback TCP and a synthetic
  proposal server. The strict runner has a separately selected, exact-path
  loopback mode; other suites retain offline rejection. The fresh CLI child
  installs the same test-only Python network guard. This is not an OS sandbox.

All newly composed paths require their own CI execution. Existing donor test
results and source review are not execution of this checkout. Source adapters
do not supply production credentials, signing authority, model weights or
owner distribution approval.
