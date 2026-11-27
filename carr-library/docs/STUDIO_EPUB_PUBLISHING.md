# Synchronized EPUB3 publication from Audio Studio

**Audio Studio → Synchronized EPUB3 publication** exports an explicitly selected
completed production to a new local `.epub` file. The dialog lists both Audio
Studio productions and saved Scene Studio jobs with their distinct original
identities. Publication uses existing measured audio; it does not synthesize
another voice or download a model.

## Native workflow

Choose **Load jobs / receipts**, select a completed job, enter a language such
as `uk`, `en` or `und`, an optional creator, and a new EPUB filename in an
existing local directory. **Export EPUB3** verifies every published source
artifact, prepares local MP3 audio with the configured audio runtime, and saves
the EPUB and its durable receipt. The shared background operation provides
progress and cancellation. The keyboard and screen-reader status field remains
available while the operation runs.

The destination is never overwritten. A failure after atomic publication can
leave a completed EPUB with a prepared receipt; choose that receipt and
**Verify receipt** before retrying. Verification rehashes the file and checks
its text/audio graph. After backup restoration, explicitly select the existing
EPUB before verification, even when the old directory name is unchanged.

The default resource allowances are 32 GiB for the complete source artifact
graph and staging, and 60 minutes. Both can be changed explicitly. Metadata
uses the existing bounded JSON allowance; at most 100,000 measured utterances
are accepted. Audio is streamed rather than assembled in Python memory.

## Two original production contracts, one publisher

Scene Studio requires its completed `ProductionService` job, matching owner
marker, the exact exported `project.json`, `timeline.json`, and all artifact
receipts. Timeline order, roles, exact text, measured line intervals and chapter
coverage must agree with the original project.

Audio Studio uses its original `ProductionDirector` job and project fingerprint,
matching owner marker, verified `production.json`, chapter WAVs, master and
other outputs. It retains original cue IDs, global source offsets, accepted
pronunciation replacements, reading-copy hashes and derived-to-original
mapping. The original project snapshot and source reference are embedded in
provenance; the original job and accepted-cache journal hashes are retained.
The adapter supplies a publication layout without changing the identity or
schema of that original project.

Each source is held under its existing renderer lock. All source bytes and the
completed job checkpoint are checked again before final publication. Restored
render jobs require a newly approved output; publication cannot silently
reopen a former machine's external render directory.

Verified existing MP3 output is reused byte for byte. Otherwise the verified
WAV master is converted through the same configured local FFmpeg runtime.
The packaged media type is `audio/mpeg`, and codec, measured duration, sample
rate, channels and hashes are recorded.

## Text, timing and accessibility boundaries

Every synchronized speech interval points to its exact XHTML span. Audio
between scripted lines receives an explicit untranscribed-interval label;
music and sound effects are never given an invented transcript. These are
measured utterance boundaries, not word or sentence alignment. Microsecond
serialization does not increase the underlying alignment accuracy. MP3 can
include codec padding.

The source page preserves the original script independently of the reading
copy. `META-INF/metadata.xml` contains source and audio provenance.
No model weights, executables, API credentials or private worker source is
included. Original source material and production journals are not edited.

The package contains XHTML, navigation, SMIL3 media overlays, OPF metadata,
MP3 and a CSS highlight class. The internal verifier checks the exact package
graph, local references, entity-free XML, original text, timing coverage and
audio hashes. It uses defusedxml for XML parsing, including UTF-16 input.

Discovery metadata describes contents, structural navigation and measured
synchronized text/audio. Headings, role labels and provenance pages are not
narrated. Some audio has no transcript, and hazards are marked unknown.
The exporter does not assert full auditory/textual equivalence, WCAG or EPUB
Accessibility conformance.

## Read a verified synchronized publication

**Audio Studio → Read synchronized EPUB** opens this exact publication subset.
Select the EPUB and choose **Load verified publication**. The reader captures
the selected bytes, checks the source/timing/audio graph, and decodes the
verified MP3 with the existing audio runtime into private PCM. The displayed
publication hash identifies that captured version even if the selected file
later changes.

Use the native chapter and utterance choices, **Previous interval**, **Next
interval**, or a measured seek position. Navigation stops playback.
**Play** is always explicit; **Pause** and **Stop** retain the current position.
Playback reuses the existing WavePlayer and the application's progress-audio
quiet contract. The current reading copy is displayed with the original cue
and accepted source mapping where the primary Studio supplied them. POSIX
ffplay position is approximate; Windows MCI reports device position. Neither
backend creates word-level alignment.

The existing CPython3.11 player supports ordinary PCM RIFF WAV, not RF64.
Before decoding, the reader rejects PCM that would exceed the RIFF32 boundary
of 4 GiB, reserving 64 KiB for headers. This is a byte-format limit that varies
with sample rate and channel count; the 32 GiB staging allowance does not
promise playback of a 32 GiB WAV. Longer work requires separate chapter
recordings. No multi-hour device-playback qualification is inferred from this
preflight test.

Preparing audio and rechecking its bytes run through the shared background
job. Cancellation stops preparation. Close waits for confirmed device stop
before deleting private playback files; uncertain device handles remain
retained for safe cleanup.

This reader accepts the verified Resonance package graph, not arbitrary EPUB
media-overlay layouts. Third-party EPUB reading compatibility, DAISY2.02/3
authoring and reading, Braille output and manual NVDA review remain distinct
acceptance requirements. The official DAISY Pipeline provides EPUB3-to-DAISY
conversion, but no reviewed Pipeline installation or DAISY validator/player
qualification is included here. ZIP/XML or EPUBCheck success does not qualify
those behaviors.

## Official external conformance check

The application does not download or run Java or EPUBCheck automatically.
The separate acceptance command takes an explicitly supplied archive and Java:

```powershell
python -B scripts/epubcheck_acceptance.py --archive C:\\Tools\\epubcheck-5.3.0.zip --java C:\\Java\\bin\\java.exe --lock packaging/epubcheck.json --report epubcheck-report.json --require-installed
```

For source tests, omit `--require-installed`. The installed native UI probe is:

```powershell
python -B scripts/windows_publishing_smoke.py --require-installed
```

`packaging/epubcheck.json` pins official EPUBCheck5.3.0 source commit
`029831b8f477e4519e9734c984ee24357547a698` and the release ZIP
SHA-256 `6c07e68584b2e2ce2f89fe06e1246dfead3eb36b46b340e7d93524f29dcff6c5`.
The official release API confirms its 33,071,108-byte size and digest.
That exact source uses BSD-3-Clause; its archive license and third-party
notices must be retained for checker use. Java and the checker are CI tools,
not application payloads.

The acceptance script checks actual generated files, requires zero EPUBCheck
errors and warnings, verifies that validation did not change their bytes, and
requires rejection of a deliberately invalid package. Its fixture audio is
explicitly synthetic PCM through real FFmpeg, not evidence of speech quality.

## Source provenance and qualification

The initial exporter, native dialog, fixtures and checker script were reused
from reviewed peer commit `20e275e5112566cd07c3aa56459bd8634240bc3e`, with
integration changes for both job families, strict restore receipts,
defusedxml, the existing menu/progress lifecycle and both supported installed
audio-runtime layouts.

This integrated candidate requires its own source, native and official
EPUBCheck results. A previous peer's passing report is not qualification of
these changed bytes. When no matching report exists, external validation and
native accessibility status are **NOT_RUN**.

## Primary references

- [EPUB3.3 Recommendation, 13 January2026](https://www.w3.org/TR/2026/REC-epub-33-20260113/)
- [EPUB3.3 media overlays](https://www.w3.org/TR/2026/REC-epub-33-20260113/#sec-media-overlays)
- [EPUB Accessibility1.1](https://www.w3.org/TR/epub-a11y-11/)
- [EPUBCheck5.3.0 release](https://github.com/w3c/epubcheck/releases/tag/v5.3.0)
- [Exact checker license](https://github.com/w3c/epubcheck/blob/029831b8f477e4519e9734c984ee24357547a698/LICENSE.md)
