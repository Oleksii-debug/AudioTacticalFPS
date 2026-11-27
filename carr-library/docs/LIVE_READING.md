# Shared live source and reading-copy reader

Resonance uses one bounded live controller in the existing source reader and
the canonical **Voice reader and reading copy** dialog. The latter retains
`ReadingDocument`, its exact source map and reversible edit history, and
`ReaderDraftStore`. There is no separate narration-project format or second
voice engine. Synthesis uses the configured canonical `VoiceRegistry`; audio
uses the canonical `WavePlayer` through a validation-only controller facade.

## Reading controls

Open a source page from the library, or open **Voice → Voice reader and reading
copy**. Voice discovery runs in the background. Select a voice explicitly:
neither the first discovered voice nor a recovered draft's voice starts reading
automatically. Engine configuration comes from the current library's `state`
folder, including an optional locally configured dordiai adapter.

| Action | Default modal shortcut | Behavior |
| --- | --- | --- |
| Read current text | F6 | Read the displayed source location or current reading copy. |
| Read from cursor | F9 | Start at the exact native text cursor. |
| Read selection | Ctrl+Shift+F6 | Read only the selection; never continue into another location. |
| Pause or resume | F7 | Keep the current audio chunk and its measured Windows playback position. |
| Stop | F8 | Cancel synthesis/playback and release its temporary audio. |
| Resume saved source position | Ctrl+Shift+F8 | Revalidate and restart the saved source chunk explicitly. |
| Voice status | Ctrl+Alt+F8 | Focus the named live status control on request. |

Source navigation, bookmarks, ordinary text editing and the canonical global
shortcut settings remain available. Global progress keeps its canonical
Ctrl+Alt+P command. The table describes the modal defaults; it does not claim a
separate configurable hotkey system. Tab and Shift+Tab reach named native
controls. Background discovery, rendering and status updates do not move focus.

Rate and volume apply to the next requested reading operation and are enabled
only for capabilities declared by the chosen canonical voice. Automatic
continuation to the next source location is off by default. Seek is explicitly
limited to seconds within the current synthesized chunk; a stale seek dialog
cannot move a later chunk. On Windows, seeking while paused keeps audio stopped
until explicit resume.

**Use voice for progress** explicitly prepares the selected local voice for the
canonical progress settings. Discovery does not select a voice or turn on spoken
progress. The existing progress mode remains the user's choice; cancelled or
closed dialogs release an unused prepared provider.

## Exact source and position meaning

Native selection positions are resolved through `TextCtrl.GetRange` prefixes,
so UTF-16 surrogate pairs and native CRLF presentation are never treated as
Python character indexes. Displaying an unchanged CRLF reading copy does not
create an authored edit. The canonical source map and undo history remain the
authority for edited text.

The live status reports the exact synthesized character range and audio time
within that chunk. It provides **no word alignment or NVDA cursor tracking**.
Windows MCI supplies measured audio position; the canonical POSIX fallback uses
an explicitly labelled estimate. The audio duration is measured from the WAV.

Source authorization and active revision are checked before reading, throughout
synthesis and playback, and fully again before each chunk is played. A saved
reading copy may carry typed source bindings; those bindings must reconstruct
the exact original source before playback or export. Revocation, archived
sources, changed revisions, cancellation and device failures stop the operation.

## Persistence and exports

`live-reading-state.json` stores only authenticated source locations, exact text
lengths, chunk offsets and nonsecret voice settings. Resume never starts by
itself. A stopped chunk restarts at its verified beginning; completion advances
the saved position only after playback actually ends. Edited-copy live chunk
positions remain session-only, so they cannot be mistaken for original-source
positions.

Canonical `.reader.json` sessions use version 1 with the canonical document
inside: independent documents remain `reading-copy.v1`, and source-bound
documents use `reading-copy.v2` with an explicit `source_anchor`. The compatibility
view of ordered source locations is derived from that anchor. Strict interim
`reader-session.v2` envelopes remain readable; an envelope cannot override a
different canonical anchor. Neither a filename nor an imported anchor grants
workspace authority. The complete-backup adapter authenticates bound drafts;
workspace-scoped backups omit independent drafts. A recovered draft with missing
or different lineage is retained unchanged instead of attaching it to the
currently selected source. Exported `.reading.json` documents retain canonical
lineage and edit history. Seeding a transcript draft never replaces an existing
authored copy.

**Save WAV** uses the same canonical renderer and atomic writer as other speech
operations. Its output remains private until a final complete source check.
Cancellation or revocation leaves no published WAV, and an existing destination
is never overwritten. Save As also rechecks the source after its native chooser.

## Bounds and cleanup

The default live budget is 600 characters, 60 seconds of synthesis time, 180
seconds of audio and 32 MiB per chunk. A session is bounded by 500,000 characters,
four hours of synthesized audio and six hours of elapsed time. The controller
checks free disk space before each chunk. Discovery and rendering observe the
canonical registry's cancellation and timeouts.

One worker owns playback and temporary files. Closing the window requests
cancellation without waiting on the native GUI thread. The progress-media lease
is released only after silence is confirmed. Each WAV is removed after the device
releases it. A failed stop or close retains its owned player and WAV for diagnosis;
unconfirmed silence retains the media lease and blocks another playback attempt.
This admission check also applies when optional cue muting is off. Progress cues
are suspended immediately; playback waits for actual cue quiescence rather than
relying on a later timer.

## Acceptance boundaries

Pure tests exercise real source databases, canonical draft schemas, actual PCM
files, cancellation, revocation, source-map preservation, native-prefix
conversion and no-clobber publication. Their controllable audio fixture is
explicitly synthetic and does not establish physical audio or NVDA behavior.

`scripts/windows_live_reading_smoke.py --output report.json` requires actual
Windows, native wx controls and installed SAPI synthesis. It exercises both
existing reader hosts and the library-specific voice configuration. Its
controlled PCM device is identified in the report; physical audio, listening
quality and NVDA remain `NOT_RUN`. `scripts/windows_v3_smoke.py` also verifies
the shared dialog's accessible native control names in the broader production
acceptance route. Both activate bundled native DLLs before importing wx.

Run the separate hardware gate on a Windows audio-capable machine with an
explicit installed SAPI ID:

```text
python scripts/windows_live_player_probe.py --library-root C:\ResonanceData --voice-id SAPI_ID --output player-report.json
```

That probe uses actual Windows MCI and fails if its device or timing checks
cannot run. It does not substitute a synthetic device or count a skip as a
pass. Human listening quality, target Windows 11 accessibility, NVDA operation
and any advertised voice catalogue still require their own acceptance evidence.
This reader does not claim full Balabolka feature parity.
