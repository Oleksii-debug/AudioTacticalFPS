# MusicGen runtime qualification — 2026-11-26

## Final integrated Linux CPU inference: PASS

The current production assembly was requalified at exact source `fc90e308231b1c083af35c926e071367988e717d`, tree `99fece0a6cf183a42b595d139f314775c67ff785`. Its application source is the current `ffc2e68aa52c0db6f2b5174eeef945b4f52e6dd7` assembly, including the stricter filename checks and the shared complete RIFF/RF64 validator. The qualification script, workflow, model revisions and original neural worker were retained.

Successful run: https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45913204547
Job: `140226195795`. The run was created at 23:49:48 UTC on 2026-11-26; the final genuine inference report was emitted at 23:58:10 UTC. The completed run and every job step report success. Its artifacts API returned an empty list.

The [complete exact final-source report](acceptance/musicgen_cpu_fc90e308.json) preserves every field from the emitted PASS JSON, including the full installed package inventory. The original `render_music(engine="musicgen", duration=2.0)` path produced actual model audio inside the network-disabled namespace; no substitute engine or procedural fallback was used.

| Final-source measurement | Observed result |
|---|---|
| Output | 96,000 frames; 48 kHz; mono PCM16; 2.000 seconds |
| Absolute peak PCM16 sample | 3,283 |
| Complete render stage | 83.585 seconds, including inventory verification, model loading, generation and post-verification |
| Maximum child-process RSS | 3,556,996 KiB |
| Complete verified installation | 32,881 files; 4,224,030,781 payload bytes |
| Post-generation verification | All inventoried paths and hashes passed again |
| Public model/audio/runtime artifacts | None |

The WAV SHA256 was `015c8d1e422508dbb250fe7d6c2ce0fb8811e955a64702a8b02aace8e8cd7a7a`, identical to the earlier two-second qualification. This is evidence for these two observed runs, not a cross-platform determinism guarantee. The new installation manifest SHA256 was `bb2abee5ac3d68e19c93f767b579a041bc53bcdc35fec73a0a2f3570e62a7689`.

Final qualified production SHA256 values:

```text
audio/music.py
6a54e2eb1310b462b32c5ea22461bcd38a36323ab220e3018a3494faaedda9f9

audio/model_manifest.py
4a1950182c34498c0a6bf46c6d5a2f125d879b30c88dc9973a2c0a769d5f22a9

audio/engines.py
8a95a06db72ec9624209569b7ffa86b925e0e2d04fcb6267b5ed0d33fa4a764b
```

Windows neural inference, human listening, multi-scene neural continuity and multi-hour neural generation remain **NOT_RUN**. The runtime remains an optional explicit local installation. The measured elapsed time is not a pure generation benchmark.

## Earlier Linux CPU inference at source 591b960: PASS

The unchanged production `render_music(engine="musicgen", duration=2.0)` adapter and its existing worker generated a genuine two-second MusicGen sample on an isolated Ubuntu 22.04 runner. This is actual model inference through the application, with no stub modules, replacement worker or procedural substitute.

Exact qualified source: `591b9609965bf1be63b90ea858462ca81da01daf`.
Successful run: https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45911284780
Job: `140220026209`.
The run completed successfully from 23:30:03 to 23:36:19 UTC. Its workflow artifacts API returned an empty list.

The [complete exact success report](acceptance/musicgen_cpu_591b960.json) preserves the final JSON emitted by the qualification script, including the full installed package version inventory and qualified production code hashes. It contains public measurements and identities only; no runtime, weights, private profiles, credentials or generated audio are included.

| Measurement | Observed result |
|---|---|
| Output | 96,000 frames, 48 kHz, mono PCM16 |
| Duration | 2.000 seconds |
| Absolute peak PCM16 sample | 3,283; nonzero audio |
| End-to-end render stage | 77.48 seconds, including verification, model loading, generation and post-verification |
| Maximum child-process RSS | 3,570,448 KiB |
| Runner | Linux, 4 CPUs, CPython 3.11.18 |
| Complete verified installation | 32,881 files; 4,224,030,781 payload bytes |
| Post-generation verification | Complete inventory and all hashes passed again |
| Public model/audio/runtime artifact uploads | None |

Generated WAV SHA256:

```text
015c8d1e422508dbb250fe7d6c2ce0fb8811e955a64702a8b02aace8e8cd7a7a
```

Complete installation manifest SHA256:

```text
54a227518517e086581190c32e196e5f28bcb017c347342247a8ff81300ff6e8
```

The 77.48-second measurement covers the complete render stage. It is not an isolated neural-compute benchmark and must not be extrapolated into a promised speed for long-form generation. Verified payload bytes describe the inventoried files, not total runner filesystem allocation.

## Qualified local runtime and checkpoint identities

- AudioCraft source: `896ec7c47f5e5d1e5aa1e4b260c4405328bf009d`.
- MusicGen Small: `facebook/musicgen-small`, revision `4c8334b02c6ec4e8664a91979669a501ec497792`.
- T5 Base: `google-t5/t5-base`, revision `a9723ea7f1b39c1eae772870f3b547bf6ef7e6c1`.
- CPU runtime: Torch 2.1.0+cpu, Torchaudio 2.1.0+cpu, Torchvision 0.16.0+cpu.
- Transformers 4.33.3, Xformers 0.0.22.post7, NumPy 1.26.4, SciPy 1.11.4.
- PyAV 11.0.0, built from its upstream source against Ubuntu 22.04 FFmpeg development libraries.

All six checkpoint/tokenizer files passed their exact reviewed byte counts and SHA256 values. The installation used the approved personal noncommercial MusicGen profile. The T5 snapshot was materialized as regular files under the legacy `t5-base` cache identity requested by this checkpoint, without symlinks or duplicate weight blobs.

The complete physical CPython prefix and all installed package files were inventoried. The actual unchanged worker imports and real CPU Torch scaled dot-product attention passed before weight acquisition. Inference used the pinned upstream Torch attention backend; no CUDA kernel was required.

Both the import probe and actual render ran in a separate network namespace with the network unavailable. The existing production worker's audit restrictions were retained before framework imports. Model acquisition occurred only in the separate explicit installation phase; no API or network fallback was used for generation.

Qualified production SHA256 values:

```text
audio/music.py
cd8d2186c7ed3942c8c1f02812af798b3898d674120520ad5cf2859a7fb7c61b

audio/model_manifest.py
ea0999a00aab1b297ff5a9b8bf30617527aa8d6dd375dd7444c885852c8dffe8

audio/engines.py
8a95a06db72ec9624209569b7ffa86b925e0e2d04fcb6267b5ed0d33fa4a764b
```

## Earlier measured inventory defect and narrow correction

Measured source: `7d00b9ded4aa574c50e31a2d8f0cd0ad221563db`.
Earlier run: https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45910399117
Job: `140217101956`.

The earlier actual isolated runtime contained 38,259 regular files and 41,182 entries. Removing only 5,386 redundant bytecode files whose corresponding Python sources remained reduced it to 32,873 files and 35,665 entries, saving 150,179,065 payload bytes. All package code, tests and sourceless bytecode were retained.

The post-import preflight measured 32,874 files, 35,670 entries, 2,253,356,336 bytes and a 4,768,271-byte compact manifest. Its sole rejection was the prior 32,768-file cap, with an additional 16-file model/cache reserve. That earlier run downloaded no weights and performed no MusicGen inference.

The narrowly revised cap is 40,960 files. The 16 MiB JSON limit, 65,536-entry traversal limit, complete inventory, asset byte limit, hashes, path checks and explicit license/weight consent are unchanged. The existing `MAX_MODEL_FILES + 1` regression automatically checks rejection at 40,961. The subsequent successful run above qualified this correction against the complete real runtime and model installation.

## Windows configured runtime preflight: PASS; neural inference NOT_RUN

The existing worker's original guarded import prefix and a real CPU Torch SDPA operation passed on Windows at exact source `0f9d7b0aaf9b9e65c98b79889334da1093b6c299`, tree `607a7b8523948c3d51678b29b419da1fd2a7357b`. The [successful run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45915760966), job `140234446625`, emitted its final `PREFLIGHT_PASS` report at 00:24:40 UTC on 2026-11-27. All job steps succeeded; its artifacts API returned an empty list.

The [complete exact Windows preflight report](acceptance/musicgen_windows_cpu_preflight_0f9d7b0a.json) preserves the emitted JSON, including all installed package versions, the complete Conda base SHA256 lock, 320 local native-module paths, the production code hashes and the runtime inventory digest. All paths in that module list are relative public package paths inside the reviewed prefix.

| Windows preflight measurement | Observed result |
|---|---|
| Isolated interpreter | CPython 3.11.14, physical local Conda prefix |
| Runtime | Torch 2.1.0+cpu; Torchaudio 2.1.0+cpu; Torchvision 0.16.0+cpu |
| Supporting versions | Transformers 4.33.3; Xformers 0.0.22.post7; PyAV 11.0.0; NumPy 1.26.4; SciPy 1.11.4 |
| Attention path | Upstream Torch SDPA, actual finite CPU result |
| Complete runtime inventory | 36,645 files; 39,923 entries; 3,050,343,584 payload bytes |
| Inventory JSON | 4,852,697 bytes |
| Native library origins | All loaded modules were within the reviewed local prefix or Windows system directory |
| PyAV FFmpeg ABI | libavcodec 60.31.102; libavformat 60.16.100; libavutil 58.29.100 |
| Operating-system network check | Previously reachable socket blocked for the runtime interpreter; errno 13 |
| Inventory after guarded imports | Complete file set and all hashes unchanged |
| Checkpoints, generated audio, public runtime artifacts | None |

Runtime inventory SHA256: `172b9bf7016524f85f3b8de7d2adafd3adba7ba89feede2ac8b5efd6e65d249c`.

The verified artifact identities are recorded in [the public package pin evidence](acceptance/musicgen_windows_runtime_pins.json). The exact conda-forge PyAV artifact is `av-11.0.0-py311ha83308b_0.conda`, SHA256 `0535da653551e31631684b1cf4d5be8a9816043852fd7f55c5b85eec9f056b5f`. The pinned CPython and Miniforge archive hashes also passed before use, and the installed runtime passed `pip check`.

The prior source `516c243` already installed those dependencies successfully, but its firewall setup rejected a mixed-separator executable path. The successful source canonicalizes the existing executable path and verifies the firewall application's target before running the probe. The production worker, dependency constraints, resource limits and network-denial policy were retained.

**Environment boundary:** this preflight explicitly places the reviewed runtime root, `Library/bin`, `Scripts` and Windows `System32` on the child process's `PATH`. Its result qualifies that configured local runtime. An unconfigured Windows application launch is not covered by this receipt.

**Inference boundary:** the preflight downloaded no model checkpoints and generated no audio. It tested imports, native-library origins, real CPU attention, complete runtime integrity and the operating-system firewall plus original worker audit hook. The runtime-only inventory is not presented as a complete MusicGen model installation. Windows model loading, two-second neural generation, listening, multi-scene continuity and multi-hour generation remain **NOT_RUN**.

## Acceptance boundaries

- Linux CPU MusicGen inference through the existing production adapter: **PASS**, including final integrated source `fc90e308231b1c083af35c926e071367988e717d` and the earlier `591b960` run. Both qualifications cover the exact recorded runtime and two-second sample.
- Configured Windows runtime, original guarded imports and real CPU SDPA: **PREFLIGHT_PASS**, at source `0f9d7b0aaf9b9e65c98b79889334da1093b6c299`.
- Windows neural inference and an unconfigured Windows application launch: **NOT_RUN**.
- Human listening, musical quality and accessibility of the generated result: **NOT_RUN**.
- Multi-scene neural continuity and multi-hour original neural generation: **NOT_RUN**.
- No claim is made that the optional model/runtime is bundled or preinstalled on a user's machine. Acquisition, local resources and the reviewed manifest remain explicit installation requirements.

These receipts qualify the exact recorded source commits. A different renderer or configuration API requires its own compatibility review and execution.

This evidence update changes documentation only. It does not change or rerun the qualified workflow, worker, model, or application code.
