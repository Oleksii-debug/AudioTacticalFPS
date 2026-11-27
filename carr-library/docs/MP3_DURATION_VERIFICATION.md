# MP3 delivery rate and complete decode verification

The MP3 export profile uses MPEG-1 at 32, 44.1 or 48 kHz. Source rates at or
below 32 kHz are explicitly resampled to 32 kHz; rates above 32 and at or below
44.1 kHz use 44.1 kHz; higher rates use 48 kHz. Original PCM, narration text,
source documents and saved projects are unchanged. Resampling is not a claim
of increased source quality.

This profile addresses an observed framing difference in the exact sealed
Windows FFmpeg 9.1 runtime. The synthetic diagnostic at source
`5a70f5ea9c04d5bdc488bf896ce99929369558e2`, Windows audio run
[45915596179](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45915596179),
measured both 128 and 192 kbit/s requests at six rates. At 8, 16 and 24 kHz,
the native decoder returned exactly 529 more PCM samples than the source.
Its first packet announced 576 skip samples, compared with 1105 in the Linux
FFmpeg 7.1.5 observation. Changing bitrate did not change that difference.
At 32, 44.1 and 48 kHz the Windows decoded counts exactly matched the source.
Only synthetic generated PCM and count/trim metadata were involved; no audio
artifact was published.

The production verifier still checks the actual container, codec, channels,
sample rate, every decoded packet and stable file identity. It rejects a
truncated, longer, malformed or substituted output before publication. It does
not permit a 529-sample tolerance or use a trimming filter to make a damaged
output look complete. Resampling permits at most one sample of rounding.
The separate existing AAC end-padding rule is unchanged.

`tests/audio/test_encoded_verification.py` additionally performs independent
full decodes for 8, 11.025, 12, 16, 22.05 and 24 kHz input sources and checks
the new delivery rate, complete duration and unchanged original bytes. The
22-case selection passed locally with actual FFmpeg 7.1.5. Acceptance of the
corrected profile on the sealed Windows runtime requires the successor run;
the earlier diagnostic run correctly failed the old export gate.

`scripts/windows_mp3_verification_probe.py` remains a raw codec diagnostic,
separate from production export policy. Its low-rate measurements are not a
claim that those raw encoded files satisfy application export acceptance.
