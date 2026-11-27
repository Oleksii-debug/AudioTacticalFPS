# Book audio: review a split and resume its Studio items

Open a library or topic and select an existing book in **Books and Collections**.
Choose **Audio Studio → Book audio: split and render**. The command is also
available through the existing command palette and keyboard settings.

## Review before rendering

1. Choose **Load selected book**. The inventory comes from the existing Reading
   Desk and stays attached to that edition and revision.
2. Choose **Discover local voices**, then select an installed voice. Discovery
   starts no speech. This workflow has no provider account or cloud transfer.
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

Whitespace-only fragments join a neighboring item. Concatenating every item's
original text reproduces the exact captured reading text. The existing reader
may use two newline characters between separate source positions; the original
retained document bytes are never changed.

Limits are **2,000,000 selected source characters and 256 parts**. Choose a
smaller source range or larger parts when the plan exceeds a limit. Duration
estimates and automatic duration-based splitting are unavailable. Actual line
and chapter timing comes from the existing renderer after synthesis.

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

The canonical complete library backup includes the immutable book plan and
typed Studio Projects/jobs. It retains the original source through the normal
library asset backup. No alternate archive or source-path rewriting is added.

After restore, select the saved plan, choose a **new output parent**, and
Prepare it. The existing Studio restore transaction assigns fresh destination
names. Read those names and confirm Run separately. The old output folder and
its contents remain unchanged. Rebuildable audio cache is optional in canonical
backups; missing cache is regenerated only after the new explicit Run.

## Accessibility and evidence

All fields and buttons have native accessible names. Tab/Shift+Tab move through
controls, text previews are read-only multiline controls, and Escape closes the
dialog. Source verification, regex processing, voice discovery, preparation,
rendering and owned voice cleanup use the desktop's existing worker.

Automated source tests exercise real library revisions, EPUB/PDF imports,
isolated regex execution, canonical backup/restore and actual FFmpeg output
with clearly labelled synthetic tone fixtures. Those tones establish pipeline
and integrity behavior, not speech quality. The native Windows smoke also uses
the actual installed SAPI voice and real wx controls. A source-reviewed test is
not a passing execution result; consult evidence for the exact assembled head.
Manual keyboard, NVDA, language quality and publisher qualification remain
separate acceptance records.
