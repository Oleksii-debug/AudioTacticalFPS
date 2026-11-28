# Spelling review with an explicitly selected Hunspell runtime

## Status and scope

This optional Reader feature checks a chosen portion of the editable reading copy with an explicitly selected local Hunspell library and dictionary pair. It presents issues and suggestions for review. A correction requires Preview and Apply; Undo restores the previous reading copy.

The source and tests in this change have been prepared and independently reviewed for selected ownership and source-scope boundaries. Execution of this composed change is **NOT_RUN** until the exact candidate completes its required source, installed, C API and native Reader gates. Test counts below are static inventories, not passing results. No dictionary-quality, universal-language, grammar, acoustic or manual NVDA qualification is claimed.

The application supplies no Hunspell executable library or language dictionary. It does not download, discover or activate one automatically. The selected profile exists only while the spelling window is open. There is no cloud request, account transfer, persisted engine selection or new background worker.

## Open and use the review

1. Open Voice Reader for the desired retained source or editable reading copy in the current library. Spelling review needs a selected library, but it does not require an installed speech voice or an audio endpoint.
2. Select a text range if only that range should be checked. Choose **Spelling review with Hunspell** from the Reader's controls or its existing command list. No new default keyboard shortcut is assigned.
3. Select three distinct local files: a compatible standalone native Hunspell library, the matching `.aff` affix file and the matching `.dic` word file. Enter the dictionary language and the engine/dictionary origin or license notice. These fields record your explicit choice; they do not establish language coverage.
4. Choose **Review selected profile**. This reads and hashes the exact selected files without loading the library. The read-only identity field shows what was reviewed.
5. Choose **Whole reading copy** or **Original selected text**, then **Check spelling**. The existing Reader worker starts the bounded child process. No text changes during checking.
6. Select an issue and, if useful, a dictionary suggestion. You may enter a different replacement. An empty replacement explicitly removes that word.
7. Choose **Preview correction**, inspect the exact current range, then **Apply reviewed correction**. Each application is one undoable edit. **Undo last apply** reverses that edit while its exact applied snapshot is still current.
8. Use the Reader's normal save or export actions for the derivative. Automatic recovery of the edited copy uses the current Reader recovery store. The retained library asset and original source text are preserved.

Changing the selected files, profile fields, text, selection, source availability or current library invalidates the relevant earlier review. A same-size dictionary edit is also a change. Re-review the current files and check the current reading copy before applying another result.

**Cancel check** requests cancellation of the active operation. **Close** remains available while checking. The existing owner retains the child, staged files and resource reservation until native cleanup finishes. Closing a spelling window does not authorize a late callback to change a destroyed control or a different source.

## What the check means

The adapter invokes the selected library's Hunspell C API. Dictionary entries and affix rules determine what that specific dictionary accepts and suggests. A word absent from that dictionary is a review item; it is not proof that the word is incorrect.

Word scanning preserves Python character positions across Unicode characters, combining marks, emoji before words, line endings and apostrophes. Its finite letter/mark/apostrophe/hyphen heuristic is not universal linguistic segmentation. A dictionary's declared encoding is honored without replacement decoding. A token that cannot be represented in that encoding is reported as uncheckable. Tokens longer than the supported per-word bound are likewise visible as uncheckable rather than silently accepted.

This slice provides spelling suggestions and explicit single-word edits. It does not provide grammar correction, automatic whole-document replacement, dictionary editing, automatic language detection, a 135-language quality certification or a separate reader.

## Selected-runtime and source boundaries

The library is executable native code. Choose the engine and dictionary files deliberately. The adapter accepts explicit regular local files, rejects aliases or links that violate its file contract, hashes opened bytes with finite bounds, and checks their identities again before accepting the result.

The native child receives a fixed, bounded, hash-bound request file and fixed arguments. It makes private verified copies of the three selected files. The application does not pass arbitrary commands, discover companion DLLs through user directories, or allow the response to choose files. Missing native dependencies, invalid dictionary encoding or structure, native warnings, crashes, malformed replies and changed file identities fail the check.

The current library's existing ResourceBroker covers profile hashing, private staging, native work, response checking and cleanup. The declared operation uses one CPU slot and 1 GiB of memory allowance; these are admission declarations, not measured performance claims. The ordinary native deadline is 30 seconds. Input limits are 64 MiB for the library, 16 MiB for the affix file and 128 MiB for the dictionary; the request is at most 8 MiB. A review scans at most 20,000 word occurrences, with a 128-character native-word limit, at most 500 issues and at most 10 suggestions per issue.

The exact retained source and current library identity are checked before queued work and before accepting an edit. Original library content is not modified by spelling review. An in-memory edit remains visible and undoable if a later recovery save fails; the UI reports that separate save problem.

## Evidence required for this composition

| Gate | Required scope | Current status |
| --- | --- | --- |
| Portable domain tests | `tests/speech/test_spelling.py`, 54 statically enumerated cases | NOT_RUN |
| Shared-worker/modal tests | `tests/speech/test_spelling_handlers.py`, 20 statically enumerated cases | NOT_RUN |
| Actual Reader closure tests | `tests/speech/test_spelling_reader_hook.py`, 9 statically enumerated cases | NOT_RUN |
| Build-helper boundaries | `tests/packaging/test_hunspell_build.py`, 31 statically enumerated cases | NOT_RUN |
| Actual selected C API | `scripts/hunspell_acceptance.py`, tiny original dictionaries, actual child cancellation/deadline and installed-source proof | NOT_RUN |
| Native Reader | `scripts/windows_hunspell_smoke.py`, actual wx controls, Reader worker, Preview/Apply/Undo and live-child Cancel/Close | NOT_RUN |
| Dictionary-language quality | Owner-selected dictionary, representative text and human review | NOT_RUN |
| Manual assistive-technology review | Actual keyboard use and NVDA with the intended user environment | NOT_RUN |

The four whole test modules total **114 statically enumerated cases**. All are intended for both source and installed strict execution with no skipped or expected-failure cases. The installed Reader-hook tests retain their existing adjacent Find/Replace fixture. Test fixtures deliberately distinguish simulated native replies from the separate actual C API probes.

The headless installed acceptance requires 40 unique checks and 12 exact application module identities. The native Reader probe requires 28 named checks, 20 exact installed module identities, actual C API execution, actual Reader-worker use and owned-child cleanup. Controlled delay cases exercise cancellation/close ownership; they do not represent dictionary performance. Neither probe plays speech or qualifies an audio device.

See [HUNSPELL_WINDOWS_BUILD.md](HUNSPELL_WINDOWS_BUILD.md) for the pinned official source and the separate ephemeral build and execution gates.

## Upstream source and notices

The Windows qualification source is official Hunspell tag `v1.7.2`, resolved to commit `2969be996acad84b91ab3875b1816636fe61a40e`. This is a fixed reproducibility choice, not a claim that it is the latest release.

Official sources inspected on **2026-11-27**:

- [Pinned official source](https://github.com/hunspell/hunspell/tree/2969be996acad84b91ab3875b1816636fe61a40e).
- [C API header](https://github.com/hunspell/hunspell/blob/2969be996acad84b91ab3875b1816636fe61a40e/src/hunspell/hunspell.h).
- [Hunspell license notice](https://github.com/hunspell/hunspell/blob/2969be996acad84b91ab3875b1816636fe61a40e/license.hunspell).
- [Upstream Windows build project](https://github.com/hunspell/hunspell/blob/2969be996acad84b91ab3875b1816636fe61a40e/msvc/libhunspell.vcxproj).

The upstream notice offers MPL 1.1, GPL 2 or later, or LGPL 2.1 or later alternatives. The build recipe retains the exact source and its five declared notice files. Dictionary notices are separate inputs. This documentation does not select a redistribution license or grant permission to distribute a library or dictionary; no such payload is added to the application bundle by this feature.
