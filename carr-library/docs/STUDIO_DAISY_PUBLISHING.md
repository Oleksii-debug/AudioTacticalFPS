# DAISY 2.02 talking-book interchange

The existing Studio publication service now exports a DAISY 2.02 file-set ZIP
from an explicitly selected completed production. The same native publication
dialog provides EPUB3 and DAISY choices. This is an implemented, bounded
interchange profile. Independent DAISY-validator, third-party reader and human
Windows/NVDA/listening acceptance remain separate; it is not a Braille export.

## Workflow

Open **Publish synchronized EPUB3 and DAISY** from Studio. Load the completed
jobs, explicitly select a job, choose **DAISY 2.02 file set (.zip)** and provide
the publication language and publisher. Publisher identity is entered by the
user; no owner identity, rights grant or synthetic narrator identity is inferred.
Choose a new ZIP filename in an existing folder, then export. Existing files
are never overwritten. Extract the entire file set together before selecting
`ncc.html` in a compatible external reader.

The equivalent CLI uses the same service and receipts:

```text
python -m carr_library studio --data-dir LIBRARY publish-daisy JOB_ID --output BOOK.zip --language uk --publisher "Chosen publication publisher"
python -m carr_library studio --data-dir LIBRARY publications
python -m carr_library studio --data-dir LIBRARY verify-publication RECEIPT_ID
```

After relocating/restoring the library, verification requires explicit selection
of the existing exported file with `--file BOOK.zip`. The saved receipt binds its
exact bytes. Backup preserves publication state; externally chosen exports are
not silently acquired or executed on restore.

## Implemented file set

| File | Meaning |
| --- | --- |
| `ncc.html` | XHTML navigation, title/chapter audio targets, explicit language/publisher and required DAISY metadata; no invented page numbers |
| `content.html` | Retained spoken lines, role labels and clearly marked intervals without scripted transcription |
| `book.smil` | SMIL 1.0 parallel text/audio intervals with measured beginning/end positions and required IDs |
| `master.mp3` | Exact verified Studio MP3 when available, otherwise MP3 encoded from the verified PCM master through the selected local audio runtime |
| `source.html` | Complete original script, including preserved carriage returns, distinct from the audio/text derivative |
| `provenance.json` | Exact job/project/artifact identities, source hashes, timing, audio identity and explicit qualification limits |
| `styles.css` | Fixed local readable-text styles; no remote resources |

The conservative `audioPartText` profile keeps audio without a scripted line
explicitly untranscribed. Studio speech-line boundaries are measured render
intervals, not acoustic word alignment. Headings, role labels, provenance and
the preserved source script are not narrated. Zero-duration scenes remain in
the text but have no invented audio-navigation target. MP3 codec padding is
separate from the exact measured presentation endpoint.

## Integrity and recovery

DAISY reuses the EPUB publisher's verified-source admission, bounded audio
staging, cancellation, selected directory identity checks, final source recheck,
atomic no-clobber publication and prepared/completed receipt protocol. It does
not first publish a temporary EPUB or create an orphaned EPUB receipt.

The inspector accepts exactly this seven-file profile. It checks chapter and
SMIL targets, required metadata, contiguous measured intervals, text/audio
hashes and the preserved original script. It rejects duplicate or unexpected
archive members, external references, unsupported active content, additional
XML declarations/entities, changed MP3 and oversized input. Fixed XHTML/SMIL
DOCTYPE identifiers are never fetched during inspection. This structural check
is narrower than independent full DAISY conformance and reader interoperability.

The new optional receipt format is `daisy-2.02`. Older EPUB receipts omit the
format and retain their existing interpretation. Verification never accepts an
unknown format or substitutes current export preferences for the saved receipt.

## Evidence and references

`tests/studio/test_daisy_publishing.py` performs actual Studio/FFmpeg package,
MP3 reuse, source mutation, no-clobber, cancellation, prepared receipt recovery,
explicit relocation and corrupted/external graph regressions. Existing EPUB
tests remain required. CLI and native-handler tests exercise the same actual
export path. `scripts/windows_publishing_smoke.py` requires real installed wx
controls, keyboard actions and both publication formats; its current-run result
is recorded separately. Synthetic tone fixtures prove packaging and lifecycle,
not speech quality or actual external-reader behavior.

The profile follows these primary specifications, reviewed on 2026-11-27:

- [DAISY 2.02 format specification](https://daisy.org/activities/standards/daisy/daisy-2/daisy-format-2-02-specification/)
- [DAISY 2.02 technical specification](https://dl.daisy.org/standards/z3986/specifications/daisy_202.html)
- [W3C XHTML 1.0](https://www.w3.org/TR/xhtml1/)
- [W3C SMIL 1.0](https://www.w3.org/TR/REC-smil/)

No DAISY player, model, voice, private worker or copyrighted source publication
is distributed by this change. Reader compatibility and rights for actual audio
and source material require their own acceptance.

### Executed structural acceptance

The current shared publisher and DAISY module passed 71 focused source tests on
27 November 2026, including actual FFmpeg export/reuse, CLI/native handlers,
clock carry and rejection of external XML bases or nested SMIL layout content.
The expanded publication menu retains its earlier command identity, so an
existing publication shortcut remains assigned after the menu label changes.

The separate `scripts/daisy_schema_acceptance.py` accepts explicitly supplied
schemas pinned in `packaging/daisy-validation-schemas.json`. Using lxml 6.1.1 /
libxml2 2.14.6, actual exported NCC, content, original-source and SMIL documents
passed their XHTML 1.0 / SMIL 1.0 DTDs. A deliberately invalid XHTML was rejected.
The run used a separate network namespace, with an allowlisted local resolver
and no network access; no DTD is downloaded by production code. Exact module,
publication and schema hashes are in
[the structural report](evidence/daisy-structural-20261127.json).

The same shared publisher separately passed all three official EPUBCheck 5.3.0
cases with zero warnings/errors and rejected the intentionally invalid EPUB;
see [the exact-byte report](evidence/epubcheck-shared-publisher-20261127.json).
These results establish package structure for synthetic audio fixtures. They do
not establish all DAISY-specific semantic rules, external-reader compatibility,
human NVDA operation or speech quality.
