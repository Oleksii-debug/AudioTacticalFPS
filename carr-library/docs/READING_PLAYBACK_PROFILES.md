# Editable-copy reading and reviewed local voice profiles

This slice reuses the current local production, resource and playback services.
It adds two native workflows without another synthesis engine or project format.

## Read the editable copy

The Reading copy window offers selection (or all when nothing is selected), all
text, and text from the native cursor position. F6 invokes selection/all, F7
pauses or resumes, and F8 stops. The original read-only text is never substituted
for the editable copy. Native text range APIs preserve Windows UTF-16 positions;
Python string indexing is not used to reinterpret a native selection.

Choose a voice explicitly after Reload reading voices, then set its advertised
language, rate and volume. Literal narration preserves bracketed text and speaker
labels as text. It does not interpret them as production commands. The existing
production service chunks long text and assembles a PCM WAV while holding the
same cooperative heavy-media reservation used by Studio and transcription.
The explicit budgets are 16 MiB of UTF-8 text and 256 MiB of temporary production
audio; excess input/output fails visibly without truncating the text.

Preparation runs on a bounded background worker. Editing, changing reading-project
identity, revoking the source/workspace guard, or choosing Stop prevents an obsolete
result from starting playback. The native window waits for the producer and player
to finish before closing. A native cleanup failure retains the audio and media
hold; it is not reported as a successful stop.

Playback uses the existing WavePlayer. Progress feedback must confirm silence
before playback starts. Native pause/resume/seek use the current Windows player.
The optional non-Windows player retains its existing supported controls.
A prepared WAV can be exported without a physical playback device. Export uses
a separately written, checksum-verified file and atomic no-replace publication;
it never silently overwrites another file or creates an alias to the temporary
audio. Generated previews are temporary, not silently saved Studio projects.

## Local voice profiles

Audio Studio and the reading window expose Local voice profiles. The store is
`voice-settings.json` directly under the library's private application root.
The supported selections are the already existing Piper executable/model
manifest adapter and the already existing dordiai-voices manifest adapter.

Verify and save checks the manifest, declared model license and listed bytes.
It does not execute the selected program. Review and enable is a separate,
explicit session action. Runtime selection must be treated as a local code
execution decision. The existing adapter's limits and rights requirements still
apply; a manifest does not establish voice/language quality or sandbox arbitrary
native code.

One profile per optional engine can be enabled in the current session. Saved
presence never grants execution authority. The registry binds session identity,
the saved row, manifest identity and selected executable identity before use;
the existing adapters verify model/runtime assets. Changing or removing a row,
changing a pinned executable/manifest, or disabling the session prevents reuse
of a stale registry. Active synthesis receives the existing cancellation signal
if the session is disabled.

The version-1 settings schema has at most 32 profiles and a 64 KiB byte limit.
The complete-backup helper snapshots under the library's already-held writer
lock and exports disconnected metadata: profile ID, label, engine and pinned
hashes. It removes external manifest/executable paths and follows no model
directory. Restore does not enable a profile; select and review local paths
again. Temporary reading audio, external runtime/model installations and
separately exported WAV files are outside this small settings snapshot.

## Reuse ledger

| Existing source | Behavior reused | Current integration |
| --- | --- | --- |
| Canonical `4b6f8169986c4a58b43e4a23bc081fbefadbbdcb`, `ui/speech_dialog.py`, blob `a05c44cff2bd5d21caba1d473ef342c4759f11e1` | Selection/all/cursor, F6/F7/F8, rate/volume, WAV export | `ui/reading_playback.py` and the existing narration editor |
| Same canonical head, `ui/engine_settings.py`, blob `573b3e29119028170f6b409b209d811d4907b13d` | Explicit local installation selection and review | `ui/local_voice_settings.py`; only currently supported root adapters |
| Root `audio/engines.py` | Piper, dordiai-voices, installed SAPI/eSpeak/Flite and per-voice language declarations | Session-bound wrapper; no competing registry or helper protocol |
| Root `audio/production.py` | Literal source-bound narration, chunk cache and PCM assembly | `audio/reading.py` calls `plan_narration` and existing rendering |
| Root `operations/studio.py` and `audio/player.py` | Shared heavy-media admission and current local playback | No second resource broker or player |
| Root `progress.py` | Content-media suppression and observed progress-audio quiescence | Same ownership methods as Studio |

Canonical timed sound cues, minimum scene duration, unsupported music engines
and incompatible production-project JSON are not flattened by this adapter.
Their conversion requires an explicit compatible timeline/clip plan.

## Qualification boundaries

The dedicated source gate covers actual filesystem identities, disconnected
restore, session revocation, UTF-16 range handling, original/copy separation,
stale completion, cancellation, cleanup failure, atomic WAV publication and
shared resource admission. Linux additionally exercises the actual existing
FFmpeg pipeline with a declared synthetic PCM speech adapter.

`scripts/windows_reading_playback_smoke.py` exercises installed code and real
native wx controls with a synthetic renderer/player contract. It verifies native
selection after a supplementary Unicode character, local profile persistence,
source preservation, cancellation and actual WAV bytes. It does not claim a
physical device, an optional real voice runtime, acoustic/language quality,
screen-reader speech or NVDA acceptance. Those remain separate checks.

The declared packaged desktop target remains Windows CPython 3.11. This slice
does not change the separately documented older immutable-reader portability
boundary on Windows CPython 3.12.
