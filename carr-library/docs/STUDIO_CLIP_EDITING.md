# Clip editing in the current Studio

The **Edit chapter clips** window uses the same structured script, retained PCM
assets, render cache and NarrationDraft undo/recovery as the existing Add, Move,
Remove and Preview actions. Source narration is not parsed as a production
script unless that mode is selected explicitly.

## Operations

- **Copy** captures one saved clip and binds the process-local clipboard to the
  current project directory and exact retained audio identity and SHA256.
- **Cut** removes the clip directive from the current draft and captures the
  same clipboard. It keeps the retained audio and an explicit duration for an
  otherwise empty chapter.
- **Paste** creates a new clip identity at the selected chapter's Start value.
  The original clipboard controls are preserved; changed audio or a different
  project folder requires a new Copy. New/Open project state does not authorize
  access to a different audio folder.
- **Split** divides the selected source interval at an interior whole sample.
  Source and output sample intervals must remain consecutive and keep the same
  total duration. An interior cut through an existing fade is refused because
  the current clip model has no continuation envelope.
- **Join** combines adjacent selections of the same retained source, with
  matching track, gain and pan and no interior fade. It accepts either selection
  order and preserves the first clip identity. Consolidating arbitrary sources
  or different processing into a new rendered asset remains a separate workflow.

All operations retain the32-clip chapter bound and current typed numeric
validation. No audio source is deleted or rewritten. Apply results are accepted
only if the captured Studio draft and output folder still match; stale results
cannot replace the current draft or clipboard. Copy alone does not add an undo
entry. Cut/Paste/Split/Join use the existing draft undo history.

The clipboard is deliberately session-local; saving a project persists the
resulting script and retained inputs through existing project/backup producers,
not a new global clipboard store. Preview still uses the existing owned FFmpeg
worker, resource broker and output quieting. The extra fields are in a native
scrollable panel so controls remain reachable without extending the dialog
beyond its previous height.

## Acceptance boundary

Source regressions cover real PCM retention, exact split/rejoin sample windows
at8/16/44.1kHz, empty-chapter cut/paste, changed-source and cross-project refusal,
scene limits, cancellation, stale draft rejection and undo. The existing native
Windows clip probe now drives Copy, Cut, Paste, Split and Join in addition to its
previous actions. Its control checks are separate from physical-device/NVDA
acceptance.

This candidate was prepared while the local executor was unavailable.
**No execution is claimed until the composed source/native CI completes.**
