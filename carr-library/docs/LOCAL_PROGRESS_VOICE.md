# Explicit local progress voice

Progress output has four independent modes: Off, Beep, Speech, and Beep + Speech.
It starts Off. In Audio Studio, reload local voices, select an installed voice and
its advertised language, then choose **Use voice for progress**. Open **Progress
sound settings** to select the output mode, volume, minimum interval, mute and
background behavior. Choosing a progress voice does not change the reading text,
project narrator, rendering settings or saved sound mode.

The choice applies to the current session and library. Studio and its parent
window own independent copies of that choice. Changing the library, registry or
selected engine configuration requires another explicit choice. Loading a
project or restoring preferences never selects an engine or authorizes a voice.
A saved Speech/Both mode without a session choice stays silent and explains the
required selection. There is no fallback voice.

## Existing components and bounded delivery

This adaptation reuses the localized notices and private-player design from
canonical `speech/progress_speech.py` at commit
`1eff701fe848a3bd9eb088c85e5372a7c0a73861`
(blob `761717f8bde4b39168b61e570bf5e34ea557c5b2`). It uses this product's existing
`VoiceRegistry`, local engine processes, `WavePlayer`, shared `ResourceBroker`
and the existing progress dispatcher. It does not introduce another speech
engine, resource coordinator, project format or job executor.

Notices use English, Ukrainian or Slovak only when the selected voice advertises
that language. Ukrainian and Slovak notices do not read untranslated English
internal phase labels. Percentages come from the existing immutable progress
snapshot. Unknown totals, pause, resume, cancellation and failure remain distinct.
Only an already accepted completed operation can announce 100 percent.
Notification errors never alter its durable outcome.

Qualified notification adapters in this slice are the CPU SAPI5/SAPI5-32,
eSpeak NG and Flite adapters. Neural engines are refused explicitly because their
notification device placement has not been qualified. The ordinary text/book
rendering paths and their supported engines are unchanged.

The shared dispatcher keeps at most the active notification and the latest
queued request. A newer state cancels stale speech. Synthesis runs off the GUI
thread and requests one CPU slot, a declared 256 MiB memory allowance and the
private PCM disk allowance from the existing library resource coordinator.
Admission waits at most 50 ms; busy resources drop the notice. These are
cooperative resource reservations, not an operating-system RSS sandbox.

Each notice has at most 240 printable characters, a 20-second synthesis deadline,
a 30-second total deadline, and at most 20 seconds / 2 MiB of validated PCM.
The private staging inventory is checked at engine cancellation checkpoints and
again before playback; it has a separate 32-entry / PCM-plus-32-KiB limit.
An engine that does not cooperate between checkpoints can temporarily exceed a
declared resource allowance; final validation still prevents oversized playback.
The existing engine subprocess runner remains responsible for owned child
cancellation and cleanup.

Content playback claims the existing media suppression token before requesting
silence. The GUI polls actual notification quiescence every 25 ms for at most
three seconds; it does not block the GUI thread. Only a current, uncancelled
request may start content. Stop, project replacement, close and a newer play
request invalidate pending starts. The final tiny native notification start and
content admission share the same lock.

Private notification PCM is deleted only after the owned player confirms close.
A device error without confirmed buffer release retains both the PCM and device
ownership and prevents further audio admission. Restart is required if the
device cannot release its buffers. Speech never enters a rendered or recorded
media file. Normal application shutdown makes a bounded attempt to finish the
one owned feedback worker.

## Preferences and backup

The existing root `progress-settings.json` remains the sole preference file.
Its strict version 1 shape still supports Off/Beep. Version 2 adds Speech/Both
without adding fields. Unknown fields, duplicate keys and malformed/budget
violations are rejected. Existing typed library backup validates and preserves
this file through the same allowlisted producer.

No engine ID, executable path, voice authorization, pending audio or synthesized
PCM is saved in preferences or restored as an active operation. This slice does
not use `state/ui` or `state/progress`; those historical namespaces remain
separate from current preferences.

## Verification and limits

Required portable cases:

- `tests/test_progress.py`
- `tests/ui/test_progress_view.py`
- `tests/ui/test_audio_playback_controls.py`
- `tests/audio/test_progress_speech.py`
- `tests/ui/test_progress_voice.py`

The new cases use actual typed registries, PCM files, broker reservations,
backup/restore and production handlers. Device and engine doubles are explicit
where native hardware is not under test. They cover cancellation during
synthesis and playback, final admission after real synthesis, changed scope,
resource contention, byte/duration limits, retained unknown device ownership,
observer failures, deferred-start invalidation and restoration without authority.

For an installed Windows package, run:

~~~powershell
python -I -B scripts/windows_progress_smoke.py
python -I -B scripts/windows_progress_voice_smoke.py --native --output progress-voice-result.json
~~~

The second probe uses a real installed SAPI5 voice, real native controls and the
existing WinMM player. Read-only player instrumentation delegates every device
operation unchanged and records synthetic PCM identity before playback. It
checks original project/catalog bytes, native content interruption, cleanup and
source equality. Missing SAPI or output hardware cannot produce a playback PASS.
An unavailable output device is reported as UNAVAILABLE with exit 77; an actual
SAPI PCM result can be recorded separately in that failed acceptance attempt.
The final report is published only after owned worker/device cleanup.

The isolated workflow is `carr-progress-voice-qualification.yml`. Its artifacts
contain only synthetic acceptance reports and source-test logs, never voice
audio or model/runtime binaries. Source checks, actual SAPI synthesis, native
device playback and human listening/NVDA acceptance are separate evidence.
This document does not claim any of those gates ran merely because the source
exists. Human listening, screen-reader quality and unqualified neural notices
remain outside an automated PASS.
