# Reviewed document batches, splitting and names

The **Convert → Document batches — split, name and produce** command composes
the existing document converter, reading copies and Studio production jobs.
It uses the desktop's existing background-operation owner. There is no second
speech engine, general queue, folder watcher or automatic execution on restore.

## Native workflow

1. **Capture files**, **Capture folder**, or **Capture saved reading copies**
   explicitly selects inputs. Folder capture includes subfolders in a stable
   order, excludes unsupported file extensions with a visible summary, and
   rejects links and reparse points. Capture retains bounded managed snapshots;
   the original files are only read. Saved reading copies keep their original
   text, edit history, source map and quotation restrictions.
2. **Prepare reading text** runs the existing isolated converter over those
   snapshots. Original paths may be removed afterward. Each prepared text,
   conversion report and reading copy is bound to its exact source and bytes.
   Formats and their limitations remain those of **Supported document formats**.
   OCR requires explicitly selected installed language identifiers. No model,
   Pandoc executable or voice is downloaded by this workflow.
3. Choose the split method, text filename template, existing destination,
   separate/combined grouping and output action. **Preview split and names**
   shows every part's Unicode character range, name, reading-copy source map
   and SHA256. It identifies occupied names before approval and writes no
   external output.
4. **Save reviewed plan** verifies the current source, options, destination
   identity and names again. This saves the plan only. Changed controls cannot
   reuse a previous approval, including a silent native `ChangeValue` update.
5. **Run / resume saved plan** explicitly publishes exact UTF-8 text with
   provenance, or admits canonical Studio jobs for the selected installed
   voice and format. Text exports preserve all selected characters, including
   Unicode, CR, LF, CRLF, spaces, brackets and apparent script commands.
6. **Pause** and **Stop operation** request cancellation through the shared
   background owner. The window waits for its owned work before closing.
   Reopen with **Reload saved jobs**, select the same ID, and explicitly run
   its saved plan to continue. Completion requires current byte verification;
   a stored status alone is not a new verification result.

## Split rules and naming

| Method | Exact behavior and boundary |
| --- | --- |
| Whole copy | One part containing the complete reading text. |
| Paragraphs | Stored CRLF, CR or LF line breaks. This does not infer paragraph semantics from a PDF layout. |
| Blank lines | Two or more stored line breaks, with optional spaces/tabs. A single CRLF is one break. |
| Sentences | A disclosed punctuation/closing-quote heuristic. No linguistic-model claim. |
| Characters | Unicode code-point count. It does not promise grapheme or word alignment. |
| Literal marker | Cuts before the exact marker; the marker remains in the following part. |
| Regular expression | The existing isolated bounded regex worker supplies checked offsets. No in-process user regex or arbitrary code execution. |
| Sections | Exact converter section offsets, or the reviewed source-file boundaries in a combined copy. |
| Pages | Actual supplied physical-page positions, such as extracted PDF pages. Logical sections are never labeled as printed pages. |
| Estimated duration | An explicitly chosen whitespace-word rate and requested seconds. Both the UI and plan say **ESTIMATE ONLY**; no audio has been measured at this stage. |

The shared `plan_split` API also accepts explicitly provided `TextBoundary`
chapter and bookmark positions. The native document importer does not invent
these from text that merely resembles a heading. Its native choices expose
the section/page information actually available from the converter and reading
copy. A caller that already owns real chapter/bookmark positions can pass those
to the shared planner without introducing another split engine.

Combining copies inserts exactly two LF characters between the retained texts.
The combined output is a separate derivative and records all contributing
source hashes and the separator rule. Original reading copies remain intact.

Text naming templates support `{title}`, `{source}`, `{heading}`, `{first_line}`,
`{index}`, `{part}`, `{total}` and `{document}`. Sequence fields accept bounded
integer formats such as `{index:04d}`. `.txt` is appended automatically.
Substituted text is normalized, visibly shortened and stripped of path/device
characters. The complete preview rejects Windows device names, paths, trailing
dots/spaces, excessive encoded lengths, and case/NFC filename collisions.
Every text also has a `.txt.provenance.json` completion sidecar. Existing files
are never replaced, even when their bytes happen to equal an intended output.

## Canonical Studio composition

For the Studio action, choose **Load installed voices** and then an explicit
voice. Each document becomes a canonical project with one scene per reviewed
part and exact spoken-line text. The adapter constructs `Project`, `Scene` and
`Line` directly: imported `[Scene: ...]` and `Narrator: ...` text is spoken
literally, not parsed as production commands. The original script and reading
copy references remain in the derived project.

Studio retains its existing measured chapter/master rendering, formats,
resource admission, source checks, cache and completion receipts. Its internal
filenames remain canonical; the text template does not rename audio artifacts.
The preview shows the actual new Studio job directory names. A changed reviewed
split or voice creates a separate derivative project, preserving earlier
projects, revisions and output bytes. Repeating a completed batch verifies the
same jobs without rediscovering voices or synthesizing them again.

A whitespace-only text part remains valid for exact text export. Studio
preview refuses such a part and asks for fewer boundaries rather than dropping
source characters or inventing an audio duration. Speech quality, a language's
pronunciation and third-party audiobook-player behavior require their own
engine and human qualification.

The current native batch form uses one reviewed method, voice and format for
the selected group. Per-book mixtures of voice/rate presets and arbitrary
renaming of Studio's internal chapter/master files are not implemented by this
form. Existing Studio project editing remains available for individual jobs.

## Durable records, cancellation and backup

Records live under `state/conversion/batches`, using existing `JobStore`
identities/locks and the shared atomic JSON reader/writer. Inputs and prepared
artifacts use managed relative references, so normal library backup retains
them. The existing library writer guard excludes backup while a batch is
actively publishing; wait for completion or stop the operation first.

Each completed text has exact bytes and a plan/source-bound external completion
sidecar. A process crash after publishing that pair but before saving the
private checkpoint can reconcile the pair on the next explicit run. A lone
file or unrelated sidecar is not accepted as completion: choose a new
destination. Completed items are rehashed before reuse and before final batch
completion. A changed, renamed or replaced destination directory fails the
operation; approval is bound through each writer's late publication check.

After restoring a library at a different root, the saved job is visibly
**waiting-input**. It cannot run against remembered absolute external outputs.
Choose a new destination, preview and approve it explicitly. Re-review keeps a
bounded history of earlier plans and receipts. It does not silently execute
old private engine profiles or borrow external output files.

Default capture limits are 256 selected documents, 20,000 scanned entries,
64 directory levels and 1 GiB of retained source bytes. A reading copy uses the
existing 2-million-character limit; aggregate prepared text defaults to
8 million characters. One preview supports at most 10,000 parts. Saved-job
listing is bounded by 1,000 records and 32 MiB of aggregate metadata with
cancellation between records. Native previews have a 4-million-character
display budget. A reached limit is an explicit error, not a capacity claim.

## Command-line review and resume

The installed `python -m carr_library documents` entry point calls the same
`DocumentBatches` service as the native form. Select an **existing** library
with `--data-dir`; an ordinary folder is never silently turned into a library.
Progress goes to standard error and machine-readable JSON goes to standard
output, both as UTF-8 even when the redirected Windows console uses a legacy
encoding. No command opens the desktop or acquires an engine automatically.

Run the following commands separately, replacing `JOB_ID` and `REVIEW_SHA256`
with the exact values returned by the preceding steps. The destination must
already exist outside the private library, and `review.json` must be a new file.

```text
python -m carr_library documents --data-dir LIBRARY capture INPUT_FILE_OR_FOLDER
python -m carr_library documents --data-dir LIBRARY prepare JOB_ID
python -m carr_library documents --data-dir LIBRARY preview JOB_ID --destination OUTPUT_FOLDER --mode blank-lines --template "{index:04d}-{source}-{part:03d}" --output review.json
python -m carr_library documents --data-dir LIBRARY approve --review review.json --review-sha256 REVIEW_SHA256
python -m carr_library documents --data-dir LIBRARY run JOB_ID
python -m carr_library documents --data-dir LIBRARY show JOB_ID
python -m carr_library documents --data-dir LIBRARY jobs
```

`capture` accepts several explicit paths. Add `--reading-copies` for saved
reading-copy JSON inputs, or repeat `--ocr-language` for selected installed OCR
languages when capturing raw documents. Preparation uses retained inputs and
can continue after the originals have been removed. `preview --combine`
combines them explicitly; all split modes retain the planner's actual-boundary
requirements. Use `--action studio --voice-id EXACT_VOICE_ID --language LANGUAGE
--format FORMAT` to review Studio admission through the same production service.
`--seconds` and `--words-per-minute` apply only to the disclosed duration estimate.

Previewing creates no batch outputs. The saved review includes its complete
plan and SHA256; `approve` checks the exact supplied fingerprint, current inputs,
destination identity and output names again. It saves approval without executing
the plan. `run` is a separate command, and repeating it re-verifies completed
receipts before reusing them. `show` and `jobs` report stored metadata only.

Ctrl+C requests cancellation at the service's existing boundary and returns
exit code **130** after retaining completed receipts. Explicit `pause JOB_ID`
and `cancel JOB_ID` commands submit control requests; their response does not
pretend that an active worker has already acknowledged the request. Resume
with `prepare` or `run` as appropriate. Success returns **0** and a domain or
filesystem error returns **1**; invalid command syntax uses argparse's **2**.
Restored jobs require a fresh preview, approval and explicit destination just
as they do in the native form.

## Evidence and required gates

Source tests use generated documents and real isolated converter processes.
They cover Unicode/CRLF preservation, same-stamp folder membership changes,
finite selection, source/provenance corruption, no-clobber behavior, changed
destination identity, cancelled/resumed jobs, a real abrupt child-process exit
between publication and private checkpoint, and actual backup/delete/restore.
Studio tests use explicitly synthetic PCM voices through actual FFmpeg and
ffprobe; they do not claim TTS model or language quality. Handler tests execute
the production closures with widget/scheduler stand-ins and the real service.

Required source/installed suites:

- `tests/conversion/test_split_planning.py`
- `tests/conversion/test_document_batches.py`
- `tests/conversion/test_document_batch_handlers.py`
- `tests/conversion/test_native_document_batch_driver.py`
- `tests/conversion/test_document_batch_cli.py`

The Windows invocation is:

```text
python -I -B -X utf8 scripts/windows_document_batch_smoke.py
```

It requires an already installed package (or explicit `CARR_PROGRAM_DIR`
bundle) whose six checked production modules exactly match the candidate
source. It uses normal wx `MainLoop`, foreground/PID/focus verification and
paired real `UIActionSimulator` keys. A synthetic local keymap dispatches the
original registered menu action; there is no default product shortcut change.
Only explicit picker paths are substituted. The script then captures two
Unicode sources, deletes those originals, runs real converter children,
reviews/approves/runs four text outputs, verifies every receipt, repeats the
same job without rewriting outputs, and refuses an occupied-name preview.

A Linux test pass does not count as this native gate. The native script reports
file-picker automation, interrupted native resume, human keyboard use, speech
quality and NVDA as **NOT_RUN**. Its final PASS requires every native business
assertion and exact installed module identity; no skips count as success.
