# Local dordiai-voices in the shared reader and Studio

Open **Local audio, voice and music engines**, choose **Connect installed
dordiai-voices**, and select the existing `kx47Runtime` directory. Confirm the
local executable choice. The application checks the reviewed worker identity
and local installation structure before saving the selection. It does not
download voices, import private engine source into this package, or contact a
remote synthesis service.

Reopen the Voice reader or Audio Studio to enumerate the installed voice and
language pairs in the background. Their single shared `VoiceRegistry` uses the
existing reviewed `DordiaiVoicesAdapter`; generic JSON voice helpers remain a
different interface. **Disable dordiai-voices** removes the saved selection.
Installed files and completed recordings remain on disk.

## Persistence and identity

Schema-1 `state/audio/engines.json` gains an optional `dordiai` field. Older
seven-field files remain readable and are not rewritten by loading them.
The default is `null`. Disabled configurations do not inspect a private runtime
or start a worker. Saved fields are an explicit enable flag, local runtime path,
and optional reviewed interpreter/worker identity. Credentials, reference audio,
model data, embeddings and voice samples are not stored in this file.

Changing a model or voice resource changes the shared synthesis fingerprint.
The adapter's bounded PCM stream is framed and published by the existing shared
speech path; cancellation and output limits preserve existing files and discard
partial output. Registry disposal releases owned workers, including failed or
superseded provider instances. Reader closure waits for its bounded cancellation
before removing temporary audio.

Stop also cancels initial voice discovery. Different local installations keep
distinct voice identities even when their profile and language names match.
The utterance time budget includes asset verification and cold discovery;
final stream cleanup and deadline checks happen before the completed WAV is
published. Model-byte fingerprints and the existing shared registry remain the
authority for cache reuse. These controls do not add a second speech engine.

## What has been executed

The primary103 source initially failed two actual shared-runtime integration
tests: content changes were missing from the registry fingerprint, and explicit
dordiai discovery was unavailable. After applying the existing provider contract
and desktop wiring, seven strict Linux suites passed **93 cases with no failures
or skips**. These exercise real subprocess JSONL framing with explicitly
synthetic PCM, persisted configuration, actual shared WAV output, changed assets,
cancellation, no overwrite, budget failures, and worker cleanup. The source gate
requires the same three adapter/configuration suites on Windows.

These results establish software integration. They do not establish acoustic
pronunciation, similarity of cloned voices, the quality of any installed neural
model, support for all 135 languages, or a completed manual NVDA review. Installed
language resources and a declared compatible profile are reported separately
from those acceptance results.

The reused public adapter and provider are pinned to AF5 source commit
`ee7baa6644ecf0b73db12dff30c41d8be9069060`; private worker files and resources are
not part of this repository. `DORDIAI_VOICES_CONTRACT.md` records the reviewed
protocol. `DORDIAI_SPEECH_HANDOFF.md` retains the historical provider integration
notes; its proposed registry changes are already applied here.
