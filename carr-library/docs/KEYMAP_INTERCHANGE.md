# Review, transfer and undo application shortcuts

Open **Help → Keyboard shortcuts** (`Ctrl+Alt+K` unless you changed that
assignment). These shortcuts belong to this application's native menu commands.
They do not register Windows global hotkeys or replace screen-reader keys.

## Import with review

1. Choose **Preview keymap import…** and select a local JSON file.
2. Read **Shortcut settings status**. It lists recognized commands by their
   stable menu path and shows their current and proposed keys. The preview does
   not change the menu, accelerator table or saved settings.
3. Resolve reported conflicts before importing. An unavailable command, a
   changed original shortcut, a reserved key or two explicit owners of one key
   blocks the entire import. No partial import is applied.
4. Choose **Apply reviewed import** to merge the listed choices. Unmentioned
   saved overrides remain intact. If a proposed explicit key displaces an
   implicit default, the preview names the default that will become unassigned.
   Existing explicit choices are never silently removed to resolve a conflict.

The file must use the existing schema 1 preference format, with at most 128 KiB
and 512 explicit command choices. New exports use UTF-8 JSON. Reading preserves
the existing decoder's UTF-8 BOM, UTF-16 and UTF-32 compatibility; loading such a
saved file does not rewrite it:

```json
{
  "schema": 1,
  "overrides": {"file/save": "Ctrl+Shift+S"},
  "originals": {"file/save": "Ctrl+S"}
}
```

An empty shortcut explicitly disables that command's key. An empty map merges
no choices; use **Reset all shortcuts to original keys** for a complete reset.
Import does not execute commands. Numeric native menu IDs are never exported
or trusted; the current command registry supplies the new session's actual IDs.
An expanded visible label can retain its existing stable command identity.

The review binds the selected file's exact bytes, current saved settings,
library object and directory, and live menu command identities. A changed file,
another settings window or a changed library invalidates the review. Editing
the shortcut field or selecting another command also clears the pending review.
Select and preview the file again before applying it.

## Export and recovery

**Export keymap…** writes the current applied choices and their original keys
in that same format. Choose a new file outside the private library directory.
Existing destinations are never replaced. The shared export service performs
atomic no-clobber publication and removes an unpublished temporary file on
failure or cancellation. Linked, redirected and hard-linked files are rejected
by the existing local-file boundaries.

**Undo last change** restores the complete keymap immediately before the last
successful Apply, Reset or reviewed Import in this dialog. Undo checks that the
result it is reversing is still current; it cannot overwrite another window's
newer preferences. The dialog keeps one undo step, with no cross-session undo
history. It restores valid assignments, not damaged preference-file bytes that
were rejected when loading. Closing discards an unapplied preview; explicit
changes already applied remain saved.

Preferences remain in the existing `state/ui/shortcuts.json` file. The ordinary
full library backup records its bytes and SHA-256 in the manifest and restores
them with the rest of the private state. Reopening the restored library maps
those stable command paths to the current session's native IDs.

## Verification and remaining qualification

`tests/ui/test_keymap_interchange.py` exercises actual bounded JSON files,
same-size/time file changes, exact preference comparisons, library and command
identity changes, explicit cancellation, no-clobber exports and a real
manifest-bound library backup/restore. `tests/ui/test_keyboard_settings_dialog.py`
exercises the production dialog handlers using named widget and explicit picker
stand-ins. Existing keyboard and command-palette tests run alongside them.

`scripts/windows_command_palette_smoke.py` covers the installed native command
registry and the keyboard settings workflow through the normal wx event loop.
Its keymap file selection is an explicit test fixture; that does not qualify the
Windows file picker, physical keyboard use or human NVDA interaction. Actual
Windows probe results must be recorded separately from source tests.

Tray controls and OS-wide/global shortcuts are outside this workflow and remain
separate scope gaps. No global hook, background file watcher or second shortcut
registry is introduced.
