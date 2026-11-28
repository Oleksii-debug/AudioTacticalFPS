# Reader appearance in the current application

## Available behavior

The reading-copy editor and the current live source reader expose **Reader
appearance**. The same native dialog previews an installed font, text and
background colours, and zoom from 50 to 400 percent. Save applies the choices
to the open reader and later readers in this library. Reset previews the
system defaults; a separate Save makes that reset durable.

The editor styles both its immutable original-text view and its editable copy.
Appearance does not change text, line endings, source provenance, revisions,
selection offsets, reading cursor, speech rate or voice. It does not load a
font file or start a speech engine. An unavailable saved font remains recorded
but the current OS font is used until the user chooses an installed one.

Windows high contrast takes priority over saved colours. An unsuccessful
high-contrast query also keeps system colours. Preferences do not disable
keyboard focus, native text selection, Tab navigation or the existing playback
controls. The source reader retains its original playback controls, independent tempo/pitch
settings and selected-reader assistant action. Appearance and pacing add named controls.

## One existing state and backup path

This reuses the released PR143 appearance schema, with the current application's
local-path checks, native JSON reader/publisher and root writer. The only new
registered sidecar is `state/ui/reader-appearance.json`, at most 4 KiB:

```json
{"schema":1,"preferences":{"font":null,"foreground":null,"background":null,"zoom_percent":100}}
```

A non-null font has exactly `face`, `points`, `weight`, `style`,
`underlined` and `strikethrough`. Face names are values for the OS font
enumerator, never file paths to execute or install. Colours are uppercase RGB
hex strings or null. Unknown fields, duplicate JSON keys, non-finite values,
links and unreadable paths are refused.

Save holds the current application writer, then the released
`state/ui/.reader-appearance.lock`. The captured SHA-256 token must still
match. Reset cannot silently overwrite an invalid file. A directory sync
failure after a real atomic replacement is reported as a saved preference
with a cleanup warning only after the exact new bytes are re-read.

Complete backup holds the same preference lock under its application writer.
It preserves exact bytes, validates the typed schema, excludes the native
lock, and restores into a separate library. Existing reader-only v3 and managed
v4/v5 manifest envelopes remain unchanged. No neighboring UI file or active
runtime profile is added to the allowlist.

## Source validation gates

All new cases in this checkpoint are **NOT_RUN** while the executor is
unavailable. Required source and installed-wheel suites:

- `tests/reader/test_appearance.py`: bounded state, stale saves, malformed
  payloads, linked files, actual pre/post-publication faults, native lock
  contention and full-library backup/restore.
- `tests/ui/test_reader_appearance.py`: actual handlers with native-lifetime
  stand-ins, font/colour preview, high contrast, default-font reset, original
  bytes and selection preservation, destroyed/stale owners and constructor
  cleanup.
- Existing `tests/ui/test_narration_editor.py` and
  `tests/ui/test_live_reading.py`: unchanged editor persistence and live
  reader ownership. The narration stand-in declares the two actual native appearance lifecycle
  events; source/text assertions and the preparation action remain intact.

The installed native gate is
`python -I -B -X utf8 scripts/windows_reader_appearance_smoke.py --native`.
It opens the actual editable and live wx readers, uses actual native appearance
controls and current persistence, verifies unchanged original/project bytes,
and resets the saved preference through the real dialog. Font/colour picker
decisions are deterministic seams returning real installed font/colour values.
This gate does not claim physical picker interaction, NVDA, speech synthesis,
audio-device qualification, or native PASS before actual execution.

## Explicit paced reading

Both current readers also expose **Paced reading**. It is an optional visual
presentation of a selected range, the text after the initial cursor, or the
whole current section. The user chooses 40–1200 whitespace-delimited words per
minute and one to five words per group, then explicitly starts. It supports
pause/resume, previous/next, and Stop/rewind. A late GUI timer advances by one
shown group; it never skips unseen words to catch up.

Each displayed group is the exact original source slice, including internal
whitespace. The existing reader highlights those same character offsets.
Close and Stop restore the earlier selection if the same source is still
current. A changed source or owner stops the timer; it never retargets a new
book. The live reader refuses pacing while speech or automatic book advancement
is active. No speech, audio processing, text normalization or inference is
started by this mode. No reading-speed or comprehension benefit is claimed.

The explicit profile is at most 200,000 source characters and 50,000 tokens.
Choose a smaller section when the current source exceeds that profile.
Pacing controls are session choices; the appearance sidecar schema is unchanged.

Additional strict suites are `tests/reader/test_pacing.py`,
`tests/ui/test_paced_reading.py`, and `tests/ui/test_paced_reader_hooks.py`.
They cover exact Unicode/CRLF offsets, delayed timers, manual navigation,
stale/closed owners, original history preservation and the active-speech
exclusion. The same installed native appearance probe now also starts the
actual pacing timer, pauses it, navigates groups and closes through the real
current editor. These additions remain NOT_RUN until actual execution.

Explicit spelling and number/date/unit/symbol preparation remains in the current
source-bound reading preparation workflow. This port preserves its selected
Hunspell/normalization runtime, review/apply controls and provenance; it does not
replace that runtime with the owner's alternate spelling implementation.

## Primary API references

- wx.Window native font/colour/reset and ownership:
  https://docs.wxpython.org/wx.Window.html
- wx.Font attributes and fractional point sizes:
  https://docs.wxpython.org/wx.Font.html
- wx.FontData selected font contract:
  https://docs.wxpython.org/wx.FontData.html
- Windows high-contrast setting:
  https://learn.microsoft.com/en-us/windows/win32/winauto/high-contrast-parameter

## Reconciliation identity

The four appearance/pacing modules and their selected tests are ported from owner
`f8d5f85619b61fb52df24c7eb2f5dd4d1febc18f`. Shared reader hooks are composed
onto the existing candidate; its audio admission, progress, exact source cursors,
independent playback settings, explicit preparation and assistant actions remain.
The appearance fixture contains only the selected native-control test seams; no
alternate spelling package is required. Source review is not runtime acceptance.
