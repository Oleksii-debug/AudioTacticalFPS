# One transcription entry, two explicit retained protocols

This composition reconciles canonical e69ba2403515f21509686d66f4bc8b3673756d81 with the strict workspace service published in 9f0ea29e5bcd2fc1f34c1ef1607d08c6bce4fd6a. It reuses the canonical local-file adapter and its bounded validators and keeps the earlier supplied-worker receipt contract.

## Compatibility

| Entry | Protocol | Retained store | Scope and authority |
| --- | --- | --- | --- |
| TranscriptionService(app, workspace) | carr-transcription-receipts/1 | state/transcription/workspace-sha256/jobs | Fresh library authorization; strict declared capabilities, model inventory and six requested exports when word timestamps are selected |
| TranscriptionService(root, protocol='local-file-job/1') | local-file-job/1 | state/transcription/profiles and state/transcription/jobs | Explicitly inspected and trusted interpreter, worker, installed distributions and model profile; canonical verified local-file checkpoints |

The root-only constructor remains compatible with the accepted canonical API. The workspace constructor, DEFAULT_SETTINGS, helpers and monkeypatch seams stay in carr_library.transcription. The old transcription.py is explicitly moved to transcription/__init__.py; it must not remain alongside the package. No failed result is decoded through the other protocol.

The installed transcription menu asks which reviewed worker protocol to use, then opens the corresponding controls through one installed action and the existing keyboard/menu capture. Existing direct show_transcription and build_transcription_dialog call sites remain available for their exact native probes. There is no second ASR implementation or model download.

The single CLI accepts --protocol carr-transcription-receipts/1 or --protocol local-file-job/1. Existing --workspace commands select the strict protocol; accepted canonical root-only command shapes select the local-file protocol. Supplying workspace authority together with the local-file protocol fails. Both transcribe and transcription command names are host aliases to this one CLI.

## Process and resource ownership

Both adapters use the existing bounded process runner with owned_tree=True and the shared resource broker. The same job cannot start again while its original worker family remains uncertain. A pending cleanup transfers the job lock and resource reservation to the existing CleanupOwner, including when saving the review status fails. File locks permit release on the cleanup owner's completion thread. A returned status, a dead UI observer or a cancellation flag alone never proves native quiescence.

The strict adapter retains its Python bootstrap/audit network restrictions. The canonical local-file adapter retains its explicit owner-trust and offline-library environment boundary; that adapter is not advertised as a network sandbox. The private worker files and their two distinct source identities remain outside this repository. Synthetic protocol workers in tests do not qualify either engine acoustically.

## Backup and restore

The active writer remains the host's existing composite format 5, with its checked schema 6 catalog. Canonical root-relative flat backups are admitted by the bounded compatibility reader. The two ASR namespaces have separate validators; each profile, job, retained source, accepted artifact, edit and useful checkpoint is checked against its declared schema and exact manifest hashes. Flat-envelope aggregate limits and native-state aggregate limits retain their existing values.

Restore keeps sources and historical receipts, revokes profile trust and marks jobs for explicit review. Only manifest-verified transient local-file launch controls are removed; fresh run control carries no execution authority. Complete read-only results remain readable without a configured old runtime. Empty recomputable cache directories carry no files or authority, and an undeclared file in such a directory fails restoration. Noncanonical aliases of the new reserved flat namespace fail before publication. Existing workspace records retain their prior case-aware review boundary.

Saved state/operations/resources.json contains passive declared allowances only. Restoring it constructs no broker, live lease, model, device or hardware-capacity discovery.

## Evidence status for this source overlay

All source additions prepared during the unavailable-host period are NOT_RUN until the exact integrated source and installed Windows gates execute. Reused upstream evidence is separate. Required qualification includes the original strict worker suites, canonical isolated synthetic worker/runtime/admission suites, dual-store backup/restore and ownership regressions, and both native transcription probes. The platform probe retains actual native SAPI PCM, quiet-mode and cleanup assertions through this host's existing OperationProgress. Human screen-reader use, microphone quality and real model inference remain separate evidence.
