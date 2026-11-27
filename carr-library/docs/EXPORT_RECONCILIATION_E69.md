# Export reconciliation with canonical e69

Reviewed canonical: `e69ba2403515f21509686d66f4bc8b3673756d81`.
Application preimage: `55725629bc0cb79b62f7f3979842a573d5fd868f`.
This additive source follows the Open Original handoff corrections through
`dd64b93f97d50e004e2864e64dd1f58a3a905d8f`; it does not replace that worker/UI
boundary. New source and native execution are **NOT_RUN** while the local
executor is unavailable. Donor test results are not results for this composition.

## Exact reused behavior

The canonical publisher blob
`c60b6f387c086c51f4de7ed535e9fd5d675f88a4` supplies the narrow changes to
`research/export_file.py`. Current native metadata helpers remain shared with
`native_file_metadata.py`; no second Windows API wrapper is introduced.

- Refuse a create-only conflict, including a path previously captured as absent,
  before opening its file contents. A fresh output remains no-clobber even when
  a caller does not repeat the explicit absent flag.
- Check the live cancellation callback before and after each bounded existing-file
  fingerprint read. Existing 32 MiB / 0.25 second replacement-approval bounds
  remain unchanged. Use the platform's nonblocking-open flag where supported so
  a substituted FIFO cannot make this regular-file check wait indefinitely.
- Keep publication acknowledgement separate from residual staging cleanup.
  The additive `ExportReceipt.cleanup_warning` already existed in this build;
  `export_completion` now carries it through the remaining native handlers.
  A warning does not cause a repeat publication.
- Recheck the captured library/topic after the explicit replacement question,
  before rendering, after rendering and immediately before publication in the
  academic bibliography/outline actions.
- Keep the academic controller's historical text return by default. Its explicit
  `export_result` returns both text and publication receipt; the native dialog
  displays that receipt without reading the output back.
- Preserve the selected reading-state export cancellation callback through final
  staging. Current live reading, source lineage, editable DOCX citations,
  regeneration, streamed import and durable scan continuation remain in their
  existing modules.

The eight existing product files are `research/export_file.py`,
`ui/academic_actions.py`, `ui/academic_library_dialog.py`,
`ui/catalog_insights_actions.py`, `ui/catalog_tools_dialog.py`,
`ui/import_center.py`, `ui/reading_desk.py`, and `ui/desktop_app.py`.
The desktop delta is only the export helper import and the existing
`DesktopFrame.export_topic` receipt return/report. The distinct Open Original
method is unchanged by this reconciliation.

## Required validation

The new `tests/reader/test_export_reconciliation.py` is reused from canonical
blob `50f23a75fddb439dc3a5fccb0cd979d4546aec3a`. Its real files cover no read
authority over an unapproved target, a competing writer at the final publication
boundary, cancellation after the first real fingerprint block, closed native
descriptors, and unchanged receipt/native-digest shape.

Existing native-policy test modules retain their current explicit-consent,
private-root, changed-destination and original-data cases. Additional cases
exercise real publisher receipts through bibliography, topic document, catalog,
reading-state and import-report handlers, plus stale scope at confirmation,
queued execution, rendering and final staging. The matrix action test also
retains a saved-file cleanup notice after a late stop request. These controlled
event fixtures are not physical Windows/NVDA evidence.

`carr-windows.yml` retains every previous test path and adds strict source,
isolated installed-package and portable Linux execution for the reconciliation
and the seven affected handler suites. Required installed JUnit identities must
be present with zero failures and zero skips. The existing real create-only
child-process probe binds its child import path to the source/installed publisher
under test.

Before claiming this behavior executed, retain the exact source/wheel identity
and terminal test result. Run the existing bibliography/editable-DOCX,
regeneration, binary/text export, native academic and independent accessibility
gates as part of the final combined build; no tests have been waived here.
