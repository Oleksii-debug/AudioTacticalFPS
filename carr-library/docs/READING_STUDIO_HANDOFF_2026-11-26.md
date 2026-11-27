# Reading-copy to Audio Studio integration checkpoint

Development session: 2026-11-26. Source only; no voices, generated recordings,
model weights, credentials or user documents are included.

## Implemented behavior

The reading-copy editor has an explicit **Save and open Audio Studio** action.
Local voice discovery runs in a cancellable worker. An explicit voice choice is
required; missing voices do not trigger a download or substitution. The action
saves the current reading revision and creates a separate, unrendered studio
project. Closing the nested Studio returns to the same reading editor.

The project retains the authored text, reading-copy identity and saved digest,
derivative/original text hashes, and the exact historical source location when
present. A derivative gives no quotation approval. Pronunciation replacements
transfer only when already applied to the reading copy. Source files and research
evidence are preserved; independent authored text gets no fabricated library
source. Revoked or damaged sources and failed reading saves block the handoff.

An unchanged native text display cannot silently normalize saved CRLF into LF.
Actual editing still updates the reading copy. Discovery cancellation and closing
the editor suppress late UI callbacks. Speech discovery forwards cancellation to
the existing installed-engine adapter rather than starting an unrelated helper.

## Observed local verification

| Suite | Final observed result |
| --- | --- |
| tests/ui/test_narration_studio_handoff.py | 19 passed; strict ci_check, zero failed/skipped |
| tests/ui/test_narration_editor.py | 13 passed; strict ci_check, zero failed/skipped |
| tests/ui/test_command_registry.py | 107 passed; strict ci_check, zero failed/skipped |
| tests/speech/test_local_speech.py | 18 passed; strict ci_check, zero failed/skipped |
| tests/ui/test_keyboard_manager.py | 29 passed; strict ci_check, zero failed/skipped |
| tests/ui/test_reader_keymap_context.py | 11 passed; strict ci_check, zero failed/skipped |

The handoff suite used actual reading/project SQLite persistence and an audio-free
studio backup/restore. It checked Unicode and exact text, source/derivative hashes,
historical revisions, original bytes, explicit pronunciation application, failed
saves, scope revocation, source corruption, native-display newline preservation,
and real background-thread cancellation/dispatch. Voice identities were explicitly
synthetic; synthesis quality and listening are not established by these tests.

The extended Windows reading probe creates real nested wx dialogs, cancels one
background setup, chooses an explicit synthetic voice, inspects the saved Studio
project, and returns to the reading editor. Its actual Windows run belongs to the
integration workflow. Local compilation passed; no Windows, NVDA, or listening
result is inferred from the headless Linux tests.

## Recovery and source identity

The execution host briefly became unavailable during a final edit. Successful
source/test/doc changes were reconstructed through GitHub blobs without updating
refs. When the host returned, all five reconstructed files matched their original
local Git blob identities exactly. The interrupted CRLF-display edit had not
applied; it was then deliberately applied and the final 19-test handoff suite
passed. There is no remaining uncertainty about that edit.

These verified blobs preserve the completed handoff changes. They are checkpoints,
not a claim that exact-commit Windows acceptance has finished.

| Path | Verified Git blob |
| --- | --- |
| src/carr_library/ui/narration_editor.py | a7d4943f77aa3c855d79157529000e7e8dad7ce4 |
| tests/ui/test_narration_studio_handoff.py | 8571e2e98fcb7102300a67b6eca748902a86aedc |
| scripts/windows_v3_reading_smoke.py | 67dc57646c447042e6595a530f78602e51f21a3b |
| src/carr_library/speech/local.py | 0fb960b54ddeea0c1a5d589655e0a84f577ff3c2 |
| src/carr_library/ui/builtin_keyboard_commands.py | f75ef9b98c9817ddd1aa912b78a4003624c7140e |
| src/carr_library/ui/reading_desk.py | d03016c344feca2fd6650f20e9daef3f2f3eb9b2 |

The required Windows source-suite list must include
`tests/ui/test_narration_studio_handoff.py`. The existing source and installed
calls to `scripts/windows_v3_reading_smoke.py` exercise the extended native handoff.
The complete-backup integration has its own acceptance evidence; reconcile its
documentation before declaring final backup coverage.
