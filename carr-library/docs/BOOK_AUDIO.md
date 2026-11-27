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
   output format; discovery and preview start no speech.
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
Duration-based splitting is unavailable.

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

## Source authority and completed results

A plan retains the non-authorizing ReadingDocument anchor, exact source text,
capture-time metadata/warnings, selected range, split options and current Role
fields. New jobs use current Project schema 2; no effects/schema 3 migration
occurs.

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

Schema 2 origin rules also apply to book jobs without audio clips. The local
origin nonce is excluded from backups. After restoring to a different path
**or the same path**, open the saved plan, choose a new output parent, Preview,
Prepare, then review the newly assigned names before Run. Old output paths
stay inert; restoration neither reads them nor resumes work or opens engines.
Missing jobs in an interrupted Prepare stay unprepared.

The passive validator checks each present deterministic job/project against its
exact BookPlan and a persisted source receipt against
`plan.provenance(index)`. Copied database anchors are validated without old
path access or treating historical receipts as current authority.

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
receipts. Success does not establish manual NVDA use, listening quality,
third-party voice rights or private neural-model inference.
