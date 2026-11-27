# Canonical compatibility review — 27 November 2026

This is a source and saved-format compatibility review, not a migration or a
release acceptance report. It compares the frozen development candidate
[`e2567344c17c6671ff5081b39629bf6e58bff0f4`](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/e2567344c17c6671ff5081b39629bf6e58bff0f4)
with canonical
[`966e5ccf892243ead6942c635af5fbab47e72551`](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/966e5ccf892243ead6942c635af5fbab47e72551).
No existing user's profiles were opened or converted for this review. No private
engine was run. The source links below identify the exact implementations under
comparison; later revisions need their own review.

## Integration decision

These snapshots contain different complete implementations of several shared
features. A successful test of one snapshot does not qualify the other, and a
shared JSON version number does not establish compatible meaning. Preserve the
current producers, saved identities and publication guards while adopting
individual reviewed features. Do not replace either saved-state registry or
silently reset an unsupported profile to defaults.

Canonical `966e5cc` has parents `1a0cd095563c9d86d9ca280323ff4449816ad3f7`
and `ff8034940f7b30782a83c0754a6522412b342841`. The first parent contains the
bounded post-flush cancellation correction; the second is a separate qualified
preview lineage. The master V2, V3 and V3-finalization requirement documents have
identical blobs in the two reviewed snapshots. The different implementation
families therefore require reconciliation, rather than an assumption that the
latest commit changed those requirements. See the [canonical merge](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/966e5ccf892243ead6942c635af5fbab47e72551)
and the existing [finalization requirements](UNIVERSAL_SCOPE_V3_FINALIZATION.md).

## Saved-profile compatibility matrix

“Same envelope” below means that the selected fields or producer source match.
It is not a claim that an entire backup has passed cross-version restoration.

| Family | Candidate `e256734` | Canonical `966e5cc` | Required reconciliation |
|---|---|---|---|
| Keyboard shortcuts | `state/ui/shortcuts.json`: `schema: 1`, `overrides`, `originals`; action identities are normalized menu paths, with the original default retained for each override. | Root `keyboard-shortcuts.json`: `version: 1`, `bindings`; application uses `CommandRegistry` identities and command contexts. | An explicit action mapping must preserve disabled keys and existing owners. Renaming a file or copying bindings is insufficient. Unknown or conflicting commands must not silently acquire a new action. [K1][K2] |
| Optional progress feedback | `state/ui/progress.json`: `schema: 1`; modes `off`, `beep`, `speech`, `both`; interval 1–120 seconds, default 10; volume 0–1, default 0.25; `quiet`, `background`, `mute_during_audio`. | Root `progress-preferences.json`: `version: 1`; combined mode `beep_speech`; interval 1–60 seconds, default 5; integer volume 0–100, default 30; `muted`, `allow_background`, `quiet_during_media`. | Map names and units explicitly. Intervals above 60 seconds and non-integral percentage volumes have no lossless representation in the other format. Do not clamp, round or restore audible defaults without a deliberate decision. [P1][P2] |
| Resource admission policy | `state/operations/resources.json`: `carr-resource-policy/1`, with declared CPU, memory, disk, reserve, GPU and lease limits. Runtime leases are separate ownership records. | No matching `state/operations/resources.json` producer in the canonical backup registry. | Preserve the operator's declared limits and keep live process ownership distinct from saved policy. A restored file must not become evidence of a live reservation or measured hardware capacity. [O1][B2] |
| Transcription engine and jobs | `state/transcription/engine.json` selects the interpreter, worker and model directory; scoped jobs retain receipt-v1 source/configuration identities and editable revisions. | A separate supplied-transcription adapter and worker protocol, including `resonance-transcript/1`; no matching `state/transcription/*` backup producer. | Protocol and receipt conversion needs its own source-bound design. Do not interpret a completed record from one worker as verified output from the other, activate saved external paths, or discard paused jobs. [T1][T2][B2] |
| Provider accounts | `carr-provider-profile/1`; includes `google-translation-v3`, explicit project/location endpoint, OAuth credential reference and Advanced model selection. | Same profile envelope, but the adapter allowlist supports Basic v2 and excludes `google-translation-v3`. | The envelope alone cannot preserve an Advanced account. Retain endpoint/model/account identity and nonsecret credential references; require fresh explicit authority for remote work. Never reinterpret Advanced configuration as Basic v2. [A1][A2] |
| Bookmarks and source positions | Root `reading-state.json`, `carr-reading-state`, version 1, positions and bookmarks. | The same JSON envelope and decoding fields; the file-reading helpers and backup integration differ. | Preserve workspace, edition, exact revision, source/text digests and physical-page versus logical-segment identity. Test archived and historical locations against the copied database before claiming cross-version restoration. [R1][R2] |
| Voice-reader sessions | `state/speech/<slot-hash>.reader.json`, `carr.reader-session.v1`. | The selected `speech/session.py` producer is byte-identical, including document, voice, rate, volume, cursor and audio-seconds fields. | Keep the stored reading document and its source validation authoritative. Identical session serialization does not establish compatibility of every surrounding reading/backup component, or authorize opening external files. [S1][S2] |
| Local audio configuration | `state/audio/engines.json`, schema 1, explicit FFmpeg/FFprobe, Piper, voice helpers, music models, FluidSynth and optional Dordiai declarations. | The same top-level schema is recognized; runtime resolution and backup validation implementations differ. | Preserve declarations passively. Restoring settings must not probe/install an engine, enable an unavailable runtime, or substitute another voice. Recheck the selected executable/model identities at actual use. [E1][E2] |
| Studio originals and publication receipts | Typed recording JSON plus matched WAV under `state/studio/recordings`, publication receipts, and the `studio/backup-inputs.json` producer retain approved original inputs. | The registry recognizes its own Studio projects/jobs/generated-audio formats; it has no adapters for these recording/publication/input-recovery families. | Keep original audio with its exact receipt and digest. Preserve input approvals and inert orphan data; do not infer publication authority or drop required media because the other registry does not recognize it. [M1][M2][B2] |

## Backup envelope is only the first boundary

The candidate retains the state-tree backup family: version 3 adds `state` and
`generated_audio_included`; version 4 adds `pending`; version 5 adds
`reading_state`. Its reader also recognizes the historical exact reading-only
version-3 shape. Canonical `966e5cc` normalizes these historical shapes and its
own sidecar-list family through an exact-field discriminator. It still validates
each normalized filename through an explicit producer registry. [B1][B2]

Consequently, recognition of a version-5 manifest does not mean that a backup
containing `state/ui/shortcuts.json`, `state/ui/progress.json`,
`state/operations/resources.json` or `state/transcription/*` can be restored by
the canonical snapshot. Its registry rejects those unrecognized producers.
Conversely, the candidate's exact manifest reader must not be described as
accepting the canonical sidecar-list family. Preserve the source manifest and
original data; any future reader adapter needs explicit shape discrimination,
producer validation, source binding and an atomic new-folder restore. This note
does not add such an adapter. [B1][B2]

## Finite feature reuse at this checkpoint

The exact four-line post-`fsync` cancellation guard from
[`5cdd5982817514b7b1b524dd7a33ccd6776d6389`](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/5cdd5982817514b7b1b524dd7a33ccd6776d6389)
is included in `e256734`, together with both real publication regressions in
`tests/audio/test_publication_fsync.py`. It prevents publication when cancellation
arrives during the flush; cancellation after the successful link does not turn a
committed output into failure. Existing process ownership and cleanup behavior
remain intact. The local focused publication/process gate passed 21 cases with
zero skips. This is local evidence, not full Windows acceptance.

Separately reviewed follow-ups are being composed for topic literature matrices,
catalog-quality hints, custom styles through the existing Node formatter, and
additional document extractors. They are **not part of the frozen `e256734`
snapshot described here**. Their source, tests and integration evidence must be
recorded at their actual adoption commits. They do not justify replacing the
keyboard, progress, resource, transcription, account or backup families above.

## Evidence boundaries

The earlier four successful workflows belong to exact `ff803494`: Desktop
[45948977094](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45948977094),
V3 [45948977079](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45948977079),
bibliography [45948977127](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45948977127),
and native launcher [45948977073](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45948977073).
They do not qualify either `966e5cc` or `e256734`. The latter has its own Desktop
[45958710545](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45958710545)
and V3 [45958710380](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45958710380)
checks. Their complete terminal results and exact source identity, rather than
an early bundle capture or a sum of selected cases, determine acceptance.

[K1]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/e2567344c17c6671ff5081b39629bf6e58bff0f4/carr-library/src/carr_library/ui/keyboard_settings.py
[K2]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/966e5ccf892243ead6942c635af5fbab47e72551/carr-library/src/carr_library/keyboard_preferences.py
[P1]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/e2567344c17c6671ff5081b39629bf6e58bff0f4/carr-library/src/carr_library/ui/operation_progress.py
[P2]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/966e5ccf892243ead6942c635af5fbab47e72551/carr-library/src/carr_library/progress_preferences.py
[O1]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/e2567344c17c6671ff5081b39629bf6e58bff0f4/carr-library/src/carr_library/operations/broker.py
[T1]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/e2567344c17c6671ff5081b39629bf6e58bff0f4/carr-library/src/carr_library/transcription.py
[T2]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/966e5ccf892243ead6942c635af5fbab47e72551/carr-library/src/carr_library/integrations/supplied_transcription.py
[A1]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/e2567344c17c6671ff5081b39629bf6e58bff0f4/carr-library/src/carr_library/assistant/profiles.py
[A2]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/966e5ccf892243ead6942c635af5fbab47e72551/carr-library/src/carr_library/assistant/profiles.py
[R1]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/e2567344c17c6671ff5081b39629bf6e58bff0f4/carr-library/src/carr_library/reader/reading_state.py
[R2]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/966e5ccf892243ead6942c635af5fbab47e72551/carr-library/src/carr_library/reader/reading_state.py
[S1]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/e2567344c17c6671ff5081b39629bf6e58bff0f4/carr-library/src/carr_library/speech/session.py
[S2]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/966e5ccf892243ead6942c635af5fbab47e72551/carr-library/src/carr_library/speech/session.py
[E1]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/e2567344c17c6671ff5081b39629bf6e58bff0f4/carr-library/src/carr_library/runtime_settings.py
[E2]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/966e5ccf892243ead6942c635af5fbab47e72551/carr-library/src/carr_library/runtime_settings.py
[M1]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/e2567344c17c6671ff5081b39629bf6e58bff0f4/carr-library/src/carr_library/studio/backup_media.py
[M2]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/e2567344c17c6671ff5081b39629bf6e58bff0f4/carr-library/src/carr_library/studio/backup_inputs.py
[B1]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/e2567344c17c6671ff5081b39629bf6e58bff0f4/carr-library/src/carr_library/library_backup.py
[B2]: https://github.com/Oleksii-debug/AudioTacticalFPS/blob/966e5ccf892243ead6942c635af5fbab47e72551/carr-library/src/carr_library/backup_sidecars.py
