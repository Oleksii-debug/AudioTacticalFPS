# Shared application V3

This implementation promotes the existing catalog/revision V2 schema and the existing topic/evidence/saved-query proposals into one explicit fresh V3 database. It preserves the original V1/V2 schema identities. There is no automatic live migration of earlier candidates.

## Publication and retained identity

`Library` opens per-operation SQLite connections under the existing cooperative writer guard. Every bound UI action rechecks the active workspace and, where selected, topic within the same transaction that reads or mutates data. Collection authorization and source retrieval use that same snapshot. A reference encodes immutable revision, page ID, exact offsets and optional collection; it is a locator, never an authorization credential.

The original `Ingest` job journal remains responsible for checkpoints, cancellation and recovery. `CatalogIngest` records immutable explicit registration intent. `CatalogPublisher` composes `register_batch`, `stage_revision`, the active index, and revision-bound metadata in a single storage transaction. Partial extraction never publishes a partial catalog. A process exit after database commit but before journal commit is recovered through the existing sealed-revision checks.

Original bytes are retained with the existing no-overwrite content-addressed service. Strict text verification independently resolves these bytes, including BOM, CRLF and form-feed segment behavior. Metadata approval records preserve user-supplied attribution; they do not approve PDF extraction, OCR output, or printed labels.

## Saved research

New evidence uses the existing version 1 span envelope. The reader accepts strictly canonical legacy single-anchor payloads without rewriting their stored bytes or checksums. Notes remain editable; quote identities remain immutable. Multi-page evidence is read/exported as separate exact source parts. Search never invents a phrase across their boundary.

Workspace and topic archive hide active views while preserving history. Structural integrity checks validate topic reachability, source binding, snapshots and checksums, including archived/revoked records. Those integrity checks do not grant copy permission: live actions still require current authorization and source attestation.

## Backup and restore

Full V3 backups bind the checked database and every registered original to a versioned manifest. They require explicit all-workspaces selection and include archived data. Pending import journals/unregistered retained staging assets are excluded and explicitly identified as such. Restore verifies schema, checksums, bounds and original-byte inventory before publishing to a fresh destination. It never replaces an existing library. An incomplete restore must remain unopenable; a separately retained manifest digest is required.

## Executable entry points

`python -m carr_library` / `carr-library gui` opens the existing wx workspace shell through `desktop.launch`. Imports run on a worker with per-operation connections; native controls remain on the GUI thread. `carr-library --help` exposes text commands for accessible automation and testing. The wheel includes the reused implementation and its licenses, including cairnsearch, wx-accessible-grid, rispy and rifflux.

This contract defines the runnable candidate. It does not replace independent Windows/NVDA acceptance, parser isolation/security work, source-review integration, lawful corpus acceptance, or final release approval.
