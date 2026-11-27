# Synchronized EPUB and DAISY from the existing Audio Studio

The Production page exports a completed render as an EPUB 3 media-overlay book or a DAISY 2.02 ZIP file set. It uses the same Studio project, typed production plan, generated WAV master, measured timeline, worker, cancellation controls and optional shared resource broker. It does not run a speech model during export.

The XML, SMIL, navigation and package-graph implementation is adapted from canonical PR152 at 934b632f98098d55253354e0c6f7d8d482b73461 (studio/publishing.py and studio/daisy.py). The adaptation reads this application's audio/production.py contracts instead of importing the donor's alternate ProjectStore, ProductionService or AudioRuntime.

## Source and timing authority

New completed render receipts contain plan_sha256, the canonical hash of the complete typed ProductionPlan. Export checks this against the retained plan file and, through Studio, against the currently reviewed plan. It also validates every completed artifact hash, the exact original script, every spoken line and role, all integer sample positions, chapter coverage and the actual PCM master dimensions.

An old completion marker that has no plan hash is not sufficient export authority. Choosing **Render / resume** recomputes the existing production identity from the current plan, input hashes and engine identities, verifies the saved files, and only then adds the missing hash. A conflicting existing plan hash is refused. This migration does not change speech cache identities or re-synthesize accepted chunks.

Synchronization describes measured line boundaries. It does not establish word timing or acoustic alignment. Gaps between spoken lines retain the audio and have explicit “Audio interval without a scripted line” labels. Titles, role labels, original script and provenance are not presented as narrated content.

Both formats retain the exact original script in bounded JSON provenance. XML 1.0 cannot represent form-feed page separators: the readable source document displays [Source page boundary], while the original form feed remains in the hashed provenance. Ordinary CR/LF characters are preserved with XML character references.

The exporter encodes the verified PCM master as MP3 with the configured local FFmpeg and then decodes that MP3 to a bounded temporary PCM file to check its actual rate, channels and sample coverage. It permits only the documented small encoder-padding tail. One final output-rate padding sample covers fractional resampling endpoints. These extra samples do not change the recorded spoken intervals.

## Publication and recovery

Choose a new .epub or .zip destination outside the private library and source audio project. The existing destination-capture rules bind the exact selected path and parent directory. Publication is atomic and never overwrites an existing file.

Before that move, the project saves audio/publication-<32hex>.json with status prepared, the output hash and the source receipt. After publication, the output is rechecked and the receipt becomes complete. A process exit or directory-sync failure after the move leaves a prepared receipt that **Verify saved audiobook** can recover without re-encoding or replacing the book. A late cancellation after the successful move does not turn a completed publication into permission to retry over it.

Project and complete-library backups retain these data-only receipts. External exported books are not copied or followed through the saved filename. After restoring to another root, verification requires explicitly selecting the existing book. Verification checks exact bytes and packaged provenance without FFmpeg, a model, credentials or the old source location.

Input and audio reads, ZIP writing and hashing use bounded blocks. Metadata has individual and aggregate limits; the selected output budget covers working audio copies and packaging, with a separate free-disk reserve and deadline. An available shared broker reserves the declared CPU/RAM allowance and estimated working disk until the native encoder and verification finish. No whole PCM master is loaded into memory.

## API and gates

Audio PublicationService takes an existing project directory, private library root and optional FFmpeg path. It provides export_epub(run_key, output), export_daisy(run_key, output, publisher=...), publications() and verify(publication_id, output=...). The inspect_epub and inspect_daisy functions perform read-only checks of the produced bounded profiles; original canonical v1 package profiles remain readable.

Required adapted gates:

- tests/audio/test_publication.py
- tests/ui/test_audio_publication.py
- Existing production, integrity and project-backup suites
- Complete managed-audio backup suites
- tests/assistant/test_production_bridge.py
- scripts/windows_publication_smoke.py for native metadata controls

The native gate explicitly reports the full operating-system file-picker flow and manual NVDA as NOT_RUN. New source and adapted tests are not execution evidence until run on the integrated exact commit. Donor test results do not qualify this adaptation. Independent EPUBCheck/DAISY validation, hardware-reader interoperability, listening and tactile/Braille qualification remain separate acceptance work.

Primary format references:

- EPUB 3.3 and media overlays: https://www.w3.org/TR/epub-33/
- EPUB Reading Systems 3.3: https://www.w3.org/TR/epub-rs-33/
- DAISY Format 2.02: https://daisy.org/activities/standards/daisy/daisy-2/daisy-format-2-02-specification/
