# Word bridge validation and evidence

This is the finite validation contract for the optional bridge to an already-open
Microsoft Word document. It supplements [the user guide](WORD_BRIDGE.md).
Source, tests and scripts are checkpointed with exact Git blob readback.
The first portable preflight executed on Linux; its failures and the corresponding
corrections are recorded below. **The corrected source and all native/Office
execution remain NOT_RUN until their exact candidate executes.** The local
executor is unavailable. Static review and an unchanged source hash must never
be reported as an actual Windows, Office, visual-layout or NVDA pass.

## Required portable source and installed suites

Run each entire module through the existing strict runner with its ordinary
offline policy, no skipped/XFAIL/XPASS acceptance and existing finite child
cleanup. Run the same complete suites against the installed application with
its normal exact-origin checks. Nothing here needs external network, Office,
a voice, an audio device or an audio-engine download.

| Whole module | Static expected cases | Main evidence |
|---|---:|---|
| `tests/academic_integration/test_word_records.py` | 50 | Exact passive records, full sequence/source/metadata binding, real canonical CSL for APA/IEEE/Chicago, locator evidence, rich text, foreign-range refusal. |
| `tests/academic_integration/test_word_bridge.py` | 20 | Real LibraryApp, CSL processor, broker and owned child; approval/source/Word changes; completed versus uncertain; actual pending-child and staging lifetime; deferred interruption after native quiescence. |
| `tests/academic_integration/test_word_worker.py` | 17 | Actual fixed protocol children and finite deadlines; production native admission with explicit doubles; active/foreign undo protection; first-write grant expiry; aggregate capture bound; native offsets instead of Python display offsets. |
| `tests/ui/test_word_citations.py` | 17 | Actual dialog factory with controlled wx and a real borrowed serial worker; explicit default-No approval, stale modal/queued scope, close, late completion, actual workbench hook. |
| **Total** | **104** | The original 101 cases were collected in the first preflight; three added Unicode cases are statically enumerated. The corrected 104-case union has not executed. |

The adjacent companion
`tests/academic_integration/word_fixtures.py` is required. Keep the
`tests/academic_integration` and `tests/ui` relationship when copying test
files: the UI suite intentionally loads that same companion. Its worker main
imports the exact requested application package and production
`word_worker.py` before installing the explicitly synthetic native backend.
Do not replace it with a fake completion result in the host.

Retain every pre-existing bibliography, CSL, citation, DOCX export, document
regeneration and UI/export test. These new modules add a new bridge and do not
replace those contracts. No dependency pins change: the existing comtypes
1.5.0 and canonical citation dependencies remain in the reviewed closure.


## First portable preflight and correction

[Run 46042518081, job 140653551461](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46042518081/job/140653551461)
checked out exact source `1fc5fb4cc4bf412568d76c3175a64c89326c1150`
on Linux/Python 3.11.18. Its four Word suites collected 101 cases: 62 passed,
two failed, and 37 had setup errors; none were skipped. This is a failed
portable source result, with no Word or Windows qualification.

The corrected candidate retains every assertion. Both source fixtures and
both native scripts now call the existing keyword-only
`AcademicController.export(identity=...)` API. Record serialization rejects
unpaired Unicode surrogates in keys and nested values before the unchanged
ASCII transport encoding; three additional cases also preserve valid
supplementary Unicode. The two final source-grant expiry checks raise
`TimeoutError`, matching the existing `expired` failure category while
retaining their positions, the five-second grant and the real slow-child
no-mutation assertion.

The complete preflight artifact is
[15221073460](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46042518081/artifacts/15221073460),
99,056 bytes, SHA-256
`f18f3e975ecda141588fddd13cd18567a9d5e3e37c830e336628423d739419dc`.
The whole 43-suite run had 1,336 passed and 53 failures/errors with zero skips;
only the four Word suites are described here. A passing predecessor or static
review does not qualify the corrected candidate.

## Required Windows native host and protocol probe

Use the installed application's interpreter after the current full-bundle
installation and native DLL activation setup. The script itself retains the
existing `activate_bundled_runtime()` handle before importing wx and through
all host/worker cleanup:

```text
<installed-python> -I -B scripts/windows_word_bridge_smoke.py \
  --source-root <exact-checkout>/carr-library/src \
  --require-installed \
  --synthetic-transport \
  --output <new-report>/word-bridge-native.json
```

The exact checked-out companion above must be available beside the source root.
The explicit `--synthetic-transport` acknowledgement is mandatory. This probe
uses real native wx controls, native keyboard input, the actual DesktopFrame and
its one worker, the actual canonical CSL formatter, ResourceBroker, fixed child
protocol and private Windows Job. Only the COM backend is replaced by the named
protocol fixture. It does not launch or impersonate Microsoft Word.

Acceptance requires all of the following in the actual report:

| Field | Required value |
|---|---|
| `status` | `PASS` |
| `actual_native_ui` | `PASS` |
| `actual_csl` | `PASS` |
| `synthetic_word_protocol` | `PASS` |
| `owned_child_cleanup` | `PASS` |
| `owned_child_invocations` | Exactly 3 |
| `checks` | Every one of the 24 distinct names below; no duplicates or missing phase |
| `installed_modules` | The 20 exact module origins below, with installed locations and matching checked-out bytes before and after |
| `actual_word` | `NOT_RUN` |
| `manual_nvda` | `NOT_RUN` |
| `office_document_quality` | `NOT_RUN` |
| `synthetic_companion_sha256` | SHA-256 of the checked-out companion used by the actual child |

The 180-second whole-probe watchdog remains active through native window close,
the real host finalizers, owned child death and private Job release. Cleanup
fallbacks cannot satisfy assertions: all three children must already be dead,
all private Job handles closed, temporary transport absent and shared leases
released before a successful report. A real native default-No question and
explicit Yes are required; directly calling the dialog decision is insufficient.

Failure cleanup also has an actual nested-native control: the probe answers only its own process/title/owner-checked MessageBox with No, leaves suspended parents intact, then closes one captured active modal loop on a later event turn. A failed probe never reports these cleanup controls as successful Office execution.

### Required native check names

- all twenty Word host and protocol module bytes match their requested origins
- failure cleanup answers owned Word confirmation No before closing only its active modal
- every invoked Word action is enabled in its actual native state
- native Word application confirmation starts on No
- actual current DesktopFrame owns the selected library and one worker
- opening Word review launches no child or document mutation
- Word operation and preview controls have distinct accessible names
- actual inspection uses one reaped protocol child without a mutation
- document references remain a read-only inert display
- native Space selects an explicit source reference
- actual canonical CSL preview is not a synthetic citation string
- the held preview retains its child staging and shared reservation
- preview alone does not write a mutation or final grant
- native No leaves the held preview unapplied
- explicit native Yes completes exactly the reviewed proposal
- confirmed completion returns accessible status and releases the private Job
- native copy exposes only the reviewed inert document references
- busy Close stops and reports before destroying the native review
- review closure never shuts down its borrowed desktop worker
- existing workbench formatting remains the Word entrypoint
- all Word protocol work uses the existing single off-main worker
- bibliographic source bytes remain exact after every native action
- actual host finalizers settle before native completion is reported
- application and synthetic companion bytes remain unchanged after native cleanup

### Source and installed module origins

- `carr_library.academic_integration.word_bridge`
- `carr_library.academic_integration.word_worker`
- `carr_library.academic_integration.word_native`
- `carr_library.academic_integration.word_records`
- `carr_library.academic_integration.formatting`
- `carr_library.citations.bibliography_workbench`
- `carr_library.citations.csl`
- `carr_library.ui.word_citations_dialog`
- `carr_library.ui.citation_styles_dialog`
- `carr_library.ui.academic_library_dialog`
- `carr_library.ui.desktop_app`
- `carr_library.ui.worker_lifetime`
- `carr_library.audio.process`
- `carr_library.audio.windows_job`
- `carr_library.audio.cleanup`
- `carr_library.operations.broker`
- `carr_library.operations.models`
- `carr_library.application`
- `carr_library.durable_state`
- `carr_library.native_runtime`

The native probe checks these origins before and after its actual work. For the
portable installed suites, bind the same newly introduced production modules
and their existing runtime dependencies in the ordinary installed-gate origin
verification. A source file supplied as a test companion must not become an
import path for `carr_library`.

## Optional actual Office qualification

Microsoft Word is not assumed present in CI. An unavailable Office installation
does not turn this probe into a passing test or qualify the production COM code.
Keep this separate from required synthetic transport and native host evidence.

An operator must create or select a disposable Word document containing exactly
one paragraph, with this text followed by Word's normal paragraph mark:

```text
Resonance Word bridge disposable qualification
```

The document must have no content controls, fields, endnotes or prior application
metadata, must be unprotected with Track Changes off, and must have a collapsed
caret at position zero. Keep that document active. The bridge does not create,
open, save or close it. Word's existing AutoSave setting remains unchanged.

Run from an interactive terminal with explicit consent and the document's exact
current name:

```text
<installed-python> -I -B scripts/windows_word_office_probe.py \
  --source-root <exact-checkout>/carr-library/src \
  --require-installed \
  --document-name "<exact disposable document name>" \
  --style apa \
  --approve-disposable-edits \
  --output <new-report>/word-office-apa.json
```

Repeat separately with `--style chicago-notes` and a new clean disposable
document to exercise actual endnote insertion. These are separate observations;
a single style result must not be presented as all-style coverage.

The command-line consent authorizes only three displayed disposable phases:
insert a synthetic reference citation, edit its prefix while retaining the stable
citation ID, and insert/regenerate a managed bibliography. Before every phase,
the actual held native capture must match the exact original document process,
HWND, process creation time, full document identity, Office version, unchanged
outside text and preceding verified metadata. It has no permission to choose a
different document or mutate an arbitrary selection.

After the edit has completely returned and all children/leases are released,
the operator is prompted to move the caret with Ctrl+End in the same disposable
document. No helper or resource reservation remains live while waiting for that
input. The actual subsequent capture must still match. The 600-second total
operator/work/cleanup deadline is finite and explicit.

A successful report has `actual_word: EXECUTED`, eight named checks, 11
source-bound installed origins, the actual Office version and the three completed
phase names. It still records manual NVDA, visual publication layout and all other
Word versions as unqualified. It does not log the user's real Word content:
the admitted document is constrained to the known disposable marker and generated
synthetic citation changes.

If any phase returns uncertainty, the probe stops and asks for inspection in
Word. It never automatically retries, undoes user changes, saves, closes or quits
the application. A synthetic backend is not accepted on this path.

## Boundaries exercised by the tests

- Exact original LibraryApp/workspace/root identity and current reference
  snapshot are checked before admission, after resource waits, during finite
  preview and immediately before the host's final grant.
- One existing broker lease covers the idle COM helper, canonical CSL work,
  private records and process cleanup. No additional executor or queue exists.
- The total default session deadline is 180 seconds, at most 300; approval
  lasts 60 seconds; the last source grant must still be within five seconds
  at the first actual content mutation.
- At most 16 MiB of serialized protocol data, two million text units per
  admitted range, 512 controls, 128 citation clusters and 1,000 references are
  admitted. The capture has a running aggregate budget before retaining another
  overlapping range; native field preservation stores hashes, not thousands of
  repeated large strings.
- Only the exact reviewed managed ranges and the new collapsed insertion point
  may change. The bridge uses native range segments for outside-content proof,
  not Python offsets in displayed text. Foreign fields, controls, legacy
  metadata and unsupported style migrations are never silently adopted.
- The helper reads its immutable completion receipt after actual native
  quiescence, including a deferred KeyboardInterrupt or SystemExit. A complete
  receipt remains COMPLETED after later cancellation or cleanup warnings.
- A visible grant or begun marker without a valid completed receipt is UNCERTAIN.
  That record cannot authorize automatic retry or replacement.
- Private transport survives any pending native cleanup. A deferred filesystem
  cleanup is owned by the existing cleanup owner and broker until quiescent.
- Only the owned custom UndoRecord may be closed. Existing revision tracking,
  protection, foreign undo records and Word's own AutoSave remain untouched.

No new persistent library namespace, backup producer, entitlement, Office
account, document-opening permission or external runtime acquisition is introduced.
The user guide records the official primary API references and their retrieval
date separately from application execution evidence.
