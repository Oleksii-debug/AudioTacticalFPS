# Optional dordiai provider in the canonical speech and Studio services

Status: the canonical public adapter and ownership reconciliation are prepared
for integrated acceptance. The earlier component results below are historical;
the new combined source and installed/native gates remain **NOT_RUN** until
their exact commit completes CI. Actual private neural inference, Windows
private-worker execution, pronunciation, physical playback and NVDA acceptance
also remain **NOT_RUN** for this integration.
An installed voice inventory is evidence of resources, not voice quality or
qualification of the complete selected 135-language catalog.

## One speech and production architecture

`speech/dordiai.py::DordiaiProvider` implements the provider interface consumed
by `speech/runtime.py::VoiceRegistry`. It returns the existing canonical
`Voice` type. The registry retains `SpeechSettings`, measured `SpeechResult`,
native comtypes SAPI, eSpeak, Flite, Piper and reviewed generic helpers. It alone
joins, checks and atomically publishes the final PCM WAV.

`integrations/dordiai_speech.py::DordiaiSpeechProvider` is a canonical settings
facade over that same owned provider, not a second transport or renderer. Its
installation-qualified IDs preserve canonical selection semantics. Direct
utterance calls use the existing create-only speech output boundary; the
registry still owns the final combined WAV.

`runtime_settings.voice_registry(state_root)` includes one optional provider
only when exactly one saved declaration for that library is explicitly enabled. The
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

`state/audio/engines.json` remains schema 1 and gains the canonical optional
`dordiai` declaration. An omitted declaration reads as `None` without rewriting
the file. Its fields are `enabled`, `runtime_directory`, `python_executable` and
`expected_worker_sha256`; disabled or absent settings never probe a runtime.
New explicit Connect actions write this existing engine-settings store.

The historical root-level `dordiai-voices.json` remains readable with its
65,536-byte limit and exact fields `version`, `enabled`, `runtime_directory`,
`python_executable` and `expected_worker_sha256`. Its old profile/language voice
IDs remain unchanged. A new canonical connection requires explicitly disabling
the historical connection first; saved jobs are not silently retargeted to new
IDs. If both declarations are enabled, discovery fails before starting either
provider. Disable signals only this library and updates the existing declarations.

Both files contain declarations, not secrets, voice models or an installation. Absolute POSIX and Windows declarations can be validated
as data; an enabled runtime must use a path absolute on the current host before
the bridge can open it. Relative paths, network paths, device paths, URLs,
control characters and traversal are rejected.

`speech/dordiai_settings.py::validate_settings_bytes` and
`restored_settings_bytes` are pure data operations. The historical root-file
restore preserves declarations with `enabled=False`, without looking up,
probing or executing a runtime. The common backup integration separately owns
the canonical engine-settings inclusion and scope. The current common-format
backup retains canonical engines.json bytes, including its enabled declaration,
without constructing a registry or probing any referenced path. It does not
rewrite that declaration as disabled. A stored reference does not establish
that its installation exists or is qualified on the destination host; subsequent
explicit voice discovery and selection use the current runtime checks. Ambient enable environment variables are
never used to enable a restored or selected library.

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
probe and state/writer helpers. Inventory is limited to 100,000 entries and
64 GiB of hashed bytes. Incremental directory enumeration checks cancellation
and the budget for every encountered entry, including ignored root files and
empty directories. Unreadable directories fail the fingerprint; they are not
silently omitted. Every hash chunk also checks cancellation. File and directory
identities are rechecked to reject changes during inspection. No stat-only
memoization makes preserved timestamps sufficient for reuse.

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

## Worker and registry ownership

Each canonical provider retains one adapter and serializes its own operations.
Successful requests can reuse that provider's warm process. A changed content
fingerprint refreshes the inventory and stops that exact old worker before a
new request can use changed model bytes. A different library's registry and
workers remain separate. Historical profile/language IDs and their root-level
declaration format are unchanged. Canonical installation-qualified IDs include
the selected installation identity; enabling a different connection never
rewrites earlier jobs or claims that two installations are interchangeable.

The transport retains both an unfinished metadata probe and a warm worker
until their native process and reader/writer threads confirm termination.
A failed cleanup cannot replace the retained handle with a new child. Closing
attempts both retained owners, even if one raises. Child execution uses the
canonical speech boundary's explicit host-environment allowance together with
offline and no-bytecode flags; unrelated ambient report/cache settings are not
inherited.

`VoiceRegistry.close()` returns `True` only when all its providers have
confirmed cleanup. An exception, `KeyboardInterrupt`, or explicit `False`
from a provider leaves that exact owner retained, records a notice and registers
one shutdown retry. Failed discovery preserves its original error; adding a
provider does not close unrelated active providers when the new one fails.
Both cancellable fingerprint and expected-fingerprint synthesis capabilities
are explicit, so older injected registry interfaces can keep their existing
call signatures while the real registry retains all identity checks.

Studio closes registries it created, retains an unconfirmed owned registry for
retry, and does not close a registry borrowed from another view. Closing during
cold discovery cancels the operation and prevents late adoption. Explicit
progress-voice activation owns its registry; copies share that ownership until
the last selection closes, when bounded cleanup runs away from the GUI thread.
A directly supplied progress registry remains borrowed.

The live reader adapter closes each registry it creates for discovery or
rendering on success and failure. A cleanup notice cannot change an already
committed WAV into a failed render. Unconfirmed real-registry cleanup still has
its retained owner and shutdown retry. The reader does not take ownership of
another window's registry or start a second speech implementation.

These are resource and process boundaries around deliberately selected local
code. They are not an operating-system sandbox for a malicious installation.

## Historical component acceptance recorded on 2026-11-27

The following overlapping batches were recorded for earlier canonical component
work before the current ownership and integration reconciliation. They are
retained as historical evidence, must not be added together, and do not qualify
the newly composed provider, transport, reader, Studio or installed wheel:

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

The reconciled regression selection also includes
`tests/integrations/test_dordiai_convergence.py`,
`tests/integrations/test_dordiai_ownership.py`,
`tests/integrations/test_dordiai_probe_cancellation.py`, and
`tests/speech/test_progress_speech.py`. These preserve the actual synthetic
child-process checks for warm reuse, content-change restart, scoped cold
cancellation, failed cleanup retaining the exact process, explicit
`close() is False`, last-owner release, service-owned versus borrowed registries,
and live-reader completion/error cleanup. The transport tests retain real
descendant termination and environment isolation. Their fixture uses the
retained historical saved connection. Additional suites
`tests/integrations/test_dordiai_speech.py`,
`tests/integrations/test_dordiai_settings.py` and
`tests/integrations/test_dordiai_canonical_convergence.py` exercise the canonical
facade, explicit settings transition, disabled references, later rendering after
the discovery deadline, real caller/provider cold-probe cancellation and
concurrent cancellation isolation. Both cohorts use the same checked-in
synthetic peer and actual selected product imports; no environment override
substitutes a speech package. The current transport, probe-cancellation and
owned-registry cases remain required.

The reconstructed convergence is **NOT_RUN**. Neither the external candidate
nor these fixture and stop-forwarding corrections inherit an older source,
installed or Windows pass count.

Native Windows acceptance must run against the eventual integrated commit.
The preceding old-stack Windows run `45918580963` failed on an obsolete FFmpeg
graph option in that older stack. It does not establish acceptance or failure
of the canonical DSP implementation, which uses the current graph option.
The current adapter's synthetic actor is explicitly not a neural or acoustic
substitute for the separately installed engine.
