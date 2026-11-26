# Local speech and reading derivatives

This is a production adapter boundary for V3-02/03/04/13, with real local WAV
rendering. It does not establish native-speaker quality or full V3 acceptance.
No original source, approved quotation, or catalog record is changed here.

## Public API

```python
from pathlib import Path
from carr_library.speech import (
    SpeechRequest, discover_voices, engine_fingerprint, render_speech,
    Pronunciation, prepare_reading,
)

voices = discover_voices()  # tuple of frozen Voice records; no downloads
# Select an actual ID from this result; there is no invented default voice.
voice = voices[0]
reading = prepare_reading("NATO", [Pronunciation("NATO", "Nay toe")])
request = SpeechRequest(reading.text, voice.id)
identity = engine_fingerprint(voice.id)
result = render_speech(request, Path("chosen-output") / "new-speech.wav")
```

`Voice` contains `id`, `engine`, `name`, `language`, `capabilities`.
`SpeechRequest` contains text, discovered voice ID, optional language, relative
rate 0.5–2, volume 0–1 and relative pitch -1–1. Pitch is an eSpeak-native relative
control, **not calibrated semitones**. SAPI5 currently advertises no pitch
capability and rejects a nonzero pitch request. Rate is engine-relative; it is
not a guarantee of exact audio-duration scaling. A language mismatch fails
instead of silently selecting a different voice.

`SpeechResult` contains the published `path`, `sample_rate`, `channels`, `frames`,
`duration_seconds`, `engine`, `voice_id`, output `sha256`, and actual
`engine_fingerprint`. The latter includes executable bytes/stat/version,
installed voice metadata and, for eSpeak, phoneme/voice/dictionary bytes and
available shared-engine library bytes. A caller must include this identity and
the whole request in its chunk cache and verify the cached WAV checksum.

SAPI5 identity currently includes its host executable and advertised token
attributes, **not an exhaustive inventory of each vendor's voice DLL and model
data**. A third-party voice update that preserves those attributes is not
proven to invalidate its cached audio. Stable cross-restart SAPI model identity
requires a verified vendor-specific asset inventory and Windows acceptance;
this limitation is not satisfied by the stronger eSpeak byte checks. Microsoft's
object-token documentation describes attributes, CLSID and optional associated
files, rather than a universal complete voice-weight manifest:
https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ms717058(v=vs.85)

`cancelled` accepts a callable or a threading Event. Per-chunk process execution
has a configurable `timeout_seconds` default of 600. Text is bounded to 65,536
characters and PCM to 512 MiB **per chunk**. Project duration is not restricted by
this adapter; the persistent studio scheduler must split large narration and
resume verified chunks. Both output parsing and hashing use bounded reads.

## Engines and installation

- Windows SAPI5: discover both available 64-bit and 32-bit Windows PowerShell
  hosts and their installed SAPI tokens. No pywin32 dependency, DLL installation,
  execution-policy override, SSML interpretation, shell interpolation, or
  automatic voice fallback. Static PowerShell code receives JSON through stdin,
  renders through `SAPI.SpVoice`/`SAPI.SpFileStream`, and never plays to the audio
  device. Voice IDs preserve host bitness and token identity.
- eSpeak NG/eSpeak: use a separately installed executable found on absolute PATH
  entries, excluding the working directory. An explicit
  `CARR_ESPEAK_EXECUTABLE` may point to an absolute installed executable.
  `CARR_ESPEAK_DATA_PATH` optionally selects the absolute installed data directory
  or its parent. No package or model is downloaded by discovery/rendering.
  MBROLA-specific entries are excluded pending their own runtime/license review.
- SAPI4 and dordiai-voices are explicitly unavailable in this adapter. SAPI4 needs
  a verified compatible helper; dordiai-voices needs its actual versioned local
  contract. Neither is represented by simulated speech or a guessed API.

All output is newly generated local PCM16 WAV. Existing files, symlinks, remote
UNC paths, directory symlinks/reparse points and unsupported output formats are
rejected. Render occurs in a private staging directory beside the target; after
success, actual WAV frames and checksum are checked and publication is atomic
and no-clobber. Cancellation/failure removes the staged output. Native error
messages cannot echo private input through the application's exception text.

The adapter has no remote client or download path. Installed SAPI engines remain
third-party native code: their offline behavior and any OS-enforced child-process
egress policy require separate verification. Do not claim universal hard network
isolation merely because the adapter itself uses no network. Existing licensed
voices remain installed by their owner; do not bundle proprietary voice assets.

## Immutable reading preparation

`prepare_reading` applies explicit, case-sensitive, literal pronunciation rules;
The longest eligible match takes priority and replacements are not recursively
applied. A longer literal that fails its whole-word boundary does not hide a
valid shorter rule at the same position.
Unicode combining marks count as part of a word. Optional CR/LF normalization
is explicit. `apply_reading_edits` accepts bounded, nonoverlapping source-offset
edits for an editor/director layer. Both return a frozen `NarrationDerivative`
with exact `original_text`, derivative text, edits, separate hashes and a span
map. Mapping inside a changed word returns its full source range and never
fabricates word-level alignment. An inserted range maps to an empty source range.
Do not use reading derivatives as verified quotations. No automatic punctuation,
Ukrainian stress model, native Slovak accent or meaning-preserving rewrite is
claimed by these literal adapters.

## Reuse and provenance

No upstream speech engine code or voice data is copied into this package.
The adapters call mature installed engines through their documented interfaces:

- eSpeak NG 1.52.0 tag, exact upstream commit
  `4870adfa25b1a32b4361592f1be8a40337c58d6c`:
  https://github.com/espeak-ng/espeak-ng/tree/4870adfa25b1a32b4361592f1be8a40337c58d6c
  and its `src/espeak-ng.c`/manual CLI (`--voices`, `--stdin`, UTF-8, rate,
  amplitude, pitch, WAV). Upstream GPL-3.0-or-later plus documented file-specific
  notices; separately installed runtime, not a proprietary-voice distribution.
- Microsoft SAPI5 `SpVoice`, plain-text `Speak` flags, `Rate`/`Volume`, and
  `SpFileStream` documented API:
  https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ms723609(v=vs.85)
  https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ms723606(v=vs.85)
  https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ms723615(v=vs.85)
  https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ms722562(v=vs.85)

Development acceptance used official Debian trixie packages, extracted outside
the repository, with no system installation or bundled runtime:

| Package | SHA256 |
| --- | --- |
| espeak-ng 1.52.0+dfsg-5 amd64 | b64da7b5e21b3540a5cfe5a2fd5c650f6f366ffccb9b810eb337ea4677435d31 |
| espeak-ng-data 1.52.0+dfsg-5 amd64 | 7c239b30e419897313b5b8c98123cf3ec871cfedbf43bbb956b985a492e37f19 |
| libespeak-ng1 1.52.0+dfsg-5 amd64 | 845886095f5be3ba7680c8e15de068f76d67600275cbd7b6f6c7d00e53c05b34 |
| libsonic0 0.2.0-13+b1 amd64 | 07a1d67546465f07638be05f0931d79e0d6bb1a7831e1f67ac6611d1ba297009 |
| libpcaudio0 1.3-1 amd64 | db95c30434707a60774ed9ad0193ba680e061ae7d99f354a1a93f147317a4ea5 |

Runtime source URLs: https://deb.debian.org/debian/pool/main/e/espeak-ng/,
https://deb.debian.org/debian/pool/main/s/sonic/,
https://deb.debian.org/debian/pool/main/p/pcaudiolib/.

## Verification

From the repository root:

```
PYTHONPATH=carr-library/src python -m unittest discover -s carr-library/tests/speech -p 'test_*.py' -v
PYTHONPATH=carr-library/src python carr-library/tests/speech/probe_installed_speech.py
```

The first command covers adapter validation, literal source fidelity and mapping,
real process stdin/timeout/cancellation/diagnostic limits, safe path handling,
WAV truncation, failure cleanup and concurrent no-clobber publication. Synthetic
PCM used by safety fixtures is explicitly not speech evidence.

The second command requires an actual installed engine and fails if none exists.
On Linux with the above eSpeak NG runtime: real Ukrainian, Slovak and English
PCM16 render, relative-rate duration ordering, zero-volume silence, Unicode output
names, checksums and existing-output protection passed. Discovery returned 140
voice entries, not 140 independently verified languages. No native-quality or
human-listening acceptance is implied. Windows SAPI5, SAPI4, dordiai-voices,
Windows/NVDA, playback, audio-device recovery and listener-quality acceptance
remain separate tests; they were not run by the Linux adapter tests.

## Optional native PCM player

`carr_library.speech.player.LocalPlayer` provides `open(path)`, `play()`,
`pause()`, `resume()`, `stop()`, `seek(position_seconds)`, `close()`, `status()`
and `available()`. The status dictionary contains availability, backend, state,
path, position/duration in seconds, rate, capabilities and an unavailability
reason. Construction never plays audio. Opening a file never starts playback;
stop rewinds, pause retains position, seek while paused remains silent, and
close releases only this instance's device. Context-manager usage closes it.

The dependency-free Windows compatibility backend uses the installed WinMM
waveaudio driver. DLL loading is restricted to System32, filenames travel in
typed Unicode structures, and no MCI command-string or shell parser is involved.
No recorder or global-volume setting is exposed. Only validated local RIFF
mono/stereo PCM16 WAV is supported initially; URLs, UNC, symlinks, reparse paths,
truncated input and unsupported formats fail. RF64/other containers and richer
device selection require another verified playback backend. Independent playback
speed is honestly marked unsupported: `set_rate(1)` resets the default, other
values fail rather than rewriting synthesis or pretending to preserve pitch.

Microsoft classifies MCI as legacy and recommends MediaPlayer for new Windows
10/11 applications. This small adapter provides the initial built-in PCM path;
future MediaPlayer/mpv support can replace it through the same interface.
Official ABI references checked, including the `pshpack1.h` structure packing:

- https://learn.microsoft.com/en-us/windows/win32/multimedia/waveform-audio-command-set
- https://learn.microsoft.com/en-us/windows/win32/multimedia/mci-open-parms
- https://learn.microsoft.com/en-us/windows/win32/multimedia/mci-status-parms
- https://learn.microsoft.com/en-us/windows/win32/multimedia/mci-seek-parms
- Microsoft SDK mirror `mciapi.h` blob
  `7480ffeb20f95a88f9e5b87c52354d702396f584` and `mmsystem.h` blob
  `26476d069dc7d0684be621a8a44e7dcdb51ca6e4` in
  https://github.com/microsoft/win32metadata/tree/main/generation/WinSDK/RecompiledIdlHeaders/um

`tests/speech/test_player.py` exercises file validation, packed 32/64-bit ABI,
owned-device lifecycle, failure cleanup, state transitions, end/restart and
seek/pause semantics using an explicitly fake device. These are not real audio
or native Windows acceptance. On a Windows machine with an audio output:

```
python tests/speech/probe_windows_player.py
python tests/speech/probe_windows_player.py --play
```

The first command checks actual native open/position/seek/close without sound.
The second also plays a quiet synthetic tone and exercises pause/resume/stop.
Both fail explicitly if native devices are unavailable. Listening and NVDA
remain separate even when these command checks pass.
