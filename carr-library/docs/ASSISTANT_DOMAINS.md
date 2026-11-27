# Reviewed local assistant domain commands

The existing **Tools → Local assistant** dialog now accepts a finite set of typed
commands for retained reading, saved evidence, existing audio services and saved
Book plans. Preview displays the proposed plan and selected permissions. **Run**
is a separate action. Saved named workflows use the same assistant plan/job
store, and restored jobs remain review-only until a fresh plan is submitted.

This adapter does not start a model server, microphone or playback automatically.
It does not accept Python, shell commands, FFmpeg filter strings, arbitrary
provider names or guessed input/output folders. A local model can propose only
the same typed vocabulary; the existing profile selection, timeout and review
rules still apply.

## Existing service routes

| Typed operation | Required selection and behavior |
| --- | --- |
| reader.read | Exact current edition/revision and bounded retained source range. Returned text and locator remain separate from quotation authority. |
| evidence.search, evidence.read | Current selected library and existing saved evidence. Reuse rechecks current source/note authority. |
| evidence.export | Exact evidence IDs and an explicitly named new output under the chosen folder. Existing files are not overwritten; an uncertain earlier export is not repeated. |
| voice.list | Actually configured local voice inventory, optionally narrowed by declared language and count. This does not synthesize or qualify a voice. |
| audio.effects, audio.inspect | Existing installed audio runtime and shared resource admission. Inspection requires a selected input path; effects expose bounded typed controls and actual installed capabilities. |
| audio.convert | An ordered list of exact selected paths and SHA-256 values, explicit new output name/format/settings. The existing conversion batch owns its journal, source checks, output receipts and resumable completed items. |
| audio.produce, audio.render | The existing Scene production callback. Both use the same explicit voice chooser for text proposals; a supplied typed project retains its selected roles. Partial producer output cannot complete the assistant step. |
| book.review, book.prepare, book.run | An existing saved immutable Book plan ID/fingerprint and a named existing output parent. Prepare needs the exact current review digest. Run requires its separately reviewed prepared digest. |
| book.batch.review, book.batch.prepare, book.batch.run | An existing saved ordered Book selection ID/fingerprint and one explicit existing output parent per book. Existing Book/Studio stores, source guards and completed jobs remain authoritative. |
| player.status, player.control | Only an assistant explicitly opened from the current Voice reader. Control permits reviewed Pause/Stop for that exact live session/source; it never creates a player or starts output. |

Audio inspection, conversion and production need an explicitly configured local
audio runtime when those existing services require one. The transferable
application's optional runtime setup and redistribution boundary are unchanged.

## Typed plans and permissions

Select the current library, allowed input folder and output folder in the
assistant. Typed JSON uses the same Preview/Run controls as guided instructions.
For example, this proposal only reads the current player's status when the
assistant was opened from the reader:

~~~json
{
  "steps": [
    {"tool": "player.status", "arguments": {}}
  ]
}
~~~

Use **Current-reader assistant…** in the Voice reader to obtain that connection.
A status result contains the current **session** and **source_sha256**. A later
reviewed Pause/Stop command must repeat those exact values with
"action": "pause" or "action": "stop". Closing the reader, changing its
source/library, or closing that assistant withdraws the previous authority.
Resuming an uncertain player dispatch never repeats it automatically.

Book operations consume IDs and fingerprints already saved by **Book audio** or
its ordered batch dialog. They never select an edition, split text, choose a
voice, or invent an output on the user's behalf. A Book review returns an exact
**review_sha256**. Preparation changes that review by creating the existing
deterministic Studio jobs. The new prepared digest is required for Run; the
earlier review does not silently become execution approval.

The Book invocation shares the existing production root, cache, job identities,
resource broker and selected voice owner. Its time and output budgets are
narrowed to the approved assistant scope. Completed original Book outputs are
checked against their original project, source provenance and full artifact
receipts before being reused.

## Cancellation, recovery and complete backup

Pause/Cancel retains already committed outputs and stops later assistant steps.
Unknown outcomes remain explicit; the adapter cannot infer ownership from an
occupied output path. Changed sources, output receipts, selected scopes or
review digests fail without an automatic rerender or overwrite.

Native child cleanup can remain pending after cancellation. Existing cleanup
owners retain the stage, assistant job lock, Book/Studio locks and shared
resource lease until actual quiescence is confirmed. Closing the native
assistant withdraws its reader bridge immediately and queues closure of its
owned lazy production service after the existing worker has settled.

Complete backup uses the existing typed state validators. It preserves these
finite plan arguments, result receipts and historical intent, while applying
the existing restored-review and new-output barriers. Restoring a plan does not
reactivate its former filesystem, voice or playback permissions.

## Qualification

The added portable suites use real SQLite state, file locks, FFmpeg/ffprobe and
existing synthetic PCM fixtures. Native controls are exercised by
scripts/windows_assistant_domains_smoke.py; its source and installed modes
check module origin and drive the real reader/assistant dialogs, Preview, Run,
Pause/Stop acknowledgement and reading-copy Save/Close.

These source additions are **NOT_RUN until the exact composed candidate's
gates complete**. Synthetic PCM, a native event dispatch or a successful file
receipt does not establish voice similarity, neural model quality, microphone
quality, human listening or manual screen-reader acceptance.
