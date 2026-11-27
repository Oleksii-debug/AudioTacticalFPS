# Retained playback queue — observed Windows qualification

Exact component `565b9a6b21cd0d01e04a601b44e0ba2bbdf636f5`, tree `5bad44c940c00b7754091597e579bb3628edb95f`, passed [run 46016663005](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46016663005), job 140565410777, on 2026-11-27. Source 141 cases passed in 32.766 seconds; installed 141 cases passed in 31.938 seconds; all 43 exact native checks passed. The aggregate gate and job completed successfully.

The native probe drove actual Windows queue controls, retained two independent PCM inputs, selected the second item and saved cursor 3000 at 8000Hz through Save All. Reopening remained passive and required explicit review. A deferred Play retained all verified readers without opening an output device; Stop released those readers and preserved the saved cursor. Complete backup/restore/rebackup retained exact PCM and state bytes. The saved and restored queue SHA256 was `ebee98d99ae3403f87db42c6aac8feadb310f3fde7eaab8c950067491bbba6f8`.

Wheel SHA256: `3fa422821f04ffbc04e690db02b8b7ef3316ca1d6a957db2f95ee53f1e262c8a`. Native receipt SHA256: `0cad40de3c05b6b4db73773bbd9fe1e99ac32d6c42d4e404705e4378cbe02d7d`.

Artifact `15211641814` contains 392200 bytes; GitHub metadata and actual upload log report `sha256:391cefcbd586df2663ace0e6e0978968444c99cfc9bf7a1d884227c0f222aa84`. The companion JSON retains actual source/installed/native/aggregate receipts and artifact metadata. It does not claim a separate local ZIP extraction.

Physical playback and manual NVDA acceptance were explicitly NOT_RUN. These results qualify the exact frozen component; they are not transferred to a later composed root. Original earlier frozen heads and their outcomes remain unchanged.
