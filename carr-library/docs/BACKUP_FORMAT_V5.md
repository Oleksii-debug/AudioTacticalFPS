# Resonance backup compatibility and recovery

## Supported envelopes

The top-level manifest has kind `carr-library`. Its independently retained
SHA256 must match before the reader chooses a format. A number alone never
selects between the two older implementations.

| Envelope | Distinguishing fields beyond the database and original inventory | Result |
| --- | --- | --- |
| Historical 1–2 | No application sidecars; schema fingerprint from version 2 | Checked catalog restoration |
| Historical reading state 3 | `reading_state` | Exact source-bound reading locations |
| Historical sidecars 3 | `sidecars` | Checked source positions and declared compatible sidecars; original historical archives retained where required |
| Historical canonical state 3–4 | `state`, `generated_audio_included`; version 4 also has `pending` | Checked state and pinned import recovery |
| Historical recovery 4 | `sidecars`, `recovery` | Compatible catalog plus inert older Studio, provider, agent, and queue records |
| Historical extensions 5 | Canonical state, canonical pending component, and `extensions` | Typed historical additions described below |
| Historical reading state 5 | `state`, `generated_audio_included`, `pending`, `reading_state` | Exact separately bound reading state with checked canonical recovery |
| **Current common inventory 4** | `sidecars`, `generated_audio_included`, and optional non-null `pending` | One inventory for current root sidecars and producer-owned state |

The current writer always emits the common-inventory version 4 envelope.
Each sidecar filename is relative to the library root, so a producer file such
as `state/studio/jobs/<id>/job.json` keeps its `state/` prefix. Root settings
such as `reading-state.json` stay at the root. Every entry binds its exact
filename, byte count and SHA256. An optional `pending` component uses the
existing pinned-import validator and includes the original bytes needed for
recovery. The current writer does not add a top-level `state` or `extensions`
field.

Each historical parser still requires its complete authenticated field set and
exact file inventory. Numeric versions overlap between historical producers;
the number alone never selects a fallback parser. Combining fields from two
layouts or changing an old manifest's version does not convert its content.
SQLite schema versions remain independent from backup envelope versions.

## Historical typed version 5 additions

The following extension layout remains readable for existing backups.
Its original component bytes and validation rules remain intact. Current
common-inventory backups carry the corresponding supported producer files
through the typed sidecar registry; they do not manufacture this historical
`extensions` envelope.

The `extensions` directory contains allowlisted root sidecars and complete inert
legacy archives. Its manifest binds the workspace inventory, independent-state
scope, every copied filename/size/SHA256, typed state producers, and required
external inputs. Portable path validation rejects NFC/casefold aliases in both
filenames and ancestor directories, and file/directory prefix collisions.

Nested extension version 1 supports reading positions, source-bound reader
sessions, canonical audio projects, dordiai declarations, and inert archives.
Nested version 2 adds transcription jobs and the transcription runtime
declaration. Nested version 3 adds a verified Studio input receipt, publication
receipts, recording receipts, and explicitly unverified final recording originals.
Versions 1 and 2 retain their exact previous field sets. The archive version,
nested extension version, and SQLite schema version are independent identities;
canonical schema5 databases and exact historical schema3/4 fingerprints are checked
before restoration, and older databases upgrade only on ordinary application open.

- `reading-state.json` binds bookmarks and page resume locations to workspace,
  edition, sealed revision, original SHA256, location kind/index and exact text
  SHA256. Historical source revisions stay historical.
- `live-reading-state.json` additionally binds Unicode code-point offsets and
  exact text length. These are chunk-start resume locations, not word alignment.
  Restoration does not start playback.
- `state/speech/<slot>.reader.json` is validated through the canonical
  `decode_reader_resume` API. Source-bound v2 sessions must match the joined exact
  referenced page text. Their edited derivatives and undo history remain separate
  from quotation evidence. Unbound v1 sessions remain independent private state;
  a hashed filename never supplies workspace authority.
- Canonical `state/studio/projects/<id>/` and typed saved jobs use the existing
  project parser. Full backups copy explicitly selected cue originals into
  `state/studio/inputs/<SHA256>.audio` and bind them to the exact original project
  and job bytes through `studio/backup-inputs.json`. After all snapshot hashes
  pass, restoration rebinds only those cue fields to the new root and requires a
  newly selected output folder before any restored job can run. Original archive
  bytes and initial recovery provenance remain intact. Older external declarations
  and imported project paths require explicit local review before capture or use.
  Voice IDs and nonprocedural music engines remain declared external dependencies;
  no model or private runtime is opened during backup.
- `state/studio/recordings/<id>.json` binds its exact retained WAV size, SHA256
  and PCM header. Final `<id>.wav` files without receipts remain opaque, inert
  originals; no successful capture record is invented. Temporary
  `.pending-recording-*` data and `.recording.lock` are unfinished and excluded.
- `state/studio/publications/<id>.json` is validated as a historical declaration.
  Its original production hashes are preserved even when restored job paths change.
  The external EPUB is listed with `included=false`; relocation requires explicit
  output selection and exact verification before the producer will use it.
- `dordiai-voices.json` is a validated local declaration. Restore always writes it
  disabled; reconnecting is a separate explicit action. No private runtime,
  Python executable, credential or model is probed during backup or restore.
- `state/transcription/<job-id>/` uses the transcription producer's pure saved
  job and retained-artifact validator. Every source byte and completed receipt
  is checked, including valid receipts committed before a crash updated the
  completion list. Runtime/model identity is declared, not bundled. Restored
  queued/running jobs become paused; every restored job requires review and its
  control command is pause. The backend refuses execution until explicit resume.
- `transcription-runtime.json` is included only in full backups and is restored
  disabled without looking up its separately authorized runtime manifest.

`required_inputs` lists unavailable external dependencies with `included=false`.
A saved declaration or an existing generated recording is not evidence that the
corresponding model, license, voice installation or external cue is available on
another machine.

## Complete and workspace-scoped backups

`create_backup(app, destination, include_generated=False, cancelled=...,
authorized_workspaces=None)` preserves the canonical options. `None` authorizes
the complete host state selected by the application's explicit backup action.
`include_generated` retains the established generated-cache policy.

An explicit workspace collection is an authorization boundary, not a request to
filter database rows. Every workspace in the consistent catalog snapshot must
belong to that collection. Otherwise the operation fails before publication.
Within that scope, exact-source positions and explicitly bound reader sessions
are included. Independent drafts, audio projects and inputs, recordings, publication
receipts, transcription jobs/runtime settings, private voice declarations and
complete old host archives are omitted.
No scope is inferred from a filename, the current selection or a global default.

## Legacy preservation and narrow queue import

The compatibility package contains data parsers and frozen schema identities.
It does not install a second active Studio, provider manager, local agent, render
engine or credential vault. A valid old archive is retained byte for byte at
`legacy-backups/<original-manifest-SHA256>/`; the required empty `originals`
directory is preserved even for an empty catalog.

The adjacent `<SHA256>.json` notice records component outcomes. The common
catalog, registered originals and compatible source positions are restored.
Old narration projects, Studio revisions/receipts/audio, provider profiles and
agent receipts remain inert until an explicit validated canonical import exists.
The notice does not claim they are runnable in the current application.

A narrow queue adapter can import retained TXT/PDF requests when all current
canonical checks succeed: exact schema, request/workspace/edition relationships,
source MIME and SHA256, checkpoint identity, review bindings and complete pinned
originals. Only the explicit `installed` OCR-profile representation is mapped to
the current schema. Job and edition identities remain unchanged; source paths
and the data-root binding are rebound by the existing canonical pending restorer.
No archived external source pathname is opened. A successful adapter adds the
`canonical-pending-imports` restored status while the original archived queue
remains inert. Unsupported managed profiles, old batches, ambiguous requests or
incompatible extraction identities stay preserved and inactive.

An inert batch can retain an absolute source declaration from another operating
system. Its declaration is validated without rewriting it or opening that path;
unretained batch inputs are listed in the recovery notice. Such a batch does not
become a runnable current queue merely because its catalog can be restored.

Re-backing and restoring a version 5 library reauthenticates the retained old
archive and its notice. A recorded successful queue import must still have a
valid canonical mapping. No original legacy bytes are rewritten or silently
discarded.

## Publication and cancellation

Restoration always targets a new directory. It never merges into or overwrites
an existing library. Inputs are checked into private staging before publication.
Originals are checked in bounded hash chunks; database and state copies,
pending imports, typed component verification and file publication cooperate
with cancellation. SQLite integrity scans use progress callbacks.

Cancellation before publication leaves no destination. Once publication starts,
the destination contains `.restore-incomplete` until every component is durable
and temporary hard links are gone. Cancellation or I/O failure in that phase
keeps the marker, and normal application startup rejects that directory. Removal
of the marker is the completion boundary; a later cancellation request does not
turn an already completed restore into a reported failure.

Source sidecars are rehashed before snapshot success even if an equal-size write
preserves its filesystem timestamps. Runtime/model probing and remote requests
are outside every backup path.

## Verification scope

Committed fixtures under `tests/fixtures/legacy-backups-5580d4d` were generated
offline by the actual frozen writer at checkpoint
`5580d4dfa99991592d45ec40dd1d1736408d43c8`, tree
`98ad0a16c2654965f3f956c3ef4f4c9d51720616`. They contain synthetic catalog text,
authored project records, placeholder provider references and retained test
inputs. They contain no private runtime, model, API key or user recording.

Required source suites cover the original canonical v4 lost-source resume,
full/scoped v5 restoration, authentic old envelopes and re-backup, portable
namespace collisions, same-metadata writes, real compatible old queue resume,
inert incompatible queues, transcription receipt/retained-input integrity,
disabled runtime settings, and cancellation before/during publication. Native
Windows qualification belongs to the separate actual Windows workflow; source
tests do not establish NVDA speech, audio hardware, ASR quality or model rights.

## Trusted-client recovery bytes

A complete common-inventory backup preserves only the two known files
`state/trusted-client/entitlement.json` and
`state/trusted-client/entitlement.pending.json`, each at most 64 KiB, as
unverified recovery bytes. This includes damaged or incomplete owner state so
an application update or later recovery does not silently discard it. Other
filenames under that directory are not accepted. Ordinary single-link,
non-reparse file and complete inventory/hash checks still apply.

Copying these files does not verify a signature, current validity, publisher
policy or protected installation anchor. Backup and restore neither open the
credential vault nor activate an entitlement. The normal entitlement reader
continues checking the signed envelope and independently protected anchor
before allowing a managed operation; malformed, forged or unanchored restored
bytes remain rejected. Protected monotonic state is not reconstructed from a
backup. Reading, export and recovery remain governed by their existing policy.
