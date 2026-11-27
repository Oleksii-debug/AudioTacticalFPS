# Local studio effects and output formats

The existing typed role-effect chain now exposes 21 effect types through
**Add role effect** on the studio Script page. Choose **Structured production
script**, select a role and effect, then enter the named numeric settings.
The action inserts a reviewed directive after that role's existing effects and
before its first spoken line. It retains the original source and the draft's
normal undo/recovery history. Each role still has a maximum of 16 effects.

The same bounded model validates saved projects, native controls and script
directives. No text can become a shell command, media URL or arbitrary FFmpeg
filter graph. The Script and keyboard help dialog lists the actual ranges,
defaults and units from that model.

## Effects

- Existing highpass, lowpass, equalizer, gate, compressor, delay and denoise
  retain their previous filter graphs when new optional controls are absent.
- Gain, low/high shelves, notch, limiter, declick, declip, deesser, four-reflection
  reverb, chorus, tempo, pitch, stereo width and target loudness are added.
- Gate exposes ratio and reduction; compressor exposes knee, makeup gain and
  mix; high/low pass expose Q; denoise exposes the floor; delay exposes mix.
- Pitch uses the actual PCM input sample rate, including when output resampling
  selects another rate. Its tempo compensation preserves duration. Tempo
  changes duration without intentionally transposing pitch.
- Width operates after the role's stereo placement. When enabled, the declared
  effect chain processes that stereo signal. Existing chains without width
  keep their former mono-effects-then-pan order.
- Reverb is four bounded reflections. It is not convolution or a calibrated
  room/RT60 model. Denoise/declip/declick do not guarantee restoration of missing
  information. No speech, music or voice quality is inferred from signal tests.
- The final existing safety limiter remains active. Choose a target loudness
  and inspect the measured output; a requested normalization target is not an
  independent listening or broadcast-compliance certificate.

Examples:

    [effect Narrator notch frequency=50 q=10]
    [effect Narrator compressor threshold_db=-20 ratio=3 knee=2.8 makeup_db=2 mix=0.8]
    [effect Narrator pitch semitones=2]
    [effect Narrator tempo factor=1.1]
    [effect Narrator loudness target_lufs=-18 peak_db=-1 range_lu=7]

## Outputs

WAV, FLAC, MP3, Opus, M4A, M4B and AIFF remain supported. Ogg Vorbis, raw AAC
(ADTS) and WMA (ASF) use the same sealed local FFmpeg encoder and no-overwrite
publication. WMA and Opus encode at 48,000 Hz; the WAV master/timeline retain
the selected project rate, and the plan preview states this distinction.
Container support for metadata/chapters varies; the production
manifest and subtitle outputs remain the detailed provenance record.

## Validation boundary

The new strict suite runs real synthetic PCM through the selected FFmpeg,
measures tempo/pitch/notch/width/loudness/limiting, checks all added effect
defaults, and encodes then decodes the three added formats. Source and installed
Windows gates must pass before treating this extension as verified for the
delivered runtime. The existing native Windows/NVDA and listening gates remain
separate. No supplied private model, recording or source is included.
