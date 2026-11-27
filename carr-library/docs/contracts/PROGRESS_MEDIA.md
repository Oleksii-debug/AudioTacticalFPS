# Progress feedback with the selected local audio tools

This adapter targets the selected PR99 audio architecture in the CARR11 delivery:
`audio.engines.VoiceRegistry`, `ui.audio_studio.AudioStudioDialog` and the
existing `audio.player.WavePlayer`. PR112's `audio.voices` and PR103's
`speech.runtime` have different APIs; their files are not replacements for
these selected modules.

## Use

Progress feedback is Off by default. In Audio Studio, load the installed local
voice list, select a voice and its supported language, then activate **Use
selected voice for progress**. This connects that existing registry voice for
the current library session. It does not start speech or change the feedback
mode. Use the existing Progress feedback settings to choose Speech or Beep
and speech, adjust the notification volume, or keep Off.

A different library requires a new explicit voice choice. The voice registry
and installation paths are not copied into progress preferences or backups.
Closing Audio Studio keeps the explicitly selected voice available within the
same library session.

Quiet during media reads the actual Studio transport state. Completed native
MCI playback or a completed ffplay process stops suppressing notifications.
Pause, resume and natural completion are transport observations; a guessed
duration or stale Play button flag is not used. A failed state query remains
quiet. Studio and progress speech own separate player instances and never
change NVDA's volume.

## Shared APIs

`install_media_progress(frame)` installs one idempotent bridge and preserves
any earlier media/recording flags. The Studio registers a getter for its actual
player and unregisters after closing it. `before_playback()` cancels queued
progress notifications when the existing quiet preference is enabled.

`LocalVoiceProgressSpeech(registry, voice_id=..., language=..., media=bridge)`
implements the shared `speak_progress(text, volume=0..100, cancelled=callback)`
contract. It reuses the explicitly selected registry and its existing local
engines. A missing voice, unsupported language or unavailable transport fails
visibly; another engine is never substituted.

The feedback worker owns synthesis and its own player. Notification text is
bounded to 240 characters; output must be validated PCM, no more than 8 MiB
and 20 seconds. The 30-second monotonic allowance is a cooperative
cancellation request, not a hard deadline for every existing provider's
asset-verification preflight. Expired or cancelled synthesis cannot start
playback. Temporary notification audio is removed after the owned player
closes and is never passed to the production renderer, project cache, stems
or exported recording.

## Recording integration boundary

The selected delivery does not yet contain a microphone transport. This
adapter supplies a preflight contract; it does not claim to implement recording.

A future recorder must request `lease = bridge.begin_recording()` before
opening its microphone. The request marks recording active first, cancels
queued and active progress feedback, and then exposes `lease.ready`.
Poll readiness without blocking wx. Open the microphone only when ready is
true, and keep the lease until the actual recorder has closed; then call
`lease.close()`. This suppression applies even when Quiet during media is
disabled.

Readiness requires both shared feedback quiescence and confirmed cleanup of
the owned notification transport. An unsuccessful native stop/close leaves
readiness false. The existing best-effort dialog cleanup cannot be interpreted
as proof of acoustic silence. A short OS progress tone already playing must
finish before the preflight becomes ready.

## Validation boundaries

Backend tests exercise the real existing VoiceRegistry contract, actual
temporary PCM files, real installed Flite synthesis where available, actual
feedback threads, recording/cancellation races and a failed native-stop
boundary. Player/device calls in those unit tests are explicit doubles. Native
voice-selection button tests require a real wx display. The source tests do
not establish Windows audio-device playback, acoustic quality, NVDA speech or
microphone capture acceptance; those remain explicit native/listening gates.
