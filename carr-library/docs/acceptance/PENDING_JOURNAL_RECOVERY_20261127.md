# Actual interrupted-import journal recovery — 2026-11-27

## Observed failure and scope

The installed Windows257MiB TXT /129MiB PDF stand failed at [run46016847823, job140566043335](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46016847823/job/140566043335), source `798e62ddeef8d459f3ef088665fa1ae8c2df9557`. After actual worker cancellation, creating the pending backup raised `sqlite3.OperationalError: attempt to write a readonly database` while reading the journal schema. The pending-backup implementation was unchanged through the semantic convergence commit `c55d24e310e2ed4e8919a9b63b1695cbbcca49fb`; this was an open defect in that source, not evidence of successful large-file recovery.

An interrupted SQLite transaction can leave a hot rollback journal. SQLite must restore a consistent database before even an ordinary read. That recovery requires a writable connection; `SQLITE_READONLY_ROLLBACK` explicitly describes the failing read-only case. WAL recovery can also require write access. See the primary SQLite references: [locking and hot journals](https://sqlite.org/lockingv3.html#hot_journals), [result codes](https://sqlite.org/rescode.html#readonly_rollback), and [preserving matching journal files](https://sqlite.org/howtocorrupt.html#backup_or_restore_while_a_transaction_is_active).

## Current-owner correction

Candidate `33260dfd83d1e81f4a814987539bda30f1b2e2bb`, with the test-only follow-up `63891cc78392fe5e3f867dc534f3e4957a14a871`, adds one live recovery step immediately before the existing read-only journal validation.

The complete-library backup already owns the application writer and every existing import request/batch guard. Under that ownership, the helper validates the selected local database and its declared size, opens only the existing `imports.sqlite` using `mode=rw`, sets `trusted_schema=OFF` and `query_only=ON`, and performs a bounded schema read. SQLite applies its own committed-state recovery. There are no application INSERT/UPDATE/DDL statements, no `immutable=1`, and no discarded or manually renamed journal files.

The original `_journal` verifier remains byte-identical and opens `mode=ro`. It still enforces exact schema, integrity, foreign keys, row counts, source/profile identities and cancellation. The existing online snapshot then emits a self-contained database and validates it again. Imported backup verification never invokes writable live recovery.

## Required regression evidence

Eight added cases use a real owned child, a real SQLite transaction and an abrupt process exit. They cover:

- Hot rollback and dirty WAL: previously committed marker/checkpoints survive, the uncommitted transaction disappears, complete backup verifies, restore retains the same job, and resume recovers the complete original quotation.
- An untrusted archived hot database remains rejected by the read-only verifier with identical bytes.
- Actual application writer, request and batch guard contention remains excluded during recovery.
- Cancellation before or after recovery does not publish a partial backup.
- The selected journal byte limit is enforced before writable opening.
- A committed foreign schema still fails the unchanged exact validator.

These are appended to the existing mandatory `tests/storage/test_pending_backup.py`. The large-source installed preflight now includes that suite, and the complete-corpus workflow watches both its source owner and tests. The actual257MiB/129MiB and2GiB size cycles remain mandatory after preflight.

**The new recovery cases are NOT_RUN at preparation time.** They require execution on their exact composed commit. Source review is complete; no successful recovery is inferred from the code alone.

## Separately observed c55 baseline

The exactc55 Linux fast replay passed [job140610685440](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46029878242/job/140610685440):18source suites/368cases,17installed suites/328cases, and2loopback suites/45cases, all without failures/skips. Typed transcription passed40cases in each source/installed mode; the owned CI runner passed40cases. These results do not qualify this later pending-journal correction or Windows execution.
