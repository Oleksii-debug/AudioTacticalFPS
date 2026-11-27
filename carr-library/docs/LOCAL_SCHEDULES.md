# Local backup and watched-folder schedules

This feature saves explicit local rules and runs them through the existing full-backup/Vault and import-batch services. Saving a rule does not run it. Each GUI session requires a separate review and **Arm selected rule** action. Only that open Local schedules window owns the active session.

The implementation and tests in this slice still require source, installed-wheel and native Windows execution. No unattended service, physical media failure, power-loss or manual screen-reader qualification is claimed.

## What a rule can do

| Rule | Existing operation | Scope |
| --- | --- | --- |
| Backup | A complete canonical format-4 backup stored through the existing incremental Vault adapter | One explicitly selected, already initialized local repository, outside the active library and its ancestors |
| Watched folder | SHA-based scan, existing durable import-batch planning and isolated import | One explicitly selected local folder, one current library workspace, optional subfolders and explicit collections |

Backup successors reuse unchanged Vault chunks and retain independent complete snapshot inventories. They are ordinary unencrypted incremental snapshots; encrypted backup, synchronization and scheduling while the application is closed are separate features. This window never initializes a repository, deletes snapshots, prunes data, uploads a file or sends a message.

The watched-folder rule does not change source bytes or delete retained library editions when an external file disappears. Files already present in the selected workspace are excluded by the existing content-identity checks. Changed content can become a new retained edition. Unsupported file types are skipped; rejected inputs, links and partial import outcomes require attention. An empty or fully deduplicated scan completes without creating an empty import batch.

This is an application feature. It does not read or change ChatGPT automations, OS scheduled tasks, startup configuration or system services.

## Desktop workflow

1. Open the intended library and select the workspace for a watched-folder rule.
2. Choose **Library → Local backup and folder schedules...**.
3. Choose **New backup rule...** or **New watched folder rule...**. Select an existing local folder. For a backup, initialize the Vault first through the existing backup-repository workflow and keep the independently obtained current snapshot token.
4. Enter a title, interval in minutes and a complete UTC start time. Review subfolders and installed OCR language IDs for watched imports. Saving leaves the rule inactive.
5. Select the saved rule. The read-only review shows the exact library folder, selected external folder, scope, times, rule SHA256 and last historical result.
6. Choose **Arm selected rule**. The default answer is No. Confirming Yes arms only this exact saved policy for this window.
7. Keep the window and application open. The native timer asks the existing host worker to run due work. If another host operation is active, the timer waits; it does not add a second executor.
8. Use **Disarm this window**, the existing operation cancellation control, or **Close local schedules**. Already committed work keeps its actual receipt. Active work stops through its existing cooperative cancellation and process-cleanup boundaries.

The status field receives focus on completion and exposes the actual import-batch ID or snapshot and backup-manifest SHA256 values. Keep backup checksums separately from the repository. Source and native tests cover keyboard dispatch; manual NVDA testing remains a separate acceptance task.

Closing the window, switching or replacing the library directory, a backwards wall-clock change, and application exit revoke the in-memory session. Reopening a window or restoring a backup never arms any rule. A restored declaration cannot reuse the old root identity: create a new rule by explicitly selecting the current library and external folder.

## Timing and missed work

All persisted times are UTC. An interval is from 60 seconds to 7 days. A start window is from 1 second to 1 day; the GUI currently uses a one-hour start window. The first start must be between the current time and 366 days ahead.

After a fully recorded successful attempt, the next start is completion time plus the chosen interval. The service does not replay missed intervals or create catch-up work. An expired start window, unresolved attempt, cancellation, partial import or uncertain persistence disarms that rule.

**Review attention...** saves a newly chosen start time after explicit review. For a backup, supply the independently verified current Vault token. This action does not relabel the previous attempt successful. Existing import-batch recovery remains in the Import Center; the scheduler never guesses whether a crashed import or backup should be repeated.

## CLI

Run the current application interpreter. The directory must already contain an existing library. These examples use illustrative local paths; substitute the explicitly reviewed paths and IDs.

```text
python -B -m carr_library local-schedules --data-dir /data/library list

python -B -m carr_library local-schedules --data-dir /data/library create-backup \
  --title "Evening local backup" --target /data/backup-vault \
  --at 2026-12-01T19:00:00Z --interval-seconds 86400 --lateness-seconds 3600

python -B -m carr_library local-schedules --data-dir /data/library create-watch \
  --title "Selected research intake" --target /data/incoming \
  --workspace WORKSPACE_ID --at 2026-12-01T19:00:00Z \
  --interval-seconds 300 --lateness-seconds 600 --ocr-languages ukr+eng

python -B -m carr_library local-schedules --data-dir /data/library run \
  --rule RULE_ID --sha256 REVIEWED_RULE_SHA256 --run \
  --wait-seconds 3600 --max-occurrences 4
```

For an existing nonempty backup repository, add `--parent-sha256 CURRENT_SNAPSHOT_SHA256` when creating or reviewing a rule. Omitting the parent declares an empty repository and is rejected if the actual HEAD differs. `--include-generated` explicitly includes supported generated audio in a backup. Watched-folder creation supports repeated `--collection` and `--no-recursive`; it never downloads OCR models.

`run` requires `--run`, the exact current policy SHA256 and a finite process lifetime. `--wait-seconds` is bounded to one day; `--max-occurrences` is bounded to 32. A zero wait permits one immediate due attempt. SIGINT requests cooperative cancellation and all exits disarm the session.

`disable --rule ... --sha256 ...` disables the saved policy. `review --rule ... --sha256 ... --at ...` records a fresh explicit review without arming. CLI output is UTF-8 JSON. Exit 0 requires the requested attempts to return completion with recorded results; attention/not-due uses 3 and interruption uses 130.

## Persistence, ownership and recovery

Saved declarations and last-attempt metadata occupy one bounded, typed namespace: `state/local-schedules/<rule-id>.json`. At most 32 rules are admitted, each at most 64 KiB. There is no second import journal, snapshot format, process manager or operation queue.

A volatile ticket binds the exact saved bytes, file identity, library identity and selected external directory identity. The service checks these identities before dispatch and at the existing scan/backup cancellation checkpoints. Replacing a directory at the same path does not transfer approval to its replacement.

The current native lock serializes edits and attempts. A durable dispatch claim is written before domain work. A planned import-batch ID is recorded before its execution. Only a validated actual returned domain result can be recorded as completed. A same-byte competing file or an unresolved crash is never adopted as proof of publication.

After domain completion returns, observer callbacks, cancellation or a schedule-receipt write failure do not erase the committed result. The caller receives the real receipt with explicit persistence or cleanup attention, and automatic repetition stops. A dispatch claim with unconfirmed directory synchronization never authorizes work.

Scanning and backup creation use the current shared ResourceBroker. The declarations are cooperative CPU/memory policy; they are not measured memory consumption or a hard quota. Imports retain their own existing admission and subprocess lifetime rules. The scheduler creates no extra child process and does not release a domain cleanup owner early.

The existing full-backup and state-backup validators copy and validate these inactive declarations. They reject unknown files, path aliases, oversized records, incoming lock/temp files and nonexistent copied workspace references where the full copied database is available. They never resolve saved external folder paths or execute a rule during backup or restore. No arming ticket, active timer or executable consent is serialized.

## Acceptance inventory

The focused suites are:

- `tests/storage/test_local_schedules.py`: actual canonical backups/restores, isolated watched imports, content deduplication, cancellation, root/target replacement, strict admission, actual crash-child boundaries, no-clobber publication and preserved committed receipts.
- `tests/ui/test_local_schedules.py`: current host worker, queued and late scope changes, explicit default-No arming, no unrelated cancellation, window/root cleanup and repeated timer-stop handling.
- `tests/storage/test_backup_local_schedules.py`: full/state round trips, passive restored declarations, exact namespace/count/size validation and cancellation.
- `tests/packaging/test_local_schedules_cli.py`: actual foreground CLI children, UTF-8 output, explicit run authorization, missing-root refusal and finite waiting.
- `scripts/windows_local_schedules_smoke.py`: installed source identities, actual DesktopFrame/menu/keyboard/timer, actual TXT import, actual canonical snapshot/restore and inactive reopening with external folders removed.

Source presence is not execution evidence. Installed and native results must be bound to the final composed commit. The native probe uses local synthetic document bytes; it makes no physical-media, power-loss, manual assistive-technology or unattended-service claim.
