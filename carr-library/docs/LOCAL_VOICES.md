# Local voices and reading copies

The integrated studio discovers installed voices, renders actual PCM speech in
small cancellable chunks, and preserves the original narration text. A voice
only appears in the available list after its local engine enumerates it.

## Implemented adapters

Windows SAPI 5 uses a static installed PowerShell helper and the documented
System.Speech API. It discovers both native and WOW64 voices where those hosts
exist, preserves engine identity, checks the selected voice again before
rendering, and writes mono 24 kHz, 16-bit WAV. Source text goes through JSON and
the plain-string Speak method; it is never PowerShell or SSML. An unavailable
voice fails clearly instead of falling back to a different speaker.

An optional local FFmpeg build with Flite discovers its actual English voices.
The compatibility environment provided five voices. Flite relative speed uses
FFmpeg time stretching; it is distinct from native linguistic prosody. Optional
eSpeak NG discovery exposes installed language codes. No eSpeak language pack is
downloaded. Natural pronunciation, stress and native-accent quality require
listening acceptance for each chosen language and voice.

SAPI 4 remains unavailable until its real Windows 11 compatibility route is
tested. **dordiai-voices** is named correctly, but remains unavailable until the
actual project's interface is supplied and reviewed. No guessed private API is
presented as a completed integration.

Each result records engine and voice identity, exact input-text SHA256, audio
SHA256, PCM sample format, frame count and measured utterance duration. The
runtime fingerprint covers executable bytes, reported versions, the static
helper and installed voice identities. Opaque proprietary voice model assets
are not claimed to be individually hashed. Reload after installing or changing
voices. The wrappers make no service calls; a third-party installed SAPI engine
also needs its own offline/privacy verification. This is not a universal OS
network sandbox for arbitrary installed COM software.

## Reading edits

`NarrationDerivative` keeps an immutable original string and its checksum.
Accepted replacements, insertions and deletions refer to original Unicode
offsets. The reading copy exposes a mapping back to those offsets, a diff,
reject/undo/redo operations and versioned JSON recovery. Modified phrases map
to the complete original source span; this mapping is a reading aid, never
quotation approval. The module cannot write original files or the citation
database.

Pronunciation dictionaries support literal word/phrase substitutions with
language, voice and project scopes. Specific scope overrides precede generic
rules; already accepted manual edits win conflicts. Regex-looking characters
remain literal. Executable regex replacement, universal stress restoration,
phoneme/style synthesis and unsupported native accent promises are absent.

## Native preview

`LocalPlayer` opens a local PCM WAV without autoplay. Play, pause, resume, stop,
seek and status use typed WinMM messages. Seconds are converted explicitly to
native milliseconds. Filename text is never a command string. The preview
does not alter global output volume or NVDA audio settings. It supports the
installed Windows default audio device and reports missing-device errors.
RF64/compressed playback and advanced device/rate controls need a broader
verified playback backend; use individual WAV chapters for this preview.

## Verification boundaries

Portable tests include real local speech, timing and amplitude changes,
Unicode filenames, command-like text, cancellation, no-overwrite behavior,
runtime provenance, immutable source mappings and persisted undo history.
The native installed-package SAPI probe requires actual Windows speech output.
The separate native player probe reports `NOT_RUN / DEVICE_UNAVAILABLE` if no
audio device exists. Typed-message unit tests are not audible-playback evidence.
Manual Windows 11 listening and NVDA verification remain distinct acceptance.

## Primary interface references

- [Microsoft installed voices](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer.getinstalledvoices)
- [Microsoft plain-string speech](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer.speak)
- [Microsoft speech rate](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer.rate)
- [Microsoft WAV output](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer.setoutputtowavefile)
- [FFmpeg Flite filter](https://ffmpeg.org/ffmpeg-filters.html#flite)
- [Microsoft waveform audio commands](https://learn.microsoft.com/en-us/windows/win32/multimedia/waveform-audio-command-set)
- [Microsoft typed MCI structures](https://learn.microsoft.com/en-us/windows/win32/multimedia/mci-structures)

Existing engines retain their respective license terms. No voice binaries,
third-party model weights or private audio are included by these source files.
