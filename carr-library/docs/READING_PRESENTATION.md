# Reading appearance: native font, colors and zoom

The Voice reader has a **Text appearance...** button. It opens native controls
for a library's reading-copy display profile. The profile changes the editor's
font, foreground and background colors, and font zoom. It does not change the
reading text, retained source, pronunciation rules, speech settings, cursor,
selection, editing history or reading-project format.

## Use the workflow

1. Open the Voice reader and choose **Text appearance...**. Tab navigation and
   named native controls expose every action; Escape closes this dialog.
2. Choose a font with the OS font chooser, choose opaque text/background colors
   with the OS color chooser, or retain the OS theme defaults. Font size is
   bounded to 6–72 points and zoom to 50–300 percent.
3. Select **Preview appearance**. The sample shows the exact selected profile;
   the reading copy and saved profile are still unchanged.
4. Select **Apply to reader and save** to apply the reviewed choices and save
   them for this library. Changing a choice invalidates that review.
5. **Reset to OS theme and 100 percent** prepares a default preview. Select
   **Apply to reader and save** to commit it. Closing without applying discards
   uncommitted choices. Closing returns keyboard focus to the reading copy.

The saved profile is restored when another Voice reader opens for that library.
No speech, external program, font download or theme installation starts. A
missing font or malformed profile produces a visible notice and keeps the
reader's native defaults; the saved bytes are preserved. Users can retain OS
font/colors independently of zoom, or choose only one custom display attribute.

If another window changes the saved profile, applying a stale review is refused
and the reading display is rolled back. **Reload saved profile**, preview, and
apply again to adopt the current state. A closed reader, changed library object,
replaced library directory or changed retained source also invalidates apply.

## State and source integrity

`state/speech/reading-presentation.json` is a typed, non-executable sidecar with
an 8 KiB intake limit and schema `carr-reading-presentation/1`. Reading uses the
shared JSON file reader. Saving uses the existing state lock, atomic writer and
an exact saved-byte fingerprint. It creates no second preferences database.

The normal library backup includes the profile. Restoring into another library
directory requires no absolute-path migration. The source test suite actually
backs up a library, removes its old state, restores into another directory, and
checks the profile alongside reading-source, undo-history and cursor state.
Appearance does not call the text editor's text-replacement or history methods.

## Acceptance and limits

The three source/installed module suites are:

- `tests/speech/test_reading_presentation.py`: typed limits, malformed/duplicate
  state, local-path guards, compare-before-save, and actual backup/restore.
- `tests/speech/test_reading_presentation_handlers.py`: the real dialog and
  presentation methods with bounded widget stand-ins; preview/apply/reset,
  explicit picker return values, stale review/close, rollback, Unicode selection
  and transient partial zoom input.
- `tests/speech/test_reading_presentation_reader_hook.py`: the actual reader
  closure, including changed library/root/source, busy/closed state and focus.

`tests/ui/test_native_reading_presentation_driver.py` is a **source-only**
probe-helper suite. It verifies paired-key retry and independent cleanup, owned
nested-modal order, the inactive-parent barrier, foreign-window exclusion and
bounded failure exit. It does not execute wx or qualify a native display.

After installing the exact candidate wheel, run the native gate from outside
the checkout import path:

```powershell
python -I -B -X utf8 scripts\windows_reading_presentation_smoke.py
```

The probe verifies six installed module origins and their exact source bytes.
It runs an actual wx MainLoop, actual keyboard actions and two real Reader
modal sessions. Its assertions cover a native Unicode edit, selection/caret and
undo preservation, preview separation, actual TextCtrl font/colors/zoom,
persisted restart, reset, focus return and subsequent canonical reading undo.
Every native key is admitted only after foreground owner, process and focus
match, and is released with the same modifiers. The whole run and each stage
have finite deadlines.

The font/color chooser **selections are explicit fixtures over native wx data
objects**, so the probe does not qualify interaction with the OS picker windows.
The final JSON distinguishes the actual reader/display checks from those
fixtures. Speech, hardware playback, font legibility, color contrast, manual
keyboard usability and NVDA qualification are separate checks. This workflow
does not add reader tabs or a new renderer, and its portable tests do not imply
a Windows-native result.
