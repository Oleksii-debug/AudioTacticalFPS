# Scoped pronunciation in the existing reading editor

This change adds language and voice scopes, case-insensitive literal rules, and bounded regular-expression matching to the current `reader.narration` projects and native reading editor. It reuses the current pronunciation preview, revision history, source provenance, save coordinator and export destination approval.

The behavioral source is the canonical `speech/reading.py` and `speech/regex_worker.py` at `c9cdacd93dd57e8644889761a44b01b59c4fda20` (regex worker blob `630f6d9a6b5a90355125b932cc469fe4d628f883`). The alternate speech runtime, reader store and studio are not imported.

## Compatibility and rule meaning

Existing version 1/2 reading projects remain readable. Ordinary literal rules still write version 2 with exactly the existing three fields: `written`, `spoken`, `language`. Their optional language value remains a descriptive label, matching previously saved behavior. Saving or opening an old project does not silently change which rules apply.

New rule fields are `language_scope`, `voice_id`, `regex` and `ignore_case`. All default to false/empty. A language filter is active only when `language_scope=true`; an explicitly named voice is always a filter. Projects containing any new capability write version 3, with all seven rule fields explicit. Older schema versions reject injected scope/pattern fields instead of activating them without a version change.

Example:

```json
{
  "written": "\\bword\\b",
  "spoken": "слово",
  "language": "uk",
  "language_scope": true,
  "voice_id": "chosen-engine:chosen-voice",
  "regex": true,
  "ignore_case": true
}
```

The preview asks for the language and voice context when scoped rules are present. Language matching is case-insensitive and otherwise exact: `uk` does not implicitly match `uk-UA`. Voice IDs match exactly. An empty context uses only rules without that scope. A saved voice ID is data; it never installs, locates or executes a voice engine.

The current longest-match, non-cascading behavior is retained. At the same source position, the longest match wins; ties prefer the more specific voice/language scope, then dictionary order. Replacements are never scanned again. This makes imported canonical dictionaries explicit: scopes and patterns transfer, while the current reading editor's replacement semantics stay stable.

## Pattern isolation and cancellation

Case-insensitive rules and regular expressions run in a fixed package-owned Python worker with `-I -B -S`. Text and rules travel through stdin JSON, never a shell or executable argument. The existing local worker supervisor owns cancellation, deadline, bounded diagnostics and process cleanup.

The default pattern deadline is one second; callers may select 0.05–10 seconds. The worker accepts bounded text and rules, returns at most 10,000 matches and 900 KiB of JSON, and enforces a 512 MiB address-space ceiling on POSIX where the resource API is available. Windows uses bounded requests/responses and the same supervised deadline; a native Windows heap ceiling is not claimed.

Regex replacements support Python capture expansion. Literal replacements remain literal, including backslashes. Invalid expressions, invalid capture references, match explosions, cancellation and timeouts return no partial transform. Preview produces a candidate only; applying it remains an explicit editor action and a reversible reading revision. The original source and quotation authority are unchanged.

The native editor performs preview work outside its UI thread. Cancel preview and Close signal cancellation. Close waits for the owned preview to settle, then uses the existing unsaved-draft/save-completion flow. Generation and closed-state checks suppress late callbacks. Save All reports an unfinished preview instead of claiming a durable save.

## Dictionary interchange

The current unversioned literal JSON list remains accepted. The canonical `carr.pronunciations.v1` envelope is explicitly imported by mapping `pattern/replacement` to `written/spoken` and preserving its language, voice and flags.

New exports use `kind=carr-pronunciation-dictionary`, `version=1`, with complete rule fields so descriptive legacy labels cannot accidentally become language filters. Unknown fields, duplicate effective scopes, non-boolean flags, invalid voice identifiers and oversized dictionaries fail visibly. JSON is limited to 1 MiB, with at most 1,000 rules and the existing 64 KiB aggregate phrase/replacement budget.

The native Import dictionary and Export dictionary actions use existing checked local readers and captured destination approval. They cannot overwrite the immutable external reading source. Exports include identifiers and substitutions only, without executable paths, voice resources, credentials or model bytes. Complete library backup uses the existing validated narration producer and preserves version 3 rules.

## Acceptance status

The new source includes real child-process timeout/cancellation tests, Unicode and capture tests, scoped-rule selection, zero-width matches, output bounds, legacy/versioned roundtrips, canonical dictionary import, complete-backup restoration and late UI callback/failure tests.

This candidate was prepared after the executor became unavailable. Its new tests have not run. Required source/installed gates are `tests/reader/test_scoped_pronunciation.py`, `tests/ui/test_pronunciation_preview.py`, existing `tests/reader/test_narration.py` and `tests/ui/test_narration_editor.py`, reading backup suites and the existing native reading/Save All gates. Windows/NVDA acceptance and language-specific pronunciation quality require separate actual results.
