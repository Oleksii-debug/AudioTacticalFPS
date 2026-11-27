# Scene audio clips

Open **Audio Studio → Edit scene audio clips**. Choose a saved production or
create an audio-only project. Choose its scene, then **Add audio file** or
**Add saved recording**. The latter uses a completed recording from
[Record microphone](MICROPHONE_RECORDING.md); it verifies that recording's
receipt before retaining the exact selected bytes.

The named native controls edit the clip name, start in the scene, start in its
original audio, selected duration, gain, pan, fade in, fade out and track role.
An empty duration retains the rest of the selected original. **Save clip
settings** validates and publishes one new project revision. **Move to another
scene** moves the same clip in one revision; **Remove clip** removes its placement
and keeps retained media and earlier revisions. Minimum scene duration can add
silence, but never truncates a longer placed clip.

**Preview saved clip** renders the selected range with saved gain, pan and
fades. It omits the leading scene silence. **Stop playback** also invalidates a
pending preview waiting for progress speech to become quiet. Closing during an
import or preview requests cancellation and waits for the owned operation to
finish before disposing its controls. No playback or microphone capture begins
when this window opens. All controls and buttons are keyboard reachable;
Escape closes or cancels and waits.

Render the complete project from **Audio Studio → Script and audio production**:
open the saved project, then Render. The existing master, chapters and speaker
stems are retained. Edited clip projects additionally publish whole-master
aligned `stems/clips/{sfx,ambience,music,microphone}.wav` for roles that occur in
the project. Music clip stems are before sidechain ducking. `timeline.json`
records measured selection duration, sample-aligned placement, source hash and
saved controls. A recorded clip does not create invented transcript text or
word timings.

## Preservation and recovery

The selected local original is copied into existing private Studio inputs under
its SHA-256 identity. Copying, decoder verification and recording-receipt binding
must complete before a project revision is published. Only that selected file
is admitted; neighbouring files are not authorized. Editing never rewrites its
audio samples. A concurrent project change requires reloading rather than
silently replacing the newer revision.

Schema 1 project serialization and fingerprints remain byte compatible. New
clip controls use project schema 2. Saving an unchanged loaded script preserves
clips, source bindings, performance mode and sample rate. Changing the identity
of a scene with clips requires explicitly moving or removing them first.

The existing complete backup includes retained clip inputs, current projects,
historical revisions and production jobs. On restore into a new library, input
paths are rebound and output destinations require explicit selection. A real
regression deletes the old original, library and outputs before restoring and
resuming the same edited-clip job; its new PCM master matches the prior bytes.

## Execution evidence

The portable tests use actual local FFmpeg decoding and mixing, with known
synthetic PCM. They check audible source trimming, silence placement, left/right
routing, fade amplitudes, full-length stem alignment, cancellation, changed
source rejection, stale edits, exact original preservation and destructive
backup recovery. Controller tests cover queued selection binding, close/cleanup
and late completion. These are not microphone quality or human accessibility
tests.

Windows acceptance runs `scripts/windows_clip_editor_smoke.py` with the installed
interpreter. It requires real native focus, a wx main loop and Space key events,
then imports, edits, renders, rejects a stale revision and removes a clip. The
file-picker result is an explicit fixture; native file-picker and Windows 11
NVDA/manual listening qualification remain separate.


## Cut, copy, paste, split, join and revision history

The scene editor exposes these operations as named native buttons in its
keyboard-scrollable form. They run through the same background operation and
ProjectStore used by the original clip controls. Opening the dialog, selecting
a clip or applying an edit does not start audio.

- **Copy clip instructions** verifies and retains the selected media in this
  library, then keeps its timing/track controls in an editor-local clipboard.
  It does not overwrite the system text clipboard. Copy does not change the
  project revision.
- **Cut clip** performs that verified copy before removing the selected cue.
  Retained original bytes stay available. If the removal cannot publish, its
  error remains visible and the source project is unchanged.
- **Paste copied clip** asks for its new scene start. It adds a new clip identity
  and rechecks the retained content before publishing one project revision.
  It never replaces the destination scene or another clip.
- **Split selected clip** asks for seconds from the beginning of that clip,
  rounds to the project's output-sample grid, and creates two contiguous
  instructions referencing the same original. Mixing resamples the original
  stream before selecting sample intervals, so each new cut does not restart
  the resampler at its own boundary. Existing outer fades stay at
  the outer boundaries. Splitting inside an existing fade is refused until
  the user adjusts that fade, because this schema cannot preserve a partial
  envelope across the new boundary.
- **Join adjacent clips** joins two contiguous cuts of the same retained
  original only when source/scene timing, gain, pan and track match and there
  is no fade at the shared boundary. It preserves the left clip identity and
  outer fades. Joining unrelated recordings or differently processed clips
  requires the existing render/concatenation workflow; this command does not
  silently flatten their processing.
- **Undo clip edit / Redo clip edit** restore the latest 32 edits in this editor
  session by publishing new, increasing revisions. The existing persistent
  project revision files remain available to backup/recovery. Redo is cleared
  by a new edit; opening a different externally changed history does not allow
  overwriting it. This is not a claim of an unlimited cross-session undo stack.

Every clip publication compares both the reviewed revision and complete project
fingerprint while the existing ProjectStore lock is held. A competing change
that reuses the same revision cannot be overwritten. Original script, spoken
lines and source references remain separate from clip instructions.

Changing the active library refuses new work, cancels queued operations and
prevents a late result from repopulating the new library's controls. A completed
write still belongs to its original root; cancellation does not undo a committed
revision. Closing waits for owned work, confirms playback closure, disposes only
the editor's owned service, and leaves borrowed services alive. Preview cleanup
does not delete retained originals.

### Verification of this extension

The existing clip suites now cover actual FFmpeg PCM equivalence before/after
split and join, including 44.1 kHz originals into 24/48 kHz projects and
cuts between source samples; cut/paste and durable undo/redo revisions; source tampering;
cancelled and stale operations; preserved originals; and owned/borrowed cleanup.
The existing installed Windows clip probe drives the real copy, split, join,
undo, redo, paste and cut controls, including actual numeric/choice dialogs.
These additions are prepared source until their exact-head CI executes.
Physical device and human NVDA/listening acceptance remain separate.

Markers, loop preview, dry/wet controls, a complete effects-chain editor and
recording pause/monitoring retain their own requirements. The added operations
do not close the entire professional-studio acceptance group.
