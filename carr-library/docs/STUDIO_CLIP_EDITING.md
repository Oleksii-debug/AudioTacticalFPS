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
