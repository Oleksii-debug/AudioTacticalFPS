# Windows 687: sealed-bundle and current capture receipts

The complete-bundle reader probe failed before attempting playback because its process did not receive FFmpeg or FFprobe paths. The current virtual-input probe separately failed while opening the explicitly selected DirectShow device. These are distinct failures.

## Exact evidence

Application commit: `68777f9f36b142ed1ce847ee47d82e20d0bdd132`.
Ordinary run: [46075232185](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46075232185).
Completed read-only receipt reader: [46080720722 / job 140778804584](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46080720722/job/140778804584).

The reader verified complete immutable ZIP hashes before reading any member. The two failure JSON files and three endpoint JSON files here preserve their original bytes, including CRLF where present. Their numbered base64 chunks were reconstructed from the completed reader log and checked against their original member lengths and SHA256 values. See `provenance.json` for the exact artifacts, reader source, hashes and qualification limits.

## Sealed-bundle failure

`bundle-reader-player.json` reports FAILED after 12 successful provenance/device checks. Its traceback reaches the FFmpeg/FFprobe path construction and raises `TypeError: expected str, bytes or os.PathLike object, not NoneType`. Playback has not started at this failure point.

The existing probe resolves unprovided tool arguments through `resolve_active_audio_tool`. The complete-bundle invocation supplied neither argument. Its earlier audio activation occurred in another interpreter, so it did not establish an active audio handle in this probe process. The probe's native-DLL activation does not activate the separate audio-tool registry.

The prepared workflow correction obtains both executable paths from the already reviewed bundled audio activation, validates each through the current executable hash resolver while that handle is alive, closes the handle, and passes both paths through the probe's existing arguments. The recording probe reuses the same verified FFmpeg path. Only two workflow hunks change; application, runtime, probe, device-selection, assertions and watchdogs remain unchanged. This correction is source-reviewed and **NOT_RUN**.

Earlier sealed receipts include Book 72, protected voice 48, reader controls 108, appearance 65, output selection 47 and synthetic recording controls 65 checks with their recorded PASS status. `bundle-checkpoints.json` preserves the other parsed receipt statuses and counts without manufacturing a status for records that omit one. The later reader-navigation, EPUB/DAISY, monitor, translation and final bundle aggregate receipts listed in `provenance.json` are absent. This evidence does not qualify the whole sealed bundle.

## Current capture result

`native-recording-device.json` records the exact selected device name `CABLE Output (VB-Audio Virtual Cable)` and opaque identity `1cd152d9f269e8b67842046c12f5a209096e4315b5b9b5a38c6b89b752ca2814`.

The owned FFmpeg worker exits with code 4294967291 while opening this selected DirectShow input. It reports that the audio-only capture device has no usable output pin, then reports an input I/O error. The receipt records `actual_directshow_capture: false` and `actual_mci_output: false`.

Source and installed recording-control receipts each report 65 successful checks; the owned-process receipt reports 15. Those synthetic-control and ownership results do not establish successful device capture or human listening. Physical microphone, listening and NVDA remain NOT_RUN.

## Current endpoint and service receipts

The second bounded reader [46082166844 / job 140783380670](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46082166844/job/140783380670) completed successfully against the same immutable artifacts. It verified the full ZIP hashes again and recovered the three exact endpoint members. At this later read, the original ordinary run had completed with failure; the earlier reader had observed it still running.

`endpoint-evaluation.json` records the reviewed Pack46 archive and valid driver/catalog signatures, installed driver version `3.5.2.0`, service `VBAudioVACMME`, a started devnode with problem code 0, and no reboot requirement. Both enumerated output devices returned 0 for the 16 kHz, mono, 16-bit format query; the mapper query also returned 0. The receipt explicitly limits its qualification to actual WinMM endpoint inventory and format queries.

`audio-endpoint.json` lists `Speakers (VB-Audio Virtual Cabl` and `CABLE In 16 Ch (VB-Audio Virtua`, with no truncated inventory. Its format query succeeds, its status is READY, and actual playback is NOT_RUN in that particular receipt. Its `mci_326_message` field is an explanatory Windows error string, not a recorded MCI failure.

`audio-services.json` records AudioEndpointBuilder and Audiosrv changing from Stopped to Running in the existing startup step. Both retain Manual startup type; no startup-policy change is recorded. The endpoint evaluation also records `trust_store_changed: false`, `security_policy_changed: false`, `service_startup_policy_changed: false` and `vendor_setup_exe_executed: false`.

These reports contain **no microphone consent registry values, raw capture-policy values, WASAPI IAudioClient.Initialize HRESULT or actual MME waveInOpen result**. Their output-format checks do not establish that the selected input can open. The current cause remains unresolved beyond the separately measured DirectShow output-pin failure. A previous VM's access-denial or MME result is not attributed to this runner.
