# Native SAPI protocol and existing Windows COM client

The fixed request/reply worker adapts the application's canonical native SAPI
protocol from commit e3cf74c243d4ef930b526b5648ca9003c0900057:
speech/sapi_native_worker.py blob 0faf8b266ff9fa5a0dc5db61243e5d8e5ce99135.
It keeps the current audio.engines API and existing owned-process supervisor.

The canonical implementation used comtypes. This adaptation uses the already
admitted **pywin32 312** dependency; no comtypes code or unused comtypes licence
is copied. No new dependency resolution or additional runtime pin is claimed.
The current integrated Windows x64 CPython 3.11 lock admits the pywin32 wheel
with SHA256 d11417d84412f859b722fad0841b3614459ed0047f7542d8362e77884f6b6e8a.
Its reviewed source is
https://github.com/mhammond/pywin32/tree/2a277cb5552756c2b4d42b524dc36d25e0bb6354.
Existing packaging/wheel-notices/pywin32-312-COM-LICENSE.txt and
pywin32-312-WIN32-LICENSE.txt retain its upstream licence notices. The wheel's
own licence metadata and files remain part of package admission.

Only SAPI.SpVoice, SAPI.SpFileStream and SAPI.SpAudioFormat are constructed,
using CoCreateInstance with CLSCTX_INPROC_SERVER and dynamic dispatch.
No running-object attachment, generated typelib, shell, PowerShell startup,
filename-speaking flag or implicit XML parsing is used. The actual installed
SAPI voice DLLs remain separately trusted local software. The worker protocol
is not a sandbox for malicious native libraries or an acoustic-quality claim.

## Saved voice identity

Microsoft's System.Speech source at tag v9.0.0 establishes that VoiceInfo.Id is
ObjectToken.Name, which RegistryDataKey derives from the registry path leaf:

- https://github.com/dotnet/runtime/blob/v9.0.0/src/libraries/System.Speech/src/Synthesis/VoiceInfo.cs
  blob 10f432b90429c0899f38f5eaf02865b7c5aec2d7.
- https://github.com/dotnet/runtime/blob/v9.0.0/src/libraries/System.Speech/src/Internal/ObjectToken/RegistryDataKey.cs
  blob 1acdb786aaca1f9952e1516e3ba962252086702a.
- https://github.com/dotnet/runtime/blob/v9.0.0/src/libraries/System.Speech/src/Internal/ObjectToken/ObjectToken.cs
  blob fea500a3041301850f63134cfc0db9bc815b5440.

The current sapi5:<SHA256(leaf)[:24]> identity is retained. The full COM token,
name and leaf are additionally bound and rechecked for each render. Duplicate
leaves are rejected rather than silently selecting an alternate token.
Changing the host adds an explicit native-worker/client identity to new cache
keys; an old output is never relabelled as a newly qualified native render.

Literal text uses SVSFIsNotXML=16. Explicit punctuation uses
SVSFNLPSpeakPunc=64:
https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ms720892(v=vs.85)

Discovery uses the application's real interpreter architecture. An unrelated
32-bit voice registry is not claimed. SAPI5Engine remains an explicit legacy
PowerShell compatibility API; include_powershell=True additionally discovers
the existing sapi5-32 host. Unsupported SAPI4 remains explicitly unavailable.
No automatic fallback substitutes a different voice or engine.
