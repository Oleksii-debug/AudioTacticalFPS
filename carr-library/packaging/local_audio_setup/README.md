# Optional owner-local audio setup

These two thin launchers call the existing reviewed `prepare_windows_audio.py`
and `windows-audio.lock.json`. They contain no executable runtime payload and
do not change the audio-transfer gate. A delivery archive must omit all
`audio/` and `audio-runtime/` payloads and accurately declare audio absent.

## Package layout

Copy the CMD launchers to the extracted program root alongside `Install.cmd`.
Copy the unchanged reviewed helper and lock to `setup/` in that root. Include
all four source files in the package manifest. Do not include FFmpeg binaries
under another name or inside another archive.

The application installation must finish first. Neither normal installation nor
normal application startup runs these optional setup commands.

## Explicit setup choices

- `SetUpLocalAudio-Download.cmd` explicitly acquires the pinned publisher ZIP
  using the helper's `--download` option.
- `SetUpLocalAudio-Offline.cmd` makes no network request. First provide the
  exact `ffmpeg-9.1-essentials_build.zip` in
  `%LOCALAPPDATA%\Resonance\runtime-cache\ffmpeg-9.1`.

Both modes prepare a **new** external directory:
`%LOCALAPPDATA%\Resonance\runtimes\ffmpeg-9.1`.
The helper verifies the whole archive, selected member sizes/hashes and AMD64 PE
headers, retains LICENSE/README/provenance, and never executes the binaries.
The CMDs preserve the helper's nonzero exit status across their readable pause.

An existing destination is left unchanged and is **not** declared verified by
the launcher. A previously successful setup need not be repeated. To use a
different new destination, call the same helper with explicit `--cache`,
`--output` and optional `--download` as documented by its command help.

After successful preparation, open the desired library and select **Audio
Studio → Local voice and music engines…**. Use **Choose FFmpeg executable** for
`bin\ffmpeg.exe`, and **Choose FFprobe executable** for `bin\ffprobe.exe`
inside the prepared external directory. Close and reopen Audio Studio.
Settings belong to the selected library; no administrator privileges, system
PATH edits, sealed-program changes or implicit runtime downloads are required.

## Evidence boundary

The existing helper and lock are unchanged. Source review of these CMD wrappers
does not claim native Windows/NVDA execution of the wrappers or a signed
installer. Native audio results for the same pinned runtime remain separate from
package transfer policy. This workflow does not establish public or third-party
redistribution clearance for the runtime.
