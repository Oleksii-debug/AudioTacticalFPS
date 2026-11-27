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

## Required integration acceptance

The reused implementation includes real subprocess JSONL framing regressions with
explicitly synthetic PCM, persisted configuration, actual shared WAV output,
changed assets, cancellation, no overwrite, budget failures and worker cleanup.
The new composition requires fresh exact-head execution; donor results do not
qualify it. Tests use the actual shared runtime and its exact settings class. They also
verify isolated bytecode-free children, a copied environment without inherited
report-file destinations, and cleanup of the owned child process tree. Platform
acceptance must execute the same suites and native controls on Windows; Linux
results are not substituted for that gate.

These results establish software integration. They do not establish acoustic
pronunciation, similarity of cloned voices, the quality of any installed neural
model, support for all 135 languages, or a completed manual NVDA review. Installed
language resources and a declared compatible profile are reported separately
from those acceptance results.

The reused provider slice was reviewed at historical commit
`97ac6746857f3857774c4b5ff117d1fa4f8920db`, which identifies earlier AF5 source
`ee7baa6644ecf0b73db12dff30c41d8be9069060`. Private worker files and resources are
not part of this repository. `DORDIAI_VOICES_CONTRACT.md` records the historical
upstream protocol review. `DORDIAI_SPEECH_HANDOFF.md` describes the current shared
provider contract; no separate registry patch or replacement speech package is
needed.
