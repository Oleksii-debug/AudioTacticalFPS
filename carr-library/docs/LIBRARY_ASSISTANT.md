# Local library workflow assistant

The **Tools → Local library workflow assistant** command runs explicit, reviewed
workflows against the current library. This is the deterministic library adapter
for V3-09. It does not claim general natural-language understanding or control the
separate audio production director. It needs no language model, remote service,
API key or additional dependency.

## Keyboard workflow

1. Open the desired library, then use the Tools menu to open the workflow dialog.
2. If the task imports files, enter or browse to an **Allowed input folder**. If
   it exports or backs up, choose an **Allowed output folder** outside private
   application data. Both folders must already exist. Paths in commands are
   relative to these folders; use `/` between folder names.
3. Enter commands in the named multiline editor. Choose **Preview plan**. This
   validates every step, checks source files and shows the full plan without
   changing library records or creating a job.
4. Read **Plan review**, then choose **Approve and run**. Changing the commands,
   selected folders or backup permission invalidates the previous approval.
5. Read **Library workflow status** and **Step results**. Operations run on the
   desktop worker, leaving the UI available. **Pause workflow** saves completed
   work; it is not an undo command. Backup preparation and verification check
   pause at their safe checkpoints. Once final snapshot publication starts, it
   finishes before pause is honored; resume verifies the saved snapshot.
6. For recovery, choose **Refresh saved jobs**, select a job in this library,
   choose **Load for review**, inspect its folders and remaining steps, then
   choose **Approve and run**. Closing while a worker runs requests pause and
   keeps the dialog alive until the worker reaches its safe checkpoint.

All controls are native wx controls with accessible names. Tab/Shift+Tab reach
commands, folders, buttons, plan, results and status. Escape closes the dialog or
requests a safe pause while work is active. No visual timeline or mouse selection
is required. The headless controller tests verify state and focus requests;
manual Windows/NVDA spoken-behavior acceptance is still required.

## Short commands

Use one command per line. English and Ukrainian command names are accepted;
source text, titles and queries retain Unicode.

```text
import: book.txt
topic: Literature review
export: @1 -> review.md
search: railway history
topics
```

This imports the selected UTF-8 text file, creates a topic, exports that topic and
searches the current library. `@1` refers to the topic created by the second step;
references start at zero and must point to an earlier `create_topic` step. An
empty newly created topic exports an empty evidence document. Search results are
not automatically saved as evidence or transformed into an invented outline.

| Command | Ukrainian names | Effect |
| --- | --- | --- |
| `search: exact phrase` | `пошук:`, `знайди:` | Find source text in this library |
| `topic: Topic name` | `тема:` | Create a research topic |
| `import: relative/file.txt` | `імпорт:` | Import exact UTF-8 TXT through existing retained-source orchestration |
| `export: topic-id -> new-file.md` | `експорт:` | Export verified saved evidence and separate notes from the named topic |
| `topics` | `теми` | List visible topics and their IDs |
| `backup: new-folder` | `копія:` | Create an explicitly permitted complete backup in `new-folder/snapshot` |

Unknown commands reject the whole plan before any step executes. Use **Import
Center** for PDF, DOCX, EPUB, OCR and review; those formats are not silently
converted by this adapter. Audio, model, provider, shell, arbitrary code and
network tools are not present in the allowlist.

## Typed JSON plans

The same editor accepts a JSON object containing only a `steps` list:

```json
{
  "steps": [
    {"tool": "create_topic", "arguments": {"name": "Literature review"}},
    {"tool": "create_topic", "arguments": {"name": "Methods", "parent": "@0"}},
    {"tool": "search", "arguments": {"query": "railway AND history", "mode": "advanced", "limit": 20}},
    {"tool": "export_topic", "arguments": {"topic": "@1", "filename": "methods.md", "format": "markdown"}}
  ]
}
```

| Tool | Arguments |
| --- | --- |
| `search` | Required `query`; optional `mode`: `phrase`, `word`, `terms`, `thematic`, `advanced`; optional `limit`: 1–100, default 20 |
| `list_topics` | Empty object |
| `create_topic` | Required `name`; optional existing `parent` ID or earlier `@N` reference |
| `export_topic` | Required `topic`, `filename`; optional `format`: `markdown`, `text`, `ris`, `bibtex`, `csv` |
| `import_text` | Required relative `path`; optional `title`; source hash is added by preview, never accepted from user JSON |
| `backup` | Required relative `folder`; requires the separate complete-backup permission |

The library identity and allowed folders are constructor/UI scope, not step
arguments. A JSON step cannot switch libraries or expand its permitted roots.
Duplicate JSON keys, unknown tools, unknown arguments, future topic references,
nonfinite numbers, path traversal, URL paths and repeated output names fail
validation. Plans currently contain at most 32 steps and 64 KiB of user JSON/text.
These are explicit resource budgets; larger production scheduling belongs to the
shared V3 director.

## Complete backups are explicit

Ordinary tools operate only in the chosen library. The existing complete backup
API includes **all libraries and registered original documents in the data
folder**. Its operation therefore requires checking **Allow complete backups of
ALL libraries and original documents** and approving the prominently labelled
backup plan. Merely choosing an output folder does not grant that permission.

The output contains `assistant-owner.json` and a `snapshot` subfolder. The owner
marker binds recovery to the reviewed job. Use the normal backup restore command
with the `snapshot` folder and the manifest SHA256 shown in Step results. An
incomplete or unowned destination fails closed and is never overwritten.

## Durability and integrity

Approved jobs are stored privately in
`library-workflows/<SHA256 of workspace ID>/<job ID>.json`. Each manifest has a
version, a checksum, its approved plan digest, allowed roots, per-step states,
results and publication intents. Writes use a flushed temporary file followed by
atomic replacement. The existing native file lock serializes cooperative job
runners for the same workspace.

Each action is its own durable checkpoint. There is no claim that an entire
multi-step plan is one database transaction. A later failure preserves earlier
completed steps and explains which step did not finish. Resume skips completed
operations after verifying their durable write results; it does not repeat them
or silently replace changed output.

Topic and imported-edition identities derive from the job and step, so a crash
after a database commit but before the journal update reconciles the existing
record. TXT ingestion reuses the original retained-source journal and planned
asset checks. Exports record expected UTF-8 output hashes and the flushed staging
filename before exclusive publication. A crash between hard-link publication
and staging removal is recovered only when the recorded file and output have
the same inode, exactly two links and matching bytes. Preview only validates;
the locked runner removes that proven staging twin. Unknown additional links,
replacement files and changed bytes are rejected without modification. Existing
matching output can be recovered; different output is reported as a conflict.
Each tool's saved result and intent also has a checked schema. Completed steps
must form an ordered prefix, and saved topic identities must match the job
before later `@N` references can be resolved.

Complete backup creation and recovery use the same
authoritative verification as restore: the owner marker, exact file namespace,
manifest and database hashes, supported database schema and invariants,
catalog-bound original files, and every saved reading-state byte and source
binding must agree. Verification uses a private temporary database and never
restores over or modifies the backup being checked. The workflow forwards pause
requests to this verifier both for a new snapshot and when resuming completed
steps. Pausing verification preserves the snapshot and the earlier checkpoint;
resume rechecks it without creating a replacement.

Preview binds imported bytes by SHA256. Both approval and ingestion recheck them.
Preview uses the actual search parsers and limits for every query, including
steps after planned mutations, so unsupported syntax is rejected before a job
is started.
Source files containing apparent commands remain source data. Search uses its own
application handles so it does not replace the open notebook's selected result
or discard an unsaved note. Search results in the journal are execution-time
snapshots; use the ordinary source/evidence actions for fresh quotation approval.

Folders, ancestors and files are checked for traversal, symlinks, reparse points,
hard-linked inputs and reserved Windows names. Output is outside private library
data and uses new names. The filesystem boundary assumes trusted local folders;
it is not a sandbox against an unrelated process replacing directory entries at
the precise publication syscall. Journals are bound to the current data-folder
path. Portable job relocation and the broader V3 settings/queue backup format are
separate integration work; copying a journal does not silently authorize old
paths on another computer.

## Integration API

```python
from carr_library.library_assistant import LocalLibraryAssistant

assistant = LocalLibraryAssistant(
    library_app,
    selected_workspace_id,
    input_root=chosen_input_folder,
    output_root=chosen_output_folder,
)
plan = assistant.plan("import: book.txt\ntopic: Literature review")
# Display describe_plan(plan), then use the user's explicit review approval.
job = assistant.start(plan, approved=True)
result = assistant.run(job["id"], cancelled=cancel_event.is_set, progress=report)

# Reopen a previously approved job in its original library and data directory.
assistant = LocalLibraryAssistant.open_job(library_app, selected_workspace_id, job["id"])
review = assistant.load(job["id"])
result = assistant.run(job["id"], cancelled=cancel_event.is_set, progress=report)
```

`run` returns a journal with `state` equal to `complete`, `paused` or `failed`.
Invalid scope, invalid journal and an already-held worker lock raise before a
tool executes. A director must inspect the returned state rather than treating a
returned object as completion. The UI hook is
`carr_library.ui.library_assistant.install_library_assistant_actions(frame)`.
It adds one action to Tools without replacing any existing menu or notebook.

## Verification

The focused suites use real SQLite, retained UTF-8 originals, exact saved evidence,
filesystem outputs and backup recovery. They cover full-plan rejection, approval,
scoped search, untrusted source instructions, path escapes, changed input,
checkpoint pause/reopen, concurrent runner exclusion, output conflicts, checksum
corruption and explicit complete-backup permission. A subprocess terminates with
`os._exit(73)` after committing a topic but before saving its result; reopening
recovers exactly one topic. Another subprocess terminates immediately after
export link publication; recovery verifies the exact recorded twin and never
rewrites the published output. Extra links, changed bytes and a same-content
replacement inode all fail without mutation. Tests also reject malformed typed
results for all six tools, invalid progress and retargeted topic references.
Controller tests execute services on worker threads and preserve notebook drafts
and scope.

```sh
python scripts/ci_check.py \
  tests/library_assistant/test_library_workflow.py \
  tests/library_assistant/test_library_workflow_ui.py
```

Windows native widgets, NVDA spoken announcements, physical power loss and the
full audio/model/director acceptance are not established by these tests.
