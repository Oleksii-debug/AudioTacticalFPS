# Full backup receipts in assistant jobs

The `assistant.backup_receipt` helper binds every file and directory of a
completed backup to a compact checkpoint. It is packaged with the application;
it does not require the optional speech/studio runtime.

The accompanying `ASSISTANT_BACKUP_RECEIPT.patch` is a narrow integration patch
for the existing assistant producer. Its exact preimage is
`assistant/service.py` SHA256
`e12dba03c09597268277b159bca5721dd6ffd122ce6a19d9ba7b804220b2a131`.
Check or reconcile that preimage before applying; do not replace newer service
changes with an older whole-file snapshot.

## Behavior

The producer passes its approved byte budget and cancellation callback to
`LibraryApp.backup`. Format 4 enforces the budget before publishing the
completion manifest. The producer then records the full-tree receipt, including
the independently retained manifest digest, total file bytes and file count.
Reusing a completed result rechecks the output scope and every file byte.
Manifest-only results from older checkpoints require verification and are not
silently treated as fully verified.

The helper refuses links/reparse points, unexpected namespace changes,
over-budget output, files changed during scanning and cancellation. It never
deletes or rewrites the original library or completed backup.

## Reproduced integration evidence

The exact producer above was exercised with a real `LibraryApp`, its normal
backup implementation and synthetic local data. Baseline and corrected results
are preserved in `../validation/assistant-backup-receipt.json`.

* With a 4,096-byte approved budget, the baseline reported completion for a
  246,052-byte backup. The corrected producer refused it before publishing the
  completion manifest.
* After a completed backup, a same-size database byte was changed while its
  manifest stayed unchanged. The baseline resumed as complete. The corrected
  producer refused to reuse the changed backup.
* The source library remained unchanged in both cases.

The isolated integration probe placed its job store in an explicitly selected
`acceptance-control` directory. This evidence verifies the receipt/budget
boundary; it is **not** evidence that running jobs or every optional producer's
state can already be backed up. Their typed sidecar adapters are separate.

Required helper regression suite:

```sh
python -m pytest -q tests/assistant/test_backup_receipt.py
```

Twelve cases passed, including real complete-library backup, same-size database
and retained-source corruption, added/removed/renamed entries, changed files,
links, malformed receipts, resource limits and cancellation. Native Windows
acceptance must be run against the final installed wheel.
