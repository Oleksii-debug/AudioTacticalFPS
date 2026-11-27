# Focused native playback evidence — 27 November 2026

The completed [run 46075231974](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46075231974), job 140761298828, passed all four existing native source and installed reader/player and EPUB/DAISY probes on `68777f9f36b142ed1ce847ee47d82e20d0bdd132`.

The reader/player probe completed **198 checks in each mode**. The EPUB/DAISY probe completed **65 checks in each mode** and reached its final complete phase. These counts include imported-module and native key-event assertions.

The actual reader scenarios cover short-path MCI, explicit output identity, pause/seek/resume/Stop, A/B and sleep controls, listening-copy conversion, explicit live volume controls, and the long retained queue waveOut path. The queue advances measured position, retains its exact source through Pause and Save, reopens passively, and proves native closure on normal Close and direct destruction. EPUB/DAISY covers actual native publication, import and recorded-PCM playback with source and prepared-file cleanup.

## Identity and evidence

- Source tree: `ab4bd12008e03e7b7ce6b65755776114ff6a0ffd`.
- Source subtree: `883b1638c6894b6dfb970bed24884713ca8ea9b2`.
- Verified wheel SHA-256: `60e0218290c6aec7cc9e84b8acfc9270384999d7e3ba00550249bf1142568638`.
- CPython: 3.11.9 (tags/v3.11.9:de54cf5, Apr  2 2024, 10:12:12) [MSC v.1938 64 bit (AMD64)].
- The completed wheel/installation check compares all 376 Python files (6112489 bytes) against the exact checkout and the unique installed RECORD owner.
- Original artifact: [15233929643](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46075231974/artifacts/15233929643), 64689 bytes, SHA-256 `d876a5af7bdd62dc011174ea1f2f00675af363a3b969d0ecb04e21968b3a1122`.

This directory preserves bounded evidence extracted from the completed job log. Files ending in `.stdout.json` contain the exact printed JSON payload with its log timestamp removed and one newline added. The EPUB phase files preserve their timestamped log records. The original archive member bytes and their individual hashes have **not** been recovered in this extraction; the original ZIP digest is reported by both GitHub artifact metadata and the upload log. `manifest.json` hashes only the exact files in this directory.

The secondary output is selected explicitly for the routing scenarios. Queue and EPUB playback retain their existing session default-route contract. No default device or capture-consent policy is changed.

## Qualification limits

This is exact-source native source/installed evidence on CPython 3.11.9. It does not qualify the complete ordinary aggregate, the sealed delivery bundle or its pinned CPython 3.11.18, actual capture, physical devices, audible listening, voice-model inference or quality, human NVDA use, DAISY hardware readers, or full independent DAISY rule validation. Earlier failed runs remain failed historical evidence.
