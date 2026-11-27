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
