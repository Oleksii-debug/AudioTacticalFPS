# Book audio: one retained source, reviewed parts, existing Studio jobs

This workspace splits **one explicitly selected book or source range** into a
bounded batch of ordinary Audio Studio jobs. It adds no second execution queue
and grants no quotation approval.

Open **Audio Studio → Book audio: split and render…** after selecting a retained
edition in **Books and Collections**. The command also belongs to the existing
keyboard command registry.

## Review, prepare, then run

1. **Load selected book** reads the current revision through ReadingDesk. Its
   retained original hash, metadata, reading positions and extraction warnings
   remain visible.
2. **Discover local voices** uses the configured VoiceRegistry on the desktop's
   existing worker. Only actually enumerated voices appear. Select a voice and
   output format; discovery and preview start no speech. **Role effects and presets…**
   opens the existing ordered Studio effect editor. It supports numeric controls,
   bypass, presets and undo/redo of the editable chain; nothing is rendered here.
3. Select the whole source or both first/last reading positions. Choose a split
   and a local output parent outside private library data.
4. **Preview split and names** shows exact Unicode character ranges and
   sanitized destinations. Editing a field invalidates that review.
5. **Prepare saved items** has a default-No confirmation. It stores immutable
   review metadata and deterministic Studio jobs; it starts no speech.
   Interruption can leave a valid plan with some jobs prepared. Repeating
   Prepare fills missing jobs without replacing the immutable plan.
6. **Run or resume saved items** is a separate default-No confirmation for the
   displayed saved destinations. Every item uses ProductionService, existing
   CPU/device/disk admission, owned local runtimes and no-clobber publication.
   Completed artifacts are checksum-verified before reuse.

One selected source is limited to two million Unicode characters and 256
parts. Each workspace has at most 4096 saved plans, with at most 16 MiB of
metadata per plan. A large source can also exceed the current declared
resource/disk policy. That refusal does not disable bounded runtime checks.

## Split and naming semantics

Splits are whole source, actual EPUB chapters, physical PDF pages, text line
breaks, punctuation-based sentences, Unicode character count, blank-line
groups and a bounded regular expression. Sentence and paragraph splits are
deterministic text conventions, not linguistic analysis. Separators and
whitespace remain part of the text: the parts cover every selected character
exactly once.

EPUB chapters use the imported spine inventory. PDF page mode requires
physical pages and refuses logical TXT segments. Custom expressions use the
existing isolated one-second reading worker with bounded matches/output.
Measured duration is a separate post-render action described below. Text splits
never estimate how long a voice will speak.

Names permit only `{index}`, `{book}` and `{part}`. Include the index; only a
one-to-four-digit decimal format is allowed, for example
`{index:04d} - {book} - {part}`. Names cannot become paths or object access.
The deterministic job suffix remains in each displayed destination. Text such
as `[Music: suspense]` or `Narrator:` is spoken literally and never parsed as
Studio instructions.

Current local output formats are WAV, FLAC, MP3, OGG, Opus, M4A, M4B, AIFF and
WMA, subject to the actual configured runtime's codec support. M4B and
line-alignment sidecars keep Studio's measured semantics. No word timing or
human listening-quality claim is added.

## Split a completed part by measured duration

1. Finish **Run** for the exact saved Book plan, then select **Measured duration
   parts…**. Choose one completed source part, a maximum in milliseconds, a
   name template, and a new ZIP outside the library and the original job folder.
2. **Preview measured parts** verifies the completed job, original source,
   timeline and actual PCM bytes. It displays sample frames, rate/channels,
   ranges, filenames and a review checksum. It creates no job or speech.
3. **Publish reviewed parts** requires its own default-No confirmation. Any
   changed source, options, names or output invalidates the old review.
4. The ZIP contains named WAV parts, exact original Project/timeline/Book
   provenance and a bounded manifest. Concatenating the part PCM yields every
   original master frame once, with no gap, overlap, resampling or re-encoding.
   RIFF and RF64 integer PCM masters are supported; long individual parts use
   RF64 sizes when RIFF cannot represent them.

Duration boundaries are actual sample frames. A split through a measured line
keeps that whole line's source reference and is explicitly marked approximate;
there is no word or character timing claim. The maximum applies to each part,
rounded down to an exact frame. A publication has at most 256 parts, so choose a
longer interval for a long source. A maximum from 1 millisecond through 24 hours
is accepted; the source itself has no new one-day ceiling. Explicit time and
byte/resource allowances can still refuse a large operation.

The existing shared broker admits CPU/memory/disk before source hashing or
copying. Cancellation keeps the original completed job and removes pending
staging. A file is never overwritten, and an identical competing file is not
adopted as this invocation's success. A crash after publication leaves the
prepared receipt for explicit verification. If receipt finalization or cleanup
fails after a file was actually published, the result retains its receipt and
asks for verification; do not repeat Publish.

The same route is available through the current Book CLI:

```text
python -m carr_library studio book --data-dir LIBRARY --workspace WORKSPACE duration-review PLAN_ID --part 1 --output NEW_PARTS.zip --maximum-ms 600000
python -m carr_library studio book --data-dir LIBRARY --workspace WORKSPACE duration-publish PLAN_ID --part 1 --output NEW_PARTS.zip --maximum-ms 600000 --approval EXACT_REVIEW_SHA256
```

If the plan was captured under a topic, provide that same `--topic`.
Both commands accept explicit `--timeout`, `--max-output-bytes` and
`--reserve-disk-bytes` allowances; these never override source or integrity checks.
A batch preview request may include a `voice.effects` list of the existing typed
`{kind, parameters, enabled}` values. The returned review includes that exact
chain and needs the same Prepare/Run approvals as every other Book setting.

## Source authority and completed results

A plan retains the non-authorizing ReadingDocument anchor, exact source text,
capture-time metadata/warnings, selected range, split options and current Role
fields. An unchanged plan retains its exact schema 1 serialization and Project
schema 2 identities. Explicit role effects create a new Book plan schema 2 and
use existing Project schema 3; no old plan is rewritten. Ordered effects and
bypass values are part of the immutable review fingerprint. They use the same
Studio rendering, source validation, cache keys and copied-state validation.
Role chains preserve timing: tempo is refused, pan envelopes need fixed pan
zero, and stereo-width envelopes belong to stereo scene/master buses. A role
envelope restarts for each measured speech line. Reopen a new plan to change
an already saved chain.

Fresh source checks run before queued work, after resource waiting, around
progress callbacks, during rendering and immediately before publication.
Workspace/topic revocation, changed retained bytes or metadata/revision,
replaced data root, or changed immutable plan stop the operation. An optional
progress observer cannot swallow authority, process or publication errors.

Each part has a deterministic Studio identity and literal line references.
Generic Studio Run/Resume refuses reserved book references without the
validated Book audio guard. Other historical source_ref values keep their
existing behavior.

The part's `book-source.json` and timeline digest bind output to the immutable
review. These are provenance, never quotation permission. Capture-time
extraction flags remain historical; narration neither changes the original nor
confirms extraction or creates a research citation.

Pause/cancel preserves accepted nodes and committed files. An error stops the
remaining part loop without relabeling earlier completed items. Resume checks
current source authority and completed artifacts before reuse.

## Backup and restore

Book plans belong to the existing typed complete-backup format 4 and its
original-asset, source-anchor and Studio-job checks. The backup contains
declarations and retained library bytes. Local engines, credentials, trusted
code, external output directories and execution authority are not transferred.

The same schema 2/3 origin rules apply to book jobs without audio clips. The local
origin nonce is excluded from backups. After restoring to a different path
**or the same path**, open the saved plan, choose a new output parent, Preview,
Prepare, then review the newly assigned names before Run. Old output paths
stay inert; restoration neither reads them nor resumes work or opens engines.
Missing jobs in an interrupted Prepare stay unprepared.

The passive validator checks each present deterministic job/project against its
exact BookPlan and a persisted source receipt against
`plan.provenance(index)`. Copied database anchors are validated without old
path access or treating historical receipts as current authority. Measured
publication receipts use the same typed publication producer; the external ZIP
is not silently copied into the backup. After restore, existing ZIP verification
requires explicit selection of the file. Restored Book jobs still require fresh
output selection and a new Run review before another measured publication.

### Recovering a publication interrupted after its final file appeared

An abrupt process stop can leave the published ZIP hard-linked to a temporary
staging file. Verification deliberately refuses that linked file. Keep the
original ZIP unchanged and do not repeat Publish. Make an explicit ordinary
file copy at a fresh path, then choose that copy when verifying the saved
publication receipt in Studio. The same exact receipt digest, Book source
metadata, archive structure and PCM content must still verify. This does not
render or synthesize again. The application neither removes an unknown alias
nor silently adopts an existing output.

## Desktop lifetime and accessibility

The dialog uses the existing desktop worker and ProgressHub. Source checks,
voice discovery and registry cleanup stay off the native UI thread. Progress
is indeterminate when no measured total exists. Controls have native
accessible names; Tab/Shift+Tab and Escape retain their usual behavior.

Changing the exact LibraryApp, data root or active scope invalidates queued
work and suppresses stale status/focus updates. Closing or forced destruction
requests cancellation. Registered idempotent finalizers run on that same
worker after active work, even when ordinary queued saves are cancelled.
Failed cleanup remains process-owned and emits a diagnostic. Only successful
explicit retry on an existing worker releases that owner; no automatic retry
thread or new queue is created.

## Qualification

This describes the composed source contract. New source, installed-wheel and
native acceptance are **NOT_RUN** until the exact successor passes its gates.
Existing test files and donor results do not qualify this composition.

Focused tests cover exact split coverage, original-source checks, default-No
and field review, actual FFmpeg output/reuse, cancellation, no-clobber
publication, new/same-path restore, owner cleanup and real thread-start
failure. The Windows probe requires actual wx controls, an enumerated SAPI
voice, pinned external FFmpeg/ffprobe, measured chapter PCM and exact source
receipts. The extension also drives the actual effect editor, renders its
role chain through Studio, then previews and publishes exact PCM parts through
the shared worker. Its small native RIFF fixture does not by itself qualify a
multi-hour RF64 throughput target; bounded RIFF/RF64 cases are tested separately. Success does not establish manual NVDA use, listening quality,
third-party voice rights or private neural-model inference.
