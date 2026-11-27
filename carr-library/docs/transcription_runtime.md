# Current supplied-worker transcription runtime

The integrated implementation is the existing library-scoped `TranscriptionService` in `src/carr_library/transcription.py`, with exact runtime review in `transcription_runtime.py`. It extends the supplied worker protocols. There is no second transcription package or competing ASR engine.

See [Transcription workstation](TRANSCRIPTION.md), [runtime review and named profiles](TRANSCRIPTION_REVIEW_PROFILES.md), [supplied adapter review](SUPPLIED_TRANSCRIPTION_ADAPTER.md) and [cooperative resources](runtime_resources.md).

## Current source contract

- Select a library and explicitly inspect/review the exact local Python, worker, model and runtime inventory before trusting execution. Named profiles preserve selectors; changing reviewed bytes invalidates execution and cache acceptance.
- Prepare retained audio/video input files or a selected folder separately from Run. The source remains immutable. Supported settings come from the selected worker protocol; missing translation or diarization capability is explicit.
- The same canonical service backs the native workstation, `transcribe` / `transcription` CLI aliases, and scoped shared operation controls.
- Pause/cancel is durable. The actual worker owner acknowledges closure before releasing shared resources. An interrupted or durably rejected attempt remains non-authorizing. Explicit retained-originals copy creates a new reviewed job; it cannot rehabilitate old rejected receipts.
- Selected TXT/SRT/VTT/JSON/DOCX/PDF exports use verified retained results, immutable original recognition data and separate edited derivatives. Unselected formats are not invented or silently substituted.
- Typed backups preserve supported inputs, accepted evidence, edits and passive configuration. Relocated restore revokes external execution trust and retains unresolved host review vetoes. It never resolves or executes an old external worker path automatically.

The older package-shaped implementation on the canonical branch was reviewed as an integration input. Its runtime review, controls, source binding and restore-revocation behavior were reconciled into this service, preserving the existing receipts-v1 and V5 child ownership boundaries. The superseded package-shaped imports are not a public second execution surface.

Actual synthetic worker protocol tests establish application behavior only. Real supplied Python/SDK/model installation, authorized recordings, long-input resource measurements, offline target hardware and independent recognition quality require their own exact evidence. No missing dependency, registered language or completed synthetic fixture implies acoustic qualification.
