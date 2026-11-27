# Reader integration checkpoint — 2026-11-27

## Scope and evidence

This additive checkpoint reuses selected code from PR112,
`40c58b4f631e48b863ef2867d61100ee8445092a`, on
`f841a3ad46860c5ee03038cce9f78d427c8b68a1`.
It covers command search, reviewed Find/Replace, exact native cursor mapping,
and bounded skip/percentage/A–B/sleep controls.

**Local execution: 336 PASS across 16 strict suites; zero failures, skips,
expected failures or unexpected passes. Windows and NVDA: NOT_RUN.**
The initial unreferenced checkpoint
`17dc49178845b9e899f1d5a458a3dfae56e021cf` was prepared during an executor
outage. After recovery, actual tests identified and corrected the donor's
overbroad shortcut-default exception and completed the existing source-bound
reader fixture interfaces. The complete 16-suite selection then passed on
Linux/Python 3.12.14, including two actual ffplay transport cases.
The donor's prior runs do not qualify this composition. The integration owner
must still record installed-package and native results against the final commit.

This supplements [the requirement snapshot](RESONANCE_REQUIREMENT_STATUS_20261127.md);
it does not mark the complete reader or platform qualified.

## Reused boundaries

| Component | Existing authority and retained behavior |
| --- | --- |
| Command palette | Reads the same menu/keyboard registry; rechecks native menu identity, scope and enablement; closes before the original handler runs. |
| Saved shortcuts | Schema 1 and explicit old assignments remain; a conflicting new palette default becomes unassigned without rewriting saved bytes. |
| Find/Replace | The current ReadingDocument and bounded regex child; review before one undoable edit; source text, lineage and quotation authority remain unchanged. |
| Native text positions | Verified wx ranges map UTF-16/CRLF positions to character offsets; reader-session v1 remains readable. |
| Playback navigation | Borrows the current WavePlayer and progress quiet-start path; binds one PCM file/generation; Stop, replacement and close revoke queued actions. |
| UI lifetime | Reader-owned timer stops for the owner's destroy event; child destroy events propagate without closing it. |
| Persistence and cleanup | Current Save All, recovery store, voice-engine ownership and audio cleanup remain in place. |

The donor's alternative player/cleanup, ASR, backup, pronunciation editor and
keymap interchange implementations are outside this checkpoint.
ReadingSnapshot is the donor's immutable reader-capture type, placed directly
with Find/Replace so it does not require the unrelated pronunciation dialog.

## Required checks

Shared source and installed-package selectors (no intentional platform skips):

- `tests/ui/test_command_palette.py`
- `tests/ui/test_native_palette_driver.py`
- `tests/ui/test_playback_navigation.py`
- `tests/speech/test_find_replace.py`
- `tests/speech/test_find_replace_handlers.py`
- `tests/speech/test_find_replace_reader_hook.py`
- `tests/speech/test_reader_cursor.py`
- `tests/speech/test_playback_reader_hook.py`

Existing keyboard, reading/session/source, Save All and cleanup gates remain
required. The Save All and source-bound Save As native-control fixtures gain
the actual range and destroy event interfaces now used by the reader. Their
persistence, late-callback, original-byte, recovery and authorization assertions
remain unchanged. The saved-keymap exception is limited to the introduced
palette's Ctrl+Shift+P default; existing conflicts with other commands still fail.

On POSIX, `tests/audio/test_playback_navigation_posix.py` is a separate required
gate with real ffplay and SDL's dummy sink. It verifies actual PCM transport,
owned child replacement/cleanup, unchanged input, natural EOF and explicit Stop.
It does not qualify an output device.

On Windows, run the installed package through
`scripts/windows_command_palette_smoke.py` and
`scripts/windows_playback_navigation_smoke.py`.
The palette probe retains native typing, Tab/arrows, Enter/Escape, original Save
and Select all, remapping and disabled/stale command rejection. Keymap
interchange stages are omitted because that feature is not imported.
The playback probe uses a declared synthetic clock transport and actual PCM
publication. It labels MCI, output devices, voice quality and NVDA `NOT_RUN`;
the existing real audio gates remain mandatory.

Physical keyboard/NVDA, measured output-device behavior, live playback speed,
reader tabs/presentation profiles and other open requirements need their own
evidence. No new dependency, model, network endpoint or private artifact is added.
