# Local prepared-track FFmpeg compatibility probe

Scope: synthetic-only independent V3-13/14 compatibility probe. Reuse FFmpeg filters, not a custom DSP implementation. Input WAV fixtures at 48 kHz mono PCM16; typed track start, gain and pan; sidechain compression, stereo mix, limiter, exact timeline, staged no-clobber publication. No network URLs, shell or external APIs; only trusted local fixture directory and explicit FFmpeg binary. The WAV input protocol is restricted to file; OS-level egress denial is not established by this test. Not a hostile-input sandbox: path check/open races, Windows reparse points, bounded subprocess output/cancellation, media parser isolation and production rights still require review. The 60s/8-track limits belong only to this narrow probe, not product requirements.

Actual local execution Linux CPython 3.13.5, installed FFmpeg 7.1.5-0+deb13u1 (Debian GPL-enabled build). No FFmpeg binary/source bundled or redistributed. Candidate upstream license and pinned source are in PR94; this installed Debian build is not asserted to equal that upstream SHA. Commands from this folder:

    python -S -m unittest -v

5 PASS, 0 FAIL, 0 SKIP, exit 0, 0.871s. The first run also passed but emitted unrelated Python site-startup warmup noise; the isolated -S rerun had none. Tested Git blobs: mix_probe.py 594090fa169ef64dc1286ee876af266b8846343a; test_mix_probe.py d11ef4054e3265adbb1d355ccc0d2315f7f242d6.

Measured actual 4-second stereo WAV: 192000 frames per channel, left 440Hz amplitude 0.449999 / right effectively zero; right 660Hz 0.450000 / left 0.000000678; centered 880Hz symmetric; background 220Hz amplitude 0.106066 before voice and 0.031129 during (ducking); peak 0.502335 full-scale. Inputs unchanged; invalid parameters, traversal, linked inputs and output overwrite rejected. These are synthetic sine tones, not actual human speech, music or neural output.

NOT_RUN: MusicGen, ACE-Step, actual TTS, local agent orchestration, long-form generation/assembly, native Windows, NVDA, perceptual quality. Next: approved typed scene contract, durable multi-hour chunk/scene rendering with checksums, tested local music provider, actual Windows audio path. Do not merge as production or weaken current academic acceptance. Related PR94 and issue #1 claim 7204181949.
