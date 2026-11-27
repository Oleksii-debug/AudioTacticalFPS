# Generated audio: backup retention and consumer authority

This addendum corrects the timing distinction in the earlier canonical assistant/provider/backup/progress disposition. Canonical `backup_generated.py` validates a completed receipt/media graph while collecting a backup. The current Studio backup instead preserves recognized cache and rendered files as passive bytes, including older or interrupted cache records. Those are different contracts.

## What current backup actually promises

The unified backup validates exact inventory, SHA-256, size, path/link boundaries, saved and recovery drafts, source bindings and required input assets. Project snapshots additionally relocate the output folder into the newly restored project. Neither operation grants authority to synthesize, start a decoder, play a file or accept a saved journal as a completed Studio result.

This passive behavior is intentional and already covered by positive compatibility tests: a project snapshot may contain a WAV/MIDI/music-metadata trio without its cache receipt, and a complete-library snapshot may retain an old untyped cache marker. Rejecting every such historical record would discard recoverable drafts and bytes. Opening or recovering a Studio project clears its current result; it does not replay a completed operation.

| Consumer | Authority check |
| --- | --- |
| `ChunkCache.get` | On an explicit reuse attempt, validates receipt/key, exact payload SHA/size, actual PCM frames/rate/channels and companion hashes. Invalid files are retained in quarantine and still count against the disk budget. |
| `production._verified_output` | Validates the current completed render. This component adds actual media-container and PCM metadata checks before returning a complete result. |
| `PublicationService._source` | Independently binds the exact typed plan, source, timeline, file graph and actual master PCM before a new publication. |
| Typed audio-batch and saved-playlist backup adapters | Already validate their own completed-output graphs and PCM. This repair does not relax those producers. |
| Historical-envelope adapters | Preserve original old state and manifests passively. They are not current execution or completion authority. |

## Concrete consumer defect and repair

The old completed-render verifier checked hashes, sizes and selected names. A malformed WAV or encoded file could therefore be placed in a self-consistent completion marker and returned as complete without having the expected audio format. The cache validator did not cover this independent completed-render path.

The existing production verifier now keeps an immutable input handle across its checksum and bounded media inspection. It uses the existing PCM reader for every WAV, binds master/stem frames, sample rate, channels and duration to the completed result, and checks bounded signatures for the existing encoded formats. These inspections do not activate a runtime or voice. A hash-consistent format or metadata contradiction raises before admission, quarantine, synthesis or encoding, preserving the suspect bytes for review.

Existing explicit Render behavior for ordinary checksum-damaged outputs remains unchanged: that already authorized path may rebuild a broken output from independently validated cache. The new hard refusal concerns a completion claim whose own hash graph agrees but whose media format or measured metadata contradicts its claimed result.

## Regression population and qualification

The new module has **36 authored cases**: ten resealed wrong-container controls, ten measured-PCM/metadata contradictions, one real replacement attempt between hash and header inspection, four passive old/incomplete-cache backup→verify→restore→rebackup controls, ten controls using actual FFmpeg output formats, and one real-render restored forgery that must refuse before any fresh work.

The two groups using FFmpeg account for eleven cases. The existing pinned `CARR_FFMPEG` supplied by the Windows source and isolated-installed gates is required; no new dependency or download path is introduced. Existing valid completed-output resumption and historical draft/cache portability tests remain unchanged.

At this checkpoint these changes and tests are **not executed**. Container signatures do not establish full decoding or perceptual quality, and synthetic PCM does not qualify real voices, ASR models, languages or NVDA. The paired JSON records exact source owners, the canonical contract, test identities and the precise non-equivalence.

