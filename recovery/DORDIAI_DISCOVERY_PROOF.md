# Bounded Dordiai discovery-cancellation proof

This branch composes the reviewed public provider boundary over shared commit `8c981c7e947f8d144c8706b5ea022990517c7dd9`. It is not the final application composition or a release.

The canonical facade formerly forwarded only caller cancellation during cold discovery. Its own Stop event was set before adapter.cancel waited on the probe's state lock; the probe never observed that event and reached its deadline. The fix forwards the current operation's cancellation plus the provider Stop event, mirroring the existing render callback. The admission-before-clear and concurrent-operation guards remain.

Source review was independently accepted twice. The unchanged actual-child test must first reproduce caller PASS/provider FAIL on the original public facade. The corrected source must then pass all four existing provider/probe suites with no skips. Exact source blobs and unchanged source hashes are recorded. No test assertion or timeout is relaxed; the 8c prepublication voice/WAV attestation remains in the runtime union.

This proof uses synthetic local worker actors and real child processes. It does not run owner voices, models, an installed desktop, audio devices, or NVDA.
