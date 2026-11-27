# Synchronized EPUB3 publication from Audio Studio

**Audio Studio → Publish synchronized EPUB3** exports a completed Studio job to
a new local `.epub` file. The same operation is available in the Studio CLI.
It uses the job's actual measured line timeline, original script, role text and
verified audio artifacts. It does not generate speech, invent a transcript,
start a model or open a reading application.

## Native workflow

Open the publication dialog and choose **Load jobs / receipts**. Select a
completed job explicitly. Enter a publication language such as `uk`, `en` or
`und` for undetermined, an optional creator, and a new EPUB filename in an
existing directory. With EPUB3 selected, **Export publication** performs source verification, local MP3
encoding if necessary, packaging and final byte verification in the shared
background job lifecycle. The cancel control remains available; source and
destination permissions are checked again before publication.

The default byte limit is 32 GiB for the complete source artifact graph and
for publication staging. The default operation time budget is 60 minutes.
Both are explicit controls. The exporter also bounds metadata with the existing
private-state JSON limit and accepts at most 100,000 measured speech lines in
one publication. It streams audio and archive members rather than loading a
whole recording into memory.

The selected destination must be outside the entire private library and the
completed production folder. Its identity is captured before queued work and
checked again before publication. The selected destination is never overwritten. If cancellation or failure
occurs after the completed EPUB was atomically published, the file remains and
its prepared receipt can be explicitly verified. Choose **Load jobs / receipts**,
select that receipt, and choose **Verify receipt**. Saved status alone is not a
fresh integrity check. After relocating a restored receipt, explicitly select
the existing EPUB file; its exact size, SHA-256 and internal graph must match.

## CLI

```powershell
python -m carr_library studio --data-dir "C:\ResearchLibrary" jobs
python -m carr_library studio --data-dir "C:\ResearchLibrary" publish-epub JOB_ID --output "D:\Publications\reading.epub" --language uk
python -m carr_library studio --data-dir "C:\ResearchLibrary" publications
python -m carr_library studio --data-dir "C:\ResearchLibrary" verify-publication PUBLICATION_ID
```

For a relocated file, add `--file "D:\Restored\reading.epub"` to verification.
`--max-bytes` and `--timeout` accept explicit publication and verification
budgets. Results are JSON on stdout; progress and errors go to stderr.

## Source and timing contract

The exporter requires a completed `ProductionService` job, its matching output
owner marker, and the entire hash-checked artifact manifest. It uses
`Project.from_dict`, retaining the application's accepted project schemas.
The published `project.json` must match the job fingerprint, and published
`timeline.json` must match the completed checkpoint. Every measured speech line
must have the same scene, line identity, role, order and exact text as the source
project. Overlapping or invalid line/chapter intervals are rejected.

Audio is copied into private staging while its expected bytes are checked.
An existing verified `audiobook.mp3` is reused without encoding. Otherwise the
verified WAV master is encoded to MP3 by the existing configured local audio
runtime. The actual codec, sample rate, channels, duration and content hash are
recorded. The exporter embeds **MP3 only**, with media type `audio/mpeg`; WAV is
the source master, not the declared EPUB overlay codec.

Each measured speech interval points to its exact XHTML text span. Audio
between scripted lines has its own interval label and remains in the playback
sequence. These labels are not transcripts of music, sound effects or imported
recordings. Serialization rounds measured boundaries to six decimal places and retains the
unrounded line values in provenance. Overlaps, out-of-bounds intervals and lines
that would disappear at that precision are rejected. Only generated non-source
gaps fill the remaining master audio; source speech intervals are never clamped.
The decimal precision does not establish sample, word or acoustic alignment. MP3 may contain
codec padding, and individual reader playback behavior requires testing.

The readable source page preserves the original script separately from the
rendered role text. Container-level `META-INF/metadata.xml` retains exact script
content, source job/project/artifact hashes, audio identity and interval
provenance. No model executable, model weights, API profile or private worker
is included. Original source files and the completed Studio job are unchanged.

## Format and verification boundaries

The package contains XHTML content, navigation, a SMIL 3 overlay, OPF metadata,
local MP3 audio and a CSS highlight class. The uncompressed `mimetype` entry is
first. OPF connects content to its overlay and records total and overlay
durations. The internal checker verifies ZIP members, local references, XML
identities, source text, interval coverage and packaged audio hashes.

Accessibility metadata describes the table of contents, structure and measured
text/audio synchronization, with an explicit accessibility summary and unknown
hazard status. Headings, role labels and source/provenance pages are not
narrated. Untranscribed audio can remain. The exporter therefore does not assert
complete auditory or textual equivalence, WCAG or EPUB Accessibility conformance.

The application does not download or run EPUBCheck automatically. The separate
acceptance script, `scripts/epubcheck_acceptance.py`, requires an explicitly supplied
archive and Java executable. `packaging/epubcheck.json` pins EPUBCheck 5.3.0 to the
official release archive hash and exact source commit. This version checks the
implemented EPUB 3.3 standard. Its BSD-3-Clause license and bundled third-party
notices were inspected; the checker and Java are CI tools and are not distributed
in the application.

Upstream commit `20e275e5112566cd07c3aa56459bd8634240bc3e` records that, on
27 November 2026, actual local Studio production and the pinned checker passed
three cases with zero errors or warnings: multiple narrated scenes with retained
Unicode source and pauses, byte-for-byte reuse of a job's existing MP3, and a
schema-2 retained audio clip without an invented transcript. A deliberately wrong
EPUB MIME type was rejected. Reports bind the checked EPUB bytes by SHA-256;
the archive is not modified after validation. The Windows workflow repeats these
cases using the exact installed package and bundled FFmpeg, with Java and the
application runtime covered by outbound firewall rules. Windows evidence is
recorded per commit, separately from this local result.

Conformance checks do not qualify a real reading system, DAISY interchange,
Braille output, acoustic quality, WCAG conformance or manual NVDA behavior.

## Normative references inspected for this implementation

- [W3C EPUB 3.3, Recommendation 13 January 2026](https://www.w3.org/TR/2026/REC-epub-33-20260113/): core audio media types; OCF ZIP and container metadata; package, navigation and media overlay requirements.
- [EPUB 3.3 media overlays](https://www.w3.org/TR/2026/REC-epub-33-20260113/#sec-media-overlays): SMIL sequences, text/audio references, clipping and duration metadata.
- [EPUB Accessibility 1.1](https://www.w3.org/TR/epub-a11y-11/): discovery metadata and separate accessibility conformance requirements.
- [EPUBCheck 5.3.0 release](https://github.com/w3c/epubcheck/releases/tag/v5.3.0) and [exact license](https://github.com/w3c/epubcheck/blob/029831b8f477e4519e9734c984ee24357547a698/LICENSE.md).
- [EPUBCheck command line](https://www.w3.org/publishing/epubcheck/docs/cli/): actual JSON reports and failure on warnings.

Source acceptance uses purpose-written synthetic PCM tones through real Studio
and FFmpeg execution, real CLI subprocesses, and ZIP/XML/audio inspection.
`scripts/windows_publishing_smoke.py` adds installed-package native wx acceptance.
Its output explicitly separates these checks from actual speech quality,
EPUBCheck, external readers and manual assistive-technology qualification.

The same dialog also supports DAISY 2.02 with explicit publisher metadata.
See [STUDIO_DAISY_PUBLISHING.md](STUDIO_DAISY_PUBLISHING.md) for its file set,
measured heading/line contract, structural validation and separate reader limits.

## Canonical integration qualification

This adapter preserves the canonical approved-input and backup boundaries. Its
XML parser uses the already-pinned defusedxml dependency, with 64 MiB per XML
member and 128 MiB total metadata limits. Local receipt hashes detect corruption
and identity mismatches; they are not signatures against a person who rewrites
all local source and receipt files. A completed atomic publication is not relabeled
cancelled by a later stop request; bounded receipt finalization may instead require
explicit verification of the retained output. Fresh acceptance for this composed
tree is recorded separately from the upstream result above.

The composed canonical publisher was independently run against the pinned
EPUBCheck 5.3.0 archive. All three actual Studio output cases completed with
zero fatal errors, errors or warnings; the deliberately invalid MIME case was
rejected. The exact source/audio hashes and checker results are retained in
[the canonical report](evidence/studio-epubcheck-canonical-20261127.json).
Four strict isolated publication, CLI and handler suites also passed all 54
cases. Ten new boundary cases failed against the unchanged upstream publisher
and passed after the scoped corrections. This evidence does not qualify
physical microphone capture, model speech quality, external readers or NVDA.
