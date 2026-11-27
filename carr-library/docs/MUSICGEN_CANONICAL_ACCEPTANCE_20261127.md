# Canonical MusicGen acceptance — 27 November 2026

## Actual Windows CPU neural generation: PASS

The selected canonical renderer generated and verified a real two-second sample on Windows at source `458fcb65ab51b436135350103f715d7866074bab`, tree `6bb1a3f776fa9fb0fb1f89b357446d1c7e892191`, based on canonical PR85 source `a708de1dac9f7fce719e5588ace8001a666f5d99`.

- [GitHub Actions run 45925325028](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45925325028), job `140265178607`, completed successfully under its 35-minute job bound.
- Exact final acceptance report emitted at `2026-11-27T02:13:15.0974063Z`.
- [Complete emitted Windows generation report](acceptance/musicgen_canonical_windows_458fcb65.json) and [complete runtime preflight, six-asset and manifest admission reports](acceptance/musicgen_canonical_windows_458fcb65_stages.json).
- The run artifacts API returned an empty list. No model weights, runtime archive, cache or generated audio were uploaded.

The trial reused the previously successful physical Windows runtime helper byte-for-byte, repeated the original canonical guarded import preflight, and required its exact source-bound full-inventory receipt before acquiring any checkpoints. It then invoked the existing `AudioRuntime.configure_music_model`, `probe_music_model` and `render_music` methods with the canonical `LocalModelConfig`, complete manifest and `private_noncommercial=True`. No production music code was changed for this trial.

| Actual output / resource measure | Result |
|---|---:|
| Duration | 2 seconds |
| PCM frames | 96,000 |
| Output format | 48,000 Hz, mono, signed 16-bit PCM WAV |
| Nonzero peak sample | 15,509 |
| Reported configure / probe / render / verification stage | 258.204 seconds |
| Complete installation files | 36,653 |
| Traversed entries | 39,933 |
| Installation payload bytes | 5,025,861,613 |
| AudioCraft Python source files | 134 |
| Canonical manifest bytes | 4,866,564 |
| Model runtime Python | CPython 3.11.14 |
| Actual compute device | CPU |

The actual WAV SHA256 was `f6408acba2cfc008b2c61a4711a5915bc33b5365de59b8d49eb2aa38226dbc9a`. The canonical model manifest SHA256 was `cb22859524f234809ef1932557773c2ad24684c31f1b4a4164dd722521cb3296`. The returned `AudioInfo` agreed with the measured frames, sample rate, channels and file hash, and the waveform contained nonzero samples. The complete model/runtime/SDK inventory, canonical manifest and separately prepared FFmpeg payload were unchanged after inference. The elapsed measurement includes configuration and verification work; it is not a standalone inference-speed benchmark.

The observed registry transitions matched the Linux result below: configuration verified assets while runtime/inference flags stayed false; the source probe set runtime verification while inference stayed false; actual completed generation set `inference_tested=true` and `available=true`.

The physical Python interpreter, FFmpeg and ffprobe each had an active Windows outbound program block throughout generation. An independent unhooked socket to the previously reachable address failed with error 13 before and after the real render. The canonical worker retained its existing `-I -S -B` startup, request JSON file, offline environment and stage-only write/network/child-command audit controls. Numba used the existing operation-stage cache.

The reviewed Windows FFmpeg CLI came from the canonical locked preparation helper, with its LICENSE and README retained. Its four pinned files and preparation receipt were checked before and after generation. The CLI version was `9.1-essentials_build-www.gyan.dev`; PyAV 11 continued to use the separate Conda FFmpeg 6 DLL closure. No binary redistribution permission is inferred.

All six acquired MusicGen/T5 files used the same revisions, sizes and hashes as the successful Linux trial. All five production module SHA256 values and the worker SHA256 also matched that Linux receipt exactly. The complete new preflight receipt retains this run's Conda explicit base lock, installed versions, native-module origin checks and full-inventory measurements.

**Scope:** actual local CPU generation through the selected canonical service is now demonstrated on both measured Linux and Windows environments. The Windows launch explicitly configured the physical runtime, `Library/bin`, `Scripts` and Windows system directory on PATH before starting Python. An unconfigured desktop launch, human listening/NVDA acceptance and multi-hour neural production remain untested.

## Actual Linux CPU neural generation: PASS

The selected canonical renderer generated a real two-second sample through its production service at source `3924f36f3af767c441d0b48532744df1650cb71e`, tree `85e55b2f6293bf35a18cd6f4b451b754568f5171`, based on canonical PR85 source `2bc7f306744a62d58695bb0852a986850679d75e`.

- [GitHub Actions run 45921407314](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45921407314), job `140252755833`, completed successfully.
- Exact final acceptance report emitted at `2026-11-27T01:22:46.6259711Z`.
- [Complete emitted generation report](acceptance/musicgen_canonical_linux_3924f36.json) and [runtime, six-asset and manifest admission reports](acceptance/musicgen_canonical_linux_3924f36_stages.json).
- The run artifacts API returned an empty list. The private qualification sample, model weights, cache and runtime were not uploaded.

The helper used the unchanged canonical `AudioRuntime.configure_music_model`, `probe_music_model` and `render_music(MusicSpec(...))` methods with the existing `LocalModelConfig` and complete manifest schema. It did not substitute an engine, worker or generated fixture. The canonical worker performed MusicGen inference using the six explicitly verified files, then the application converted and inspected the returned PCM.

| Actual output / resource measure | Result |
|---|---:|
| Duration | 2 seconds |
| PCM frames | 96,000 |
| Output format | 48,000 Hz, mono, signed 16-bit PCM WAV |
| Nonzero peak sample | 15,509 |
| Configure / probe / render / verification stage | 80.187 seconds |
| Child peak RSS | 3,511,216 KiB |
| Complete installation files | 32,881 |
| Installation payload bytes | 4,224,031,200 |
| AudioCraft Python source files | 134 |
| Canonical manifest bytes | 4,781,721 |
| Model runtime Python | CPython 3.11.18 |

The actual WAV SHA256 was `a0ee68607ff6a35eee7c665412b659d5d71e78db139fc11736066fde79e37974`. The canonical model manifest SHA256 was `88b91e106ae864d946977217f21f72f91aa717f0c2bf21c3e7bd473e9ecedf11`. The complete model/runtime inventory and manifest remained unchanged after generation, and the measured WAV frames, channels, sample rate and hash agreed with the returned `AudioInfo`.

The registry's observed transitions were:

| Production operation | Runtime code verified | Inference tested |
|---|---|---|
| Configure reviewed assets | false | false |
| Probe actual installed SDK source | true | false |
| Complete real generation and conversion | true | true |

Only after the real output succeeded did the returned capability report `available=true`.

The Linux job first repeated the exact canonical `-I -S -B`, request-file and stage-cache preflight. A receipt bound to that helper, source commit and complete runtime inventory was required before acquiring weights. Both imports and generation ran inside an empty Linux network namespace; the native interface API returned only loopback, and an independent unhooked socket failed with errno 101. The original worker audit hook remained active.

The reviewed source and asset identities were:

| Component | Exact revision |
|---|---|
| AudioCraft | `896ec7c47f5e5d1e5aa1e4b260c4405328bf009d` |
| facebook/musicgen-small | `4c8334b02c6ec4e8664a91979669a501ec497792` |
| google-t5/t5-base | `a9723ea7f1b39c1eae772870f3b547bf6ef7e6c1` |

All six asset sizes and SHA256 values are preserved in the stage receipt. The installed CPU dependency versions and all five application module hashes are preserved in the final receipt. Those five production module hashes and the canonical worker hash exactly match the successful Windows runtime preflight below.

**Scope:** this Linux receipt establishes actual local CPU generation through the selected canonical service on the measured Linux environment. The separate Windows trial above supplies its own inference evidence. Human listening, long neural productions and an unconfigured desktop launch remain untested.

## Windows runtime-only result: PREFLIGHT_PASS

The selected canonical renderer passed its actual guarded Windows runtime preflight at source `1e4bc336e76cc43b1cdc3d3c2531d7c6a2ea70bc`, tree `0a25d042d7900cefd5d9d9a13f09d8ff6ca4a4ba`.

- [GitHub Actions run 45920093185](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45920093185), job `140248499473`, completed successfully.
- Exact final report emitted at `2026-11-27T01:07:14.4147005Z`.
- [Complete emitted acceptance report](acceptance/musicgen_canonical_windows_1e4bc336.json), including all installed package versions, complete Conda explicit base lock, native-module paths, source hashes and measured inventory.
- The run artifacts API returned an empty list. No checkpoint, generated audio, runtime archive or cache was uploaded.

This result applies to the canonical `AudioRuntime` / `LocalModelConfig` implementation. Earlier qualification of the other renderer family does not establish this implementation's behavior.

## What actually executed

The helper reused the production `_SITE_BOOTSTRAP` and unchanged `_MODEL_WORKER` prefix through seed/thread initialization, stopping before the engine's checkpoint-loading branch. It launched the selected physical interpreter with `-I -S -B`, using the actual request JSON file at `argv[1]`, the production offline environment and its audit hook.

Real AudioCraft imports, CPU Torch scaled-dot-product attention and PyAV native loading passed. The original audit hook denied network name resolution, child commands and writes outside the operation stage; a write within staging succeeded. An independent, unhooked socket connected before the Windows outbound program rule and was denied with error 13 after that rule became active.

| Measured item | Actual result |
|---|---:|
| CPython | 3.11.14 |
| Torch / Torchaudio / Torchvision | 2.1.0+cpu / 2.1.0+cpu / 0.16.0+cpu |
| PyAV | 11.0.0 |
| Complete inventoried files | 36,645 |
| Traversed entries | 39,921 |
| Inventoried payload bytes | 3,050,321,920 |
| AudioCraft Python source files | 134 |
| Canonical manifest projection, including reserve | 4,873,631 bytes |
| Local native modules | 320 |

The complete runtime and SDK inventories were identical before and after the guarded import. The inventory SHA256 was `eeaf0748d2e530a3c072f7d65909f24489ddf56a29298f6ac4076371df07acac`. Every loaded native module was inside the reviewed physical runtime or the Windows system directory. PyAV loaded the FFmpeg 6 ABI; that DLL closure is separate from the application's FFmpeg command-line executable.

The canonical manifest retains its existing 100,000-file / 8 MiB allowance. This qualification did not import the other renderer's manifest implementation or change production inventory caps.

## Reproduced issue and minimal production correction

[The preceding run 45919223268](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45919223268), at `db8de61a78ebe0f01fa905d93efececb296caf9e`, passed package/source checks and the OS network block but failed during Librosa/Numba import. Numba could not create its required cache under the canonical stage-only write restriction.

The production correction is one environment entry in `audio/music.py`:

```python
'NUMBA_CACHE_DIR': str(stage / 'numba-cache'),
```

Numba's pinned [0.59.1 cache implementation](https://github.com/numba/numba/blob/0.59.1/numba/core/caching.py) tries the explicit cache directory first, and its [configuration](https://github.com/numba/numba/blob/0.59.1/numba/core/config.py) reads `NUMBA_CACHE_DIR`. The new path lies inside the existing watched, temporary operation stage. The audit hook, worker code, subprocess flags, model admission and resource budgets are unchanged.

The successful preflight checked Numba's actual configured cache location and directory. The exact production `music.py` blob is `418ba0cede508c3bc4d2054cce9cde0a0cc1f437`; its SHA256 is `cec6a7fef7d5a3bd438626cf71ec5622c57a8e2c68c42d3c928d3867e78849bc`. This same blob is included in canonical PR85 source `2bc7f306744a62d58695bb0852a986850679d75e`.

## Exact supported scope and remaining gates

- **PREFLIGHT_PASS:** configured Windows physical runtime; exact canonical isolated startup/request protocol; real framework imports and CPU attention; Numba staging cache; audit controls; OS firewall; complete source/runtime inventory.
- **Historical Windows runtime-only preflight: NOT_DOWNLOADED.** That earlier job did not acquire model checkpoints. The separate successful Linux and Windows inference jobs each acquired only the six pinned MusicGen/T5 assets after their own import/inventory gates.
- **Linux and Windows: PASS** for canonical admission of the actual model assets and two-second neural inference through the production service in the measured configured environments. **NOT_RUN:** human generated-audio assessment, multi-hour neural generation and listening.
- **NOT_RUN:** an unconfigured desktop launch. The tested environment explicitly supplies the physical runtime, `Library/bin` and `Scripts` on PATH.
- **NOT_RUN:** human NVDA acceptance. The runtime and generation receipts do not establish human accessibility acceptance or a complete product-release verdict.

The real Linux and Windows qualifications above used the canonical complete manifest, full installed SDK source inventory and actual production service calls. Their measured registry transitions confirm that runtime preflight or source probing alone does not mark inference as tested or available.
