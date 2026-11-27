# Backing up supplied-worker transcription jobs

The complete library backup includes the registered `transcription-jobs/<job-id>` namespace through one typed producer. It preserves the managed definition, immutable owned input copies, execution history, retained inference records, output receipts, verified text outputs, and the existing stages named by valid publication intents. It uses the existing library backup schema and streaming publication path.

The same backup dispatcher also retains the existing workspace profiles under
`state/transcription/`. Their protocol-specific producer stays separate from
`supplied_transcription_backup`; neither namespace is migrated or reinterpreted.
A current regression creates and restores all three profiles in one library.

## What is retained

| Data | Treatment |
| --- | --- |
| `job.json` | Immutable relative input inventory, captured settings and content identities; validated and copied verbatim. |
| `inputs/root-N/...` | Every registered input is rehashed against its definition. Queued inputs are preserved before decoder qualification, including files a decoder may later reject. |
| Current configuration, status and execution records | Bounded JSON copied as source data. A restored absolute configuration is never used to start a worker. |
| `history/<attempt-id>/...` | Previous configuration, status, accepted execution, completion and live-settings bytes remain historical. Partial archival is retained. |
| `logs/integrity-v1/{engines,inference}` | Pure envelope, content and managed-job bindings are checked. Runtime and model paths are not followed. |
| `results/verified-v43/<owner>/...` | Owned text, subtitle and JSON results, owners, output receipts and valid intents are retained. |
| Referenced publication stages | Only stages named by a validated same-owner intent are included. Their hashes and lengths must match. |
| Worker locks, stop/pause flags, temporary inference input copies, unreferenced stages | Excluded from the snapshot. They do not carry resumable authority. |

Original external source paths are provenance strings only. The backup does not open those paths, model directories or runtime manifests. It does not include Python environments, weights, private worker source or a bundled speech engine.

Unknown durable files, unregistered owned inputs, unfinished preparation directories, invalid checksums and undeclared aliases cause a visible backup failure. They are left in place for review instead of being silently omitted. A stopped job with a valid definition and partial inference/output checkpoints can be backed up.

## One lock protocol

The caller first holds the existing application writer. The producer then acquires the existing native project lock on the transcription namespace and, in sorted job order, the supplied worker's `logs/.worker-v5.lock`. The configurable lock basename reuses `audio.jobs.project_lock`: POSIX `flock` and Windows byte-zero `msvcrt.locking`, both nonblocking.

The worker lock is essential because a supplied worker may be launched independently of the desktop supervisor. A busy worker causes backup to stop before publication. Verification of an existing backup is read-only and never acquires or creates these locks.

A POSIX process can stop after creating a hard link from its stage to its output but before unlinking the stage. The producer accepts only that exact two-name, same-owner, intent-declared pair, with matching native identity, content and link count. All other aliases fail closed. Archive and restored files are copied independently; old inode identities are never reused as a publication lease.

## Reading completed results after relocation

`verify_saved_completion(job, definition, cancelled=...)` provides a read-only historical view. It requires all of the following:

- The immutable definition and each current owned input still match their captured bytes.
- The saved execution records an accepted completion with exit code zero and the same run identity as the configuration, status and completion.
- The completion hashes the exact raw saved configuration bytes; the status hashes the exact raw completion bytes.
- Old absolute input and output bindings map lexically into the immutable relative definition and its historical owner namespace.
- Captured settings and model/runtime identities match the immutable job; requested output formats and input coverage are complete.
- Each declared current output has the captured byte count and SHA-256.

Only current owned input and result files are opened. The function returns a JSON-safe summary with paths mapped into the restored job, `historical=true` and `runtime_current_verified=false`. It does not claim new inference, adopt a previous publication transaction, or approve a runtime. Missing original sources or uninstalled models do not prevent a valid saved transcript from being read.

A future run remains a separate operation: the supervisor validates the explicitly selected current runtime, rehashes retained inputs, archives the old attempt and writes a fresh configuration and run identity.

## Bounds, cancellation and compatibility

The producer allows at most 1,000 jobs, 100,000 included files and 128 GiB of included transcription data, within the overall backup limits. Each job has the existing 20,000 entry and 32 GiB retained-input bounds. Integrity records are at most 64 MiB; configuration/history JSON is at most 16 MiB. Each output or transaction is bounded to 4 GiB.

Input and output hashing and streaming copies observe cancellation. No existing backup is overwritten. The complete-backup verifier preserves the independently supplied manifest digest and final source-byte recheck. Historical library schemas keep their existing explicit dispatch; this producer adds a registered namespace without introducing a second backup format.

## Validation status

The reconciled producer passed all 28 current cases, alongside the unchanged
112-case receipt validator, 41 supervisor cases and all 42 existing workspace
backup cases (223 passing cases, zero failures or skips). The subsequently shared
status startup adds one actual child-process case, and its full 42-case supervisor
suite passed in the composed application. These are Linux component results;
the exact installed Windows candidate remains a separate gate.

The regression source covers prepared-before-launch jobs after loss of the original, relocation and rebackup, completed and empty transcripts without a runtime, historical configuration preservation, valid staged and linked publication windows, foreign aliases and traversal, changed raw receipts/settings/input/output bytes, cancellation and byte budgets. An independent subprocess holds the actual native worker lock to test cross-process refusal.

These records and audio bytes are deliberately synthetic. They test the storage boundary and do not establish supplied-model inference quality. The exact source candidate `78682c33053e6df731cc1726693df2aa25e5b893` subsequently ran on Linux with Python 3.12.14 and pytest 9.0.2:

| Strict suite group | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| New transcription backup tests | 27 | 0 | 0 |
| Shared receipt validator tests, exact 923352e source | 112 | 0 | 0 |
| Existing complete, pending, historical, assistant and reading backups, 14 suites | 284 | 0 | 0 |
| Existing project, audio and retained-input backups, 3 suites | 43 | 0 | 0 |
| Total, 19 strict suites | 466 | 0 | 0 |

All three runner invocations exited zero. The independent child process exercised the actual POSIX worker-lock boundary; the linked stage/output fixture exercised an actual two-name hard link and independent restore. These results do not establish Windows native lock behavior or installed-package acceptance: those exact-candidate gates remain required. No supplied model was run by these storage tests.
