# Optional private Windows FFmpeg runtime

The integrated `audio.production` pipeline uses one installed local FFmpeg
executable. `audio.runtime.resolve_ffmpeg(explicit=None)` chooses, in order, an
explicit absolute path, `CARR_FFMPEG`, a verified private Windows bundle at
`Path(sys.prefix).parent / "audio-runtime"`, or an executable from an absolute
PATH entry. It returns `None` when no runtime is installed. An invalid explicit
selection or incomplete/tampered bundle fails without choosing another binary.
Discovery never executes FFmpeg, downloads a package, or searches the working
directory. The desktop and CLI use the same resolver.

## Reviewed optional binary

FFmpeg's [official download page](https://ffmpeg.org/download.html) links the
[Gyan Windows builds](https://www.gyan.dev/ffmpeg/builds/). This optional private
package uses the release archive below, rather than a moving `latest` URL.
The exact archive, executable, original license and original build README were
downloaded and SHA256 verified during preparation on Linux. That establishes
the bytes, not native Windows compatibility or listening quality.

| Item | Exact identity |
| --- | --- |
| Archive | [ffmpeg-9.0.2-essentials_build.zip](https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip) |
| Archive size | 114,768,076 bytes |
| Archive SHA256 | `60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba` |
| FFmpeg source | [`946fcce07b6dcd0331c8cc609192aeff5e1924f8`](https://github.com/FFmpeg/FFmpeg/tree/946fcce07b6dcd0331c8cc609192aeff5e1924f8) |
| `ffmpeg.exe` SHA256 | `3256173f3f8bffd7df12227c68adf68025edb1832273a9530688a7bb1ed8edec` |
| Original license SHA256 | `8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903` |
| Original build README SHA256 | `0342de6bb39dd421cbec2c00d89ad274b640d75ad3f2cab09ffd647ce2d8f949` |
| Build license | GPL v3, as documented by this static build; Python wrapper licenses do not replace the binary's license |

The upstream [imageio-ffmpeg source manifest](https://github.com/imageio/imageio-ffmpeg/blob/26277bb102b64c42d939c3a550301edbe2c81921/tools/ffmpeg-sources.json)
independently pins the same executable. No imageio dependency is required.
The executable, voice/model assets and generated private audio are not committed
or published by this source change. Keep the original license/build notices and
source provenance with a private package; this is not a redistribution waiver.

## Prepare from an already acquired archive

Run from the source checkout on Windows, supplying the reviewed existing ZIP:

```powershell
python packaging/prepare_ffmpeg_runtime.py --archive C:\Setup\ffmpeg-9.0.2-essentials_build.zip --destination C:\CARR\audio-runtime --verify-native
```

The preparer verifies the complete archive hash and size, then streams only
three exact allowlisted members into a new local directory: `ffmpeg.exe`,
`FFmpeg-LICENSE.txt`, and `FFmpeg-README.txt`. It verifies each size/hash and
writes `ffmpeg-runtime.json` last. Arbitrary ZIP paths are never extracted.
Existing complete matching bundles can be checked again; existing unrelated or
incomplete contents are not silently overwritten. Linux preparation is allowed
without `--verify-native`, with `native_validation.status = NOT_RUN`.

`--verify-native` requires Windows. It runs the selected executable's version,
filter and license probes and requires the filters used by the integrated
studio, including procedural synthesis, ducking, loudness analysis and the
typed DSP effects. It does not prove all codecs, native SAPI, microphone/device
behavior, NVDA navigation, musical continuity or human listening acceptance;
those remain separately measured tests. The default library installation does
not silently add this optional runtime.

## Validation of this integration

The PR99 production foundation (`7cf3ee625b0f30342ae23470f2248a0d1de32e40`)
was independently run on Linux with Python 3.12.14 and FFmpeg 7.1.5:
`tests/audio` gave 87 passes, one native Windows skip and seven subtest passes.
The additive discovery/preparation tests cover explicit/environment priority,
missing or altered bundles, refusing relative/working-directory discovery, and
rejecting an unreviewed archive before destination creation. Native verification
of the pinned Windows executable must run on Windows; it was not simulated.

The existing `scripts/long_audio_benchmark.py` and recorded RF64 evidence belong
to this integrated production implementation. Separate experimental renderer
measurements must not be reported as execution of this pipeline. Both methods
use synthetic or repeated fixtures for long-file validation, not ten hours of
original neural music.

## Cross-platform audio boundaries

Prepared assets and local runtime paths reject Windows device names, alternate
streams, UNC shares, trailing-dot/space aliases and invalid filename characters
on every platform. This keeps a project created on another OS portable without
reinterpreting its assets on Windows. Ordinary Unicode filenames remain valid.
Imported production JSON rejects duplicate fields and nonfinite numbers.

The production and music checkpoint readers share one bounded PCM16 RIFF/RF64
validator. It verifies physical file length against the container declaration,
RF64 sentinel/size table and declared sample count, rate/channel/alignment and
data boundaries before accepting a chunk. A compatible small RIFF or RF64 file
does not require allocating its waveform in memory. Existing cancellation,
hash verification and no-overwrite publication remain in effect.

On Windows, file publication flushes generated files through a writable handle;
read-only handles cannot satisfy `FlushFileBuffers`. A flush failure prevents
publication and is reported rather than ignored. The regression reproduces this
handle requirement on POSIX too, while actual Windows execution remains a
separate CI gate.

Independent synthetic-signal tests measure high-pass/low-pass response, gate and
compressor action, music ducking/recovery, LUFS change for a known gain change,
exact PCM length, unchanged input hashes and termination of a running FFmpeg
process. They found that unequal decoder blocks from stereo narration and mono
music can truncate the sidechain output on FFmpeg 7.1.5. Both inputs now use
equal 1,024-sample blocks; the padded tail is trimmed to the exact scene length.
The mix revision participates in final/scene cache identity, so corrected mixes
are rebuilt while verified speech and generated music cues remain reusable.
