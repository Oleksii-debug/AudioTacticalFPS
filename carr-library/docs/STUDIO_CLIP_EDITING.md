# Scene audio editing

Open **Audio Studio → Edit scene audio clips**. This window edits the existing
versioned Studio project. It does not create another project database, change
an imported source document, or overwrite selected original audio.

Choose a project and scene, then select a clip in the named list. The list gives
its track role, scene start, source start and selected duration. The form is
scrollable, and every operation has a named native button. Opening the window,
changing a selection, editing a value or saving an instruction does not start
playback or recording.

## Exact time and range selection

Saved clip controls retain the existing scene start, start in the original,
duration, gain, pan, fade in/out, role and title. Blank duration means the rest
of the original. **Save clip settings** verifies the retained input and
publishes one increasing project revision.

The two range controls describe a half-open interval within the currently
saved clip: start inclusive, end exclusive. They accept seconds, decimal
seconds, or `HH:MM:SS.ffffff`. A blank range end selects the saved clip end.
The editor rounds once to the project's output-sample grid and requires at
least one sample. It does not use waveform pixels as editing authority.
Resampling happens before the exact sample cut, so adjacent cuts do not restart
the resampler independently.

Range edits preserve the scene positions of other clips. They do not ripple
spoken lines, silently move another track or shorten the containing scene.
A range boundary inside a saved fade window is refused until that fade is
adjusted: the current instruction model does not approximate the missing
partial envelope. Splitting or cutting through an enabled clip effect chain
requires explicit bypass first, because restarting a stateful filter at each
new cut would change its processing.

## Available commands

| Command | Saved result |
| --- | --- |
| Add audio file / Add saved recording | Verify and retain only the selected original, then add its placement. Recording receipts are checked before copying. |
| Save clip settings | Save reviewed source selection, scene timing, gain, pan, fades, role and title. |
| Move to another scene | Move the same clip identity in one revision; retain its original bytes. |
| Remove clip | Remove the placement while retaining media and historical revisions. |
| Copy clip instructions | Verify the selected media and put its instructions in this editor's clipboard. No project revision or system clipboard change. |
| Cut clip | Complete that verified copy before removing the placement. |
| Copy selected range | Copy the exact selected source interval and its applicable outer fades. |
| Cut selected range | Copy the interval, then leave a gap in this clip's scene placement. Other tracks keep their positions. |
| Trim to selected range | Keep only the interval, at its existing scene position. |
| Silence selected range | Remove that clip's contribution in the interval without moving its remaining parts. Other overlapping tracks continue to sound. |
| Paste copied clip | Ask for a new scene start, recheck the retained bytes and add a distinct clip identity. |
| Split selected clip | Split at an output sample, sharing the original and preserving outer fades. |
| Join adjacent source cuts | Join contiguous cuts of one original with matching role, gain, pan, routing and bypassed processing. No flattening or re-encoding. |
| Join clips as new audio | Explicitly render two selected clips in the chosen order, concatenate their actual PCM and retain a new stereo derivative. They must have the same role. Gain, pan, fades and enabled effects are included; the new placement does not apply them twice. |
| Crossfade into another clip | Ask for an overlap shorter than both clips. Apply complementary linear fades and move only the selected second clip to overlap the first clip's end. |
| Set scene duration | Keep an explicit minimum duration; a longer placed clip is never truncated. |
| Add / Edit / Remove scene marker | Save an authored label at an output-sample-aligned scene time. Markers do not add audio, alter quotations or claim transcript alignment. |
| Undo clip edit / Redo clip edit | Restore up to 32 edits in this editor session by publishing new increasing revisions. A new edit clears redo. Concurrent external edits require reload. |
| Clip / Speaking role / Scene / Master effects | Review the existing typed, ordered effect controls and save them in the same project. |
| Toggle saved clip effect bypass | Bypass or restore that clip's entire chain without discarding parameters or individual enabled/disabled settings. |
| Set master sample rate | Choose a supported rate and revalidate saved filters before saving. |

Joining unrelated recordings is explicit through **Join clips as new audio**.
It does not invent a transcript or splice the source files. The retained
derivative remains independently identified by its exact bytes. New native
work, its resource reservation and temporary files finish before the project
revision is published.

## Preview, repeat and dry/wet comparison

**Preview saved clip** renders its saved selection without leading scene
silence. **Preview selected range** renders the current range with saved clip
processing. **Preview range without clip effects** uses the same selection,
gain, pan and fades while bypassing the clip chain. These two explicit commands
provide dry/wet A/B comparison; they do not silently rewrite the chain.

**Loop selected range** renders that interval, starts it after existing progress
speech is quiet, and uses the existing playback navigation controller and its
owner-local timer to repeat it. A loop must be at least 0.25 seconds. Ordinary
preview supports shorter ranges. The timer polls at 200 ms; this is listening
preview, not a sample-perfect gapless transport. **Preview playback controls**
opens the existing seek, repeat A/B and sleep controls over the same player.

**Stop playback**, a new edit, a new preview or owner closure invalidates the
pending playback generation. A delayed repeat cannot restart a stopped or
newly selected clip. Direct native parent destruction also detaches the timer
and rejects later callbacks. No second playback engine is introduced. Native
Windows playback uses the existing WavePlayer; device and listening
qualification remain separate from synthetic transport tests.

Clip previews include only that clip's processing. Scene and master effects
are heard in the complete Studio render. Timeline chains preserve duration:
tempo conversion stays in the existing separate audio conversion workflow,
where the resulting derivative can be selected explicitly.

## Rendered output and compatibility

Render the project through **Script and audio production**. Existing master,
chapters, speaker stems, subtitle/LRC files and publication receipts remain in
use. Clip-role stems align with the whole master. Track effects are included
in their stems; scene and master effects apply to their mixed buses. Chapters
from a mastered project are exact output-sample intervals of that master.

The measured timeline records clips, the saved effect declarations and authored
markers. Marker annotations carry an explicit notice that they are not
transcript alignment or source citations. Effect and marker inventories are
passively validated during complete backup without opening a provider,
resolving external media declarations or executing a saved plan.

Schema 1 and schema 2 serialization and fingerprints remain exact. New effects,
markers, explicit bypass and preserved-stereo derivatives use schema 3.
Literal legacy schema 1 duplicate cue IDs remain readable; new edited projects
keep the existing duplicate-ID refusal. Saving an unchanged script preserves
the entire project. Script changes retain attached clips, effects and markers
and refuse to silently remove an affected scene or processed speaking role.

Undo/redo is bounded session history, while the existing persistent revision
files remain available to backup and restore. All edits compare the reviewed
revision and complete fingerprint under ProjectStore's existing lock.
Unknown producer fields stay rejected.

## Resource and preservation boundaries

Retention uses the current selected-input lock, resource broker and native
process owner. Source files are bounded to 8 GiB each and are rechecked around
copying and decoding. Preview and rendered join reserve measured PCM staging
space. Unconfirmed native cleanup retains the same temporary files and
reservation until its existing cleanup owner confirms quiescence.

Closing an owned editor closes its owned service; an injected borrowed service
stays open. Failed owned closure remains available for retry. A manual Close
that cannot release playback reports that state and keeps the owner reachable.
Changes never authorize neighbouring files. Restored media stays in the
restored library and external output destinations require explicit selection.

## Exact-composition qualification

These additions are source candidates until their exact composed CI runs.
No earlier donor result qualifies this composition.

The required portable suites preserve all prior clip/source/recovery assertions
and add real FFmpeg range, split/join, crossfade, dry/wet, retained derivative,
typed marker/effect backup and old-cache rejection cases. They compare actual
PCM, source bytes and durable project revisions. Controller tests use the real
bound handlers and existing navigation controller; their transport is explicitly
synthetic.

`scripts/windows_clip_editor_smoke.py --audio-evidence <reviewed-evidence.json>`
keeps the existing installed-byte and external-runtime checks, actual native
keyboard/form interactions and real FFmpeg output. It adds range, history,
marker, effect, dry/wet and loop controls, plus direct/native-parent teardown
before actual held callbacks are released. The loop transport is an explicitly
synthetic clock reading real generated PCM. It does not qualify MCI, an audio
device, a native file picker, physical recording, acoustic quality or NVDA.
