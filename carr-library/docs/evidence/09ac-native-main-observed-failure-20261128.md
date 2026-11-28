# 09ac early native run — observed reading-fixture failure

Exact source: `09ac4c06ec6cfc88805ea4673a7767a934b80f9b`, tree `3f00a232a7ff2af3a4df2f681db5e92da0db9220` (1,824 tracked files).

[Actual run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46131541694) / job 140937577072 / attempt 1 completed **FAILURE**. Focused step 28 (main step 30) passed Import Center; focused step 29 (main step 31) failed at `windows_v3_reading_smoke.py:88`: `frame.keyboard_actions` is absent. Four reading PASS labels precede the traceback. No final reading receipt exists. The same step's OCR-language command was not invoked; steps 30–47, including the after-payload binder, were skipped.

## Import Center now exercised

The native probe emitted its unchanged four-field PASS after its complete serial minimum-size/focus/Tab/import/search/reopen flow. Default window 920×760: client 904×721, viewport 887×637, content minimum 639×673. Minimum window 800×650: client 784×611, viewport 767×527, content minimum 639×673. Outer minimum was 240×284 at both sizes. Source contains 41 assertion sites; these are not 41 emitted labels. The probe imports current checkout source. The separate installed document-format probe was skipped and is not qualified here. NVDA remains NOT_RUN.

## Other actual evidence and limits

Search continuation passed all 21 original labels exactly once, including real native keys and verified source copying. Its four installed module origins were freshly matched to current source hashes. The before binder reports PASS for all 1,824 tracked files and 442 installed payload files. Full binder maps and the ZIP were not independently decoded. Word reports explicit NOT_RUN because desktop Word is absent. Current Basic/local and inspection successes have their own exact-source records and do not make this failed candidate integratable.

Raw completed log: 579,473 UTF-8 bytes; SHA-256 `bb7a4fed4d433b79cbca2ff540031a04cd7f0bb8494337a5052d856800fb1c2e`. Artifact 15257579427: 246,817 bytes; SHA-256 `cf3b7bcb5b39b0be8eeab694aa8eb1a1d016e329baa1508259ac36251d51575d`; uploader reports 40 files. This is failed-run history, never a final successful-main artifact.

## Provenance correction

The historical c2b 88-file audit used one stale search-probe body. Its original `9d9b…` inventory digest is invalid as a current2701 inventory. The [append-only erratum](https://github.com/Oleksii-debug/AudioTacticalFPS/blob/ec70134c192f35d59a09821116d0a8c2214053dd/carr-library/docs/evidence/import-center-audit-inventory-erratum-20261128.json) records corrected2701 `a286ab…` and separately derived09ac `b32f71…` digests. Product/probe choices and four actual Import Center consumers remain independently reviewed. This observation freshly binds 13 current source inputs.

Full main was not launched for09ac. Full platform remains NOT_QUALIFIED. The accompanying JSON retains every actual API step, exact traceback, both geometry diagnostics, source hashes, search receipts and explicit qualification limits.
