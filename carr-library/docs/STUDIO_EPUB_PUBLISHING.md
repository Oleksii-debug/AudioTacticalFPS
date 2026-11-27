# EPUB3, DAISY 2.02 and measured audiobook publication

**Audio Studio → Publish synchronized EPUB3 or DAISY** exports a completed Studio
job to a new local EPUB file or DAISY 2.02 file-set ZIP. Both formats use the
existing `PublicationService`, receipt store, source verifier and create-only writer.
The same operations are available in the Studio CLI. Publication uses the
measured Studio timeline, original script, role text and verified audio artifacts;
it does not generate speech, invent a transcript, start a model or open a reader.

## Native workflow

Choose **Load jobs / receipts** and explicitly select a completed job. Choose the
publication format, language, optional creator and a new filename in an existing
directory. DAISY requires an explicitly entered publisher. Its bounded language
contract currently accepts two or three letters, optionally followed by a
two-letter country; EPUB accepts the existing broader language-tag grammar.
Use `und` when language is undetermined.

**Export publication** verifies source bytes, encodes local MP3 when necessary,
packages the chosen format and verifies its final bytes in the shared background
job lifecycle. The selected destination directory is captured before work is
queued; a changed directory requires a new selection. CPU, declared memory and
staging disk allowances are reserved with the existing resource broker before
audio copying/decoding. The current disk, process and time limits remain active.
If native cleanup is unconfirmed, the same production lock, resource reservation
and staging remain owned until the child becomes quiescent.

The default limit is 32 GiB for the complete source artifact graph and for
publication staging; the time budget is 60 minutes. Both are visible controls.
Metadata uses the existing bounded JSON contract. Audio and archive members are
streamed. No selected destination is overwritten.

After the create-only writer publishes, a complete byte and inode check must
identify this attempt's output. Later cancellation, observer failure or cleanup
cannot turn those verified bytes into permission to export again. A receipt
write can remain prepared or unconfirmed; the result reports this separately
from output completion. Choose **Load jobs / receipts**, select the receipt and
use **Verify receipt**. Verification checks actual size, SHA-256, local archive
graph and source identity without encoding again. Saved status alone is not
current byte verification. After relocation, explicitly choose the existing
publication. Verification follows its saved format, not a newly changed format
control.

## CLI

```powershell
python -m carr_library studio --data-dir "C:\ResearchLibrary" jobs
python -m carr_library studio --data-dir "C:\ResearchLibrary" publish-epub JOB_ID --output "D:\Publications\reading.epub" --language uk
python -m carr_library studio --data-dir "C:\ResearchLibrary" publish-daisy JOB_ID --output "D:\Publications\reading.zip" --language uk --publisher "Publisher chosen for this edition"
python -m carr_library studio --data-dir "C:\ResearchLibrary" publications
python -m carr_library studio --data-dir "C:\ResearchLibrary" verify-publication PUBLICATION_ID
```

For a relocated output add `--file "D:\Restored\reading.zip"` to verification.
`--max-bytes` and `--timeout` select explicit publication/verification budgets.
Results are JSON on stdout; progress and separate cleanup notices go to stderr.
Existing imported-project input approval and restored-job destination controls
remain unchanged.

## Measured M4B chapters and LRC

Choose `m4b` in the existing Studio render format control, or use
`studio render --project PROJECT.json --output OUTPUT_DIRECTORY --format m4b`.
The existing local audio runtime embeds scene titles and sample-derived chapter
start/end positions in the actual M4B container. FFprobe then reads that container
and checks title, chapter order, times, audio format and duration. Changed titles
or chapter positions invalidate the encoded cache even when PCM is identical.
Metadata is limited to 10,000 chapters and 2,000,000 bytes. Current Studio disk,
staging, process, time and output limits still apply.

Current productions also write `script.lrc` from measured line starts in
centiseconds, alongside SRT and VTT. Each visible line retains its text. LRC does
not create word timings or acoustically aligned sentence timings. A silent
production can have an empty LRC file.

The existing job schema has an optional, strictly typed `artifact_version`:
absence or `1` means the historical output inventory; `2` additionally requires
LRC. New submissions and explicitly resumed non-complete jobs use `2`. An already
complete historical job is read and verified without adding files or rewriting
its receipt. Unsupported versions and booleans are rejected. Complete replay and
both publication formats share the same required inventory: master, chapters,
project, measured timeline, subtitles, requested codec and narration/music/clip
stems as applicable. An omitted current LRC receipt is rejected even when a file
with that name still exists on disk.

## Source and timing contract

Publication requires a complete production, its matching output owner marker
and the full verified artifact inventory. Published project bytes must match
the job fingerprint; timeline bytes must match the saved result. Every measured
speech line has the same scene, line ID, role, order and exact text as the source
project. Invalid or overlapping line/chapter intervals are rejected.

Audio is copied into private staging with its expected bytes checked. A verified
existing `audiobook.mp3` is reused; otherwise the WAV master is encoded using the
configured local runtime. The actual MP3 codec, sample rate, channel count,
duration and content hash are recorded. Both synchronized publication formats
embed MP3, with media type `audio/mpeg`.

Measured speech intervals refer to exact text spans. Audio between scripted
lines remains in the playback sequence with explicit interval labels. These
labels are not transcripts of music, sound effects or imported recordings.
Serialization uses microsecond clock precision while alignment remains measured
per line. MP3 can contain codec padding. Individual reading-system playback
needs separate testing.

The readable source page preserves the original script separately from rendered
role text. Provenance retains source job/project/artifact hashes, audio identity
and interval coverage. No model executable, model weights, provider profile or
private worker is included. Original inputs and completed Studio state are
unchanged by publication.

## EPUB3 and DAISY boundaries

EPUB contains XHTML content/navigation, a SMIL 3 overlay, OPF metadata, local MP3
and a CSS highlight class. The uncompressed mimetype member is first. Container
metadata retains original script and provenance. The internal checker verifies
ZIP members, local references, XML IDs, source text, interval coverage and audio
hashes. Accessibility metadata describes structure and measured synchronization;
headings, role labels, source pages and provenance are not themselves narrated.
Untranscribed audio can remain. Complete auditory/textual equivalence, WCAG and
EPUB Accessibility conformance are not asserted.

DAISY contains `ncc.html`, `content.html`, `source.html`, `book.smil`, local
styles, provenance and the verified MP3 master. Extract its ZIP into a separate
folder and choose `ncc.html` in a compatible reader. This fixed producer profile
uses XHTML navigation/text and SMIL 1 clipping. The internal checker rejects
undeclared members, external resources, unexpected XML/entities, mismatched local
fragments, changed text/audio hashes and invalid interval coverage. The runtime
does not download or resolve a DTD.

The optional `scripts/daisy_schema_acceptance.py` tool takes an explicitly selected
publication and schema directory. `packaging/daisy-validation-schemas.json` pins
each DTD/entity file. The tool uses lxml with network access disabled and an
explicit local resolver, and requires a deliberately invalid document to fail.
DTD validation is narrower than full DAISY rules or real-reader interoperability.

The separate `scripts/epubcheck_acceptance.py` takes an explicitly supplied
EPUBCheck archive and Java executable. `packaging/epubcheck.json` pins EPUBCheck
5.3.0 to the official release archive and source identity. This checker and Java
are acceptance tools; the application does not download or run them on export.
Historical donor reports do not establish a result for this composition.

## Backup and recovery

The receipt grammar is shared with typed backup validation.
`validate_publication_receipt` validates declarations without opening external
paths. The [current typed backup and historical media formats](BACKUP_FORMAT_V6.md)
retain receipts while EPUB/DAISY archives and generated audio remain declared
external outputs. A receipt's optional format is `epub` or `daisy-2.02`;
historical absence means EPUB. Invalid/mismatched formats are rejected.

Bounded Studio warnings and the artifact-version marker are preserved as passive
recovery metadata. Identical warnings are deduplicated. Once the 1000-entry limit
is reached, the first 999 remain and the last entry visibly reports omitted
additional history plus the latest message, still within 1800 characters. Each
current callback is also reported through the existing progress channel. A restored production still needs explicit destination/input
review. Restored publication verification requires selecting the existing exact
archive; it never re-renders. Publication and verification share the receipt
lock, and verification reads its current snapshot only after acquiring that lock.

## Qualification

Source tests use synthetic PCM through actual FFmpeg, real CLI subprocesses,
owned-child termination, byte-preserving receipt recovery and actual typed backup
and restore. M4B also has an actual local SAPI/Flite test; it is not a language
quality claim. The strict installed media gate requires pinned FFmpeg/FFprobe
and refuses skipped tests. The native publishing helper checks actual format
controls, explicit DAISY publisher, receipt verification, measured M4B/LRC and
owner destruction with synthetic audio.

Exact Windows execution, independent format validation, actual reading systems,
Braille output, acoustic sentence alignment and manual NVDA are separate gates.
A final source reconstruction after executor loss is not qualified by an earlier
local run until the exact bytes are executed again. Report actual results with
their immutable source identity.

## Primary references

- [DAISY Format 2.02 Specification](https://daisy.org/activities/standards/daisy/daisy-2/daisy-format-2-02-specification/).
- [DAISY 2 specification history and errata](https://daisy.org/activities/standards/daisy/daisy-2/).
- [FFmpeg metadata format](https://ffmpeg.org/ffmpeg-formats.html#Metadata).
- [W3C EPUB 3.3](https://www.w3.org/TR/2026/REC-epub-33-20260113/).
- [EPUB media overlays](https://www.w3.org/TR/2026/REC-epub-33-20260113/#sec-media-overlays).
- [EPUB Accessibility 1.1](https://www.w3.org/TR/epub-a11y-11/).
- [EPUBCheck 5.3.0 release](https://github.com/w3c/epubcheck/releases/tag/v5.3.0)
  and [license](https://github.com/w3c/epubcheck/blob/029831b8f477e4519e9734c984ee24357547a698/LICENSE.md).
- [EPUBCheck CLI](https://www.w3.org/publishing/epubcheck/docs/cli/).
