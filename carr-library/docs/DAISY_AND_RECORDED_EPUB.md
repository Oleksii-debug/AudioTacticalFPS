# DAISY 2.02 and recorded EPUB audio

## Publish a completed production

Open **Audio Studio → Synchronized EPUB3 publication…**. Load saved jobs and choose one completed Scene Studio or primary Audio Studio production. Select a new output file outside the library. Existing files are never replaced.

For EPUB3, choose a `.epub` filename and **Export EPUB3**. For DAISY 2.02, choose a `.zip` filename, enter the publication language and an explicit publisher, then choose **Export DAISY 2.02**. Creator metadata is optional. A completed job's verified MP3 can be reused; otherwise the existing configured local FFmpeg produces MP3 from its verified master. Neither export reruns narration.

The DAISY ZIP contains `ncc.html`, `content.html`, `source.html`, `book.smil`, `styles.css`, `provenance.json`, and `master.mp3`. Extract the complete set for a compatible DAISY 2.02 reader. Chapter links and speech-line intervals use the measured production timeline. The first heading is explicitly not narrated. Audio between spoken lines stays untranscribed; no word timing or spoken chapter title is invented. Primary Audio Studio reading-copy/source mappings and Book source receipts remain associated with their originals.

Both formats keep a saved publication receipt. **Verify receipt** checks the exact current archive bytes and its internal graph. If an interruption occurred after publication, verify that existing output before attempting another export. A restored receipt requires an explicit existing output selection; backup never restores authority to reopen an old external path automatically.

Equivalent local commands are available through `python -m carr_library studio`: `publications`, `publish-epub JOB --output FILE`, `publish-daisy JOB --output FILE --publisher NAME`, and `verify-publication RECEIPT --file FILE`. A Scene job may use its ID or `scene:ID`; a primary job uses `director:PROJECT:JOB`. Use `--help` for language and operation budgets.

## Read authored recorded audio from an imported EPUB

Import an EPUB into the library and open its full source Reading Desk. Choose **EPUB synchronized recorded audio…** for the current source section. Opening the window or selecting a segment does not start sound. Use the named native **Play**, **Pause**, **Resume**, **Stop**, **Previous recorded segment**, **Next recorded segment**, and **Where am I** controls. After a segment finishes naturally, playback can continue through the authored segments in that section. Return to the Reading Desk to choose another section.

This reader uses the book's local EPUB3 SMIL `seq/par` media overlays. It does not replace missing audio with TTS, fetch remote resources, decrypt protected books, or infer word boundaries. A text fragment may represent a word, a sentence, or a larger passage according to the publication. The source text, original archive and quotation approval remain unchanged.

Parsing runs in the existing isolated child boundary; decoding uses the configured local audio runtime and existing WavePlayer. A broken or restored runtime selection fails visibly. Preparation, Stop, changing the selected source, and closing either reader invalidate pending playback. Current limits are 2,048 segments per section, one hour per prepared segment, 32 MiB per recorded member, and PCM16 stereo at 24 kHz for the temporary playable segment. Shared resource reservations and private staging remain held until a pending decoder actually exits.

## Qualification and scope

The offline structural tool `scripts/daisy_schema_acceptance.py` consumes only the five exact official DTD/entity files pinned in `packaging/daisy-validation-schemas.json`. It uses lxml 6.1.3, already pinned in both Windows application dependency locks for the existing document workflows. DAISY production code itself adds no lxml dependency and never downloads schemas during application use. The validator rejects unapproved XML resources and includes deliberately invalid XHTML as a negative control. The DTD/entity files are validation inputs, not new application runtime assets.

CI can explicitly prepare the five pinned files with `python -B scripts/prepare_daisy_schemas.py --destination SCHEMA_DIRECTORY --report RECEIPT.json`, then pass that directory to the offline validator. Preparation accepts only the fixed official HTTPS URLs, rejects redirects, verifies each exact size and SHA256 before creating the new destination, and never overwrites an existing directory. It is separate from application startup and publication.

The standalone native diagnostic is `scripts/windows_ebook_daisy_smoke.py`, run with Windows Python `-I -B`. It exercises real authoring controls, retained source import, named recorded-audio controls and the existing MCI endpoint using purpose-written PCM. Source and installed modes verify exact imported module bytes. These new gates are **NOT_RUN** until qualified at the composed candidate SHA.

A structurally valid DAISY file set does not establish complete DAISY rule conformance, third-party or hardware-reader interoperability, manual NVDA acceptance, speech quality, or tactile/Braille output. Those claims require their separate actual checks.
