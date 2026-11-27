# Typed local assistant tools

The existing approved-plan service now calls seven additional bounded adapters.
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

No new reader, audio engine, conversion process stack, research store, provider
gateway or execution journal is introduced. Tool JSON cannot select executable
paths, models, credentials, arbitrary filters, network destinations or devices.
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
the complete report as a separately hashed output artifact. The saved assistant
checkpoint is the only completion authority; no adapter reports a completed
audio render merely from a proposal or waveform inspection.

Complete backup preserves these jobs through the existing assistant producer.
The added reader/evidence results are checked against the backup database's
exact historical workspace/edition/revision/asset identity. Reading text must
match the sealed page and metadata. A restored plan retains its original paths
and remains Attention until the ordinary new approval flow.

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
