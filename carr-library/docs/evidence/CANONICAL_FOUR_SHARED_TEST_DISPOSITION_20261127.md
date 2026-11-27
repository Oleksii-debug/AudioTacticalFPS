# Canonical shared assertion disposition: music, publication, conversion and verification

This source audit compares canonical `e5c64a5245583f8f8b404a0f042f6ca438c24d7f` with current composed base `f1ee73aa62a28aa26c5ceff81d12611fa70a0ef1`. The JSON companion records exact historical/current blobs, all **48 requested named assertion groups**, and existing owners/selectors. It does not transfer an old runtime result to the current implementation.

| Area | Disposition |
| --- | --- |
| Music controls | Component55b3 restores the required finite key and hopeful mood in the current score/director/cache/typed-backup path;23 new control cases plus1 canonical-import case await execution. Current PCM default and score instruments differ explicitly. |
| MIDI interchange | The existing actual-score case now directly checks requested tempo, both current instrument programs, note on/off, complete track/header and end marker. No case-count increase. |
| Optional named sound generator | The historical door/click/chime/noise generator is absent; V3-12 marks such a local generator optional. Current Sound/Clip retained assets are not described as the same API. |
| Model/runtime admission | Complete typed manifest/inventory, immutable identity, reviewed usage controls and isolated selected workers replace the old mutable AudioRuntime capability flags. A file check or synthetic worker is never called actual model inference. |
| Publication durability | All four historical assertions map to current writable-flush/no-replace/commit-boundary tests, including the retained11-case fsync population. The current publisher need not use the historical hardlink mechanism. |
| Text/PPTX | Strict explicit TXT codec selection and the exact public synthetic ordered-slide fixture were restored in104bf/e0ba through the current worker; no alternate ingest family. |
| HTML and spreadsheet reading | Current inert readers explicitly omit media/alt and formula-derived placeholder content with warnings; this differs from the old rendered reading text and is not claimed identical. |
| EML Subject | Separate source component9cd60ad11dc1301a8b307db9e362dd9f1452806e restores the canonical report.metadata.subject through the current bounded MIME reader and exact EML-only worker/parent metadata. Sixteen regression cases are authored; runtime pending. Body/attachment safety and unauthenticated-header warnings remain; catalog titles are not silently replaced. |
| Public backup verification | Seven direct current-format corruption/nonmutation cases are restored. The existing successful case now checks exact manifest, byte/mtime and parent inventory. Both existing V3 shape cases verify read-only before restore and preserve all original backup bytes/times afterward. |

The only new case count in this component is **7 source and7 installed**. Modified complete module counts are music20, backup recovery/verify20 (formerly13), and V3 compatibility9. Existing function identities remain. The new corruption population includes changed database, changed/missing original, extra original, extra top-level file, changed reading state and missing reading state.

No backup product source or schema changes are included. Tests use the existing current create/verify/restore API and historical fixture adapter. Runtime status remains **NOT_RUN** until the composed candidate's required gates execute.

## Follow-up source restoration: EML Subject

The separate six-path component `9cd60ad11dc1301a8b307db9e362dd9f1452806e`
restores the previously open EML derivative-report field. Its typed metadata
permits only an empty object or one Subject of at most512 Unicode characters,
with strict UTF-8/control validation before the existing final publication.
The existing MIME reader owns decoding, body selection and attachment omission;
the field is derived from the same source bytes whose SHA256 and size bind the
report. Original bytes and explicit library-registration titles stay unchanged.

The new16-case module `tests/conversion/test_email_metadata.py` covers Unicode
and path-looking passive Subjects, absent/512/513-character handling, per-source
batch identity, forged reply refusal and non-EML metadata refusal. Root and the
capacity peer independently reviewed this source boundary. Actual source and
installed execution remain pending. This documentation update does not fold
those16 cases into the seven newly restored backup cases or transfer a prior
runtime result. The final composed tree must contain both components.
