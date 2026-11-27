# Canonical MusicGen runtime acceptance — 27 November 2026

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
- **NOT_DOWNLOADED:** MusicGen and T5 model checkpoints.
- **NOT_RUN:** canonical model admission with actual weights, MusicGen inference on Windows or Linux for this renderer, generated-audio assessment, multi-hour neural generation and human listening.
- **NOT_RUN:** an unconfigured desktop launch. The tested environment explicitly supplies the physical runtime, `Library/bin` and `Scripts` on PATH.
- **NOT_RUN:** human NVDA acceptance. This is a native runtime preflight, not an accessibility or product-release certification.

A subsequent neural qualification must use the canonical complete manifest, its full installed SDK source inventory and the actual `AudioRuntime.configure_music_model`, `probe_music_model` and `render_music(MusicSpec(...))` calls. Runtime preflight alone must not mark the application's model registry as inference-tested or available.
