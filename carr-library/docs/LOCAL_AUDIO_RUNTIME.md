# Local audio rendering and instrumental engines

The integrated studio uses real local FFmpeg processes for audio assembly,
effects, analysis and encoding. Project orchestration and voice discovery are
documented separately; this document covers the renderer, local music engines
and private Windows runtime. No renderer operation downloads a binary, model,
voice or recording, and no generated recording is published automatically.

## Implemented renderer

`carr_library.audio.render` exposes these typed operations:

| API | Result and scope |
| --- | --- |
| `probe_audio(path)` | Actual sample rate, channels, frames, duration, codec and container |
| `mix_tracks(tracks, output, ...)` | Disk-backed scene, silence or stem; gain, equal-power mono pan, stereo preservation, fades and optional speech sidechain ducking |
| `convert_audio(source, output, ...)` | Explicit trim, channel/sample-rate conversion, effects and encoding |
| `concatenate_audio(paths, output, repeat=1, ...)` | Streaming assembly of compatible chunks; an explicit repeat count remains recorded as repetition |
| `analyze_audio(path, measure_loudness=True)` | Measured sample peak and FFmpeg EBU R128 loudness report |
| `runtime_identity()` | Actual FFmpeg/ffprobe executable hashes and version/build text, plus renderer/effect implementation hashes |

`Track(path, start_seconds=0, gain_db=0, pan=0, kind='voice', fade_in=0,
fade_out=0, effects=(), preserve_stereo=False)` accepts voice, music, ambience
and SFX tracks. `pan=-1`, `0`, `1` means left, center and right. Stereo music
and ambience preserve their channels; `preserve_stereo=True` retains a prepared
stereo voice lane while keeping its voice role in the sidechain. A mono export
is explicitly downmixed. Discrete multichannel buses and binaural room models
are not implemented by this renderer.

The per-scene native filter graph admits at most 128 simultaneous tracks.
The director assembles longer sequential timelines in bounded blocks and
concatenates their stereo lanes. This is a graph complexity bound, not a
programme duration limit. Long timeline offsets use streamed silence followed
by concatenation, avoiding a delay buffer proportional to the timestamp.
An empty track list with a positive duration produces actual silence, including
gaps and empty role stems. Foreground speech/SFX cannot be silently truncated
by a shorter requested scene duration.

The renderer validates local regular paths and rejects URLs, UNC paths, links,
Windows reparse points and alternate data streams. It supplies argv directly
without a shell, constrains input demuxers/protocols, and compiles filter graphs
from numeric controls rather than accepting arbitrary filter expressions.
Cancellation and deadlines stop the child process; diagnostics have a bounded
size. A private staging file is probed and measured before exclusive publication.
Existing destinations are never replaced. Input hashes are checked again before
publication. These checks assume the user's local workspace is not concurrently
modified by an adversarial process; they are not an operating-system sandbox.

Every `RenderResult` includes an output SHA256, actual frames, sample rate,
channels, duration, peak and operation metadata. Disk estimates and optional
output budgets are checked before generation. WAV uses `-rf64 auto`, so the
ordinary RIFF 4 GiB boundary does not impose a ten-minute or ten-hour ceiling.
FLAC is useful for long lossless intermediates. WAV/FLAC/AIFF frame counts are
checked exactly; compressed encoders can introduce codec delay or padding.
MP3, Ogg/Vorbis, Opus, AAC/M4A/M4B and WMA are available when the actual installed
FFmpeg includes those codecs. WAV and FLAC assembly and AAC chapter metadata
have real automated coverage; this does not certify every codec/device pairing.

Chapter metadata uses sample-aligned boundaries and escaped titles. Containers
must support chapters; production additionally retains sidecar chapter/script
data. Global input tags are removed without discarding chapter titles.

## Effects

`Effect(kind, parameters, enabled=True)` is immutable and validates numeric
bounds before execution. Chains contain at most 24 stages. The vocabulary is
gain, highpass, lowpass, equalizer, gate, compressor, limiter, denoise, deesser,
declick, declip, delay, room_echo, tempo, pitch, stereo_width and loudness.
Presets are `dry`, `narration`, `podcast` and `small_room_echo`.

The room and delay processors use four controlled echo taps. They are not
convolution reverb or a physically measured room simulation. Pitch adjustment
uses FFmpeg resampling and tempo correction; it is not voice cloning. Loudness
normalization is FFmpeg's dynamic `loudnorm` mode; sample peak and measured
loudness are distinct measurements. The final scene limiter keeps ordinary
sample peaks below full scale, but perceptual quality and true-peak compliance
still require listening and the selected delivery specification.

Primary DSP documentation: [FFmpeg audio filters](https://ffmpeg.org/ffmpeg-filters.html)
and [FFmpeg formats](https://ffmpeg.org/ffmpeg-formats.html).

## Local instrumental engines

`generate_music(MusicSpec(...), output, ...)` executes only the selected engine.
It raises a clear configuration/runtime error if that engine is absent; it never
reports an algorithmic fallback as neural generation.

| Engine | Actual implementation | Installation and limits |
| --- | --- | --- |
| `procedural` | Seeded symbolic note/chord pattern rendered by FFmpeg oscillators and envelopes | Included with FFmpeg; mood, key, tempo and seed controls; motif recurrence is explicitly reported |
| `fluidsynth` | Seeded note events rendered through FluidSynth's block audio API in an isolated child | User-installed FluidSynth and `.sf2` SoundFont; SoundFont license must be explicitly recorded |
| `musicgen` | Optional local Transformers `MusicgenForConditionalGeneration` worker | User-installed Python environment and verified passive model assets; at most 25 seconds per generation chunk |

The procedural engine accepts any project duration allowed by available disk
and user budgets. Its deliberate recurring motif is not continuously novel
neural composition. The director generates independent short chunks and stores
their identities; its fades do not establish musical continuity across hours.
FluidSynth and SoundFont rights are separate. No SoundFont is copied into the
application or Windows runtime package.

`MusicSpec(duration_seconds, tempo_bpm=80, key='A', mood='calm', seed=0,
engine='procedural', prompt='')` is the common control model.
`music_runtime_identity(options)` includes actual FFmpeg/ffprobe identities,
music/worker implementation hashes, the configured model manifest identity,
Python executable identity, device, and any selected SoundFont/runtime identity.
Changing these inputs invalidates the director's music cache.

### Optional MusicGen setup contract

Use a separate, explicitly installed local Python environment. The worker checks
the installed `torch` and `transformers` versions against an approved manifest.
It loads the processor and model from the selected directory with
`local_files_only=True`, `trust_remote_code=False` and `use_safetensors=True`.
No application path acquires weights. Python Internet sockets, DNS and package
telemetry are disabled before optional libraries are imported. Native-library
egress restrictions additionally depend on the host firewall/sandbox policy.

Pass `MusicGenConfig(model_dir, manifest, manifest_sha256,
python_executable=..., device='cpu', timeout_seconds=...)` as
`musicgen_config` to `generate_music`. CPU and explicitly selected CUDA devices
are supported by the adapter; absent CUDA does not silently select another
device. Inference performance and installed model compatibility require a real
hardware test. This development host had no Torch/Transformers environment or
MusicGen weights, so actual neural generation remains **NOT_RUN**.

The external UTF-8 manifest must contain exactly:

```json
{
  "version": 1,
  "engine": "musicgen-transformers",
  "model_id": "facebook/musicgen-small",
  "revision": "<exact reviewed 40–64 character hexadecimal revision>",
  "weights_license": "CC-BY-NC-4.0",
  "code_license": "Apache-2.0",
  "transformers_version": "<exact installed version>",
  "torch_version": "<exact installed version>",
  "files": [
    {"path": "config.json", "sha256": "<actual SHA256>", "size_bytes": 123},
    {"path": "model.safetensors", "sha256": "<actual SHA256>", "size_bytes": 456}
  ]
}
```

This is a shape example, not an installable manifest or an approval of example
weights. Inventory every actual model/processor/license file with its exact
size and SHA256, and keep the manifest outside the model directory. Unknown
files, Python source, pickle-based weights, links, duplicate JSON keys and
changed bytes are rejected. The model identifier or revision text alone is
not evidence of publisher provenance; review the actual publisher revision
and rights before creating an approved manifest. The manifest SHA256 is an
explicit installation input, separate from the model directory.

The adapter's Apache-2.0 code-license field identifies Transformers. AudioCraft's
MIT code license is separate from its CC-BY-NC-4.0 model weights. This private
noncommercial profile does not claim commercial rights to those weights or
outputs. Primary references: [Transformers MusicGen interface](https://huggingface.co/docs/transformers/model_doc/musicgen),
[AudioCraft MusicGen documentation](https://github.com/facebookresearch/audiocraft/blob/896ec7c47f5e5d1e5aa1e4b260c4405328bf009d/docs/MUSICGEN.md),
[AudioCraft weights license](https://github.com/facebookresearch/audiocraft/blob/896ec7c47f5e5d1e5aa1e4b260c4405328bf009d/LICENSE_weights),
and [FluidSynth rendering API](https://www.fluidsynth.org/api/group__audio__rendering.html).

## Private Windows FFmpeg runtime

The reviewed Windows x64 runtime is Gyan's **FFmpeg 9.1 full build**, ZIP size
264,268,459 bytes and SHA256
`f9a83be8f97b8989a223a15e4af234accd0842bacf7c126af5a3aae759785e48`.
The full build contains Flite; the essentials variant is not substituted.
All 45 original files are pinned in `packaging/audio-runtime/source-lock.json`;
the installed verifier binds that lock's own SHA256 and checks every payload
byte. All publisher notices and documentation remain included.

Explicit setup from the product directory:

```powershell
python packaging/prepare_windows_audio.py --archive C:\Reviewed\ffmpeg-9.1-full_build.zip --output C:\PrivateBuild\audio-runtime
```

Alternatively, `--download` instead of `--archive` opts into retrieval of that
one exact public ZIP. The application never invokes this setup implicitly.
The preparer verifies archive size/hash before extraction and rejects traversal,
links, device paths, ambiguous case, encrypted entries and excessive expansion.
Preparation on Windows additionally starts the exact pinned executables to
record their version. Other platforms report Windows execution as `NOT_RUN`.

Pass the prepared directory to `packaging/build_windows_bundle.py --audio-dir`.
`activate_bundled_audio_runtime()` discovers an enclosing installation only
through `sys.prefix`/`sys.base_prefix`, validates both bundle and audio inventory,
and activates the verified bin directory on Windows. It does not inspect CWD
or an environment override. The returned handle supports `close()` and nested
lifetimes. `resolve_active_audio_tool()` chooses the verified executable
directly and rechecks its bytes before invocation. Missing or modified bundled
files cannot fall back silently to another executable on PATH. Development
without a bundle can use reviewed system-installed tools.

This helper prepares private local installations. It does not certify public
redistribution of the full GPL build and all compiled dependencies. No runtime
binary, restricted weights, voice, SoundFont or recording is committed to Git
or uploaded by CI. CI preserves JSON/text/JUnit evidence only.

Publisher references: [official FFmpeg download page](https://ffmpeg.org/download.html),
[Gyan build information](https://www.gyan.dev/ffmpeg/builds/),
[exact release metadata](https://api.github.com/repos/GyanD/codexffmpeg/releases/tags/9.1),
and [reviewed FFmpeg source revision](https://github.com/FFmpeg/FFmpeg/tree/74e4393595cf2b5857f0da925c4e0dd74ea306e5).

## Actual verification evidence

`tests/audio/test_render.py`, `test_effects.py` and `test_music.py` exercise
actual FFmpeg rendering, left/right placement, center behavior, preserved stereo,
speech ducking, EQ/gating/compression/echo tails, frame counts, real peak/loudness,
chapter names, cancellation and no-overwrite publication. Model fixtures validate
the optional adapter's safety contract without masquerading as model inference.
`tests/packaging/test_windows_audio.py` uses inert synthetic PE fixtures to check
archive rejection, publisher/outer manifest binding, tamper rejection, handle
lifetime, direct executable resolution and builder/installer integration.

A real local FluidSynth 2.4.4 smoke rendered four seconds (96,000 frames/channel
at 24 kHz, stereo) using the installed TimGM6mb SoundFont, measured peak
−27.7 dBFS. The SoundFont was identified as GPL-2.0-only from its installed
copyright notice and was not copied into the product. This confirms that local
Linux combination; Windows FluidSynth and subjective musical quality remain
separate checks.

The opt-in `tests/audio/run_long_assembly.py` generated a ten-second procedural
instrumental and explicitly repeated it into 8 kHz mono FLAC. The full outputs
were decoded for peak measurement. The committed report
`docs/evidence/audio_streaming_20261126.json` records actual runtime hashes,
output hashes and these measurements:

| Duration | Exact frames | Output bytes | Wall seconds | Peak process-tree RSS |
| --- | ---: | ---: | ---: | ---: |
| 1 hour | 28,800,000 | 15,543,070 | 3.800 | 62,533,632 bytes |
| 3 hours | 86,400,000 | 46,701,454 | 12.393 | 64,782,336 bytes |
| 10 hours | 288,000,000 | 155,810,174 | 47.361 | 67,076,096 bytes |

All three measured −14.2 dBFS sample peak. The report also records cancellation
of an actual long worker with no partial published output and an unchanged
source chunk. The experiment validates streaming assembly and bounded memory;
it does not establish full-length original neural generation, seamless musical
continuity, human listening quality, or native Windows/NVDA acceptance. Durable
project cancellation/restart and per-scene cache recovery are tested separately
by the director suite. Generated long-test audio was removed after measurement;
only reproducible source and synthetic evidence remain in the repository.

Run the bounded standard checks with:

```text
python scripts/ci_check.py tests/audio/test_render.py tests/audio/test_effects.py tests/audio/test_music.py tests/packaging/test_windows_audio.py
```

Run the opt-in long proof with a new report filename:

```text
python tests/audio/run_long_assembly.py --report /chosen/new-report.json
```

Install `psutil` in the test environment to collect measured process-tree RSS.
Without it the report explicitly records no memory measurement. Do not compare
the small repeated-fixture assembly speed with neural music generation speed.
