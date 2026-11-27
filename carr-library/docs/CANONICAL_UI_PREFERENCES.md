# Retaining canonical native UI preferences

The current application reads the released canonical preference files
`state/ui/progress.json` and `state/ui/shortcuts.json` through one compatibility
adapter. It does not install the former progress presenter or keymap store.
The exact original files, including whitespace and line endings, remain in place.
Both typed files are included in complete backups and restored without activating
any shortcut, provider, executable path or voice during restore.

## Progress

When there is no current `progress-preferences.json`, opening the library maps
the old preferences into the existing current store. `both` becomes
`beep_speech`; `mute_during_audio` becomes `suppress_during_media`. Volume, quiet
mode, background permission and intervals through 120 seconds are preserved.
The current interval range is now 0.5 through 120 seconds; no old value is clamped.

The canonical file never contained a persistent voice selection. A requested
Speech or Beep and speech mode is therefore retained with `muted=true` and empty
voice identity, language and fingerprint. The native presenter explains that
the user must choose a current local voice and unmute feedback. Migration does
not discover a voice, run a speech engine or activate an archived executable.
An existing current preference file remains authoritative, even if the older
file is unsupported. A changed source or a competing publication aborts the
migration rather than replacing someone else's current preferences.

## Keyboard shortcuts

The adapter uses the current native menu and current command registry. Each
saved lowercase path must match its complete current menu ancestry; escaped
literal ampersands and both forms of trailing ellipsis retain their original
meaning. The saved `originals` value must equal that registered command's
current default. Only then is the assignment passed to the existing context,
reserved-key, conflict and native accelerator checks and written by KeymapStore.
F1–F24 are supported; fixed editing, navigation and screen-reader keys remain
protected. No wx numeric ID or callback is loaded from the file.

Missing commands, changed defaults, conflicting assignments or simultaneously
present older keybinding formats require review in the current Keyboard
shortcuts dialog. The complete old file stays untouched; the adapter does not
partially guess a map or silently discard unavailable entries. A current
`keyboard-shortcuts.json` always takes precedence. The existing library-change
handler applies the same migration rules to the newly selected library.

## Snapshot and persistence boundaries

The bounded validators accept only the two released schemas, reject duplicate
JSON keys and nonfinite values, and limit progress to 4 KiB and shortcuts to
128 KiB. Files and ancestors must be regular local paths without symlinks,
reparse points or extra hard links. Migration compares exact source bytes and
file identity again while holding the released `.progress.lock` and
`.shortcuts.lock`. Both migration and complete backup acquire the application
writer before these locks and the current keymap writer. Lock files themselves
are excluded from the backup.

Source tests exercise actual stored files, both native file locks, source and
destination races, menu registry/default comparisons, original-byte
preservation, restore and rebackup. UI tests use explicit widget doubles;
those checks do not qualify actual Windows accelerator dispatch or audio output.
