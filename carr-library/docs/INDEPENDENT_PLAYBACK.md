# Independent playback speed, pitch and levels

The reader and saved-audio previews use the existing WavePlayer. Playback settings
are separate from synthesis rate, synthesis voice volume, clip processing and
exported audio. Opening the controls, entering values or preparing a listening
copy never starts sound.

## Live source and reading-copy readers

Choose **Independent playback settings...** beside **Playback output...**.
The command also has the stable reader command ID `reader.playback_settings`,
so it can be assigned a shortcut in the existing reader keyboard settings.

Stop reading before choosing a different playback speed or pitch. Choose
**Use speed and pitch for next reading**, close the window, and start the desired
source range explicitly. The current reader worker synthesizes the original
chunk with the selected voice settings, then uses the selected local FFmpeg
runtime to prepare the private listening copy. The same worker owns playback,
source rechecks, cancellation and cleanup.

Supported speed is **0.25× to 4×**. Pitch is **−12 to +12 semitones**, independent
of speed. The original synthesis chunk and its source text remain unchanged.
The default speed of 1 and pitch of 0 use the existing path without an extra
conversion.

The current chunk's reported position and seek controls refer to the **original
speech timeline**. Mapping uses the actual source and result PCM frame counts.
It is a linear timeline mapping, not word-level alignment or a new quotation
position. The existing saved resume point remains the verified source chunk
and its original synthesis settings. Playback settings are session choices;
they do not rewrite saved projects, source editions or the shared voice profile.

## Audio Studio and clip previews

Start an explicit preview, then open **Playback navigation...** in Audio Studio
or **Preview playback controls...** in the clip editor. Choose
**Independent playback settings...**.

**Stop and prepare listening copy** stops the current preview, clears its pending
repeat/sleep actions and prepares one bounded private PCM copy on that owner's
existing worker. The original recording and saved clip project remain unchanged.
Use **Play current listening copy** explicitly after the result is accepted.
Opening a different original recording resets the private speed/pitch copy.

**Enter original playback speed and pitch** only fills in 1× and 0 semitones.
Apply them explicitly. The default reset copies the original PCM bytes without
requiring the optional conversion engine.

A–B repeat, seek and sleep remain in the existing navigation controller. Its
positions refer to the original recording. On the POSIX ffplay fallback, play
position remains an explicitly approximate clock estimate.

## Volume, mute and stereo balance

On Windows, **Apply player volume, mute and balance** scales only the bounded PCM
buffers owned by the existing player. It never changes a Windows stream,
application-session, endpoint or device volume setting. Volume ranges from
**0 to 100**, mute retains the requested volume, and balance ranges from
**−100 left to +100 right**. Balance requires a stereo recording. The arithmetic
supports unsigned 8-bit and signed 16-, 24- and 32-bit PCM, without amplification
above the original sample range.

While playing, buffers already submitted to the driver remain untouched. The
queue holds at most four blocks of **max(1, floor(sample rate / 20)) frames**.
At sample rates of 20 Hz or higher that is at most **200 ms** with its previous
levels. Lower rates retain up to four sample frames. Device latency may add to
the time before the change is heard. Paused changes
release and rebuild the queued buffers at the observed frame while retaining
the pause. Applying controls to a stopped player remains silent.

When active MCI playback changes to the owned WaveOut path, the old MCI alias
must confirm closure before the new native handle is admitted. Unsupported PCM
formats or unconfirmed native cleanup produce an explicit error; they do not
select another device.

Live-reader changes are queued to the existing reader worker. The controls
report a request; they do not claim instantaneous native completion or silence.
The POSIX backend currently disables live level controls with an explanation.
Speed and pitch conversion remain available when the selected FFmpeg runtime
supports the required filters.

## Ownership and bounds

- Shared resource admission precedes creation of the private source snapshot.
- Reads use regular, non-following descriptors and bounded chunks. The selected
  source is checked before and after conversion; result length, frame counts
  and digest are checked before adoption.
- Pending process cleanup keeps its exact existing native owner, resource lease
  and both private directories. An interrupted or obsolete result cannot
  become active playback.
- A prepared copy stays owned until its native playback transport confirms
  release. Failed closure remains visible and retryable.
- Closing a native settings window cancels only its own pending preparation.
  Direct destruction and parent-driven destruction detach queued callbacks.
- Existing synthesis, export, resource budgets and source authorization checks
  remain in effect. Slow playback can exhaust the current live-reading audio
  or wall-time budget; an explicit resume starts from the existing source
  checkpoint.

## Acceptance status

This document describes the source candidate. Source and installed tests are
being run on the dedicated playback-controls review branch. The tests use actual
FFmpeg PCM conversion and source/receipt checks. Controlled MCI/WinMM boundaries
are explicitly labelled and are not physical audio-device evidence.

The native Windows automation exercises real wx controls and keyboard events
with synthetic voices/transport where disclosed. Physical output behavior,
human listening quality and NVDA operation require their separate measured
acceptance. No results from the earlier clip-only gate qualify this playback
composition.

The Windows API reference describes handle-specific waveOut volume, while
Microsoft's legacy-audio session guidance links those volume controls to the
application session. This implementation avoids that ambiguity by changing
only the samples queued by its own player:
[waveOutSetVolume](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutsetvolume)
and [legacy audio events](https://learn.microsoft.com/en-us/windows/win32/coreaudio/audio-events-for-legacy-audio-applications).
