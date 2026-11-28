# Reviewed assistant file and native-control tools

The versioned local assistant allowlist adds document.convert, audio.effects,
reader.control and player.control. The original nine tools and plan schema remain
compatible. A plan is still previewed and explicitly run with its selected scope.

## Reading derivatives

Convert "C:/Books/source.docx" as "reading.txt" captures the selected source's
SHA-256 while preparing the review. The Ukrainian command is
Конвертуй "C:/Books/source.docx" як "reading.txt".

Execution calls the existing isolated ConversionService and shared ResourceBroker.
It does not select converter binaries or models from model-generated arguments.
Unavailable optional formats are refused during preview with the existing
Document conversion action identified as the place to select their runtime.

The output is a new UTF-8 .txt file and its .provenance.json file. The original
is never written. A reading derivative is not automatically a verified quotation,
and logical segments do not become invented printed-page numbers.

## Effect edits

Effects "C:/Scripts/source.txt" as "edited.txt" for "Narrator": [{"op":"add","effect":{"kind":"gain","parameters":[["gain_db",-6]],"enabled":true}}]

The Ukrainian command accepts Ефекти, як and для. The reserved target master
(or майстер) selects the master chain. A typed JSON audio.effects step requires
path, source_sha256, name, target and changes. The explicit operations are add,
edit, move, bypass, remove, set-chain and master-mix. Indexes are zero-based.

The actual RoleEffectEditor preserves spoken scenes, source comments and unrelated
directives. Master timing restrictions, effect parameter limits and the sixteen
effect chain limit remain enforced by the existing model. The result is a new
script plus a provenance report, not rendered audio or a silently changed Studio
draft. The user may review that script in the existing Audio Studio.

## Publication and recovery

Both tools use existing local input/output scope checks, stable verified reads,
source checksums, a two-file output budget and atomic no-replace publication.
The complete pair is prepared privately inside the chosen output folder.
The job stores the exact operation ID, two approved filenames, lengths and hashes
before publishing text and then its provenance report.

If interrupted after the first move, resume reconciles the exact existing text
and remaining prepared report. It never substitutes the current revision of the
original, reruns an already prepared edit or overwrites conflicting bytes.
A failed intent write leaves no final output; an uncommitted staging directory
requires review. Unexpected staging files are preserved, not recursively deleted.
A late Stop after both atomic moves preserves the completed outcome.

The helper uses the existing cooperative local-file boundary. It does not claim
protection against an administrator changing the process or filesystem.

## Native commands

The assistant action inside the selected live source reader supplies the current
source location, ephemeral window token, reviewed voice/playback settings and,
for player commands, the exact prepared PCM identity. A desktop assistant without
that selected live reader cannot approve those commands.

The job durably records a deterministic one-time dispatch ID before it calls the
native UI bridge. A completed receipt contains observed source selection, PCM
and player state. If execution was interrupted without a completed receipt, the
same job refuses replay and requires a new review of the current reader.
Playback-start receipts mean that a command started; they do not claim a whole
book was read or that physical audio was heard.

Complete-library backup preserves the typed jobs and receipts. Restoring them
does not reconnect a window, grant new filesystem scope or run a pending command.

## Qualification

The new file and shared-service tests declare 87 cases: 63 derivative/source/
publication cases and 24 durable interactive-service cases. The native-control
component has a separate 64-case contract/UI population and 26 native checks.
These counts describe required gates, not completed results. Actual qualification
must record the exact tested commit, complete source and installed collections,
wheel/source identities and native outcomes. The gate keeps all prior assistant
suites. Manual NVDA and physical playback remain separate checks.
