# Book audio: review individual books or an ordered batch

Open a library or topic and select an existing book in **Books and Collections**.
Choose **Audio Studio → Book audio: split and render**. The command is also
available through the existing command palette and keyboard settings.

## Review before rendering

1. Choose **Load selected book**. The inventory comes from the existing Reading
   Desk and stays attached to that edition and revision.
2. Choose **Discover local voices**, then select an installed voice. Discovery
   starts no speech. This workflow has no provider account or cloud transfer.
   Choose the exact declared voice and supported rate/volume controls. Those
   values are copied into the immutable review. Changing another window's
   settings later does not modify saved plans.
3. Leave both reading-position fields blank for the whole book, or enter both
   ends of an explicit 1-based range. These are reading-order positions; the
   preview retains actual PDF page indices or EPUB section identities.
4. Choose a split mode, output format and an existing local output parent
   outside the application's private data. Edit the name template if needed.
5. Choose **Preview split and names**. Read the original metadata, exact source
   revision, voice, ranges and complete destination paths.
6. Choose **Prepare saved items** and confirm. This persists the immutable
   review and ordinary Studio Projects/jobs. It starts no synthesis.
7. Review the prepared destinations, then choose **Run or resume saved items**
   and explicitly confirm local speech rendering.

The immutable plan contains exact source text, its existing non-authorizing
ReadingDocument anchor, original metadata, split ranges, voice and format.
Each part has a deterministic ordinary Studio job identity. Text resembling
Studio directions, including "[Music: ...]", remains literal book text.

## Supported boundaries

| Mode | Exact convention |
| --- | --- |
| Whole selected source | One item for the explicitly selected reading range. |
| EPUB chapters | Actual imported EPUB spine/chapter inventory, in its retained order. |
| Physical PDF pages | Actual physical-page positions; logical text segments are rejected. |
| Paragraphs | Text line endings. This does not reconstruct paragraph layout lost during extraction. |
| Sentences | Deterministic punctuation boundaries, including CJK punctuation without spaces. Abbreviations and decimal punctuation are not linguistically interpreted. |
| Characters | A selected number of Unicode characters, from 1 to 100,000. |
| Blank-line groups | Groups separated by blank text lines. |
| Regular expression | Boundaries at match ends, using the existing isolated reading-regex worker with a one-second limit. Matched separators remain in the preceding item. |
| Retained EPUB headings | Exact heading offsets and labels from the retained extraction metadata, including headings inside a chapter. Older imports without that inventory require another split mode. |
| Selected reading bookmarks | Explicitly selected existing Reading Desk bookmark IDs. Each boundary is the start of its exact page or logical segment; bookmarks do not store a character cursor. |

Whitespace-only fragments join a neighboring item. Concatenating every item's
original text reproduces the exact captured reading text. The existing reader
may use two newline characters between separate source positions; the original
retained document bytes are never changed.

Limits are **2,000,000 selected source characters and 256 parts**. Choose a
smaller source range or larger parts when the plan exceeds a limit. Duration
estimates from text are unavailable. Actual line and chapter timing comes from
the existing renderer after synthesis. The post-render workflow below splits
that measured PCM without another speech call.

For bookmark splitting, choose **Choose reading bookmarks...**, then select
the desired existing bookmarks. The picker identifies stale source revisions;
the worker also checks each chosen page digest and selected range. Unselected
bookmarks do not alter the plan. Adding another bookmark does not change a
saved selection; renaming, moving or deleting a selected bookmark requires a
new review. Heading and bookmark plans retain the exact captured metadata and
identities, compare them again before preparation/resume/publication, and include
them in part provenance. Text preceding the first selected boundary is kept as
**Opening text**. Coincident boundaries never duplicate source characters.

Name fields are **{index}**, **{book}** and **{part}**; the index also accepts a
one-to-four-digit decimal format such as **{index:04d}**. No attribute access,
conversion, path separator or external naming expression is accepted.
Studio sanitizes and bounds the title, then appends the stable job suffix.
**The paths in Preview are the actual destination paths.**

## Resume, cancellation and source integrity

Saved plans live under the existing scoped Studio state. They are review
metadata, not another scheduler or job-status ledger. Every item uses the
existing ProductionService, ProjectStore, resource broker, audio runtime,
node receipts and artifact checksums.

Opening or preparing a saved plan never resumes it automatically. On explicit
Run, a completed item is reused only after its artifact bytes and book-source
provenance pass verification. Missing or changed output refuses reuse.
Accepted audio nodes retain the existing Studio recovery semantics.

Pause or close requests cancellation through the current desktop worker.
Owned voice cleanup runs after active work on that same worker. A cancellation
acknowledged during source validation remains a resumable pause. A real source
revision, metadata, retained-original or authorization change remains an error.

The original library source, plan, project and reviewed output identity are
rechecked before execution, after resource admission, before final publication
links, and before accepting completion. Generic Studio Run refuses source-bound
book jobs without this original-source check; reopen them through Book audio.

Each completed part includes the ordinary Studio audio, measured timing,
unchanged literal Project and **book-source.json**. That file records the
source anchor, original metadata, full selection hash and exact part range.
It uses the existing artifact publication and checksum path. Narration creates
no quotation approval and does not make an unverified extraction verified.

## Backup and restore

The complete library backup includes the immutable book plan and
typed Studio Projects/jobs. It retains the original source through the normal
library asset backup. No alternate archive or source-path rewriting is added.

After restore, open the saved plan and choose **New output review**. This
creates a new plan and new job identities while preserving the historical
plan, job receipts and former paths. Select a **new output parent**, Preview
the exact names, Prepare, and confirm Run separately. A restored historical
plan cannot be rebound to a new output folder by repeating its old approval. Rebuildable audio cache is optional in canonical
backups; missing cache is regenerated only after the new explicit Run.

Publication receipts are also retained as typed historical metadata. External
EPUB, DAISY and measured-PCM archives are not copied into the library backup.
After restore, select the existing external publication explicitly to verify
its exact bytes through Studio. Missing external files do not prevent backing
up their historical receipts. A restored Book job still requires its own new
output review and explicit Run before it can supply a new measured publication.

## Split completed audio by measured duration

After a saved Book part has completed, choose **Measured duration parts...**.
Select the exact completed part, the maximum interval in **milliseconds**, the
part naming template and a new **.zip** file. **Preview measured parts** reads
the actual retained PCM frames and exact completed Project/timeline receipts.
It starts no speech and creates no new Studio job. The preview lists every
sample-frame range, duration and final archive member name.

Read the preview, then choose **Publish reviewed parts** and confirm. The
existing publication transaction reserves shared resources, stages a verified
snapshot, rechecks the original Book source and reviewed destination, and
publishes one new archive without overwriting an existing file. Changing the
source, source job, interval, naming template or destination requires a fresh
review. The accepted render and original library source remain unchanged.

The archive contains integer PCM **WAV** parts, the original completed part
Project, measured timeline, Book source receipt and a checked manifest. Joining
the decoded PCM frame bytes of every part in order reproduces the captured
master exactly. Splitting does not run TTS, resample, add codec padding or
predict duration from words. Each part has the same sample rate, channels and
sample width as its source.

Text provenance remains at the producer's **whole measured line** scope.
When a boundary crosses a line, that reference is explicitly marked as partial
and approximate. No character or word alignment is manufactured, even though
the audio boundaries themselves are exact sample frames. Literal source text
is retained once in the original Project; an excerpt of audio is not presented
as an exact character selection.

This bounded format supports uncompressed integer **RIFF WAV**, mono or
stereo, up to **256 parts**. RF64/compressed WAV inputs and automatic encoded
duration-part output are unavailable. Choose ordinary Book chapter/text parts
first when a completed master would exceed RIFF capacity. Maximum selected
intervals range from 1 millisecond to one day; total bytes, available disk and
operation time retain the existing AudioBudget and resource-policy limits.

Cancel/Close withdraws pending work. Temporary data and reservations are
released after the owned native operation has actually stopped; a pending
cleanup remains explicitly owned. A cancellation after atomic publication
does not relabel a saved archive as unpublished.
The existing prepared receipt supports explicit verification after a crash;
verification does not render or partition again.

The headless equivalents use the same saved plan and approval:

1. `duration-review PLAN_ID --part INDEX --output new-parts.zip --milliseconds 600000`
   returns exact ranges and `approval_sha256`.
2. `duration-publish PLAN_ID --part INDEX --output new-parts.zip --milliseconds 600000 --approval REVIEW_HASH`
   publishes only that current review.
3. Both accept the same explicit `--name-template`, `--timeout` and
   `--max-bytes`; changing a reviewed name or interval requires another review.
   Prefix these commands with the existing
   `python -m carr_library studio book --data-dir DIRECTORY --workspace LIBRARY_ID`
   and its exact `--topic`, if applicable.

Use the existing Studio `publications` and
`verify-publication RECEIPT_ID --file EXISTING_ZIP` commands to inspect or
verify the retained receipt, including after restoring elsewhere. Verification
checks archive structure, original metadata, checksums and the concatenated PCM;
it does not grant a restored Book job permission to reuse an old output folder.

## Multiple books and independent settings

Choose **Multiple books...** inside Book audio. Use **Choose library books** to
select existing editions. The literal title filter bounds the native picker
to its first 500 matches. Import source files or folders through the existing
Import Center before selecting them here; there is no parallel import path.

For each row, choose **Review selected book**. The existing split dialog opens
with that edition. Select its exact installed voice, supported controls,
split, names, format and output parent. **Use reviewed book** returns this
exact review to the batch and starts no jobs or speech. Every book may use a
different voice, supported controls, format and output folder. Move up/down sets the
execution order; remove affects only the unsaved selection.

**Preview entire batch** rechecks every source and destination. **Prepare
batch items** saves the ordinary Studio jobs. **Run or resume batch** requires
another explicit confirmation. A paused, cancelled or failed part stops this
invocation before later parts/books. Run again explicitly to verify completed
artifacts and resume remaining existing items. Already published output is
never silently replaced, and an optional progress observer cannot relabel or
repeat a durable completion.

The saved group contains only its title, library/topic scope and ordered plan
IDs/fingerprints. It has no separate progress journal or output authority.
Existing Studio jobs provide stored status, accepted audio and receipts. A
temporary stop signal identifies one active invocation, so a late request
from an old run cannot stop a newer one. Runtime signals are excluded from
backup and never cause automatic resumption.

**Saved batches** reopens these references without rendering. Their order and
settings remain immutable; **New batch** starts another explicit selection.
After an interrupted Prepare, reopen the saved selection if present and
review it again; missing unprepared items remain visibly unprepared. After
restore, stored output paths remain historical and the active output review
is unset. Choose **Fresh output reviews**, then review new folders per book,
Preview, Prepare, and confirm Run against the fresh identities and names.

A batch is bounded to **32 books, 256 total parts and 8,000,000 selected
Unicode characters**. Each individual plan keeps its original two-million
character bound. A retained plan is limited to 16 MiB. A multi-plan review
preflights its 64 MiB aggregate encoded size before loading the selection. Progress reports verified part counts and the current operation;
audio timing comes from actual completed Studio receipts.

## Headless review, preparation and resumption

Use the existing Studio command family:

```text
python -m carr_library studio book --help
carr-studio book --help
```

All operations require the existing library identity in `--workspace` and
the chosen private library location in `--data-dir`. Add `--topic` only for
the exact topic scope used by the review. The installed entry point retains
the standard installation lease.

A UTF-8 request file declares the existing edition IDs and per-book choices:

```json
{
  "schema": "carr-book-batch-request/1",
  "title": "Reviewed reading selection",
  "books": [
    {
      "edition": "EXISTING_EDITION_ID",
      "voice": {"language": "en-US", "voice_id": "EXACT_INSTALLED_VOICE_ID"},
      "options": {"mode": "blank-lines", "name_template": "{index:04d} - {book} - {part}"},
      "format": "flac",
      "sample_rate": 24000,
      "output_parent": "C:\\ReviewedAudio"
    }
  ]
}
```

Choose IDs and the exact declared language from the installed voice inventory;
the placeholders above are not defaults. Both `language` and `voice_id`
are required. Named profiles and implicit default voices are not request
authorities. Optional integer `rate`/`volume` must be supported by that voice.
Optional `revision`, `first`, `last` and `chapter` fields narrow the existing
source selection. Requests are bounded to one MiB.

For heading splitting use `"options": {"mode": "headings"}`. For bookmarks,
first run `bookmarks EDITION_ID` with the same library/topic options to list
the actual existing IDs. Set `"options": {"mode": "bookmarks"}` and add
`"bookmark_ids": ["EXACT_EXISTING_BOOKMARK_ID"]` to that book entry. No default
or inferred bookmark is selected. The preview includes the full captured
heading/bookmark metadata as well as its derived part ranges.

1. `preview --request request.json --review new-review.json` captures exact
   source text, original metadata, settings and names. It creates a new review
   file outside private library data, never overwrites a file, and starts no
   jobs or speech. Its JSON output includes the review `approval` hash.
2. `prepare --review new-review.json --approval REVIEW_HASH` rechecks sources
   and saves ordinary Studio items. Its returned **prepared** approval hash
   covers the actual destinations and differs from the initial preview.
3. `run BATCH_ID --output PLAN_ID=DIRECTORY --output OTHER_PLAN_ID=DIRECTORY --approval PREPARED_HASH`
   explicitly renders the reviewed selection. Provide every plan/output
   mapping; order comes from the saved batch.

Prefix each operation with, for example,
`python -m carr_library studio book --data-dir C:\Library --workspace LIBRARY_ID`.
Use the values actually returned by the previous step; paths containing
spaces need shell quoting. The review file contains selected source text and
should be handled as private library material.

`list` and `status BATCH_ID` inspect saved references/checkpoints without
opening a voice engine. They do not assert that output files still match
their receipts. `pause BATCH_ID` and `cancel BATCH_ID` withdraw an active
invocation and report that native cleanup is pending. They do not claim
immediate completion, delete accepted output, or start a retry.

After restore, use `fresh-review BATCH_ID --review new-review.json` with each
explicit `--output OLD_PLAN_ID=DIRECTORY`. It preserves the historical batch
and returns a new batch, new plan IDs, exact destinations and an approval
hash. Prepare that new review file and invoke Run using its returned new
plan/output mappings. Old paths and old approval hashes cannot authorize the
restored run.


## Accessibility and evidence

All fields and buttons have native accessible names. Tab/Shift+Tab move through
controls, text previews are read-only multiline controls, and Escape closes the
dialog. Source verification, regex processing, voice discovery, preparation,
rendering and owned voice cleanup use the desktop's existing worker.

Automated source tests exercise real library revisions, EPUB/PDF imports,
isolated regex execution, canonical backup/restore and actual FFmpeg output
with clearly labelled synthetic tone fixtures. Those tones establish pipeline
and integrity behavior, not speech quality. The native Windows smoke uses
purpose-written PCM fixtures through the actual renderer and native wx
controls; the separate CLI tests select an actual installed local voice.
This new composed source has not yet been executed. A source-reviewed test is
not a passing execution result; consult evidence for the exact assembled head.
Manual keyboard, NVDA, language quality and publisher qualification remain
separate acceptance records.
