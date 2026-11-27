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

## Evidence and limits

Three complete new test modules cover pure sample/curve geometry, real history
and compare-and-swap races, actual PCM split/cut/restoration, and queued native
controller behavior. Their expected static counts are 59, 29 and 22 respectively.
The required Windows Studio probe also traverses the actual keyboard-driven
timeline menu, split, undo, redo, rejoin, cut, rendered silence and exact history
restoration, retaining the existing envelope/effects checks and source origins.

At this checkpoint these new tests and the extended native probe are **NOT_RUN**.
The earlier envelope-only source result cannot qualify these later clip changes.
Actual results must name the source commit, test counts, runtime and installed
module identities. Physical monitoring, acoustic quality and manual NVDA
completion remain separate qualifications.
