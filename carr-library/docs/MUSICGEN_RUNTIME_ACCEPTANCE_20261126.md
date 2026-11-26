# MusicGen runtime qualification — 2026-11-26

Exact measured source: `7d00b9ded4aa574c50e31a2d8f0cd0ad221563db`.
Run: https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45910399117
Job: `140217101956`.

The actual isolated CPython 3.11.18 / AudioCraft CPU runtime was installed and its unchanged worker imports and real Torch SDPA passed with the network unavailable. Before cleanup it contained 38,259 regular files and 41,182 entries. Removing only 5,386 redundant bytecode files whose corresponding Python sources remained reduced that to 32,873 files and 35,665 entries. All package code, tests and sourceless bytecode were retained.

The post-import preflight measured 32,874 files, 35,670 entries, 2,253,356,336 bytes and a 4,768,271-byte compact manifest. Its sole rejection was the prior 32,768-file cap, with an additional 16-file model/cache reserve. The narrowly revised cap is 40,960 files. The 16 MiB JSON limit, 65,536-entry traversal limit, complete inventory, asset byte limit, hashes, path checks and explicit license/weight consent are unchanged. The existing `MAX_MODEL_FILES + 1` regression automatically checks rejection at 40,961.

This run downloaded no weights and performed no MusicGen inference. A new exact-source qualification must pass acquisition, complete inventory verification and genuine offline synthesis before neural audio is claimed. No runtime, weights, private profiles or generated audio are published by the qualification workflow.
