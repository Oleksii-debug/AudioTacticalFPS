# Canonical ASR optional-cache policy and retained byte bounds

This component reconciles canonical PR196, commit
`a44fac04072389caa40e0d6e799b13797712f797`, through the current historical
backup and ASR recovery producers. It does not copy the old execution service.

## Explicit cache inclusion

The exact canonical worker and worker-history cache layouts are recognized by
`legacy_transcription_schema.is_cache_file`. A historical source manifest that
omits `generated_audio_included` or sets it to false cannot contain these
optional cache entries. Recomputing the manifest SHA-256 does not grant that
permission. Retained original audio, accepted transcript artifacts and ordinary
worker results are separate paths and remain recoverable.

This check applies before restoration across v3 implicit root-sidecars, v3/v4
state lists, v4 root-sidecars and v5 state-plus-reading envelopes. The v3
implicit shape is selected only when an exact canonical ASR jobs/profiles
namespace is present; current v3 reader-only backups and workspace-scoped ASR
paths retain their existing dispatchers.

An explicitly included canonical cache is restored only inside the immutable
historical archive. It does not become a current recognizer cache, runtime
configuration, completed current job or executable authority. Current typed
workspace ASR and library-wide ASR snapshots retain their own cache-exclusion
rules. Current live canonical recovery archives preserve exact historical bytes
under their own inert archive protocol; they do not invent an optional-cache
flag for an earlier manifest.

## Exact size scopes

| Producer / operation | Per source | Aggregate |
| --- | --- | --- |
| Canonical earlier transcription job | 32 GiB | 64 GiB per job |
| Canonical historical archive input inventory | 32 GiB | 64 GiB input bytes, plus separate 512 MiB metadata allowance |
| Generic historical state | Existing path and file rules | Existing 32 GiB default; unchanged |
| Current library-wide supplied-transcription preparation | At most the job bound | Existing 32 GiB across a new current job; unchanged here |
| Older workspace receipts-v1/V5 family | Existing 8 GiB source bound | Existing 32 GiB batch bound; unchanged here |

The historical state-list reader previously ran the generic 32 GiB aggregate
check before applying the canonical typed ASR allowance. That rejected a
permitted 64 GiB historical input set even though the dedicated root-sidecar
path already admitted it. The revised reader accounts for the exact canonical
ASR entries separately and still checks every generic historical member against
the original 32 GiB default. The old portable path bound remains 4096 characters
and 2048 components; typed ASR paths retain their narrower 12-component bound.
Caller-provided whole-archive limits continue to apply.

No historical originals are deleted or split to meet the current execution
limit. A selected old batch larger than the current 32 GiB new-job limit can
still be retained, read, exported, restored and backed up in full. Preparing
current execution remains a separate explicit action governed by the current
service and its existing admission/disk-reservation rules. This component does
not claim execution equivalence for such a batch.

## Qualification

The added suite retains the canonical **128-case policy matrix**:
two worker locations × four suffixes × four cache layouts × four envelope
shapes, plus **one real rehashed-manifest restore refusal**. It additionally
checks four real noncache restore/rebackup shapes, declared per-source/aggregate
boundaries, generic limits, caller limits and disjoint current dispatch.

The existing full earlier-ASR gate declares **422 source + 422 installed
cases** (the existing 268 plus 154 new) and keeps its **53 native checks**.
Its source-wheel and isolated installed module inventories, exact JUnit
function/parameter Counter and native labels remain mandatory. The new suite
is `tests/storage/test_historical_asr_cache_policy.py`.

Large-size tests validate bounded manifest accounting without allocating
64 GiB. They are not throughput, full-size filesystem or private-ASR-model
measurements. Actual execution status must come from the exact frozen gate;
source review and declared populations do not establish PASS.
