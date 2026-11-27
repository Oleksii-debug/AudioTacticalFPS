# RW-15 — installed Windows spelling review

The reader's **Review spelling** command checks an explicit passage, or the
current reading copy when no passage is selected. It lists the languages
reported by the installed Windows Spell Checking API. The user must select one
exact language; there is no automatic regional or voice-language fallback.
Missing dictionaries remain an unavailable state. This feature never downloads,
installs, adds words to, ignores words in, or otherwise edits a dictionary.

Checking runs through the reader's existing single executor and an owned,
time-bounded isolated Python child. The child calls only the Windows factory's
language enumeration/support/creation methods, ISpellChecker.Check, Suggest,
and the read-only spelling-error properties. Every COM interface is released;
returned strings are freed with CoTaskMemFree. Cancellation and the 20-second
default worker deadline use the existing owned-process cleanup path. If native
cleanup is uncertain, the private request remains owned until its process family
has confirmed shutdown.

Input is limited to 50,000 Unicode characters per explicit review, 256 issues,
256 installed language tags, and 1 MiB of reply data. Each issue shows at most
the first 12 suggestions, with an explicit notice when more were returned.
A larger document can be checked in selected passages. UTF-16 native offsets
are converted to Python character offsets only at valid character boundaries;
foreign, overlapping, malformed or request-mismatched replies cannot authorize
an edit. Text is passed in a private temporary file, not in command arguments.
The worker executes its exact captured/hash-checked source bytes, bypassing
unchecked bytecode caches.

Every correction is previewed. Apply rechecks the exact reading snapshot,
current source access, cancellation and owner lifetime, then uses the existing
ReadingDocument.replace history. Native Replace and Delete recommendations
are still proposals requiring the same explicit acceptance. Original text,
original hashes, quotation authority and source bindings are preserved.
Applying one correction invalidates the prior issue list; the user checks again
before another correction.

## Primary API references

The narrow ABI declarations follow Microsoft's spellcheck.h in
[microsoft/win32metadata](https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/um/spellcheck.h),
reviewed header blob d4fbec568050a729a33343ff5ddfed4206a7fa5d.
The implementation uses fixed-width HRESULT/ULONG fields and the documented
vtable order rather than generated platform-dependent declarations.

- [ISpellCheckerFactory](https://learn.microsoft.com/en-us/windows/win32/api/spellcheck/nn-spellcheck-ispellcheckerfactory)
- [ISpellChecker.Check](https://learn.microsoft.com/en-us/windows/win32/api/spellcheck/nf-spellcheck-ispellchecker-check)
- [IEnumString.Next and caller-owned returned strings](https://learn.microsoft.com/en-us/windows/win32/api/objidl/nf-objidl-ienumstring-next)
- [Microsoft CsWin32 example](https://github.com/microsoft/CsWin32/blob/main/test/SpellChecker/Program.cs),
  reviewed blob b15da68d11147f3c4e4b7ade7cd560a46ff607a9.

## Qualification boundaries

Portable tests cover exact reply/source binding, UTF-16 boundaries, bounded
enumeration and string ownership, actual isolated-child startup/cancellation/
timeout, changed worker bytes, unchecked bytecode, pending cleanup, and guarded
review/apply/undo through the existing reading model. Modeled COM calls are not
native dictionary execution.

The native command is:

    python -I -B -X utf8 scripts/windows_spelling_smoke.py --report spelling-native.json

The installed package and all seven relevant module paths/hashes are recorded
and compared with the checkout when available. A source-only run requires
--source . For a sealed bundle whose application is outside purelib, its
existing wrapper must supply --installed-root <verified-import-root>; that
argument validates origins and does not add an import path.

The probe requires the **already installed exact en-US dictionary**, checks
actual OS-reported errors after a non-BMP character, drives native Check,
Preview, Apply and Close controls, and verifies unchanged source plus Undo.
An absent dictionary is a failure of that qualification prerequisite, not a
synthetic pass or a silent fallback. Human NVDA behavior and spelling quality
for other languages remain separate checks.

At source checkpoint creation, the executor was unavailable. The new tests and
actual Windows probe remain NOT_RUN until execution against these exact bytes.
