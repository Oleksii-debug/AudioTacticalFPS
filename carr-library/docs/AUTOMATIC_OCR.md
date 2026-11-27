# Automatic language and regional OCR review

Status of this integration: source prepared for qualification; new automated and native tests have not yet run on the composed candidate. An installed model, a passing import test, and a language-quality evaluation are different kinds of evidence.

## Using the existing recognition dialog

Choose the installed, Managed Fast or Managed Best model set, then add and order the languages or script models needed for the scanned pages. All safe installed identifiers remain visible in the existing searchable inventory. Install any missing Tesseract models through the existing offline model manager before selecting them.

The checkbox **Automatic language review within selected models** explicitly enables the additional offline review. It accepts up to eight selected reading models. Leave it unchecked for the current manual language choice. Page orientation remains a separate option that requires the installed OSD model.

The first OCR pass uses the exact selected model list. The detector reviews the resulting text across all languages in its shipped statistical model; the selected OCR pool does not constrain or fabricate its probability distribution. A confident language can select only an already chosen, verified Tesseract model. Missing models, ambiguous script variants, short text, weak recognition, an unavailable detector or exhausted additional budget retain the first-pass candidate with a recorded reason.

Paragraph candidates keep their own text, layout and original pixel coordinates. They are not concatenated into an alleged source transcription. Only a complete, consistent paragraph inventory can admit a coherent whole-page second pass. That replacement retains the original first-pass text and TSV as provenance.

Every OCR result remains unverified and requires comparison with the source. The detector's confidence is a statistical language score, not a recognition-accuracy score or permission to approve a quotation. Neither the source document nor the printed-page verification flag changes.

Existing presets remain readable. An explicit automatic choice is retained in the same OCR preset file; old manual presets and old seven-field pending configurations remain passive and unchanged until the user saves a new choice. Runtime identities still prevent mixing extraction checkpoints produced by different implementation or model bytes.

## Reviewed implementation and dependencies

The integration reuses PR152 at b0245fe9bc949d14bb4a8407d7c7d6e858db8bed, including its regional policy and source/model provenance. The existing page adapter, managed model verification, process supervisor, import journal, resource limits and source-review boundaries remain active.

The classifier is the unchanged published py3langid 0.4.0 implementation from upstream commit 3b99caf00d0dcbc9416a9d06f8e5b690e919a74e. Its BSD-3-Clause notice is retained as src/carr_library/extract/PY3LANGID_LICENSE; the exact package, wheel, model and source-file identities are recorded in ocr-language-detector.json.

- Official wheel: py3langid-0.4.0-py3-none-any.whl, 4,600,605 bytes, SHA256 c25bd038147951ba3a89efe9ec12cc7d2116cb6f311ff8af84ade398a8bc0616.
- Statistical model: 4,586,720 bytes, SHA256 f4f4a2c3465ca1f081541037f9cac23021d68151c2f709c55ae5eedfca522963.
- Upstream Python requirement: 3.10 or later. It reuses the application's existing pinned NumPy dependency.
- Primary source: https://github.com/adbar/py3langid/tree/3b99caf00d0dcbc9416a9d06f8e5b690e919a74e .
- Original license: https://github.com/adbar/py3langid/blob/3b99caf00d0dcbc9416a9d06f8e5b690e919a74e/LICENSE .

Runtime admission verifies the installed distribution, package origin, exact source bytes, exact model bytes and file identities. The classifier executes only those captured source buffers in a temporary private module namespace. Ordinary bytecode caches cannot replace the verified implementation. The unchanged model loader receives a private copy of the verified model and uses NumPy's allow_pickle=False path. No upstream CLI/server, model download or external language service is invoked.

Manual OCR does not load or fingerprint the classifier. The dependency is installed during the reviewed application preparation, not fetched when a document is opened. Native OCR processes continue through the same verified local runtime and existing isolated import worker; cancellation and identity checks remain authoritative.

## Bounded additional work

The optional pass permits at most 12 paragraph regions, 1,024 initial words per region, 20 million cumulative crop pixels (or the tighter selected page budget), 512 KiB per second-pass text/layout read and 1 MiB of retained language-review evidence. It uses at most 12 additional seconds within the existing page deadline and reserves time for the final runtime-identity check. These are limits on additional review, not a deadline increase.

The current PDF import path and its existing source/page limits are unchanged. The real OCR acceptance fixture is an image-only PDF with no native text layer. This integration does not claim direct raster-file import support, automatic identification of every installed script variant, 135-language accuracy, visual-review correctness or NVDA qualification.

## Qualification inventory

The focused suites cover the real pinned English/Ukrainian/Slovak classifier, out-of-pool and low-confidence refusal, source/model changes, valid poisoned bytecode, regional evidence and deadlines, exact old-request replay, passive backup/restore, and explicit chooser/preset intent.

tests/extract/test_automatic_ocr_native.py requires actual English Tesseract and the pinned detector. Its three cases verify real scanned-PDF recognition, manual mode without classifier execution, and visible baseline retention when the detector is unavailable. Missing prerequisites fail rather than skip.

windows_automatic_ocr_smoke.py uses the full existing native language chooser and actual Windows keyboard dispatch. It checks automatic opt-in, manual override, Escape cancellation, installed English inventory, unchanged selected models/limits and exact source or installed module hashes. It has an independent whole-process watchdog and keeps manual NVDA and language-quality status explicitly unqualified.

Source and installed command forms:

~~~text
python -B scripts/windows_automatic_ocr_smoke.py --source src --report ci-results/automatic-ocr-source.json
<installed-python> -I -B scripts/windows_automatic_ocr_smoke.py --expect-installed --report ci-results/automatic-ocr-installed.json
~~~

The normal CI preparation supplies the already verified OCR runtime. No separate model installer or language registry is introduced by these checks.
