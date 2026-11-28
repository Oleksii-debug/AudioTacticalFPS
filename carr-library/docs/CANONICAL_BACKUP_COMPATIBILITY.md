# Canonical backup compatibility

This reader composes the accepted backup layouts from canonical commit
`966e5ccf892243ead6942c635af5fbab47e72551` with the current composite
format 5 writer and schema 6. It does not replace either active state store.

## Exact envelope discrimination

| Backup envelope | Reader |
| --- | --- |
| v1/v2 database and originals | Existing checked schema reader |
| v3 `state` plus `generated_audio_included` | Existing state reader |
| v3 `sidecars` object with `version/source_root/files` | Existing typed sidecar reader |
| v3 `reading_state` record or null | Canonical compatibility reader |
| v3 `sidecars` list | Canonical compatibility reader |
| v4 `state/generated_audio_included/pending` | Existing complete-pending reader |
| v4 `sidecars` list plus `generated_audio_included`, optionally `pending` | Canonical compatibility reader |
| v5 `state/generated_audio_included/pending/reading_state` | Canonical compatibility reader; pending may be null in this exact legacy shape |
| v5 `state/generated_audio_included/sidecars` object, optionally `pending` | Existing composite reader and writer |

Mixed envelopes and unknown fields fail before publishing a destination.
The independently supplied manifest SHA256, signed source JSON and exact
SQLite schema fingerprint are never rewritten to make an input fit another
format. Schema 3/4/5 snapshots remain unchanged on restore; a subsequent
ordinary library open performs the existing checked migration to schema 6.

## Reused validators and retained authority boundaries

`backup_compat.py` reuses the canonical portable-name, exact producer,
location and generated-cache contracts. The pure producer/cache validators
are preserved in `backup_producers.py` and `backup_generated.py`.
Only the passive portions of the canonical shortcut/progress validators are
reused in `backup_legacy_preferences.py`; no second command registry or
progress controller is installed. Legacy preference bytes are preserved
without applying them to this host.

The new adapter streams the declared tree and rejects an unexpected entry
immediately. Enumeration failures, missing files, links/reparse points,
hardlinks, aliases, malformed records, resource-limit violations and changed
bytes are errors. Metadata is bounded at 512 MiB in aggregate; generated
media keeps the canonical 32 GiB ceiling, explicit inclusion flag and exact
completed-receipt requirement. The existing pending component remains its
sole producer, with PR177 fail-closed inventory checks and schema6 publication
receipts unchanged. Pending originals are not duplicated in the sidecar list.

After byte and producer validation, canonical state passes through this host's
existing `prepare_restored_state`. Credentials are cleared, paid-call ledgers
and assistant/translation jobs require new approval, output jobs require a
new explicit destination and engine declarations require revalidation.
Historical source paths, quotations, revision identifiers and receipt hashes
are preserved. Cancellation is checked during inventory, copy, validation and
publication; an interrupted publication keeps the existing incomplete marker.

Canonical `dordiai` declarations are structurally readable and retained
without opening their external installation. An enabled legacy declaration
cannot activate or have its restore marker cleared by an unrelated settings
save. It requires explicit migration into the existing reviewed local voice
registry. This is backup/read compatibility, not neural-engine qualification.

The canonical module references optional `reader.narration` and
`reader.reading_copy` validators that are absent from that exact canonical
tree and this host. Such records fail visibly with the missing validator
named; the adapter does not reinterpret them as arbitrary JSON.

## Evidence recovery

The existing writer also includes root `evidence-drafts.json`, bounded at
8 MiB before parsing, using `evidence_drafts.validate_evidence_drafts`.
Each draft must refer to an exact saved workspace/topic/evidence row.
Archived scopes and stale saved-note/source baselines remain intact: a
recovery copy does not grant permission to edit or reveal an old source.
The existing editor performs fresh authorization and conflict review.

Root `keyboard-shortcuts.json` and `progress-preferences.json` also survive
a restore followed by another composite backup, with their exact 64 KiB and
8 KiB raw-byte limits. Only genuine absence is optional; unreadable root
sidecars cannot silently disappear from a successful backup.

## Verification boundary

`tests/storage/test_canonical_backup_compat.py` uses real SQLite snapshots,
retained originals, reading locations, evidence checkpoints, pending imports
and producer records. Its cases cover the exact envelopes, schema3/4/5/6,
restoration authority, generated-cache receipts, inventory/cancellation
failures and subsequent use of the unchanged composite writer. Test fixtures
use canonical binary JSON writes and support installed importlib collection.

This source was prepared while the execution host was unavailable.
**The composed adapter and its new tests are NOT_RUN until the exact successor
source/installed Windows gate executes them.** Earlier PR177 and timer test
results do not qualify these new changes.
