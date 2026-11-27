# Canonical Studio project recovery

This bridge reads published schema-one projects from canonical commit
4b6f8169986c4a58b43e4a23bc081fbefadbbdcb and the exact schema-two clip extension
from PR152 commit 934b632f98098d55253354e0c6f7d8d482b73461, normally saved as
state/studio/projects/<project-id>/project.json. A selected revision JSON uses
the same schema. It does not start the canonical ProjectStore or renderer.

The user first reviews one selected source and the exact current local input
files. Acceptance creates a fresh current audio-projects/<uuid> document.
The original JSON bytes, including whitespace and line endings, are retained
inside a typed canonical-studio-project source reference and bound by SHA-256
to the draft's original source identity. The original script remains the
NarrationDraft source; the translated explicit script is a reversible edit.
The original file, other revisions, original audio and jobs are unchanged.

The existing current parser verifies the converted roles, voice identifiers,
language, rate, volume, gain, pan, scene titles, spoken text, pauses, procedural
music controls, sample rate, scene minimum duration and positional clips. Schema two also
preserves microphone tracks, trim start, selected duration, fades and clip titles;
overlapping fades unsupported by the current clip model are explicitly rejected.
Old project, scene, line and cue identities remain in the retained original;
the new managed project and generated event identities are independent.
Opaque old source labels do not become research quotation authority.

## Explicit boundaries

- A cue is read only inside a caller-selected input root. Foreign or moved paths
  need an explicitly selected replacement keyed by the exact scene/cue identity.
  There is no basename search or automatic path rebinding.
- The existing retain_pcm_asset copies checked PCM16 WAV/RF64 bytes into the
  current project's audio/assets directory. No shared source inode remains.
- Existing voice identifiers are preserved; a caller may explicitly replace
  a particular role's voice. Empty old voice/language values use the caller's
  visibly selected default controls. No voice/model installation or synthesis
  occurs during import.
- Unsupported or ambiguous mappings fail before publication: unknown fields,
  multiline or trimmed spoken text, unrepresentable role/title text, tiny
  unsupported pauses, unsupported music ranges/moods and nonprocedural music
  engines. Old model/executable configuration is never activated. The source
  remains available for an explicit edit or later supported conversion.
- Previously rendered output is not claimed as the current renderer's result.
  Rendering starts through the current review, engine-profile and admission
  paths. A schema conversion is not an audio fidelity or model qualification.
- The library writer, new project's draft lock and existing audio project lock
  are acquired in that order. The complete staged project directory is published atomically only after
  every chosen cue is retained and captured source identities are rechecked.
  An interrupted/failed import removes only its newly reserved project folder.
- Complete backups use the existing typed Studio validator. Retained source
  JSON remains inert after restore, and current managed outputs relocate through
  the existing Studio controller. Current state/studio/recordings is untouched.

## Integration

Depends on the current clip/parser extension from
6b6948c1d4a213a1f771e2eedffd4ae7a1cbd5e6 and its f129 publication follow-up. The independent UI helper calls
review_project and import_reviewed on the existing Studio executor. The current
controller validates this new source reference on open and protects its
original_path from Save As, using the existing protect_original_path helper.

Required regression file: tests/audio/test_canonical_project_import.py.
It covers semantic conversion, exact source retention, scoped independent
PCM copies, changed inputs/root, cancellation and publication faults, UUID
collision, malformed provenance, and complete-backup relocation.

The initial source candidate was prepared during an executor outage. Actual
validation results are recorded with the final integration checkpoint; native
Windows execution remains a separate gate. It contains no private project, engine payload, model or credentials.

## Observed validation on 2026-11-27

On the composed 4be source plus this bridge, Python 3.12.14 executed 128 source
checks across the new core/UI orchestration suites and existing Studio/project
backup/input-asset suites: all passed, no skips. The two new required suites also
ran through the existing strict offline runner: 38 core + 6 UI cases passed,
zero failures or skips. These UI cases use synthetic native widgets.

A fresh wheel was built and installed into a separate target. Both carr_library
and bootstrap imports were asserted to come from that installed target; all 44
new cases then passed under the existing offline Python audit policy, with a
checked JUnit report and no skips. Actual Windows native dialogs, physical audio
and model inference were not exercised by this Linux validation.


