# DAISY 2.02 publication from Studio

Open **Audio Studio → Publish DAISY 2.02**. The existing publication dialog
selects DAISY; its format control can also select EPUB3. Load saved jobs, choose
a completed production explicitly, supply its language and publisher, and
choose a new ZIP filename. **Export publication** uses the same verified job,
background operation, cancellation and receipt lifecycle as EPUB.

Publisher metadata is supplied by the user; the application does not infer a
rights holder or organization. The language is a two/three-letter code such as
`uk`, `en` or `und`, optionally followed by a two-letter country code.
Existing files are never overwritten. Private-library and completed-source
production directories are refused as export destinations.

## CLI and recovery

~~~powershell
python -m carr_library studio --data-dir "C:\ResearchLibrary" publish-daisy JOB_ID --publisher "Your publisher metadata" --language uk --output "D:\Publications\reading.zip"
python -m carr_library studio --data-dir "C:\ResearchLibrary" publications
python -m carr_library studio --data-dir "C:\ResearchLibrary" verify-publication PUBLICATION_ID
~~~

For a moved file or restored receipt, explicitly add
`--file "D:\Restored\reading.zip"`. Verification checks saved ZIP bytes, internal
graph and source identity without rerendering or opening a reader.

The chooser or explicit Export click captures destination identity before
queued work. Later files or replaced parent directories cannot acquire implicit
overwrite permission. Publication uses the existing create-only atomic writer.
A cancellation after publication retains the complete result. If final receipt
storage fails, the saved file and prepared receipt remain explicitly verifiable.

## Package, audio and source contract

The ZIP contains exactly `ncc.html`, `content.html`, `source.html`, `book.smil`,
`styles.css`, `provenance.json` and `master.mp3`. Extract the complete file set
for a compatible DAISY 2.02 reader. This is not DAISY 3/DTBook or BRF.

The matching Studio owner marker, complete artifact sizes/hashes, project
fingerprint, original script hash and measured timeline must agree. A verified
job MP3 is reused byte for byte; otherwise the existing local runtime encodes
the verified WAV master. Accepted mono/stereo sample rates produce supported
MPEG-1/2 Layer III. Unsupported MPEG-2.5 is refused. Provenance records actual
codec, duration, rate, channels, source artifacts and exact audio hashes.

Each spoken interval points to its exact original line. The original measured
start/end values are retained separately from clocks rounded to six decimal
places. This is line synchronization, not sample-exact acoustic or word
alignment. Unscripted pauses, music and recordings remain explicitly
untranscribed. Unicode, line endings and script markup remain intact.

DAISY requires the first SMIL text reference to target a heading. An explicit
zero-duration, text-only book-heading reference precedes measured audio.
It does not synthesize speech or assign another line's sound to a heading.
Chapter navigation targets each chapter's actual first measured interval.
Role labels and the source/provenance page remain non-narrated supplementary text.

## Verification and evidence

Only the fixed local file graph, exact stylesheet and bounded XML are accepted.
The checker rejects active markup, external resources, duplicated members,
invalid timing, changed audio and a source script that no longer matches its
recorded hash. Literal standard DOCTYPE declarations are removed before the
hardened parser runs; production verification never loads DTDs. Metadata uses
cancellable chunk reads with member and total byte limits. The existing explicit
byte and time budgets remain. Hash checks establish corruption/identity
consistency, not authenticity against someone rewriting all local records.

The composed implementation passed 101 tests with no failures or skips on
27 November 2026: DAISY, 15 additional boundary cases, and all existing
EPUB/CLI/handler suites. Two independent probes failed against the unchanged
prepared DAISY source and passed after correction: the first SMIL target was
a span instead of a heading, and jointly changed readable/JSON script text
bypassed the unchanged recorded source hash.

Three real canonical productions passed pinned official XHTML/SMIL DTD checks:
WAV-derived narration, exact job-MP3 reuse, and a retained clip after deletion
of its selected original. Invalid XHTML was rejected in every case.
`docs/evidence/studio-daisy-canonical-20261127.json` records package/module/tool
hashes and lxml 6.1.1/libxml2 2.14.6. The same composition passed EPUBCheck 5.3.0
for three EPUB cases without errors or warnings; invalid MIME was rejected.
That separate report is `studio-epubcheck-canonical-20261127.json`.

## Independent structural acceptance

Supply the five explicit schema inputs pinned by
`packaging/daisy-validation-schemas.json` and the separate lxml validation tool.
The script downloads and installs nothing. lxml is a CI tool, not a new
production dependency; its installed BSD-3-Clause license and bundled notices
were inspected. The schema files are external test inputs, not application
payloads.

~~~powershell
python scripts/daisy_schema_acceptance.py --schemas "C:\Validation\daisy-dtds" --report "C:\Validation\daisy-result.json"
~~~

Without `--publication`, this renders the three synthetic fixtures through
actual Studio and FFmpeg. Add `--require-installed` for installed module origins
and sealed bundled FFmpeg/ffprobe checks. It imports no pytest fixture or private
voice/model. Use `--publication "D:\Publications\reading.zip"` to validate an
existing archive separately. The allowed local DTD resolver disables network
access and checks all schema hashes again before success.

`scripts/windows_publishing_smoke.py` exercises both formats in the same native
dialog. Structural checks do not certify every DAISY rule, external readers,
listening quality, manual NVDA or BRF/Liblouis output. Those remain separately
recorded acceptance dependencies.

## Source and reuse

- [DAISY 2.02 specification](https://daisy.org/activities/standards/daisy/daisy-2/daisy-format-2-02-specification/).
- [W3C SMIL 1.0](https://www.w3.org/TR/1998/REC-smil-19980615/).
- [W3C XHTML Transitional DTD](https://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd).
- [Pinned DAISY-maintained SMIL DTD](https://github.com/daisy/pipeline-modules/blob/88d39392e9a3e10c1f55f1a82e5e7ea12b0b28b6/smil-utils/src/main/resources/xml/dtd/SMIL10.dtd).

This reuses the prepared canonical snapshot recorded as
`646a15e38bf7b56349632011e28e7e1b65e9bd3e`, whose original DAISY module blob was
`6a0762d0a43040847c84e6d8b88ee5e3e1261edf`. The earlier prepared `544be9c`
exporter informed the standards review; its alternate renderer/schema/UI were
not adopted. The existing EPUB menu identity stays unchanged. The new DAISY
entry uses the same current menu registration and publication dialog.
