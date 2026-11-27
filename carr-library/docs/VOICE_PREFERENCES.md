# Voice preferences by declared language

The reader can review the voices already present in its current voice list and
save a language default or a favorite for this library. Open **Voice preferences
by language...**, choose an explicit declared language or dialect, and choose a
voice. Saving a preference does not start speech. **Use selected voice in
reader** applies the explicitly reviewed identity to the current reader; F6
remains the separate reading action.

When the user chooses an exact declared language, its saved default is
highlighted if that identity still exists and declares that same language.
The review window does not infer language from text, replace a manual or
recovered reader selection when it opens, discover an engine, or activate a
private runtime. A missing default or a changed language produces an accessible
notice and no substitute. "en", "en-US", and "en-GB" remain distinct declarations.
Normalization only replaces underscores with hyphens and lowercases identifiers.

## Local state and recovery

The existing shared state writer stores one non-executable file:
state/speech/voice-preferences.json, schema carr-voice-preferences/1.
Its exact fields are schema, defaults, and favorites. It has a 1 MiB byte
budget, at most 256 language defaults, at most 128 favorite identities, and no
executable paths, credentials, model acquisition, or synthesized audio. The
window accepts at most 10,000 current voice identities. It preserves missing
identities for explicit review or removal.

Concurrent changes require Reload before another save. Invalid or future state
is reported and preserved. If the state writer did not acknowledge completion
but an exact readback shows the intended value and token, the window reports
that the intended state is present while this save's completion is unconfirmed.
It does not attribute an equal value written by another actor to this invocation.
An acknowledged save followed by a lock-exit error is reported separately.
A missing or different readback remains an error.

Common backup accepts only this exact known filename through the pure
validate_voice_preferences validator. It preserves the bytes and stable
identities without opening engines or converting a missing voice into a default.
It does not grant broad acceptance to unknown speech-state files.

## Qualification scope

The candidate includes actual-file service and common-backup cases, shared
bounded-reader cases, native-handler lifetime tests, and a Windows probe using
real wx controls, focus and keys with explicitly synthetic voice enumeration.
The native probe verifies exact installed module bytes and direct/parent native
destruction before replaying queued actions. It does not claim speech synthesis,
voice quality, physical human keyboard, NVDA, inferred document language, or
private-engine activation.

This newly composed source has not been executed during the executor outage.
Required gates are the full listed source and installed suites plus
python -I -B -X utf8 scripts/windows_voice_preferences_smoke.py on Windows.
There is no new package or runtime dependency.
