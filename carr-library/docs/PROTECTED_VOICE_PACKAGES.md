# Password-protected voice-profile transfer

The Reference voice packages window can export an explicitly reviewed installed
version as a new **.carr-voice.enc** file. The original **.dordiai-voice** and **.zip**
data-package routes remain available and unencrypted.

## Export

Review the exact installed version, separately confirm permission for protected
export, and select a new local destination outside the installation and research
library. Enter and confirm a password of at least 12 characters, with a maximum of
1024 UTF-8 bytes. Spaces and Unicode are preserved exactly.

The existing owner API creates the data-only package. Its exact reference files
and metadata are inspected again before encryption. The application then reuses
the authenticated version-1 envelope from its Sync capability, provided by the
pinned optional cryptography 50.0.2 dependency. A missing dependency is a failure;
there is no plaintext fallback. The output contains only the selected profile
version, never shared models or executable runtime code.

Publication is create-only. Its receipt identifies the ciphertext path, size and
SHA-256. A proven completed publication remains visible after a late Cancel or
cleanup warning. Retain such a file and resolve the warning before repeating work.

## Import and fresh approval

Choose **Inspect protected package** and enter the password. The complete
envelope must authenticate before the existing owner package inspector sees any
bytes. Decryption and owner inspection run on the window's existing worker under
one shared resource reservation. The private plaintext stage is removed before
a review is returned to the window.

Inspection does not install, activate or approve a profile. The resulting review
contains only exact ciphertext/plaintext/document and current-library/runtime
identities. Confirm reference rights afresh and explicitly select production
languages. **Apply reviewed profile** asks for the password again, decrypts and
re-inspects the exact same package and current registration, then delegates to
the existing owner installation and registration boundary. A changed package,
document, library identity or runtime registration requires a new inspection.

No password is stored in application state, backup metadata, package receipts or
child-process requests. The operation handoff drops a queued password on Cancel
or window destruction; an active operation retains only the arguments it needs
until settlement. Python/native memory cannot guarantee secure erasure, and a
process crash can leave a private temporary stage. A stage retained for uncertain
native cleanup stays owned until that exact child's shutdown is confirmed.

## Bounds and retained authority

The existing 8 GiB package limit remains in force. A protected export must also
fit its envelope overhead within that limit. Disk admission uses the actual
reviewed package allowance or captured ciphertext size, not a theoretical
multi-terabyte allowance. Crypto and owner inspection share the existing broker;
no new provider, runtime, registry, authority service or executor is introduced.

Restored voice settings remain inactive until explicitly revalidated. A library
backup never promotes a protected-package review into installation permission,
and the sibling private temporary import stages are outside library state.

## Qualification

Focused suites:

- tests/audio/test_protected_voice_packages.py
- tests/ui/test_protected_voice_packages.py

They use actual authenticated envelopes and the existing isolated synthetic owner
fixture, plus modeled native interfaces for cancellation, stale approval and
destruction. Their current new composition is **NOT_RUN** until the exact candidate
is exercised by the strict source and installed gates.

Actual Windows probe:

    python -I -B scripts/windows_protected_voice_smoke.py --source <exact-checkout> --report <source-report.json>
    python -I -B scripts/windows_protected_voice_smoke.py --expect-installed --report <installed-report.json>

The probe uses native password controls and keyboard input, real crypto, the real
existing worker and an isolated synthetic owner API. It verifies exact imported
module bytes and the installed wheel RECORD. OS file-picker interaction, manual
NVDA use, real reference rights, neural inference, similarity and language quality
are separate qualifications and are not claimed by this probe.
