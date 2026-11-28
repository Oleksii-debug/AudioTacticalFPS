# Provider settings and passive recovery

This component reconciles the selected current provider family with the canonical
profile import, credential-response guard and complete-backup behavior. It does
not install a second gateway or replace the existing translation implementation.

## Explicit settings import

AI and API connections offers Import profiles. Choose a current profile export,
review the captured account names and adapters, and confirm. Import reads at most
512 KiB, accepts at most 1,000 accounts, validates the complete batch, and performs
one atomic settings publication under the existing library writer. Duplicate
identities or a conflict with any existing account refuse the whole batch.
Credential references are cleared. No key, saved approval, request or automatic
loopback permission is imported. Remote accounts need an explicit reconnection;
every request still needs its exact existing typed approval.

The selected file is held through the existing verified reader while captured.
A later external-file change cannot replace the bytes already reviewed. A
replaced library root refuses publication. Native buttons have explicit names
and keyboard access, with two short action rows and a separate recovery action.

## Reflected credentials

Before recording a completed operation, the existing gateway rejects credential
bytes in every raw response, including generated audio. It also checks decoded
JSON values and keys after bounded HTML normalization. LLM JSON rejects duplicate
keys and nonfinite values, preventing an earlier escaped credential from being
discarded by the JSON parser while surviving in returned raw bytes. Translation
keeps its strict typed response and independent decoded-output receipt checks.
An attempted response that fails this boundary remains uncertain in the existing
durable ledger and cannot be retried automatically. Exceptions never display the
credential or response body.

## Complete-backup representation

A complete backup captures present current api-connections.json and
api-operations.sqlite into an immutable state/provider-recovery/<SHA256>.json
record. The source profile file hash and the exact ordinary SQLite file hash
are retained. Settings contain no credential reference; usage history contains
only existing approval fingerprints and typed operation IDs, request/output
digests, reservation estimates and states.

The adapter opens SQLite read-only, verifies its exact supported table/column
shape, reads a coherent transaction, and never instantiates or migrates a live
BudgetLedger. Historical six-column operation tables are accepted without adding
a column. The selected current ledger uses DELETE journaling. An active journal,
foreign WAL configuration, unreadable file, changed source, unknown schema or
exceeded limit visibly refuses the complete backup. No component is silently
dropped. The final backup check rereads the captured live source before the
completion manifest can be published.

Limits are 128 MiB for the live database, 100,000 rows per history table, 64 MiB
per recovery record, 256 retained records and 512 MiB for their collection.
These are declared refusal limits, not measured capacity or performance claims.
All existing reading, template, queue, audio, ASR and pending-import adapters
remain in the common backup.

Restore preserves only passive recovery records. It does not create
api-connections.json or api-operations.sqlite, consult a vault, install an old
approval, resume a request or restore an endpoint's authority. Existing saved
translation jobs retain their original root/ledger binding and still require
fresh review after relocation. Rebackup retains the exact immutable records.

## Recovery controls and history

Choose Recover saved provider settings in current API settings, then select a
recovery record. A bounded existing worker reads it and opens a native read-only
history summary. Import recovered settings asks for fresh confirmation and uses
the same importer described above. Export recovery history uses the existing
approved export-destination snapshot and atomic publisher, including its
no-clobber behavior and post-publication cleanup notice.

History remains audit data. Reservation estimates are not bills; the historical
ledger does not store currency labels, so the summary does not invent a
currency or aggregate unlike amounts. Export preserves the exact captured JSON.

## Qualification

scripts/provider_reconciliation_oracle.json declares 96 focused cases:
27 credential-response cases, 30 settings-import cases and 39 storage/recovery
cases. Generic tests remain strictly offline. The separate existing allowlisted
38 real loopback protocol cases are unchanged.

scripts/windows_provider_recovery_smoke.py declares 22 actual native-control and
storage assertions, six installed module origins and six exact metrics. It
drives the current named import/recovery buttons, real storage/export workers,
declined and accepted recovery confirmation, complete restore/rebackup, and
unchanged catalog and recovery bytes. File-picker and confirmation decisions
are explicitly injected synthetic choices. The probe must run in an actual
isolated installed Windows interpreter; no source-only result is a native PASS.

At this source checkpoint, the new source, installed and native qualifications
are NOT_RUN. Earlier assistant and reader PASS records do not qualify this
component. No real external service, credential-vault action, physical audio,
NVDA interaction, model identity or translation quality is claimed.
