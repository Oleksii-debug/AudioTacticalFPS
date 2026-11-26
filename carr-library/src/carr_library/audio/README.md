# Local audio engine

The studio audio adapter uses an explicitly selected installed FFmpeg/ffprobe
runtime. It does not install binaries, download voices or models, call an API,
record a microphone, open an audio device, or publish recordings.

## Public operations

- `Track(path, start_seconds, gain_db, pan, role, effects, routing)` describes a
  prepared local source. Roles are `speech`, `music`, and `effect`. `pan` downmixes
  to mono and uses equal-power stereo placement; `left` and `right` isolate that
  signal to one channel, `center` uses both equally, and `preserve` retains a
  stereo source. A mono output rejects an explicit left/right request.
- `FFmpegAudio.render_timeline(tracks, output, duration=..., ...)` creates a
  PCM16 WAV master, optionally ducking music under speech. An empty timeline
  with an explicit duration creates real silence. A long start time uses a
  streamed silence prefix, not a duration-sized delay buffer.
- `concatenate_segments(paths, output, ...)` normalizes each distinct input,
  then assembles a streaming concat playlist with one active decoder. Paths
  listed repeatedly are explicitly repeated; no loops are added automatically.
- `convert(source, output, ...)` supports the locally available WAV, FLAC,
  MP3, Vorbis/Ogg, Opus, AAC, M4A/M4B and AIFF encoders. A lossy result is decoded
  again to measure its actual frame count, rate and sample peak. Lossless
  conversion and word timing are not inferred from a filename.
- `inspect(path)` / `probe(path)` measures PCM16 RIFF/RF64 bounds, peak and
  SHA256. `measure_loudness(path)` obtains actual EBU R128 integrated LUFS,
  loudness range and true-peak dBFS through FFmpeg. Silence has no finite true
  peak, represented as `None`. A requested loudness preset is not a measurement.
- `capabilities()` inspects this installed build's filters and encoders. It
  explicitly reports recording/playback as unavailable in this batch backend.

Typed effects include parametric equalizer, high/low pass, gate, compressor,
limiter, delay/echo, tempo, denoise, loudness normalization and fades. Unknown
effects/parameters and non-finite or out-of-range values fail before execution.
Effects are composed from fixed FFmpeg filter names and numeric values; callers
cannot submit a filter graph or command. The final master limiter prevents
sample clipping at its configured 0.95 ceiling. Human sound quality and true
peak require their separate checks.

Both operations and verification stream bounded blocks. Final duration has no
ten-minute ceiling. A configurable simultaneous-track budget bounds one scene;
the durable studio scheduler renders scenes independently. A separate optional
output-byte/time budget and available disk check fail before publication.
FFmpeg automatically writes RF64 above the RIFF limit. No full audiobook is
loaded into a Python waveform array.

## File and process boundaries

Optional approved roots constrain source and destination paths. Inputs must be
regular local files; direct symlinks/devices are refused. Each source is copied
to a private processing snapshot while hashing and checking identity/size/time;
the decoder only receives that snapshot. Supported audio demuxers and the
`file` protocol are allowlisted, excluding network URLs and playlist indirection.
The only concat manifest is program-generated with safe temporary filenames.

Results are validated before an atomic, no-clobber publication on the same
filesystem. A destination that already exists, or appears during processing,
is never replaced. Cancellation terminates the child process, removes staging
files and leaves originals/completed outputs intact. Progress reports measured
rendered seconds; it does not invent an ETA. Stderr storage is bounded.

## Optional music providers

`MusicRequest` and `render_music` are in `carr_library.audio.music`.

**MusicGen** is a first-class optional local neural provider. It requires a
separately installed reviewed torch/transformers/safetensors/tokenizers runtime
and a local model inventory with externally recorded manifest SHA256. The
manifest records schema 1, engine `musicgen`, the official source URL, exact
40-character model revision, `CC-BY-NC-4.0` license and each file's SHA256. Only
inventory-listed JSON/text/tokenizer/safetensors files are snapshotted. Pickle
weights and code are refused. The worker uses `local_files_only=True`,
`trust_remote_code=False`, `use_safetensors=True`, offline/telemetry settings,
scrubbed secret environment values, and Python socket deny hooks before imports.

MusicGen's documented individual inference window is at most 30 seconds; this
is an engine context limit, not a final-project limit. The studio scheduler can
produce independently checkpointed segments and assemble long output. This
adapter currently does not claim neural continuation, musical coherence across
chunks, real MusicGen inference acceptance, or native Windows model quality.
Missing assets/runtime are an explicit unavailable state, never an automatic
online fallback.

**FluidSynth** uses an installed native library and an explicitly selected
SoundFont with its recorded SHA256 and license. It streams a deterministic
General MIDI arpeggio from tempo, major/minor mode, root note, program and seed.
The prose prompt is preserved but is not interpreted by this provider. Its
result is labeled algorithmic MIDI accompaniment, not neural text-to-music.
Native synthesis runs in a separate worker with no audio driver or network.
SoundFont parsing does not happen inside the desktop process.

`music_engine_fingerprint` includes typed settings, adapter/worker hashes,
actual FluidSynth version and mapped binary hash (Linux/Windows), or installed
neural runtime package versions, plus approved asset identity. It fails if a
required runtime/asset cannot be identified; cache identity is not inference
evidence. This implementation enables only private personal noncommercial
music usage. Assets and provider licensing remain separate from application
code; no SoundFont or neural weights are committed or redistributed here.

## Upstream references

This adapter calls installed upstream implementations and contains no copied
FFmpeg, FluidSynth or model implementation code.

- FFmpeg filters: https://ffmpeg.org/ffmpeg-filters.html
- FFmpeg protocol restrictions: https://ffmpeg.org/ffmpeg-protocols.html
- FFmpeg build licensing: https://ffmpeg.org/legal.html
- FluidSynth PCM rendering API:
  https://www.fluidsynth.org/api/group__audio__rendering.html
- Official Transformers MusicGen API, model limits and local generation:
  https://huggingface.co/docs/transformers/model_doc/musicgen
- Official MusicGen model and weights license:
  https://huggingface.co/facebook/musicgen-small

The original narrow FFmpeg compatibility probe in PR96 was inspected before
this production adapter. Its synthetic 60-second/eight-voice constraints were
not adopted. See `EVIDENCE.md` for the tests actually executed.
