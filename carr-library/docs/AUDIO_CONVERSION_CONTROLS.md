# Numeric controls for local audio conversion

**Audio Studio → Convert audio and apply effects** exposes the existing
`AudioRuntime.convert` numeric settings in a scrolling keyboard-accessible form:

- Output sample rate, from 8,000 through 192,000 Hz.
- Mono or stereo output.
- PCM bit depth of 16, 24 or 32 bits; this UI's FLAC profile permits 16/24 bits.
- Target compressed bitrate from 32 through 512 kbps for the applicable encoder.
- Start and duration, gain, tempo without pitch change, optional speech cleanup
  and an optional loudness target.

The new output filename selects WAV, FLAC, MP3, M4A/M4B, AAC, Ogg Vorbis, Opus,
AIFF or WMA. Actual support depends on the selected installed FFmpeg build.
Opus explicitly requires 48,000 Hz. WAV/AIFF/FLAC use bit depth; compressed
encoders may use target bitrate, while Ogg Vorbis retains the existing quality
setting. A target bitrate is not a promise of exact file size or constant
instantaneous bitrate. Generic M4B conversion does not add chapters; use the
Studio production/publication workflow for measured chapter metadata.

Numeric input rejects non-finite values and invalid ranges before worker
admission. The existing runtime verifies source scope, real encoder/muxer
capabilities, output rate/channels/duration, byte budgets, cancellation and
no-clobber publication. Increasing bit depth or sample rate does not restore
information missing from the source.

The conversion captures the selected library object and data root before
admission. Queued work cannot silently switch to another library's runtime
configuration. Controls are read before the dialog is destroyed; the existing
frame worker executes the conversion without accessing native widgets.

A verified converted file is already committed before optional loudness
measurement. A measurement error or late cancellation reports that the file was
saved and measurement did not finish; it does not mislabel the saved output as
a failed conversion or overwrite it on retry.

## Evidence

The 28-case handler/control suite includes actual FFmpeg conversions to 24-bit
mono WAV at 48 kHz, 32-bit stereo AIFF at 44.1 kHz, 24-bit mono FLAC at 32 kHz
and mono Opus at 48 kHz. FFprobe additionally verifies FLAC's encoded bit depth.
Tests retain exact source hashes, verify output hashes/durations, reject invalid
Opus/FLAC settings before runtime creation, preserve an already published output
after measurement failure, refuse changed-library admission and protect existing
files. Native controls are stand-ins in this suite; actual Windows keyboard,
device and listening qualification remain separate.

`scripts/windows_audio_conversion_smoke.py` adds an installed Windows native
keyboard probe for this existing form. It supplies only two explicit synthetic
file-picker paths, enters 48000 Hz, mono and 24 bits with real Ctrl+A/digit keys,
and confirms with the native OK button. The original DesktopFrame worker and
AudioRuntime perform actual conversion. The probe verifies exact installed
module hashes and the selected FFmpeg/ffprobe paths, sizes and hashes against
the pinned audio lock; a full bundle must also match its sealed manifest.
Independent ffprobe inspection checks the resulting PCM codec, bit depth,
sample rate, channels and two-second/96000-frame duration, alongside the output
hash and unchanged source hash. Native waits have an 80-second overall watchdog
and a 65-second repeating faulthandler stack report. Key release failure cannot
bypass worker cancellation/join or fixture/window cleanup. Windows execution is
pending until the installed CI probe runs; native file pickers, output devices,
listening quality and NVDA are explicitly outside this synthetic test.

The same runtime is reused; this change does not introduce a durable conversion
batch queue or a second audio engine.
