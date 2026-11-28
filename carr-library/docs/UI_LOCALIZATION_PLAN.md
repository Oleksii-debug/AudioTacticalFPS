# English-first UI localization plan: Ukrainian and Slovak

This plan addresses the UI-language commitment in
[the V3 audio-agent specification](UNIVERSAL_SCOPE_V3_AUDIO_AGENT.md).
English remains the reference interface while Ukrainian (`uk-UA`) and Slovak
(`sk-SK`) catalogs are prepared and qualified. This document records intended
work and acceptance criteria; it does not claim that either translated interface
has shipped or that its language or screen-reader quality has been approved.

UI language is separate from a document's language, speech-preparation rules,
selected voice, OCR languages, ASR profile and translation target. Changing the UI
must not rewrite retained originals, reading derivatives, citation fields,
project content or saved operation identities.

## Scope and stable contracts

Prioritize the library, Import Center, reading controls, Search Center, Studio,
queue, publishing, backup/restore, diagnostics and Help. Include visible menu
labels, button labels, status/error messages, accessible names, instructions and
confirmation text. Keep English technical names where translation would obscure
a protocol, format, provider or product identifier.

Stable command IDs, shortcut actions, enum values, JSON field names, SQLite
schemas, hashes, citation anchors and automation/CLI machine output remain
language-independent. Translated display labels must resolve to those same IDs.
Do not derive command lookup or authorization from translated text. Existing
saved keyboard bindings and projects must continue to load without migration.

## Delivery sequence

| Stage | Concrete deliverable | Acceptance required before the next stage |
| --- | --- | --- |
| Inventory | Map user-visible English strings to their current UI or service owner, including accessible names and asynchronous error paths | Every priority workflow has an owner and complete visible-state inventory |
| Catalog boundary | Add one small local catalog layer with stable message keys, English fallback, named placeholders and explicit locale selection | Missing entries fall back visibly and safely; placeholder mismatches fail validation; no network or translation-provider call |
| First translation pass | Produce Ukrainian and Slovak catalogs from the same reviewed English source, with a glossary for recurring library, audio and recovery terms | Native-language review of complete workflows and all destructive, financial, privacy and recovery instructions |
| Integration | Replace display strings in the existing dialogs and status adapters without changing operation ownership or persistence | Current English behavior, command IDs, worker cancellation and byte-authority tests remain valid |
| Native qualification | Exercise translated menus, controls, long statuses, errors and modal lifetimes on the supported desktop runtime | Keyboard, layout and accessibility evidence on each selected locale, with physical/NVDA review recorded separately |

The first catalog should remain packaged locally and available offline. Save the
explicit UI choice in existing user settings with strict allowed values. If an
in-place language change cannot safely recreate current native controls, require
closing current operations and restarting the interface; never rebuild controls
under an active worker. A missing or damaged selected catalog should use English
and a fixed local notice, without changing content language.

## Translation rules

Use complete messages with named fields rather than concatenating translated
fragments. Validate that every translation preserves exactly the required fields.
Keep raw paths and document content out of translation templates that have no
need to display them. Diagnostics retain their fixed machine-readable whitelist
even when surrounding UI labels are translated.

Review Ukrainian and Slovak grammatical number forms for counts, durations and
remaining work. Format display-only dates, numbers and units by the selected UI
locale; persisted timestamps, byte counts, monetary authorization fields and
machine-readable exports retain their existing canonical formats. A UI locale
does not choose a decimal parser or reinterpret an existing document.

Maintain a glossary that distinguishes a workspace, library, source, reading
copy, saved result, recording, queue, checkpoint and backup. Review terminology
in context. Translator notes should explain an action's consequence, including
whether it creates a new file, changes a derivative, starts a local model or
needs a separately approved network operation.

## Accessibility and regression evidence

Native menu mnemonics need collision checks within each translated menu, while
user-configured shortcuts remain bound to stable command IDs. Check tab order,
focus restoration, read-only previews, error announcement and cancellation
through the actual controls. Use longer translated strings and enlarged text to
detect clipped buttons, hidden status text and inaccessible scroll regions.

Automated coverage should verify catalog completeness for the declared scope,
exact placeholder sets, English fallback, locale-setting validation and stable
command resolution. Reuse the existing native probes with locale-specific
expected labels; keep their actual keyboard, owner-lifetime, source and
publication assertions. Test translated failure and recovery routes as well as
successful operations.

Record the tested commit, catalog version, runtime, locale, automated outcomes
and any human review. Unreviewed wording and unperformed NVDA/physical checks
remain explicitly pending. Release a locale only for its documented, completed
scope, keeping English available as the reference and fallback.
