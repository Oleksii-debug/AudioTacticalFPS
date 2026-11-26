# Local speech and audio engine provenance

## Production adapters

`audio/voices.py` uses the installed Windows .NET `System.Speech` SAPI5 host.
It sends plain text as JSON to a fixed program; text cannot become PowerShell
code or SSML. Voice IDs are checked both before and after selection. Voices,
their licenses and native engines remain the user's separately installed
components. This adapter does not install or redistribute them. The Windows
smoke generates a short synthetic sentence through the actual installed voice;
provider-double unit tests are identified separately.

`audio/studio.py` requires an explicitly selected, trusted local FFmpeg binary.
It hashes that exact executable into scene cache identities and supplies only
typed, bounded fixed audio filters. The application neither downloads FFmpeg
nor bundles it in the production wheel. Only local file inputs are enabled.
The user's binary and build options determine the available filter support and
license. Existing input audio is retained unchanged.

Primary interface references:

- https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer.getinstalledvoices
- https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer.setoutputtowavefile
- https://ffmpeg.org/ffmpeg-filters.html
- https://ffmpeg.org/legal.html

## Reproducible Windows CI engine

The dedicated audio workflow uses a **test-only** FFmpeg carrier wheel, outside
the production dependency lock and installer. No wheel, executable, voice,
recording or other runtime asset is uploaded as a CI artifact.

- Carrier: `imageio-ffmpeg` 0.8.1, Windows x64 wheel.
- Publishing source: `26277bb102b64c42d939c3a550301edbe2c81921`.
- Official release and provenance: https://pypi.org/project/imageio-ffmpeg/0.8.1/
- Wheel: `imageio_ffmpeg-0.8.1-py3-none-win_amd64.whl`.
- Wheel SHA256: `f27ca4cb85e2d4946deedbce1a81245dfd942a2bcf94b53676fb3956aa95bf1b`.
- Executable: `imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v9.0.2.exe`.
- Executable SHA256: `3256173f3f8bffd7df12227c68adf68025edb1832273a9530688a7bb1ed8edec`.
- Carrier code license: BSD-2-Clause; this does not relicense the FFmpeg binary.
  The workflow retains the exact executable's `-version` and `-L` output as
  build/license evidence. No binary redistribution approval is implied.

Both hashes were independently calculated from the downloaded wheel before
integration. CI rejects a different wheel or executable. It blocks outbound
traffic for the test Python runtime, its base executable, the SAPI PowerShell
host and the selected FFmpeg executable before exercising audio. This proves
the tested operations within that boundary, not a whole-machine egress policy
or the behavior of every third-party installed voice.

## Acceptance boundaries

Synthetic tones, noise and silence measure routing, ducking, exact duration,
safe publication and long-file assembly. They are not generated speech or music.
The hour-long synthetic assembly is not an hour of original neural generation.
SAPI4, dordiai-voices, MusicGen, ACE-Step, audio device playback and human/NVDA
listening acceptance need their own implementations or native evidence. They
must not inherit PASS from these checks.
