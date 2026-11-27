# Local assistant and optional provider connections

The assistant executes a reviewed list of local operations. The default planner
uses deterministic guided commands in English and Ukrainian. A separately
installed, explicitly enabled loopback chat server can propose a more flexible
plan. Neither a language model nor a document may extend the selected library,
input folders, output directory or tool permissions.

## Commands available now

Enter one command per line. Text within quotation marks is data and is not
interpreted as another command.

```text
Створи бібліотеку "Історія"
Імпортуй папку "C:/Books/History"
Знайди "залізничний транспорт"
Покажи книги
```

The equivalent English commands are `Create library`, `Import folder`, `Search`
and `List books`. `Import "path"` imports a single PDF, EPUB, DOCX or UTF-8 TXT
file through the existing isolated import queue. Nested folders are scanned
within explicit file/byte/time budgets. Existing duplicates, quarantine and
review requirements retain their normal library behavior.

`Extract "edition-id" as "reading.txt"` writes an authenticated reading copy
through the existing Reading Desk. Extracted source segments remain unchanged;
blank lines separate segments. This does not approve unreviewed source text or
create a quotation. `Backup "backup-name"` requires the separate **full backup**
permission because it includes every workspace in the selected application
data directory.

`Produce audio: script` or `Озвуч: сценарій` passes the remaining text to the
integrated production director. Actual available voice IDs must be selected or
provided in a structured plan. The callback must return real completed files;
a textual statement of success is rejected. A stable operation ID connects
assistant retries to the same production job.

The initial parser does not claim arbitrary-language understanding. Unsupported
or ambiguous instructions stop at preview without running anything. It has no
delete, shell, Python, browser, credential-reading, publishing or arbitrary
network tool. Source contents and provider responses are never reinterpreted
as executable instructions.

## Native host API

```python
from pathlib import Path
from carr_library.assistant import AssistantService, Scope

service = AssistantService(app, production=production.assistant_callback)
scope = Scope(workspace_id=selected_workspace,
              input_roots=(Path(selected_input_directory),),
              output_root=Path(selected_output_directory))
plan = service.preview(instruction, scope)
# Display plan.summary or plan.to_json() and the selected scope before Run.
job_id = service.submit(plan)
report = service.run(job_id, cancelled=cancelled, progress=progress)
```

`progress(completed_steps, total_steps, message)` receives execution status.
`pause`, `cancel`, `resume`, `get` and `jobs` persist across application restart.
Pause and cancellation preserve completed steps and underlying import/render
checkpoints. `Scope(allow_create=True)` explicitly permits creating a new
workspace; it cannot be combined with an already selected workspace.

Plans use `carr-assistant-plan/1` JSON with `steps` containing `tool` and
`arguments`. A proposed JSON plan may omit scope and use the scope selected in
the host. It cannot supply a different scope. `audio.produce` accepts `text` or
`script`, plus optional `voice_ids`, `title`, `format` and `stems`. The operation
ID is assigned by the executor rather than by model-generated arguments.

## Recovery and source integrity

Jobs use atomic, fsynced JSON under `state/assistant`. Operating-system file
locks prevent concurrent runners and release after process failure. The root
backup service can snapshot these files; lock and temporary files are not
recovery data. A restored job is bound to its original application directory;
review and resubmit its plan after moving to another location.

Create-library operations use a deterministic workspace ID inside the existing
authorized application transaction. Imports reuse the existing durable import
batch and revalidate its original selected file/folder scope on every resume.
Completed outputs are checked by path, size and SHA256 before reuse. Changed
outputs are not overwritten or silently re-rendered. If a crash leaves a
complete backup before the assistant records its receipt, the job requests
review instead of creating another backup or claiming unverified success.

Original bytes, source revisions and quotation approval remain managed by the
existing library services. User edits to a reading copy do not change them.
Symbolic links, Windows reparse points, network paths and output traversal are
rejected. A local trusted application data directory is still required; this
is not a sandbox against another process running with the same user's rights.

## Optional profiles and OS credentials

`ProfileStore(app.root / 'state' / 'providers')` stores non-secret
`ProviderProfile` objects. Each account has a separate stable profile ID, name,
adapter, endpoint, model, capability set, optional voice ID and opaque
`credential_ref`. Profiles are disabled by default. Multiple accounts for the
same service remain separate.

`WindowsCredentialVault.put(profile_id, secret)` writes a generic credential
to the current Windows user's Credential Manager and returns an opaque
reference. `get` resolves it just before a selected provider request; it is not
available as an assistant tool. `delete` removes it. There is no plaintext file
fallback, and non-Windows hosts cannot store these credentials through this
adapter. Profile exports clear credential references and disable restored
connections. The new device must reconnect each account. No provider test is
performed on save, open, import or application startup.

## Request and review a local model proposal

The existing **Local assistant** dialog defaults to guided commands and typed
JSON. Its **Planning source** selector additionally lists enabled, explicitly
local `openai-compatible` profiles with `llm.chat`. Create these in **AI and API
connections**, using a numeric loopback endpoint and the model ID your already
running server exposes. Opening either dialog does not contact or start a server.

Choose a local profile and **Preview plan** to request a proposal. Only the goal
text is sent; selected folder contents and library documents are not added.
The shared provider gateway uses a 120-second request budget and an 8 MiB response
limit for planning. The shared gateway applies a whole-call deadline and interrupts its owned
connection when the request is cancelled or its budget expires. The result must pass the same typed tool, argument, workspace
and path checks as a guided plan. The request is cancellable in the background.
Editing the goal, selected scope or profile discards a pending response and
invalidates a reviewed plan. **Run / resume** is a separate action that records
and executes the reviewed plan through `AssistantService`.

The CLI uses the same boundary. In PowerShell, for a configured profile named
`chosen_local`, the two separate invocations are:

```powershell
python -m carr_library assistant --data-dir "C:\ResearchLibrary" --list-local-profiles
python -m carr_library assistant --data-dir "C:\ResearchLibrary" --instructions "goal.txt" --allow-create --local-profile chosen_local | Set-Content -Encoding utf8 "reviewed-plan.json"
# Open and review reviewed-plan.json, including every operation and its scope.
python -m carr_library assistant --data-dir "C:\ResearchLibrary" --instructions "reviewed-plan.json" --allow-create --run
```

Repeat the exact workspace, input/output folders and permissions when executing
the reviewed JSON. Scope changes are rejected. `--local-profile` cannot be
combined with `--run` or `--resume`, so execution never regenerates a different
model proposal. Listing profiles makes no model request. Provider errors produce
no executable proposal. The protocol and native control fixtures validate this
flow; an actual local model and its planning quality require separate acceptance.

## Implemented provider protocol adapters

| Adapter | Operations | Protocol |
| --- | --- | --- |
| OpenAI-compatible | Text chat and proposed plans | Explicit endpoint plus `/chat/completions`; text messages, selected model, bounded tokens, optional JSON-object response |
| Local llama-server-compatible | Optional local plan proposal | Same chat protocol, explicit `127.0.0.1` or `::1` endpoint; no automatic server launch or model download |
| ElevenLabs | Speech | Official `/v1/text-to-speech/{voice_id}`, selected model and text; MP3 response |
| ElevenLabs | Instrumental music | Official `/v1/music`, explicit prompt/duration and instrumental flag; MP3 response |
| ElevenLabs | Sound effect | Official `/v1/sound-generation`, selected prompt and duration; MP3 response |

Remote speech recognition, vendor polling, native voice-catalog discovery and
dynamic vendor pricing are not implemented by this adapter set. These entries
describe implemented request protocols, not successful live-account tests or
a promise that an account, subscription, model or region supports each option.

Official protocol references inspected on 2026-11-26:

- [Windows CredWriteW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritew)
- [Chat completions](https://developers.openai.com/api/reference/resources/chat)
- [llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
- [ElevenLabs speech](https://elevenlabs.io/docs/api-reference/text-to-speech/convert)
- [ElevenLabs music](https://elevenlabs.io/docs/api-reference/music/compose)
- [ElevenLabs sound effects](https://elevenlabs.io/docs/api-reference/text-to-sound-effects/convert)

These are protocol adapters written for this product. No vendor implementation,
weights or SDK was copied. Model selection, exact weight provenance, licensing,
hardware performance and native-language tool-selection quality remain separate
acceptance steps. A local server is a user-managed trusted component; its own
network behavior must be configured and checked independently.

## Remote call authorization and limits

`Gateway(..., mode='LOCAL_ONLY')` denies remote operations **before opening a
connection or resolving a credential**. A local profile must also be explicitly
enabled; only a loopback numeric IP is accepted. The application does not use
proxy environment variables, follow redirects, or automatically fall back to
a remote service.

`APPROVED_REMOTE` or `HYBRID` additionally requires an unexpired `Authorization`
matching the exact task, account profile, endpoint, operation, selected data
class and optional request checksum. Whole-library uploads are not a permitted
data class. The user supplies a call-count limit, currency and budget, and each
call has an explicit maximum-cost reservation. Unknown costs stop for review.
These are accounting controls over the user-approved bounds; they are not a
guarantee about a vendor's eventual invoice. Actual cost stays **unknown** unless
separately reconciled; token usage is not presented as a billed currency amount.

The usage ledger records request/output hashes, account/model/operation, status,
declared cost bound and numeric usage. It omits credentials, source passages and
provider error bodies. Responses are bounded by bytes/time, checked against the
expected content type, and atomically published to new paths. Unsolicited tool
calls and echoed credential values are rejected. Audio responses still need
the studio's codec/duration/quality checks before final production acceptance.

Every attempted operation ID is recorded before transmission. A network error,
timeout or uncertain result retains its reservation and blocks automatic retry.
The user must reconcile the existing output/charge before authorizing another
operation. No real paid or external provider call is part of the automated tests.

## Verification scope

The automated suite exercises real SQLite workspaces, nested UTF-8 imports,
scoped retrieval, exact Unicode reading-copy export, restart, interrupted create
reconciliation, output tampering, queue substitution, parallel-run locking,
cancel/pause, invalid tools and path boundaries. Provider tests use actual local
HTTP servers for response handling and account routing; remote routing uses a
mock TLS transport with synthetic credentials. They cover local-only no-egress,
wrong account/destination/task/data/operation, redirects, untrusted responses,
secret redaction, request/response limits, declared-budget enforcement and
uncertain-charge retry rejection.

On Windows the credential test creates, reads and deletes only a new synthetic
credential. On other hosts it verifies the vault refuses storage. Local tests
do not establish native Windows/NVDA interaction, actual vendor acceptance,
real local-LLM inference quality or voice/music listening quality.


## Typed local reader, evidence, voice and DSP commands

These commands extend the same reviewed Plan, Scope and assistant JobStore.
Preview and Submit do not enumerate a voice engine, start FFmpeg, open a reader
source or publish an output. Run uses the existing assistant worker and the
shared resource broker where a native engine is needed.

| Tool | Explicit arguments | Existing boundary |
| --- | --- | --- |
| reader.read | edition, revision, first, last; optional topic | ReadingDesk and retained source positions; at most 128 selected positions |
| evidence.search | query; optional field, tag, limit | Authenticated saved notebook quotations and separately labelled notes |
| evidence.read | topic, evidence | One existing saved evidence ID, never a newly approved quotation |
| evidence.export | topic, ordered evidence_ids, name; optional format | Existing research exporter and no-clobber writer; 1–32 selected IDs |
| voice.list | optional exact language, limit | Actually configured VoiceRegistry; owned discovery closes after the result |
| audio.effects | none | Measured installed FFmpeg capabilities plus existing numeric DSP control bounds |
| audio.inspect | path | Existing measured audio information and exact source SHA-256 |
| audio.convert | sources, name; optional format, settings | Existing AudioConversionBatch, resource admission and per-file publication receipts |
| audio.render | text or structured script, with the existing production options | The same ProductionService callback as audio.produce |

Guided commands include `List voices`, `Покажи голоси`, `Audio effects`,
`Покажи аудіоефекти`, `Inspect audio "C:/Audio/source.wav"` and
`Search evidence "railway"`. Exact source IDs, revisions, ranges and selected
evidence IDs use the versioned JSON plan shown by Preview. The reader's `first`
and `last` are 1-based reading positions in the retained document; they do not
assert verified printed page labels.

For conversion, first run audio.inspect under the selected input scope. Review
its actual source hash, then provide the explicit conversion input as
`{"path":"C:/Audio/source.wav","sha256":"<the measured 64-character hash>"}`.
The placeholder is explanatory and is rejected as input. `sources` is an
ordered array of 1–64 such objects, and `name` selects a new child directory
under the reviewed output directory. Optional settings are only the existing
numeric `sample_rate`, `channels`, `start`, `duration`, `bit_depth`, `bitrate`
and typed `effects` objects (`kind`, `parameters`, `enabled`). There is no
arbitrary FFmpeg filter, command, URL, hidden input scan or new renderer.

Source reading and saved evidence are reauthenticated on completed-job reuse.
A changed revision, retained original, topic permission or saved note refuses
reuse; originals and previous outputs remain untouched. Research export creates
a new file only. An interrupted export with unknown publication outcome requires
a fresh explicit output review, rather than inferring ownership from a pathname.
A committed export remains accepted if cancellation arrives afterward.

Conversion uses the existing external conversion manifest as its per-file
authority. An explicit resume can reuse only the exact reviewed source/tool/
settings plan and verified completed output receipts. It does not re-encode
completed files or overwrite unrelated destinations. A late stop preserves
committed items and prevents the next item from starting. The assistant stores
the same plan and receipt references in its existing checkpoint.

Passive backup preserves these typed plans and reading/evidence results without
starting any engine. A restored assistant checkpoint keeps its original data
directory binding and cannot automatically replay output or provider authority.
Review and submit a new plan in the restored library.

The local planner may propose these allowlisted shapes but cannot invent source
revisions, evidence identities, voice IDs or source hashes. Its proposal still
passes the same scoped validator and the user approves Run separately. Voice
inventory is not a pronunciation or quality qualification; audio capabilities
report the actual selected runtime, not a guaranteed hardware resource ceiling.
