# Local assistant: reviewed local tasks

Tools → Local assistant opens a native task dialog. Its initial shortcut is
Ctrl+Alt+J and uses the same configurable Main command registry as the rest of
the application. The assistant accepts guided English/Ukrainian commands or a
versioned JSON plan. It is a deterministic local task runner; this feature does
not invoke an LLM, a remote service or a separate credential store.

Choose a library, any permitted input folders and a separate output folder.
Preview lists the exact scope, operations and budgets. Run approves that exact
plan. Changing the inputs invalidates the preview. Saved jobs show their full
original plan before Run/resume; another process must supply the fingerprint
of the plan it has actually reviewed. A checksum rewritten inside a checkpoint
does not replace the current caller's captured approval.

Supported tasks use the existing application services: create/list libraries,
browse the catalog, import a file or folder through Import Center, search,
extract a separate reading text, make an explicitly authorized all-library
backup, and produce audio through the existing Audio Studio renderer. There
are no shell, arbitrary Python, deletion, network or credential-reading tools.
Audio prose is literal narration. Directive interpretation requires an explicit
structured JSON audio step. Existing installed voices and FFmpeg are required;
this dialog does not acquire a model or promise any particular voice language.

## Source and task boundaries

Search uses a private session of the existing Research Search Center service.
It does not replace or evict the main window's selected search results. Every
verified excerpt is freshly resolved to its exact workspace, edition, asset,
revision, Unicode offsets and source hash before display. Reports retain those
identities as historical receipts. Ephemeral search handles are never saved as
portable citation authority. Opening a saved report labels its excerpts as
historical and unchecked against current sources; resuming repeats the approved
read-only search and checks the current source again. No assistant text is
automatically saved as research evidence or approved as a new quotation.

The time budget applies to each run/resume as a whole, with cooperative
cancellation in the existing producers. The displayed byte and file budgets
apply to each operation. Native calls that do not cooperate cannot be made
instantaneously cancellable by this interface. Pause retains completed work;
closing or replacing the parent library cancels pending execution and rejects
late callbacks. A successful committed write remains a receipt even when
cancellation arrives at its final boundary.

Outputs must remain in the chosen folder and outside the private data root,
including when the chosen folder is an ancestor of that root. Existing output
files are not overwritten. Extraction preserves the captured source revision;
audio uses the current renderer's operation identity and verified artifacts.
Resuming verifies completed output hashes rather than silently regenerating a
changed artifact.

## Backup and recovery

All-library backup requires its own explicit checkbox because it includes
every workspace in this data directory. It delegates to the current common
format4 backup service. By default, original inputs, projects and recovery state
are retained, while generated audio is excluded. Newly previewed backup steps
show `include_generated: false` explicitly. An advanced JSON `library.backup`
step may set `include_generated: true` to include validated rendered audio and
accepted audio cache. The choice is part of the exact reviewed plan fingerprint,
and the native review describes it before Run. For example:

```json
{"schema":"carr-assistant-plan/1","steps":[{"tool":"library.backup","arguments":{"name":"with-audio","include_generated":true}}]}
```

The selected UI scope still supplies the output folder and all-library backup
permission. Saved schema-1 plans without the option remain byte-for-byte intact;
an omitted option means generated audio is excluded. Resuming also authenticates
the backup manifest and checks that its inclusion policy matches the approved
choice; an existing snapshot is never relabelled as a different backup policy.
Cancellation and byte limits reach that producer. Before publication, the
assistant records an independently checked receipt of the complete backup tree,
including the database, retained originals and typed sidecars. A private stage
is published by the shared atomic no-replace directory operation. If execution
stops after publication but before the final job checkpoint, resume can accept
only the exact already-published tree described by that receipt. A same-size
change to the database or an added/removed file is rejected.

The common backup adapter carries typed `state/assistant` job/control documents;
only exact producer lock and crash-temporary names are omitted. Restore first
checks the original bytes against the supplied manifest. Restored job documents
then receive `requires_reapproval=true`, while their original scope and paths
remain unchanged. This applies even if a saved root string would match the new
location. They are visible for review and cannot execute until a new plan is
submitted. Restoring a backup does not automatically retarget an output folder.

## Implementation and evidence

The plan, journal, validator, whole-tree receipt and production adapter reuse
the existing public PR99 implementation at
`822e52e9edf4b6277c5ccd980513f1e78283348b`, with the scope, source-session,
approval, publication and parent-lifetime hardening described above. The common
format4 adapter composes with the current reading, keyboard and audio producers;
there is no second renderer or storage schema.

`tests/assistant/` exercises actual import/search/extraction, metadata changes,
source tampering, complete backup/restore, receipt recovery, cancellation,
corrupt checkpoints and literal audio through installed FFmpeg/Flite when
available. `tests/ui/test_assistant_actions.py` runs real workers/services for
parent/root changes, closure, reapproval and executor rejection. These tests do
not establish native Windows keyboard behavior, NVDA or listening quality;
those remain separate platform acceptance work.
