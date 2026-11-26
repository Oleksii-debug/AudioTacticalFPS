# Live Voice Reader

The existing Audio menu now includes **Open Voice Reader**, **Read selected book
or chapter aloud**, and **Local voice and audio settings**. The reader uses the
same installed-engine registry and `NarrationDraft` edit model as Audio Studio.
It has no separate production renderer or provider gateway.

Choose a local voice, select text or choose Read from cursor, then use the native
Read, Pause / resume, Stop, and Seek controls. Windows playback uses MCI's measured
position; the explicit ffplay fallback reports approximate elapsed time. Words are
not highlighted because these adapters do not supply verified word timings.
Synthesis reads literal text; Audio Studio script directions are not executed by
the reader. WAV export never overwrites an existing file.

The optional shared keyboard manager registers a `voice_reader` context with Read
F6, Read from cursor F9, Pause F7, Stop F8, Save Ctrl+S, and Save As Ctrl+Shift+S.
Native buttons and menu mnemonics remain available. Personalized combinations are
applied through the existing manager, including after settings change.

Book and chapter reading uses `ReadingDesk` authorization and exact sealed source
positions. The recovery identity contains the workspace, edition, revision and
selected ordinal range without parsing slash-delimited catalog identifiers.
Source bindings retain each page/segment locator, original asset hash, text hash
and reading-copy offsets. A changed revision does not silently become the old
reading source. Edits affect the narration derivative, never quotations.

`voice-reader-state.json` is a strictly validated, bounded recovery sidecar. It
retains edit replay, the exact source binding, chosen voice, cursor and playback
position. Reopening regenerates temporary speech using the selected installed
engine; it seeks to the saved position only when both the text/settings identity
and actual generated WAV SHA256 match. Temporary playback files are removed after
the player and synthesis worker close. No voice models are copied into this state.

`audio-engine-settings.json` stores only explicit local FFmpeg, Piper executable,
Piper manifest and dordiai manifest paths. It cannot contain API credentials,
extra helper fields or a project-supplied executable. Both sidecars use the
library's existing exclusive writer lock. Their public validation functions are
available to the typed full-backup registry; this feature's standalone source
branch does not itself claim that the later composed backup adapter is installed.

The playback and source-loading view is adapted from PR103 at
`fc84a791930dcff419d2859ab3a040c0f4481261`. Its separate Studio, voice implementations,
assistant and generic state-directory copier were not imported. Existing PR99
engines accept legitimate system executable hard links; the reader's untrusted
input-file checks continue rejecting links and reparse points.

Local regression evidence exercises actual Flite/FFmpeg synthesis, exact PCM
assembly, cancellation, revoked-source publication prevention, source-bound
recovery, source-preserving editing and non-secret settings. Native Windows,
physical playback controls, manual NVDA and listening quality require their
separate platform checks; Linux tests do not certify those results.
