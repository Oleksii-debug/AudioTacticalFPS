# Studio clip editing and saved project history

The existing Scene audio clips window now provides **Timeline edits and history**.
It uses the same selected project, background worker, cancellation controls,
shared resource broker and original content-addressed audio inputs.

## Editing saved clips

- **Split** takes a position in seconds relative to the selected clip. The two
  pieces retain the same original audio and occupy the same scene interval.
  The first stable clip identity remains; the second gets a new identity.
- **Cut** removes a nonempty interval relative to the selected clip. Its time
  remains as silence, the scene duration is retained, and other clips do not
  move. Cutting the entire clip removes that selection without deleting audio.
- **Rejoin** combines two adjacent selections of the same retained original.
  Both scene positions and original positions must be contiguous. Gain, pan,
  track role and processing must agree. This is not an arbitrary concatenation
  of different audio files; use the existing scene assembly and export for that.
- Positions are quantized to the project's output sample grid. Each retained
  piece must contain at least one sample. Source format decoding and resampling
  still use the existing configured FFmpeg runtime.

Active gain and mono-pan envelopes are shifted to the second piece's original
sample position. Their linear/hold values continue rather than starting again.
Constant gain effects remain supported. Disabled effect settings remain unchanged.
A rejoin verifies the complete piecewise curve over the second selection,
including discontinuities; it refuses an edited curve that would be discarded.

A boundary inside an existing fade is refused. A completed outer fade can remain
on its corresponding piece, and an internal fade cannot silently disappear during
rejoin. Stateful clip effects such as compression, EQ, delay and reverb require an
explicit prior render because a new clip boundary could reset their internal state.
Scene and master effects continue to process the assembled scene through the
existing graph. These restrictions are visible errors, not silent approximations.

Sample-preserving operations require a verified source frame count at the project
rate, with the original trim and selected duration on that sample grid. Use the
existing Convert workflow first for another rate or unaligned source selection.
A scene start is captured once as its rendered integer frame; later pieces use
that frame plus their integer offset. A fractional start whose rounded end would
change duration is refused with an instruction to align the saved start.

All operations validate the retained source and the current project. Inspections
use the existing broker. Saving compares both revision and exact project
fingerprint, so an intervening edit cannot be overwritten. A cancelled operation
before publication creates no new revision. Reviewed book-audio plans continue
to require their own source/effects review rather than generic Studio editing.

## Copy, cut and paste saved clips

**Copy selected saved clip** remembers its project, scene, stable clip ID and exact
saved revision. Copy does not open media or voices, alter audio, create a project
revision, or use the operating system clipboard. The selection stays inside this
clip-editor session; choosing another project clears it.

**Cut selected saved clip for Paste** removes that selection through the existing
project writer and remembers the earlier revision. Other clip positions remain
unchanged. The original audio and the removed selection's immutable history stay
available. This is separate from cutting an interval and leaving its time silent.

**Paste copied clip at a scene position** asks for a destination time in the
currently selected scene of the same project. It retrieves the exact copied
revision and verifies its fingerprint, then reads the current managed original
through the existing source/decoder checks. It gives the new clip a fresh ID,
preserves its original trim, duration, role, gain, pan, fades, title and effect
chain, and changes only the requested scene position. Later changes to the
original clip cannot silently change the copied selection. Different scenes of
the same project are supported; cross-project or foreign-file references are
refused.

Paste checks the current project's revision and full fingerprint again at
publication. Changed or missing history, changed retained audio, cancellation,
an invalid time, and a competing edit stop publication. A successful cut or
paste participates in the same project Undo/Redo history. An explicitly later
paste can reuse the copied selection; no playback or rendering starts by itself.

## Undo, redo and whole-project history

Undo and redo cover successful saved edits made in the current editor session.
They restore the entire project: clip timing, scenes, roles, selected effects,
master settings and the saved script. Unsaved text fields retain their ordinary
native text editing behavior. Reloading or selecting a project starts a new
session history. The most recent hundred session edits are retained in memory.

**Browse and restore an earlier saved project revision** works after reopening
the application. It pages the existing ProjectStore revision files one hundred
references at a time; it does not create another history database. The chooser
shows each saved revision and fingerprint prefix. Restoration reads exactly the
selected historical file, verifies its full identity and typed project content,
and writes a new current revision through the existing writer. Linked, missing,
changed, foreign or ambiguous history is refused.

Undo, redo and explicit history restoration do not rewind the revision counter,
overwrite historical snapshots, delete completed renders, open audio devices,
discover voices, or execute effects. They do not resume restored jobs or grant
access to an external source path. Original audio and completed outputs remain
available. A new edit after undo clears the session redo branch; older versions
remain in the durable project history.

## Repeat part of the current preview

Save the clip settings, then choose **Preview saved clip** or **Preview without
clip effects**. In **Preview repeat...**, choose **Play A–B repeat...** and enter
A and B in seconds within that rendered preview. The interval must be at least
0.05 seconds long. The times refer to the preview, not the whole scene or the
untrimmed original.

Confirming the two values explicitly plays that interval and repeats it. This
also works when the short preview finished while the range dialogs were open.
It reuses the current rendered file; it does not render again or change the
saved project, trim, fades, gain or original audio.

Choose **Turn preview repeat off** to return to ordinary playback. Clearing
repeat leaves a stopped preview silent. **Stop playback** ends playback and
repeat. Choosing another saved project or clip, starting another clip operation,
changing the active library or closing the editor revokes pending repeats.
A new preview needs its own explicit repeat choice.

If playback cannot yet confirm Stop, the editor retains its temporary preview
and asks you to retry Stop before closing. Cancelling either range dialog keeps
the previous playback choice. These are session controls; saved project history
and backups do not arm playback or repeat.

## Crossfade two adjacent saved clips

Select the first clip, open **Timeline edits and history...**, then choose
**Crossfade two adjacent saved clips...**. Select the following clip and enter
the overlap in seconds. Both clips must belong to the same scene and their saved
intervals must be exactly contiguous. The time entry rounds to the project's
output sample grid; the overlap must contain at least one sample and fit both
retained selections.

The operation adds complementary linear fade-out and fade-in ramps using the
existing renderer, and moves only the second clip earlier by the overlap.
It preserves both clip identities, original files, trims, durations, roles,
gain, pan, titles and effect settings. Other clips and later scenes keep their
positions. The scene retains at least the pair's previous end. Any shorter
tail becomes silence where no other scene audio occupies that interval.

Existing fades at the shared boundary, overlapping outer fades, a gap, an
already overlapping pair or incompatible active processing are refused for
review. The same restrictions on timeline effects apply as for the existing
split and cut actions. The operation checks both retained sources and saves
the paired change as one project revision. Cancelled work, a changed source,
a concurrent project edit or a BookPlan-bound project cannot silently replace
the reviewed pair.

Saving the crossfade does not render or start audio. Use the existing render
action to produce new output. Undo, redo or a selected historical revision can
restore the whole earlier project while retaining original files and completed
renders.

## Named scene markers

Open **Timeline edits and history...**, then **Named scene markers...**.
Choose **Add a named marker...**, enter its position in seconds from the
current scene's start, and give it a name. The time entry rounds to the
project's sample grid. A project can retain up to 1,024 markers; each name is
limited to 256 characters.

Select an existing marker to edit its name and time or remove it. The marker's
stable identity is retained when it is edited. The chooser shows the name and
time; saved operations remain bound to the exact selected marker and project.

Markers are saved references to positions. They do not move clips, extend a
scene, alter original audio or start playback. A real marker change creates one
current project revision; saving the unchanged name and time creates none.
Reloading the project, undo/redo
and explicit history restoration retain the corresponding marker state.
Restoration creates a new revision and never rolls back the revision counter.
The existing complete backup carries the typed saved project and history.

A script with markers must keep the same ordered scene structure when edited.
If marked scenes have repeated titles, remove or review those markers before
editing the script; unchanged saves and project-title changes remain available.
This prevents a saved marker from silently following a different scene.

Older projects keep their previous saved representation until this explicit
marker feature is used. A reviewed BookPlan remains immutable; use a separate
authored project for marker edits. A changed project or library requires a new
review instead of applying a choice to another source.

## Evidence and limits

The required whole test modules cover sample/curve geometry, saved history and
concurrent writers, actual PCM split/cut/crossfade/restoration, marker persistence,
and queued native controller behavior. Copy/Cut/Paste keeps its real source and
history checks. Preview-repeat tests use real PCM files, player state and
quiet-owner holds with a controlled native transport; they do not establish
physical playback. All earlier cases remain required alongside the new ones. The required Windows Studio probe
also traverses the actual keyboard-driven
timeline menu, split, undo, redo, rejoin, cut, rendered silence and exact history
restoration and Copy/Cut/Paste/Undo, retaining the existing envelope/effects
checks and source origins. A real nested-modal control checks failure cleanup;
host shutdown uses the actual DesktopFrame Close and existing owner drain.

At this checkpoint these new tests and the extended native probe are **NOT_RUN**.
The earlier envelope-only source result cannot qualify these later clip changes.
Actual results must name the source commit, test counts, runtime and installed
module identities. Physical monitoring, acoustic quality and manual NVDA
completion remain separate qualifications.
