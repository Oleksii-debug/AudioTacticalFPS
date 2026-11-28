# Redacted diagnostic preview and explicit export

The desktop Help menu exposes **Preview and export diagnostics**. The same stable
`help.diagnostics` command is registered for the existing command palette and
keyboard settings. Preparing a report uses the desktop's current single worker;
it does not create a background logger, uploader, device scanner or new worker
service.

## Review and save

1. Open the diagnostic command while the desktop is idle.
2. Choose **Prepare preview**. The read-only preview displays the complete JSON
   that can be saved. The status includes its exact UTF-8 byte count and SHA-256.
3. Review those fields, then choose **Save exact preview** and a new filename in
   an existing local folder outside the private library.
4. The existing create-only export writer saves exactly the reviewed bytes.
   Existing destinations are refused. Refreshing the preview requires reviewing
   the new snapshot before saving it.

A report belongs to the library object and ordinary directory identities captured
when its dialog opened. Replacing or rebinding that library invalidates the
preview. Close the dialog and prepare a new report after such a change.

**Cancel operation** requests cancellation of this dialog's current operation.
Closing the dialog withdraws its callbacks and requests the same cancellation.
A held or late result cannot recreate its preview. If publication already
committed, later cancellation or window closure preserves the saved report;
repeat export is unnecessary.

## Fixed report contents

The versioned `carr-redacted-diagnostics/1` report is canonical ASCII JSON,
limited to 16 KiB. Its whitelist includes:

| Group | Included values |
| --- | --- |
| Runtime | Product version; Python and SQLite numeric versions; normalized implementation and OS category; pointer width |
| Capability | An actual temporary in-memory SQLite FTS5 check; explicit indication that models and audio devices were not probed |
| Resource policy | Numeric configured CPU, memory and disk allowances; lease limit and number of configured GPUs |
| Resource usage | Numeric declared reservations and counts of leases, reserved GPUs and exclusive devices |
| Filesystem | Actual total and currently free bytes on the selected library's filesystem, without its path or identity |
| Capture | UTC capture time, schema identifier and fixed privacy scope |
| Unavailable state | Fixed resource-state codes; no exception text or traceback |

Resource usage is the existing broker's cooperative reservation snapshot. These
numbers do not measure actual process RSS, family memory, model quality or device
availability. The broker retains its ordinary locked snapshot and stale-reservation
reconciliation behavior. Missing broker configuration is reported as
`not_configured`; preparation does not initialize it.

The report does not collect source text, library catalog rows, job journals,
transcripts, credentials, environment variables, provider configuration, arbitrary
logs, lease IDs, device names, library names or paths. No network request is made.
Arbitrary exception messages are mapped to fixed user-facing notices. Extra JSON
fields, duplicate keys, changed hashes, malformed counters and noncanonical
payloads are refused before publication.

## Implementation and verification boundaries

The typed capture, report validation and publication adapter are in
[`support_diagnostics.py`](../src/carr_library/support_diagnostics.py).
The [dialog](../src/carr_library/ui/diagnostics_dialog.py) delegates to the existing
desktop worker and
[export writer](../src/carr_library/research/export_file.py). It does not relax
the writer's destination, identity, cancellation or no-clobber checks.

[Core tests](../tests/diagnostics/test_support_diagnostics.py) use real private
sentinels, broker reservations, filesystem replacement, flushed staged writes and
publication receipts. [UI tests](../tests/ui/test_diagnostics_dialog.py) use an
actual single executor with held completed snapshots and callback withdrawal.
The [Windows probe](../scripts/windows_diagnostics_smoke.py) exercises the actual
Help menu, keyboard controls, preview, writer, cancellation and three owner
teardown routes. Its authored file-picker answers and post-snapshot hold are
declared controlled seams. Native file-picker navigation, physical keyboard
operation and human NVDA assessment require their separate evidence.

Use the exact commit's CI reports and source/installed identity records for the
execution verdict. Source review or this guide alone is not a runtime acceptance
claim. The final application gate remains responsible for its broader offline
and integrated-runtime checks.
