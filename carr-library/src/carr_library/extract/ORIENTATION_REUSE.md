# CARR-03 — optional orientation evidence (2026-11-26)

Scope: new extraction helper and synthetic regression tests; no edits to PR #17, #18, #31, storage, UI or packaging. Base a450bd9eb97b11764f0fe2d70d72efbf9788aa5d. The helper is not yet wired to the page OCR job. Candidate only, not a verified citation or a sandbox.

## Public upstream reuse

- pytesseract 0.3.13 (Apache-2.0): https://github.com/madmaze/pytesseract/tree/bb128f3ab846e4144d1e7529e37801554d5aef75 . LICENSE blob 261eeb9e9f8b2b4b0d119366dda99c6fd7d35c64. Actual installed pytesseract.py Git blob 37837f4cfe5de220ff8d0d05bdf6e93a0a0be190 matches fetched pinned source. Calls existing image_to_osd; no replacement orientation algorithm.
- RapidFuzz 3.14.3 (MIT, test-only): https://github.com/rapidfuzz/RapidFuzz/tree/53c66fe97b6d8e8660c2ab150926b998feecd740 . LICENSE blob 42c23b2103346d5439dd72582ed6d4a0d8d7b27b. Actual installed Levenshtein.py blob 9a22f326b06bce42fc64de53523657d02612f379 matches pinned upstream. Uses actual distance API for CER/WER, no custom edit-distance implementation.
- Tesseract 5.5.0 / Leptonica 1.84.1 and Pillow 12.3.0 installed locally; existing #18 dependency evidence applies. No binary source build or wheel attestation; no new code vendored. Source files and licenses read via GitHub; direct git network access failed with DNS error. Optional wrapper packaging, exact wheel/engine/model digests and SBOM review belong to CARR-11.
- PR #17 head 4c2ee5ba43a778a2b160325cfe48678eeab213d3: all 8 vendor files in UPSTREAM.json independently matched the MIT cairnsearch repository at eb5bf13605343e75e3aaf6ce0d8d2ecef8748d93 including LICENSE. No edits or duplicated non-PDF extractors.

## Execution evidence

Linux, Python 3.13.5, pytest 9.0.2, locally installed packages above, DejaVu Sans (fixture font only; not redistributed). From carr-library root:

    PYTHONPATH=src python -m pytest -q -s tests/extract/test_orientation.py

9 passed in 4.29s; no skips, exit 0. Real local engine invoked for four rendered synthetic image cases. With whitespace collapsed ONLY for metrics (not for source output), all four have CER=0 and WER=0; no claim about general accuracy.
- English 90 / 180 / 270 degrees: OSD confidence 18.83 / 19.22 / 20.36.
- Ukrainian 90 degrees: OSD confidence 31.39.
- Other tests cover invalid, duplicate and low-confidence fields, missing engine and simulated timeout, bounded pixels, invalid input, exact right-angle pixel mapping without mutating input.

Executed source/test Git blobs: 15d57520354135306391a6e7d48adeb0c4f3cd13 and 78cc4cf5c2f26dfb0360f123a10ad9ca900e8030; post-write readback matches.

## Integration conditions

Use only on an already-rendered bounded image, with fixed command options and optional local OSD model. Never auto-publish a quote. Uncertain/failed orientation is not a correction. Caller must bind source hash, physical page, revision, original render dimensions and pixel identity, rotated dimensions, clockwise angle, raw OSD and engine/model/config to the OCR candidate, and keep bounding boxes in the rotated pixel frame or explicitly transform them. No arbitrary angles or deskew implemented. Threshold 15 is a conservative candidate heuristic, not calibrated accuracy; no generalized metric claim. PR #18/#31 integration, renderer/worker isolation and output caps, Windows, independent review, full damaged-scan benchmark and actual corpus remain NOT_RUN. G2/U4 not closed.
