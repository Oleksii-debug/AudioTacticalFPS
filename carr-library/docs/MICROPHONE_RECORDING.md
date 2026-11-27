# Bounded microphone capture in the existing Audio Studio

This selective integration is based on product commit df12b7d1da1092c14406ee329aa263ba95541720. It keeps the current production model, exact-frame renderer, LGPL runtime, resource coordinator and separate audio snapshot. Native UI integration and its actual keyboard probe are the next checkpoint; this source checkpoint does not claim that UI is complete.

## Reuse record

| Component | Exact existing source | Adaptation |
|---|---|---|
| Recorder and receipt validation | PR149 commit 906aa1a95a8f40ef119682d2d5c805c017c327f4; audio/recording.py blob f921c81cf836669191730626e4979890c0fc0e35 | Existing audio output folder, assets/ and recordings/, root runtime and project lock |
| Resource admission | PR112 commit 4d9804f03159fbdb532839abdef0ca3b1910d685; recording blob 41c252f0b3af64894a7c8717a4ff2e34ae447873 | Existing ResourceBroker.for_app policy and retained reservation |
| Capture values, process runner, cleanup, Windows Job Object | PR99 commit 731cf6c964c600fdb8ed71d751d84251aa8dc57b; blobs a94b63e644f800b80b0cb507d52377c8416052bb, dba0922edd0ba275e28243837208c6d16ae24481, f73a023a1078cce32138bfe0d8ba88076f40da4c, d23359edc2a069b4b303f14b8ecd8982c7655757 | Current durable_state and publish_noreplace; retained Linux leader identity |
| Linux group observation | Root process correction 93f2cb1bb3a5d80889da3aee81183ce2e78c75a9, included by df12 | Existing _poll_process and _wait_linux_group; capture does not reap on unconfirmed cleanup |
| Existing recording regressions | PR149 test blob 8ae6cd0c551958aeb868884e3ded9c49087d810f | Actual root service, audio snapshot and renderer roundtrip |
| Existing capture ownership regressions | PR99 test blob e900b7cf28cc1d20a8880636eab7be0fd7916ce0 | Root Linux shutdown helper; exact Windows handle ownership retained |

Canonical Project/Cue/Scene, speech engines, state/studio storage and arbitrary timed clips are not activated by this adapter. Capture does not silently translate unsupported timelines or engines.

## Ownership and publication

Only an explicitly selected current Windows DirectShow alternative device identity can start production capture. Refresh is discovery only; a single device is never selected automatically. The fixed input grammar cannot supply arbitrary FFmpeg expressions or protocols. FFmpeg identity is checked across discovery and capture. Stop sends only the fixed quit command and saves a verified PCM16 WAV. Cancel discards unfinished staging. A cancellation arriving after publication cannot erase the original.

Capture reserves one CPU slot, a conservative 512 MiB memory allowance, its bounded output plus disk reserve, and the existing microphone device token. The shared resource lease, project lock, staging directory and audio admission remain owned if the native process family has not confirmed shutdown. The retained cleanup owner retries only that exact process family. Linux leaves its leader waitable until the inherited group is quiet; it never reuses a reaped PID. A deliberately escaped descendant is outside the inherited-group guarantee. Windows creates the child inside its Job Object with an explicit standard-handle list.

Both progress tones and speech are stopped before capture. The existing progress broker waits for content and notification silence. WavePlayer shares atomic admission with capture; active capture and unresolved cleanup produce different refusals. Simultaneous monitoring or dubbing is not accepted by this scope. The per-allocation FFmpeg limit and broker memory declaration are resource controls, not a whole-process memory sandbox.

New originals are assets/<uuid>.wav and their closed, hash-bound receipts are recordings/<uuid>.json inside the selected existing external audio project. Receipt failure keeps the original visible for explicit recovery. No restored receipt grants microphone or runtime authority. The existing separate audio snapshot includes originals and receipts even when rendered audio and cache are excluded. Its existing total byte/file ceilings apply to these inputs. Restore revalidates the exact receipt, WAV format, frame count and digest before publication. No general library-backup or canonical state tree is introduced.

## Evidence requirements

The isolated workflow requires actual FFmpeg synthetic capture on Linux and Windows, actual owned process-stop failure/recovery, no-tone/no-speech admission, delete-original snapshot/restore/render and re-signed receipt rejection. It also reruns existing playback, progress and audio snapshot contracts. The test-only source is a fixed sine generator; production exposes no synthetic-source switch. OMP_THREAD_LIMIT=1 is test-only and does not change caller resource policy.

This initial checkpoint is source-reviewed and NOT_RUN. Actual source and native results must be recorded from their exact run. A synthetic FFmpeg/keyboard PASS does not qualify a physical microphone, audio-device quality, listening, NVDA or simultaneous monitoring. Those remain NOT_RUN until measured on a real device.
