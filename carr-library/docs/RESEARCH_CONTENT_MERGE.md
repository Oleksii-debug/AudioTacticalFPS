# Reviewed research-content merge

The **Library → Merge research content from two backups** command creates a new library from two independently verified full backups. Its supported profile is `published-research-content/1`.

This is a selective research-content merge. Full backup and fresh-destination restore continue to preserve their full supported profiles. The two input backups remain unchanged and retain every omitted item.

## What the profile preserves

The selected whole workspaces retain their exact workspace, work, edition, asset, revision, topic, evidence, tag, saved-query and academic-reference identifiers. It preserves published source history, sealed raw text and locators, source metadata and review state, collections, research notes and quotations, and their required retained originals. Existing canonical publication receipts remain records of the original publication; operational requests are not replayed.

Only internal SQLite page row IDs are assigned anew. Current search indexes are rebuilt from the selected canonical content and checked by the existing storage validator. References and quotation anchors are not retargeted to another edition or revision.

If two selected inputs contain an incompatible global identifier or asset description, the merge refuses that selection. It does not silently rename identifiers, combine fields or reinterpret an evidence anchor.

## What is omitted

The exact preview lists the operational, audio and settings sidecars present in the inputs. This profile omits import queues and checkpoints, audio projects and generated media, recording and playback state, reading drafts, translation operations, local preferences and other nonresearch sidecars. Both original full backups remain available for a separate full restore of that material.

A required original, raw revision, evidence dependency or academic dependency cannot be omitted to make an invalid research graph pass. Missing or inconsistent dependencies are errors.

A broader merge of operational, audio and settings profiles requires separate conflict and ownership policies. It is not provided by this command.

## Review and create a new library

1. Choose **Merge research content from two backups** in the Library menu, or use its command-palette entry. The stable command identifier is `backup.research.merge`.
2. Select the left backup folder and enter its expected manifest SHA-256. Do the same for the right backup. Supply checksums obtained through the existing backup workflow, independently of the selected archive contents.
3. Choose a destination folder that does not exist. It must be outside both input backups. Review the explicit backup resource allowance when needed.
4. Inspect the exact preview. It contains both input checksums, the proposed destination, resource policy, workspace states and omitted files. The preview SHA-256 identifies the bytes being approved.
5. Choose the complete left workspace, complete right workspace, or omission for every workspace. **Select all nonconflicting workspaces** is an explicit convenience action; unresolved conflicts still require a choice. A workspace conflict is resolved as a whole snapshot, including its history and references.
6. Acknowledge the omitted nonresearch files and choose **Create new research library**. Open the completed destination separately after the completion report. The command does not switch or modify the current library.

A changed current library, input identity, checksum, preview, source graph or destination invalidates the operation. No existing destination is overwritten.

## Publication and cancellation

Both archives are verified and restored into private staging before a preview is issued. The same inputs are verified again for publication, and the selected graph is copied inside a transaction. The existing backup writer and fresh-destination restore publish the final library.

Cancellation and failures before publication leave the input backups unchanged. Existing restore markers make an interrupted final destination explicitly incomplete rather than silently openable. A genuinely completed final restore remains a completed result if cancellation or window destruction arrives afterward; a late UI callback cannot publish a new action or revive a closed window.

The command uses the existing Desktop worker. Closing its review or Desktop withdraws that operation's authority without clearing another operation's cancellation state.

## Resource and acceptance limits

The caller selects a typed backup resource policy. Archive contents cannot raise the policy. Staging admission accounts for both verified inputs and the private materialization/publication copies; insufficient disk, memory, time or metadata allowance causes refusal. The current selective profile also bounds the preview to 10,000 workspaces. This is not a claim of arbitrary-size merge throughput.

Qualification is tied to the exact tested commit. The component checks use actual full backups, retained source history, evidence, academic records, publication receipts, genuine transactions and real worker completion. The maintained final-head checks are the authority for the integrated release. Native keyboard, physical assistive-technology and large-corpus results must be reported separately when actually executed.
