# Local audio adapter: implementation and evidence

This is an adapter inside the existing desktop product, not a second application.
It uses the user's separately installed FFmpeg and ffprobe. No FFmpeg binary,
SoundFont, voice, neural model, private recording or source document is included.

## Reused implementation

The original synthetic `bootstrap/audio_mix_compat` candidate in PR96 at
`75363548aca2a65add36e878286becb296c457d3` established the actual FFmpeg pan,
sample delay, sidechain compression, mix and limiter route. The production
adapter extends that route with typed parameters, measured input/output,
cooperative cancellation, encoding capability discovery, lossless scene
assembly and no-replace output publication. It does not copy upstream DSP code.

Scope V3 inspected upstream FFmpeg revision
`fa456433196deda8677258e695f6614d0f094c38`, license file blob
`0e75dd3030844784ae339fb51300bd1a3b1e6207`. The executed Linux build reports
`7.1.5-0+deb13u1` with `--enable-gpl --enable-libflite`; this is a runtime
compatibility result, not a redistribution determination for some other build.
Cache identity includes actual executable SHA256 values and both tools' complete
reported build/linked-library versions. Selecting a different executable changes
the identity. Source/runtime licensing must be preserved if an installer later
bundles a build.

Primary API references checked on 2026-11-26:

- https://ffmpeg.org/ffmpeg-filters.html — equalizer, agate, acompressor,
  alimiter, afftdn, aecho, afade, loudnorm, pan, adelay, amix, sidechaincompress.
- https://ffmpeg.org/ffmpeg-formats.html — audio containers, RF64 WAV,
  self-authored concat input and safe relative filenames.
- https://ffmpeg.org/ffmpeg-protocols.html — explicit per-input file protocol
  allowlist. URLs, network shares, playlists and arbitrary filter expressions are
  not accepted by the public adapter.
- https://ffmpeg.org/legal.html — a particular build's configuration determines
  its applicable FFmpeg licensing obligations.

## Supported behavior

`AudioBackend.mix` accepts typed mono/stereo tracks, sample-frame start positions,
gain, equal-power mono placement, stereo balance, ordered numeric effects and
optional speech-driven music ducking. `preserve_stereo=True` expressly preserves
a stereo premix and refuses extra pan. Empty tracks render an exact silent scene.
Every master ends in a latency-compensated limiter and an exact frame trim/pad.
That is a clipping safeguard, not a claim of suitable artistic balance.

Available effects are read from the actual selected runtime. Supported typed
controls cover EQ, gate, compressor, limiter, FFT denoise, one-tap echo, fades,
EBU R128 normalization, high/low-pass filtering and gain. Unknown parameters are
rejected. Native room reverb, formant control, untrusted VST execution, realtime
effects and binaural positioning are unavailable; an echo is not labelled reverb.
Loudness and true peak are measured from actual audio with `loudnorm` input
statistics; silence returns an unavailable numerical measurement rather than an
invented finite LUFS value.

Scene masters use PCM16 stereo WAV or FLAC. WAV enables RF64 automatically for
large output. Lossless concatenation requires a consistent codec/rate/channel
layout, uses bounded file snapshots or hard links plus a generated safe manifest,
and verifies the resulting total frame count. It never joins all waveforms in
Python memory. The configured simultaneous-track budget can be raised or managed
by the director's scene/line batching; it is not a final project duration limit.

Build-supported exports include WAV, FLAC, MP3, Ogg Vorbis, Opus, AAC, M4A/M4B,
AIFF and WMA. Encoders must actually exist. Compressed outputs report their
observed duration (encoder padding is not a lossless frame-accuracy claim).
Opus uses a 48 kHz decoded clock. AIFF's 32-bit size limit is checked; choose
RF64 WAV or FLAC for an oversized master. Chapter tags and subtitle production
belong to the director and are not supplied merely by selecting M4B.

Outputs are rendered in a temporary directory on the chosen volume, verified,
fsynced, then published by atomic no-replace hard link. Existing files are never
overwritten. Input identity is rechecked before publication. A filesystem without
hard-link support fails safely. Cancellation, time-budget expiry, runtime error
or verification failure cleans up staging. Logs use bounded draining buffers.

## Music engines and limitations

`compose_midi` writes an actual deterministic original Standard MIDI File with
note and tempo events. It streams events and checks a configurable note budget
before writing. `render_procedural` uses FFmpeg oscillators for a quiet original
four-chord cue with a repeating motif. It is explicitly procedural tone music,
not neural generation and not a promise of studio-quality composition. A fixed
seed, settings and runtime reproduce the file in the executed tests.

Optional FluidSynth rendering requires an explicitly selected installed runtime,
local MIDI and a separately licensed SoundFont with confirmed usage rights. An
empty configuration file prevents automatically loading a user startup script.
FluidSynth was not installed in the tested Linux environment; audible SoundFont
rendering remains NOT_RUN. This first implementation writes standard MIDI
directly without adopting Pretty MIDI or any additional dependency; later richer
editing should evaluate that already identified upstream instead of extending a
new general MIDI framework.

MusicGen and ACE-Step remain visible optional engines with unavailable reasons.
MusicGen is not excluded from the personal noncommercial profile: exact reviewed
weights, compatible installed worker, actual local generation and licence
manifests are still needed. No model was downloaded, no neural inference executed,
and no remote API invoked by this adapter. The file-only FFmpeg protocol policy
is not presented as an operating-system network sandbox.

## Reproduce actual tests

From `carr-library` with Python, pytest and local FFmpeg/ffprobe:

```console
python -m pytest -q tests/audio/test_ffmpeg.py tests/audio/test_music.py
CARR_AUDIO_LONG_TESTS=1 python -m pytest -q -s tests/audio/test_ffmpeg.py tests/audio/test_music.py
```

The extended command uses shell environment syntax for Linux. On PowerShell,
set `$env:CARR_AUDIO_LONG_TESTS = '1'` before the pytest command. Without that
explicit option the three long tests are labelled skipped, not accepted.

Tests measure actual left/right placement, pauses, ducking, EQ gain, gate,
compression, echo impulse timing, fades, LUFS/peak, exact scene/concatenated
frames, real codecs, no overwrite, Unicode filenames, source invariance,
cancelled render cleanup, rejected links/URLs/playlists, deterministic MIDI and
real procedural waveform output. The extended fixture writes and decodes actual
1/3/10-hour 8 kHz stereo FLAC outputs assembled from repeated synthetic silence.
It records output bytes, wall time and peak Python allocations. These are
streaming/precision tests: they do not establish long-form neural generation,
musical continuity, perceptual quality, actual Windows audio devices or NVDA
acceptance. Those remain distinct required V3 gates.
