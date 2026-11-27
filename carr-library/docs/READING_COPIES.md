# Reading copies and narration preparation

The Reading menu opens an editable reading copy, imports a UTF-8 text file, or reopens a saved project. The source reader can create a copy of its current page or logical section. The editor prepares text and can explicitly hand it to Audio Studio. Opening a copy does not start speech, infer accents or certify quotation text.

## Source and reading text

The original text remains in a separate read-only field. Its SHA256 and, when available, exact library, edition, revision, page or segment remain attached to the project. Edited text stays in the reading copy; saving never alters the original file, extracted source, research evidence or quotation approval. An independent authored/imported text is explicitly marked as having no authenticated library source.

Use **Compare original** for a text description of changes. Offsets count Unicode characters from zero. Unchanged leading and trailing text receive exact source ranges. The middle changed range is marked as derived and makes no claim of precise word alignment. This conservative linear comparison remains bounded for long sections.

## Keyboard editing

- Tab and Shift+Tab move between original text, editable text, status and buttons.
- Ctrl+S saves the current reading project.
- Ctrl+Shift+S saves a new project identity and preserves the earlier project.
- Ctrl+Z and Ctrl+Y navigate project revisions. Pending typing forms one revision when saved or transformed, rather than a revision for every keystroke.
- Ctrl+H opens literal find and replace. Replacements are case-sensitive and can be previewed before applying.
- **Normalize spacing** previews normalized line endings, repeated horizontal whitespace and excessive blank lines. It does not guess punctuation, stress or missing words.
- **Add pronunciation** stores an exact written phrase, its spoken replacement and an optional descriptive language label. **Preview pronunciation** applies the dictionary only after an explicit choice. Longest phrases win; replacements never cascade into other replacements. No input is executed as a regular expression, command or expression.
- **Pronunciation dictionary** opens editable JSON interchange for the project's rules; entries have `written`, `spoken` and optional `language` fields. An empty replacement explicitly removes a phrase from the reading copy.
- **Export UTF-8 text** writes the reading text to the chosen external destination. Existing files require the file dialog's explicit replacement confirmation. A library's internal files cannot be export destinations.

Every actionable status is available in a native read-only multiline text field. Explicit operations focus status for screen-reader reading. Autosave updates status without moving keyboard focus. The original and reading text remain available as ordinary multiline controls; no visual comparison or waveform is required.

## Open the prepared copy in Audio Studio

Choose **Save and open Audio Studio** to discover installed local voices in a
background worker. **Cancel studio setup** stops discovery; closing the reading
editor waits for cancellation before handling unsaved changes. No voice is
downloaded or silently substituted. Choose one of the discovered voices explicitly.
If no supported voice is available, the editor retains the current text and shows
the failure without creating an audio project.

The handoff saves the current reading revision and opens a separate Audio Studio
project in versioned JSON mode. The exact text, including Unicode, line endings
and whitespace, is retained in its `authored_script`. The project records the
reading-copy identity and saved digest, exact source location and hashes when
present, and an explicit statement that the derivative grants no quotation
approval. An independent copy receives no fabricated library source reference.
An archived or changed source is rejected before the audio project is created;
a valid historical revision keeps its original identity after catalog metadata
changes. Source files and research evidence are not modified.

Pronunciation rules are not applied by the handoff. Apply and review their text
preview in the reading editor first if you want those replacements spoken. Audio
Studio opens without synthesizing or playing anything; choose **Render or resume**
there to produce audio. Closing Studio returns to the same reading copy. The audio
project has its own saved revisions and backup workflow described in
[AUDIO_STUDIO.md](AUDIO_STUDIO.md).

## Saving and recovery

Projects are JSON files inside the chosen library's `narration-projects` directory. Each has a stable random identity, immutable original, revision history and pronunciation dictionary. Writes use the existing library writer lock, temporary files, file flush and atomic replacement. The save requires the digest of the version originally opened, so two editors cannot silently overwrite one another.

After an editing pause, a separate recovery draft is saved. A recovery message is distinct from a successful project-save message. Reopening explicitly offers the draft or the last saved project. Closing with edits offers **Save project and close**, **Keep recovery draft and close**, or **Keep editing**. If saving fails, the editor remains open and reports the error. Corrupt or unknown project formats are rejected, never silently reset. Save As preserves prior projects and their drafts.

The complete-library backup integrates strictly validated project and recovery files with checksums. The snapshot/restore interface is `snapshot_narration_files(root)` and `validate_narration_payload(filename, raw)`; backup callers use the existing writer lock and retain original source references. Historical references are never silently retargeted.

## Explicit current limits

This increment implements manual reading preparation, literal pronunciation rules, versioned editing, recovery and a local-voice handoff to the implemented Audio Studio. Automatic punctuation/stress models, phoneme editors, word-synchronized audio, multi-tab documents and full Balabolka parity remain separate requirements. A language label describes a rule; it is not automatic language identification.

Resource budgets are 8 MiB per text section, 50 retained revisions, a bounded history/32 MiB project, 1000 distinct dictionary entries and 64 KiB combined dictionary text. Dictionary matching has an explicit operation budget and fails without applying a partial transformation. Backup snapshots have a separate 64 MiB aggregate budget. The editor reports a budget failure and keeps existing files; it does not silently discard history. Open/export smaller sections when needed. These are current implementation budgets, not a claim that V3 large-library targets are complete.

Automated model/controller tests cover source immutability, Unicode, offset ranges, literal substitutions, undo/redo, malformed JSON, stale saves, recovery, atomic failure and path protection. Actual Windows keyboard and NVDA acceptance must be reported separately from these tests.
