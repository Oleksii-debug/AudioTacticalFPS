# Optional typed backup roots

This application adopts the lookup correction from canonical commit
`924ae83316a3cf9482aaf2ec51a6e6a5adb614c3` through its existing typed producers.
It does not install the alternate canonical backup engine or change any manifest
version, file allowlist, source binding, state lock, restore activation rule or
credential/runtime exclusion.

`backup_files.optional_present(path)` uses `lstat`. Only `FileNotFoundError`
means optional absence. Permission failures, invalid directory traversal and
other lookup failures propagate before any complete backup is published.
On an absent-name error the first existing lexical ancestor must be a regular
directory: Windows can report `PATH_NOT_FOUND` when an ancestor is a file.
Dangling links remain present and the existing regular-file guards reject them.
The helper grants no permission and follows no link.

The same rule covers managed audio, assistant and translation state, supplied
transcription jobs, recordings, resource preferences, retained historical
backups, reading histories/sessions, old UI preferences and pending imports.
Simple reader files already used a strict `lstat` path; they keep that behavior.
An already observed root disappearing during its subsequent validation remains
an error. Genuine absent roots and empty registered namespaces remain valid.

Regression tests inject denied and invalid lookup in thirteen producer roots,
verify disappearance/Windows error mapping, exercise complete backup/restore,
and prove that an inaccessible local engine-profile file is still never read or
collected. This is a completeness rule for registered state; it does not turn
backup into a recursive copy of arbitrary neighboring files.

## Completion markers require successful staged traversal

The shared pending-tree publisher now validates the staged root and every
visited directory, and propagates `os.walk` enumeration errors. A verified
staging inventory is not permission to silently skip a later unreadable
directory. Actual enclosing backup/restore fault tests confirm that a failed
walk leaves the backup manifest absent or the restore-incomplete marker intact;
the original library and source remain unchanged. The same presence helper
protects the new live-reading cursor against replacing inaccessible state.

The final composed optional-root suite passed all 34 cases without skips.
