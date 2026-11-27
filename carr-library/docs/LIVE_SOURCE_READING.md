# Live reading from an exact source or editable reading copy

Open **Read aloud from this source** in the Reading Desk (`Ctrl+Alt+R`), or
**Read aloud** in the editable reading-copy window. Refresh local voices, then
choose a voice explicitly. The current voice registry and its configured local
engines are reused; opening a reader never downloads a model or selects an
unavailable replacement voice.

## Reading and navigation

The range selector supports selected text, text from the cursor, the current
word, line, punctuation-delimited sentence, paragraph, current source location,
and the whole book from its first location. Whole-book reading loads one source
location at a time and skips empty locations. Sentence boundaries are a
punctuation navigation aid; they are not a claim about linguistic segmentation
for all languages.

**Prepare first excerpt** synthesizes a single bounded utterance without
starting an audio output device. **Read aloud** prepares and plays successive
utterances. The highlighted range identifies the exact source text for the
utterance. No word timing is inferred from elapsed time. Native control offsets
are verified against their actual text prefixes, including supplementary
Unicode characters and Windows line endings.

The existing shared player supplies pause/resume, seek and device selection.
Pause and precise seeking require its native Windows backend. **Where am I?**
shows the source location, utterance range, measured duration and the player's
reported position accuracy. Voice-native synthesis rate, pitch and volume are
enabled only when the chosen voice advertises those controls; they do not
silently substitute a different voice. Independent live playback speed/pitch
remain a separate player implementation task.

## Lifetime, cancellation and source integrity

One reader owns one background worker and at most one temporary PCM utterance.
Synthesis uses the existing resource broker, engine supervision and process
ownership. The source is checked before and after rendering. The selected
engine identity must remain unchanged for the session. The existing shared
media coordinator prevents conflicting narration/recording activity.

Before Play, a background worker revalidates the source and prepared PCM digest.
The existing immutable payload reader stays open across any media-coordinator
delay until the player releases its handle. Windows writers and renames are
excluded for this interval; changed POSIX payload metadata prevents both
handoff and a completed-cursor acknowledgement. A valid WAV header alone never
authorizes replacement audio to stand for the rendered source utterance.

The text cursor advances only after the player reports completion and releases
its audio handle. Stopping an unfinished utterance keeps its starting position
so resume can repeat it. Closing waits for pending synthesis and playback
owners before deleting temporary audio. A failed pause does not discard the
completion observer. A failed audio-handle release keeps the dialog and PCM
available for an explicit retry.

Original library text is read-only. Unsaved edits in a reading copy are spoken
as that independent copy and never borrow the original's verified source
location. Opening or speaking a source does not approve OCR or create quotation
evidence.

## Durable cursor and complete backup

`reading-speech.json` stores at most one resume row per library edition, with
the exact revision, source location, text digest, text length and completed
cursor. Independent reading copies are keyed by their text digest. The sidecar
contains no source text or PCM. Its bounded schema is validated before writes;
invalid or inaccessible existing state is not silently reset.

The existing reading-position store also remembers the current page. Restoring
a speech cursor requires that exact source text and location. A changed
revision never receives an old cursor automatically, and restoring a row does
not activate its historical voice or start playback.

Complete backup includes this typed sidecar, verifies each bound location
against the copied immutable database, and restores the bytes without retaining
links to the old library. Historical positions can be backed up without being
retargeted to an active revision. Temporary utterance PCM is not a backup input.

## Optional configured admission

A trusted host may pass its existing `TrustController`. Starting an explicit
reading request captures the existing `audio.render` admission after source
validation and before synthesis. The admitted request owns all of its bounded
utterances and, for whole-book reading, subsequent page sessions. Expiry does
not interrupt that owned work. Stop, completion and Close end that owner; a
new explicit request requires fresh admission. A nested caller cannot replace
the host authority or reuse a captured operation under a different authority.

Private installations keep their existing local behavior. Reading source
text, loading saved positions and saving or backing up owned data do not
request speech admission. Loading a saved voice setting never starts synthesis.

## Qualification of this composition

The focused suite includes exact source selection, actual resource
reservations, files, workers, immutable source libraries and complete
backup/restore. Its PCM producer and character/UTF-16 control observer are
synthetic. The prepared-audio handoff tests change actual PCM files and verify
that they cannot advance the completed cursor. Additional signed synthetic
permission tests exercise refusal before synthesis, continuation after expiry,
fresh-request admission, authority ownership and closed-operation reuse.

The current r7 source composition must run these tests on both source and
installed packages. Prior owner-branch counts do not establish a pass for this
composition.

`scripts/windows_live_reading_smoke.py` is the installed/native probe. It uses
actual wx controls and an explicitly selected installed SAPI voice to prepare
measured PCM, and verifies Unicode source highlights, focus, stop, durable
resume and modal cleanup. A probe is accepted only when the exact composed
source executes successfully. Physical listening, manual NVDA and
language-specific voice quality remain separately reported evidence.
