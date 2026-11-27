# Shared Reader and Studio voice contract

The canonical implementation uses `integrations/dordiai_speech.py` as a provider
for the existing `speech.runtime.VoiceRegistry`. It delegates the reviewed local
JSONL protocol to `DordiaiVoicesAdapter`; it does not introduce another document
renderer, audio cache, project schema or DSP implementation.

This slice was adopted from the historical reviewed implementation at
`97ac6746857f3857774c4b5ff117d1fa4f8920db`. Older instructions to apply a separate
registry patch or stage another speech package are superseded. Tests import the
actual canonical `VoiceRegistry` and its exact `SpeechSettings` class.

## Configuration and registration

`runtime_settings.py` accepts the existing schema-1 configuration, including the
historical seven-field form. It normalizes the optional `dordiai` field to `None`
in memory without rewriting an existing file. The field defaults to disabled.
An explicit local configuration contains `enabled`, an absolute
`runtime_directory`, and optionally `python_executable` and a reviewed lowercase
`expected_worker_sha256`. Paths and declarations are validated without probing a
disabled installation. No secret, reference recording, embedding or model is
saved in this configuration.

`runtime_settings.voice_registry(..., cancelled=...)` uses the existing native
providers and adds the explicitly enabled Dordiai provider through
`install_dordiai_provider`. A missing or disabled configuration neither inspects
private files nor starts a worker. Provider discovery accepts cancellation; an
interruption is not converted into a successful empty voice list. The owning
registry closes all its providers, including one that failed discovery.

The engine settings window provides explicit Connect and Disable actions.
Connection verifies the selected local runtime structure and known worker
identity; actual inventory and inference remain separate operations. Reset
removes the local configuration, not installed engine files. Users should follow
[DORDIAI_DESKTOP.md](DORDIAI_DESKTOP.md).

## Rendering and process ownership

Every structurally ready profile/language pair becomes an installation-scoped
voice ID. Only rate, volume and pitch are advertised. Rate is mapped through
`2 ** (rate / 10)`; the standard Dordiai pitch is 50. Unsupported punctuation,
word timing, emotion, phonemes and pronunciation RPCs are not advertised.

The provider writes validated mono 24 kHz 16-bit PCM into the shared atomic WAV
publication path. Byte limits, cancellation and the whole utterance deadline
also cover resource fingerprinting and cold discovery. Terminal stream cleanup
precedes publication. A malformed, changed, timed-out or cancelled result does
not publish partial output or overwrite an existing file.

Both private Python launch vectors use `-I -B`. The transport reuses the shared
local speech environment and process-tree termination boundary, with bounded
request/reply queues and no shell. Private diagnostics are not copied into user
errors. Offline environment settings are not an operating-system network sandbox.
The selected local engine remains code explicitly approved to run with the
user's account permissions.

`VoiceRegistry.fingerprint` includes the provider's content identity: selected
voice/language, actual bytes under installed `models` and `python`, runtime
modules/configuration, manifest and bridge modules. Every boundary rehashes
bytes with cancellation checks; unchanged timestamps do not excuse a changed
model. Changed assets invalidate cache identity and replace a stale warm worker.
This is a reuse identity and compatibility check, not proof of voice quality.

## Ownership across native windows

`ProductionService.discover_voices(cancelled=...)` performs cancellable cold
discovery on the existing caller worker. `close()` cancels that discovery and
closes a registry created by the service; an explicitly supplied registry remains
borrowed and usable by its other owner. Studio and Assistant queue service
cleanup after their cancelled operation without blocking the UI. CLI entry
points close their service in `finally`. A language inventory closes its
temporary registry after taking the immutable metadata snapshot.

Reader owns the registries it discovers, retires superseded registries on its
worker, and closes remaining owners during final cleanup. Explicit progress
voice activation owns its own registry. Copies of that selection share a small
ownership lease; closing one view does not close another view's voice. The last
selection releases the engine in the background after in-flight synthesis exits.
Directly supplied progress registries remain borrowed. No progress preference,
quiet-policy boundary, terminal notification deadline or audio-muting rule is
changed by this ownership mechanism.

When operating-system termination fails, the adapter and registry retain the
exact failed child/provider handles for a retry. A failed registry also retains
a standard interpreter-shutdown cleanup callback; a window disappearing does
not silently discard its last cleanup reference. The lifecycle regression suite
uses an actual warm synthetic worker to test failure followed by successful
cleanup of that same child.

## Verification boundaries

The integration suites use a small, explicitly synthetic JSONL worker and PCM
fixture. They exercise actual child processes, cancellation, framing, shared
registry rendering, exact WAV bytes, settings persistence, no-clobber output and
worker cleanup. They do not import a substitute speech runtime or download an
engine. The private installed runtime, real acoustic output, language-quality
qualification and human NVDA checks still require their own acceptance evidence.
The observed upstream protocol and historical review hashes are documented in
[DORDIAI_VOICES_CONTRACT.md](DORDIAI_VOICES_CONTRACT.md).
