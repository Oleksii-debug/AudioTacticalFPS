# Canonical speech and transcription reconciliation

This review accounts for the public workflows and retained data behind 19
canonical speech/transcription source files. Those files were read at
`e3cf74c243d4ef930b526b5648ca9003c0900057`; their exact Git blobs are unchanged
at the latest reviewed canonical `a44fac04072389caa40e0d6e799b13797712f797`.
The latter adds PR196's optional ASR cache policy, which is recorded separately.

The reviewed current root is
`db234b3bec7c4fcb25814c63410d445f4f1807a3`, tree
`0ecf9a3bbbf4b19eca704366885f3fc39114d234`. Its exact 40 relevant current
source blobs, the 19 canonical blobs and 30 cited test-module blobs are recorded
in the adjacent JSON. The additive ASR policy component `e087fce` and final
helper native-probe cleanup `f929165` are distinct source checkpoints; this
document does not silently treat them as already executed with that root.

## Workflow and state mapping

| Canonical family | Current owner and preserved behavior | Source/qualification boundary |
| --- | --- | --- |
| `speech/runtime.py`, package exports | `audio/engines.py`, `runtime_settings.py`, current reader and production own actual discovery, exact selected voices, literal synthesis and measured PCM. `audio/local_voice_helper.py` restores the explicitly reviewed JSON helper contract. | Existing engine/profile IDs stay stable. Old Python classes and return objects are not aliases. |
| `speech/_io.py` | Existing engine process owner, Windows job objects, process-group cleanup, bounded diagnostics, verified source handles and atomic publication. | No second supervisor and no claim of a hostile-native-code sandbox. |
| Native SAPI and PowerShell workers | Current native SAPI fixed protocol uses the already pinned pywin32 312 client, exact full-token/leaf/name binding, literal/punctuation flags, 22050 Hz mono PCM16 and installed private entry dispatch. Explicit saved 32-bit selection remains lazy. | The canonical comtypes implementation is not copied as an unused dependency. Actual new COM qualification is pending. Historical PowerShell evidence is separate. |
| `speech/player.py` | Existing WavePlayer/PCMPlayer, current native driver and retained playlist/session own pause, resume, seek, stop, completion, independent PCM, source hashes and frame cursor. | Exact queue component passed 141/141/43. Restore stays passive; no alternate MCI/ffplay player is introduced. |
| `speech/progress_speech.py` | Current notification worker, task snapshots and current player own progress speech, suppression and cleanup. Root adds old preference-envelope decoding and compare-and-swap save. | Registered-profile discovery is an additional current-owner component, not an assumed property of the reviewed root. |
| `speech/reading.py`, `regex_worker.py` | Current narration editor, original snapshot, reversible edits, source spans and bounded isolated pronunciation worker; old compact undo/redo is retained in immutable history. | Same words do not create exact source authority. Complete 500-step historical history remains bounded and recoverable. |
| `speech/session.py` | Existing legacy-reading store now accepts the outer `carr.reader-session.v1` envelope, preserving its exact bytes/hash and all revisions. Saved cursor is bound to saved revision. | Old voice, rate, volume and audio position are passive observations. Exact component passed 111/111/33 including real native review and rebackup. |
| `speech/source.py` | Current reading desk/state/live speech revalidate workspace, edition, revision, original asset and page. | Old “verified at capture” metadata, matching titles or matching text do not grant current access or quotation authority. |
| `transcription/{models,runtime,service,__init__}` | Existing current ASR services/receipts/host retain three distinct families, independently owned inputs, current reviewed runtime pins, pause/resume/cancel and accepted output sets. Canonical earlier state is archived and read/exported inertly. | Importing selected originals prepares a new current job and never adopts old completion or trust. Old package APIs are not inserted over the current flat `transcription.py`. |
| Dordiai integration/discovery | Current Dordiai adapter uses the reviewed manifest, actual installed resources, available languages, fixed JSONL PCM framing, current cancellation and profile re-admission. | Canonical warm-worker retention is not equivalent: current workers settle per utterance. No private multilingual quality or warm-cache performance claim is made. |
| Supplied transcription integration | Existing receipts-v1, V5-manifest and library-wide V43 contracts bind selected interpreter, worker, model, settings, inputs and complete accepted outputs. | Worker names/version strings alone are not protocol proof. Old outputs remain readable without acquiring or executing the old runtime. |
| New canonical PR196 backup policy | Component `e087fce` adds exact canonical cache recognition and refuses false/absent optional inclusion across historical envelopes; retains accepted/noncache artifacts. | Rehashing the source manifest cannot authorize its omitted cache. Actual 422/422/53 focused execution is pending. |

## Finite helper contract

The helper uses the existing manifest/profile store and process owner. No
constructor, profile registration, passive settings reopen or ordinary registry
construction executes its code. Users explicitly review and register the
complete installation, then select its voice through the current registry.

The native command is only the exact pinned executable. The additional closed
Python form is only `python -I -B -X utf8 selected-script.py`. Requests and
replies cannot supply arbitrary arguments, environment, modules, working
directories or alternate output authority. Manifest/executable/script handles
remain held through process settlement. Actual PCM remains held during final
catalogue and manifest revalidation before no-replace publication.

Canonical `SpeechSettings` has exactly rate, volume, pitch, punctuation,
timeout and output budget. Its `phonemes` string is catalogue metadata only;
there was no phonemes render field to restore. The new adapter accepts that
metadata while advertising only the controls it can actually send.

Current adapters retain their existing PCM16, 8000-character and 256 MiB
per-segment contracts. The canonical raw API's configurable sample widths and
output-budget object are not represented as source-compatible aliases, and no
silent format conversion is introduced. Long public work uses current
segmentation/production. These are explicit current-service limits rather than
a claim that every old Python call is interchangeable.

The focused helper gate declares 111 source and 111 installed tests, including
all 38 existing profile cases and six existing profile-UI cases, plus 31 native
checks. Its public actual child emits deterministic PCM over the real protocol;
it is not a voice-quality substitute. The final probe waits for its runtime
handle to close before setting PASS and explicitly forces FAIL on exceptions.

## ASR size scopes and the new retention correction

Canonical earlier ASR allows **32 GiB per source** and **64 GiB across a job**.
Historical input inventory uses the same 32 GiB per-file bound and a separate
64 GiB input aggregate plus 512 MiB metadata allowance. These are distinct from
the generic historical state's 32 GiB default.

A concrete old state-list reader defect applied the generic 32 GiB aggregate
before typed ASR accounting. Component `e087fce` removes that premature
rejection only for the exact canonical ASR entries. Generic historical members
still use the original 32 GiB default, 4096-character path bound and 2048
components; typed ASR retains its narrower path contract. Explicit caller
whole-archive budgets still apply.

The new policy suite preserves the canonical 128-case matrix and one real
rehashed-manifest refusal, then adds real noncache restore/rebackup and declared
size-boundary cases. It does not allocate or claim measured processing of
64 GiB.

Current library-wide ASR preparation keeps its separate **32 GiB total new-job**
bound. The older workspace receipts-v1/V5 family keeps **8 GiB per source /
32 GiB batch**. A larger historical set is preserved in full; preparing new
execution is a separate explicit current-service action. This review does not
claim those active execution ranges are identical, and no original is deleted
to satisfy the new-job cap.

## Exact execution evidence

| Frozen component | Actual status at this review | Evidence |
| --- | --- | --- |
| Earlier canonical ASR recovery `763bb37` | 268 source, 268 installed, 53 native PASS | Run `46013756502`; proof `0861783` |
| Retained playback queue `565b9a6` | 141 source, 141 installed, 43 native PASS | Run `46016663005`; proof `fdc59d6` |
| Historical reader session `01c18ae` | 111 source, 111 installed, 33 native PASS | Run `46025115863`; proof `f4a6b95` |
| Native SAPI `ecfc054` | Queued; declared 71/71/35 | Run `46025111819` |
| Local helper `f929165` | Queued; declared 111/111/31 | Run `46029759866` |
| PR196 historical ASR policy `e087fce` | Queued; declared 422/422/53 | Run `46030395296`; frozen `carr/legacy-transcription-pr196-policy-20261127` branch |

Each PASS is scoped to the exact frozen source and installed wheel from its
receipt. It does not transfer to a later composed tree. There is no whole
product equivalence, private ASR/voice inference, acoustic quality, manual NVDA,
physical listening or warm-Dordiai performance claim in this source ledger.
