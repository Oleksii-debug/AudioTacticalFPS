# Optional dordiai provider in the canonical speech and Studio services

Status: implemented public adapter with synthetic protocol acceptance. Actual
private neural inference, Windows private-worker execution, pronunciation,
physical playback and NVDA acceptance remain **NOT_RUN** for this integration.
An installed voice inventory is evidence of resources, not voice quality or
qualification of the complete selected 135-language catalog.

## One speech and production architecture

`speech/dordiai.py::DordiaiProvider` implements the provider interface consumed
by `speech/runtime.py::VoiceRegistry`. It returns the existing canonical
`Voice` type. The registry retains `SpeechSettings`, measured `SpeechResult`,
native comtypes SAPI, eSpeak, Flite, Piper and reviewed generic helpers. It alone
joins, checks and atomically publishes the final PCM WAV.

`runtime_settings.voice_registry(state_root)` includes the optional provider
only when the declaration for `state_root.parent` is explicitly enabled. The
desktop passes the selected library's `root/state`. The existing
`studio/render.py::ProductionService`, JSON projects, durable jobs, speaker
stems and production exports use that same registry. No previous `speech/local.py`
renderer or SQLite Studio implementation has been introduced into this stack.

The reading controller's `speech/live_adapter.py` is a thin parameter adapter
to these services. Its ratio speed maps to the nearest supported native rate
with `round(10 * log2(ratio))`: 0.5 maps to -10, 1 to 0, and 2 to 10. The
canonical dordiai provider converts that rate back with `2 ** (rate / 10)`.
Volume maps from 0–1 to integer 0–100. Neutral pitch remains unspecified;
explicit relative pitch maps to the canonical 0–99 native control. This does
not create an additional renderer or imply identical acoustic behavior across
different voice engines. An explicit requested language must match the chosen
profile; the adapter does not substitute a different language.

## Native setup and disable

Open **Audio Studio → Local voice and music engines → Connect or disable
dordiai-voices**. Select the separately installed `kx47Runtime` directory, its
private interpreter (or the displayed runtime-local default), and the SHA256
of the worker whose protocol was reviewed. Connect validates the local paths
and worker bytes and saves the declaration. It does not execute the metadata
probe, load a model or download anything.

Reload the reader's voices or reopen Studio to enumerate the actual selected
installation. The registry exposes one stable voice ID for each compatible
installed profile/language pair. Missing resources are omitted; unavailable
runtime errors remain visible. Voices use canonical rate, volume and pitch
capabilities. Neutral delivery and measured utterance duration do not imply
SSML, phoneme input, emotional acting, word timing or verified pronunciation.

Disable signals active operations belonging to this library before saving the
disabled preference. The operation registration spans initial identity hashes,
worker activity and final publication, so the signal also applies between
those phases. A failed preference write is reported; it is not reported as a
persisted disable. Operations in other libraries are not stopped. Resetting
local engine overrides also disables this connection.

## Saved declarations and restore

The root-level `dordiai-voices.json` file has a 65,536-byte limit and exact
fields `version`, `enabled`, `runtime_directory`, `python_executable` and
`expected_worker_sha256`. It contains declarations, not secrets, voice models
or an installation. Absolute POSIX and Windows declarations can be validated
as data; an enabled runtime must use a path absolute on the current host before
the bridge can open it. Relative paths, network paths, device paths, URLs,
control characters and traversal are rejected.

`speech/dordiai_settings.py::validate_settings_bytes` and
`restored_settings_bytes` are pure data operations. Restore preserves the
declarations with `enabled=False`, without looking up, probing or executing a
declared runtime. The backup integration owns the inclusion and scope rules.
A user must intentionally connect the installation again. Ambient enable
environment variables are not used to enable a restored or selected library.

## Identity, cancellation and resource bounds

The public bridge is reused from PR117, reviewed head
`18be07858bb5eedcb4295c4e191720619f650d4a`. Its two public files are
`integrations/dordiai_voices.py` and `integrations/_dordiai_probe.py`. No private
worker implementation, model, weight, voice sample or private interpreter is
included in this repository or installed automatically.

The provider's fingerprint hashes the actual interpreter executable, reviewed
worker and runtime helper files, model tree, runtime-local Python tree, optional
runtime manifest and the complete public adapter dependencies. Those include
the provider, registry, process boundary, declaration validator, public bridge,
probe and state/writer helpers. Inventory is limited to 100,000 assets and
64 GiB of hashed bytes. Every hash chunk checks cancellation. File and
directory identities are rechecked to reject changes during inspection. No
stat-only memoization makes preserved timestamps sufficient for reuse.

When an external interpreter is selected, the fingerprint does **not** seal
every package in its external global installation. A runtime-local Python tree
is hashed when present. Opaque third-party SAPI DLL/model identity remains a
separate limitation of SAPI token discovery, not a claim this adapter resolves.

Discovery is bounded by the canonical registry's overall deadline (60 seconds
by default); the public metadata probe is independently bounded. A provider
may expose at most 4,096 profile/language pairs. The canonical text splitter
limits worker requests to 2,400 characters and preserves the exact original
character stream. Whitespace-only chunks are not synthesized.

Each synthesis has one deadline across fingerprinting, all chunks, PCM checks
and final publication. The dordiai bridge emits 24 kHz mono signed 16-bit PCM.
The provider checks its live byte budget before accepting each block, and the
registry checks the aggregate WAV byte budget and exact frames. A cancelled,
oversized, malformed, empty or changed-runtime result is not published. The
worker's private stage is cleaned up and an existing destination is never
overwritten.

`ProductionService` binds speech to the exact voice fingerprint used in its
cache key, verifies cached speech against the current fingerprint, and retains
the existing receipt-before-next-step-cancellation behavior for completed
valid audio. Each production job has its own immutable output directory.
Changing a model invalidates reused speech while preserving earlier outputs.
Production nodes also include actual FFmpeg/FFprobe bytes and the canonical
audio/production implementation bytes, not only a version banner.

These are resource and process boundaries around deliberately selected local
code. They are not an operating-system sandbox for a malicious installation.

## Measured acceptance on 2026-11-27

The following are overlapping measured batches and must not be added into a
single invented total:

| Batch | Observed result | Meaning |
| --- | --- | --- |
| Public protocol plus initial canonical registry integration | 74 passed, 0 skipped, 14.21 s | Real subprocess framing and synthetic PCM; real FFmpeg production/cache path |
| Canonical registry, production, cache identity, music scheduling, fsync, system-host and native-SAPI protocol regressions | 79 passed, 0 skipped, 46.07 s | Canonical production preserved; protocol/system-host fixtures are not native SAPI execution |
| Final registry/Disable, executable lookup, cache identity and local speech regressions | 64 passed, 0 skipped, 1 deselected, 13.44 s | Actual installed Flite PCM plus explicit synthetic private-worker fixtures; the single Windows-only SAPI test was deliberately not run on Linux |

Relevant test sources are `tests/integrations/test_dordiai_voices.py`,
`tests/integrations/test_dordiai_registry.py`,
`tests/speech/test_discovery_boundaries.py`,
`tests/speech/test_runtime.py`, `tests/studio/test_runtime_identity.py`, and the
canonical production/native-protocol/fsync/system-host suites. The new provider
tests cover exact text and controls, same-size model changes, cancellable
fingerprinting, real worker timeout, streamed output budget, publication
invalidation, library-scoped disable and reuse through the actual canonical
production service.

Native Windows acceptance must run against the eventual integrated commit.
The preceding old-stack Windows run `45918580963` failed on an obsolete FFmpeg
graph option in that older stack. It does not establish acceptance or failure
of the canonical DSP implementation, which uses the current graph option.
The current adapter's synthetic actor is explicitly not a neural or acoustic
substitute for the separately installed engine.
