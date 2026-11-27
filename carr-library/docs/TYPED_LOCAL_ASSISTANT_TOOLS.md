# Typed local assistant tools

The existing approved-plan service now calls eight additional bounded adapters.
The plan schema stays `carr-assistant-plan/1`. Existing tools, original
permissions and checkpoint behavior remain unchanged. A model proposal still
needs a separate review and Run action.

| Tool | Required arguments | Actual current service | Result and authority |
| --- | --- | --- | --- |
| `reader.page` | edition, exact revision, 1-based page; optional topic | `ReadingDesk.page` | Exact raw text/location/metadata and its verification label. No quotation approval or search selection. A changed active revision fails. |
| `evidence.search` | query; optional field, tag, limit, include_archived | `LibraryApp.search_notebook` | Bounded authenticated evidence identities, source bindings, counts and explicit truncation. Existing Copy/Open still owns quotations. |
| `document.convert` | selected input path, new .txt name | `ConversionService.extract` with the current application `ResourceBroker` | New text and complete provenance file, original unchanged, both artifacts checked by the assistant journal. |
| `voice.list` | optional limit 1–256 | current `LocalProduction.voices` | Local IDs, labels and actual declared languages; no synthesis or language-quality claim. |
| `audio.plan` | same text-or-typed-script arguments as `audio.produce` | current `LocalProduction.plan` | Typed production proposal only; no render or current draft mutation. |
| `audio.effects` | exact script, target role or null master, typed effects | current `RoleEffectEditor` | Source-preserving script proposal. Existing role/master restrictions, sixteen-effect bound and optional master dry/wet validation apply. |
| `player.inspect` | selected PCM/RF64 path | current `wav_info`, retained reader and SHA256 | Audio metadata and content identity only; no playback device starts. |
| `audio.transcribe` | one scoped path, new export directory name, exact runtime manifest path/SHA256, all seventeen settings | current `TranscriptionService`, supplied worker, broker and `export_results` | Durable prepared job and independent transcript export. Exact source/settings/model/manifest/receipt bindings; completed resume verifies existing results without starting inference. |

No new reader, audio engine, conversion process stack, research store, provider
gateway or execution journal is introduced. Transcription runtime selection is
constrained to an explicit reviewed manifest and its admitted local model,
language, device and task declarations. Arbitrary executable fields, credentials,
filters and network destinations remain invalid.
The basic conversion route admits the already installed readers. Office,
Pandoc and raster-OCR selection remains explicit in the existing conversion
workflow; those runtime paths are not imported from plans.

## Permissions and budgets

Reading and evidence steps require the existing selected workspace. Reader
requests pin the revision in the approved plan; no newer edition is silently
selected. Input files remain inside the existing selected input roots.
Conversion needs the selected external output directory, at least two output
files and more than 16 MiB of its explicit byte budget for the bounded provenance
report. Output names remain new-only; a crash after publication but before a
checkpoint does not authorize adopting arbitrary same-named files. Such a
partial journal requires review through the existing error/Resume flow.

Each new descriptive result is bounded by the smaller of 8 MiB and the approved
scope byte budget. Current reader, search, parser, native-worker and resource
limits also apply. Conversion returns a compact receipt summary while preserving
the complete report as a separately hashed output artifact. Completion requires the current service's own accepted result and the saved
assistant artifact checks. A proposal or waveform inspection cannot report a
completed render. Transcription additionally preserves the exact prepared job
and export receipt, including the input and output content hashes.

Complete backup preserves these jobs through the existing assistant producer.
The added reader/evidence results are checked against the backup database's
exact historical workspace/edition/revision/asset identity. Reading text must
match the sealed page and metadata. A restored plan retains its original paths
and remains Attention until the ordinary new approval flow.

## Explicit transcription review

See [the exact plan and recovery contract](ASSISTANT_TRANSCRIPTION.md).
The existing native assistant Help, CLI `--help`, typed JSON plan and inert
template flows expose this capability. The local model may propose it only
from supplied manifest/hash/settings; ordinary Preview/Submit still revalidates
the current runtime, and a separate Run remains required.

The current service accepts a workflow-preallocated job ID only as a new name.
Saved assistant intent precedes prepare, definition pinning precedes launch, and
export intent precedes publication. Resume can acknowledge the same prepared
job or a previously published exact export; it cannot adopt a different job,
overwrite an output, or reactivate restored runtime paths.

Required suite: `tests/assistant/test_transcription_tool.py`, added explicitly
to source/installed fast contracts, the main Windows source/full bundle/Linux
inventories and the audio contract gate. The full bundle also checks the two
adapter modules' installed origins. New cases are **NOT_RUN** until an exact
composed CI run completes. Their harmless CLI peer is not private Whisper
inference or model/language qualification.

## Reuse and evidence

Implementation authority is the current integrated source at
`db15b173e5d5bec5919b7072a57ec248c7b96c06`:
`reader/reading_desk.py`, `research/notebook_search.py`,
`conversion/service.py`, `assistant/production.py`,
`audio/effect_editing.py` and `audio/ffmpeg.py`.

Required focused suite: `tests/assistant/test_typed_local_tools.py`.
It exercises actual SQLite/retained originals, evidence, the isolated UTF-8
conversion worker and broker, production parsing without synthesis, real PCM
inspection, complete backup/restore, source tampering, cancellation and budget
failure. It also rejects cross-scope inputs, executable/device fields, coercions
and forbidden master effects.

This source checkpoint was prepared while the executor was unavailable.
**New tests are NOT_RUN; no actual Windows, model, voice, audio-device or manual
NVDA qualification is claimed.** Existing unrelated native PASS does not qualify
this new source. Run this suite and the existing assistant/backup/CLI gates on the
exact composed tree before reporting functional acceptance.
