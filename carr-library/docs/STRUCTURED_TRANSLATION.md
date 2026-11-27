# Structured document translation through the current service

## Behavior

The translation workspace now accepts an explicitly ordered selection of up to
32 current library documents, inclusive source-position ranges, or actual EPUB
chapters. Its catalog has bounded 50-book pages. Selected text remains available
as a separate editable source. Switching to editable text explicitly discards
the retained-source context and requires a new review.

The shared ReadingDesk adapter captures exact active revisions, retained asset
hashes, metadata, page order and source ranges. It revalidates the whole selection
after capture and again before each remote request. Captured text verification
flags are history only. Neither a translation nor its exported provenance grants
quotation authority.

The structure profile keeps paragraphs and blank lines. Explicit Markdown mode
also retains ATX heading levels and markers and fenced code without sending the
code as translatable text. The original UTF-8 bytes and line endings remain
unchanged. This profile does not claim arbitrary Word/PDF page-layout preservation
or complete Markdown interpretation.

## Existing provider and recovery owners

Plain Basic v2 and Advanced v3 jobs retain their released schemas 1 and 2.
Structured jobs use schema3 in the same state/translation namespace and current
TranslationService, provider gateway, credential vault and integer USD ledger.
Advanced jobs retain exact project/model/glossary receipts. Literal sections
have no remote request, reservation, operation identity or reported model.

Preview binds profile, text, structure, language inventory, pair, glossary, title
and reviewed budget. Queuing sends no text. An explicit Run uses the exact saved
job digest under its native job lock. Durable request intent precedes every
remote call; accepted chunks are saved before progress/cancellation. Uncertain
charges remain Attention and are not automatically retried.

Complete backup validates the exact historical sealed source pages and metadata
in addition to the pure job envelope. Historical revisions and archived libraries
remain valid recovery data. Restore preserves exact job bytes and offline export,
while original root/profile/ledger approvals cannot authorize new requests from
the restored copy. Provider credentials and billing ledgers are not in the backup.

## Exports

The existing create-only export produces original.txt, translated.txt, bilingual.txt,
an editable narration derivative and the final translation-provenance.json.
Structured jobs additionally export structured-translation.json and escaped
structured-bilingual.html. The original/source-context records, section order,
heading levels and hashes accompany the derivative. No original document is
overwritten. A final provenance manifest marks a completed export.

## CLI

The existing launcher adds the translation command. Configure/select a provider
through the current connection workflow; secrets are never command arguments.

    python -m carr_library translation --data-dir LIBRARY profiles
    python -m carr_library translation --data-dir LIBRARY languages --profile PROFILE
    python -m carr_library translation --data-dir LIBRARY preview --profile PROFILE --input SOURCE.md --markdown --source en --target uk --budget-micros 1000000
    python -m carr_library translation --data-dir LIBRARY inspect --job JOB
    python -m carr_library translation --data-dir LIBRARY read --job JOB --part structure
    python -m carr_library translation --data-dir LIBRARY export --job JOB --destination NEW_DIRECTORY

Language refresh additionally needs --refresh --allow-network and
--approve-profile with the inspected fingerprint. Queue uses the same reviewed
source options plus --approve-profile, --approve-preview and --approve-transfer.
Run needs --job, --allow-network and --approve-job-sha256. Reinspect a paused job
before an explicitly reviewed continuation; a stale digest cannot run.

Use --selection-json instead of --input for a UTF-8 array of ordered selection
objects. Each object supplies workspace, edition, kind and selected first/last
positions; topic and expected title are optional. The whole-document profile
starts at1 with no last position. A range requires both first and last. Chapter
means one actual EPUB reading chapter, with first equal to its ordinal.
The CLI binds an existing library read-only for source capture and never creates
or migrates a missing research database.

## Verification boundary

New tests exercise real SQLite sources, actual durable provider jobs with synthetic
transport, partial resume, strict schema/byte bounds, Basic/Advanced receipts,
current native handler workers, CLI isolation and complete backup/restore/rebackup.
The native wx probe selects an actual retained book through the source-picker
dialog and exports its structured derivative. Network answers remain synthetic;
real paid-provider quality and billing are separate owner-account acceptance.

These reconstructed source/tests are NOT_RUN locally while the executor is
unavailable. Required source, installed and Linux suites and the existing native
translation probe must execute on the integrated source. Source review alone is
not a PASS result. The original 17/53 dependency profiles are unchanged.
