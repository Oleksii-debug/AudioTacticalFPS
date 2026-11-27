# Reviewed supplied-worker profiles and recoverable control

This component converges the canonical PR147/180 admission and user controls
onto the existing library-scoped `carr_library.transcription` service. It does
not install the alternate `transcription/` package or introduce another job
queue, inference engine, provider, model download, or private runtime.

## Review and identity

A runtime is inspected without executing its interpreter, importing its installed
inference modules, or loading a model. The review retains the selected lexical
Python path and its existing lstat/resolved-binary binding. It additionally seals
pyvenv configuration, operational base Python/support files, declared installed
distribution files and path configuration, wrapper sources, and the bounded
selected model inventory.

The seven declared distributions are faster-whisper, ctranslate2, av, onnxruntime,
numpy, huggingface-hub and tokenizers. Missing distributions are represented
explicitly as absent, not inferred as installed. This preserves the existing
stdlib-only synthetic receipt and V5 protocol peers. Every inspected profile
remains `qualification=unqualified` and `inference_qualified=false`; file
identity is not evidence of acoustic accuracy, supported hardware or a usable
private model. Empty model directories remain valid declarations for synthetic
receipt peers. Actual V5 execution still requires its three model files and
the existing exact V5 manifest contract.

`inspect_runtime(...)` returns the proposed configuration and fingerprint.
`register_runtime(name, ..., owner_trusted=True,
expected_fingerprint=...)` admits only the same newly inspected fingerprint.
Named jobs keep their selected profile when the global default changes.
Legacy configurations remain readable, but require a fresh explicit review
before unfinished work may execute.

Checks occur after resource admission, before child execution, after the actual
child returns, after receipt validation, and after candidate-state publication.
A late cancellation cannot skip the last review or downgrade a valid committed
result. The last already-published review finishes its bounded reads before
the host declares completion. Normal Stop before publication continues to avoid
rehashing multi-gigabyte inputs or models.

## Rejected and uncertain work

A known runtime/model identity failure writes a permanent rejected-execution
record before clearing accepted counters and receipts. Returning old bytes or
registering a new review never reauthorizes that worker cache. A changed installed
runtime seal with the same worker likewise cannot adopt the previous raw cache.

Before starting an attempt the host creates a separate
`state/transcription/<workspace-sha256>/reviews/<job-id>.json` record. The
selected worker's existing write audit cannot delete this sibling host record.
The host removes it only after acknowledged stop or completed final review.
If a crash or lack of disk space prevents recording a post-publication rejection,
this pending record still prevents a candidate `completed` job from being
read or exported as accepted. The owner must acknowledge that the old external
worker stopped; recovery then creates a fresh ready job from retained originals.
It never clears the old review/rejection evidence or automatically executes a copy.

## One durable control surface

The existing service adds named profiles, mixed file/folder intake, optional
recursion, durable Pause/Cancel, Continue, orphan acknowledgement, explicit
runtime rebind and `copy_job`. Existing `submit`, `submit_folder`,
`configure`, `run`, text/edit/export and workspace authorization APIs remain.
A new saved job can move ready → paused → cancelled → ready without starting
recognition. An active Pause/Cancel is a durable request; the actual runner owns
worker closure and only then reports its terminal state. Orphaned work needs an
explicit acknowledgement, not an inferred PID kill or automatic retry.

Copying a job retains verified original bytes, original labels, settings and
protocol in a distinct ready job. It copies no worker cache and starts no child.
A same-identity restored profile may be reviewed and rebound; a changed or
previously unsealed runtime needs a fresh copied job.

Input bounds remain 64 sources, 8 GiB per source and 32 GiB per batch. The shared
scanner retains duplicate selected content as distinct paths, rejects unsafe
links/paths, and reports deliberately excluded unsupported files/subfolders.
Nonrecursive scanning excludes subfolders only on the scanner's explicit
nonrecursive outcome, never by swallowing arbitrary rejection.

Both current protocols continue to support transcription only. Speech-to-English
is unavailable until an explicitly supported worker protocol declares it.
All required TXT/SRT/VTT/JSON outputs remain retained internally. Export selection
chooses a subset at the existing create-only export boundary; word timing exports
follow their selected SRT/VTT format. User edits remain separately labelled.

## Passive backup and restore

The typed adapter accepts only declared engines, named profiles, workspace-bound
jobs, controls, host review/rejection records, retained originals, existing
receipts, edits and exact current/historical V5 manifest relationships. It never
opens a configured external Python, wrapper, model or source path.

After all original backup bytes and relationships validate, restore marks engines
and jobs as requiring execution review, revokes named profile trust, pauses
restored control intent, and preserves original selectors and settings. Completed
verified receipts, reading, edits and explicit export remain usable without the
old engine. Running or uncertain work is restored as interrupted. Host uncertainty
and permanent rejection records remain exact passive vetoes; no lock, process,
lease or implicit execution authority is restored.

Integration uses the existing restore staging transaction. Immediately after
`studio.backup_inputs.restore_inputs(...)` and before the final sidecar
recapture/checksum loop, call:

```python
backup_transcription.revoke_restored_execution(
    db, stage, inventory.entries, cancelled=cancelled)
```

The existing shared `backup_sidecars.py` owner supplies this small hook.

## Convergence and verification

| Canonical behavior | Existing service composition |
| --- | --- |
| Full runtime/package admission | Passive transcription_runtime.py seals plus existing lexical interpreter binding |
| Reviewed named runtimes | Content-bound profiles and explicit expected fingerprint |
| Durable controls/orphan review | The same job lock, durable control and existing actual child runner |
| Mixed recursive input | Existing bounded scanner and retained source-copy contract |
| Subset exports/editor/read-aloud | Existing receipt/text/edit/export APIs and separately composed native UI |
| Restored trust revocation | Typed passive adapter and existing restore staging hook |
| Runtime drift cannot resurrect cache | Permanent rejection plus host-owned pending review |
| New alternate package/job schema | Deliberately not adopted; one existing workspace-bound producer remains |

New gate files are `tests/transcription/test_runtime_review.py`,
`tests/transcription/test_profile_controls.py`, and
`tests/storage/test_transcription_review_restore.py`. They use actual existing
supplied-protocol children, real selected venvs with inert package inventories,
real broker reservations, cross-process controls, and actual backup/restore.
Existing supplied-worker, V5 and typed-backup cases retain their receipt/source/
publication assertions, adding only the explicit post-restore review steps.

At component publication the executor is unavailable: new source and tests are
**NOT_RUN**. Exact composed source, installed and native CI gates must qualify
them. No private worker/model, physical audio, hardware or acoustic result is
claimed by this component.
