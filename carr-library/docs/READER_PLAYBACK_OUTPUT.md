# Reader playback output selection

The reader's **Playback controls** now offers **Reload available outputs** and
**Stop audio and use selected output**. These controls operate on its existing
`WavePlayer`; they create no speech engine, playback worker, or second audio
transport. Opening the controls, listing outputs, and changing the list selection
do not play audio. Applying a choice stops the reader and clears pending repeat
and sleep. The next explicit Play or Read uses that choice.

The initial choice remains **Automatic output chosen by the playback backend**.
Windows uses the existing MCI waveform player; other platforms keep their
existing ffplay route. Explicit enumeration is available on Windows only.
The output choice lasts for this reader session. It is not stored in a reading
project, library backup, or machine-independent preference file.

## Reuse and native contract

This component reuses `speech/playback_devices.py` and its ABI tests from prepared
revision `7635ddadb63025002e6f88def62d4562e91d8c31`. Only the output controls and
MCI routing were adapted; its older reader/navigation/player implementations
were not substituted for the current source guards, A–B, sleep, quiet-mode
admission, and confirmed-close behavior.

The helper uses the installed Windows `winmm.dll`, loaded from System32. There
is no new dependency or redistributed driver. Its constants were checked
against Microsoft's SDK header at
[`b07213e28bcc48221c155024f9c5e0e1a92c6497`](https://github.com/microsoft/win32metadata/blob/b07213e28bcc48221c155024f9c5e0e1a92c6497/generation/WinSDK/RecompiledIdlHeaders/um/mmddk.h).

The implementation follows these primary contracts:

- [Device interface names](https://learn.microsoft.com/en-us/windows-hardware/drivers/audio/obtaining-a-device-interface-name)
  are opaque identities. The two interface messages are handled by Windows.
- [`waveOutMessage`](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutmessage)
  takes the device ordinal cast to `HWAVEOUT` for these queries. Interface size
  is an unsigned 32-bit byte count including UTF-16 termination.
- [`waveOutGetDevCaps`](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutgetdevcaps)
  supplies display names and supplemental device capabilities.
- [MCI `set`](https://learn.microsoft.com/en-us/windows/win32/multimedia/set)
  selects an output; [MCI `status`](https://learn.microsoft.com/en-us/windows/win32/multimedia/status)
  confirms it. A status error is a refusal for an explicit route, never proof
  that a default device is acceptable.
- [`wx.Choice`](https://docs.wxpython.org/wx.Choice.html) exposes a standard
  keyboard selection. A separate named button applies the selected choice.

## Identity and lifetime

Two complete inventories must agree. Enumeration admits at most 128 devices
and 64 KiB per interface name. Unknown, duplicate, changed, malformed or partial
results fail visibly. Cancellation and a two-second monotonic budget are checked
between native calls and before returning a result. These synchronous Windows
queries have no cancellation handle: the budget does not promise to interrupt a
blocked OS call. There is no retry loop and no discovery on dialog opening.

An explicitly selected device is identified by its opaque interface and checked
capabilities, not its display name or list position. The next explicit Play can
rebind that same identity to a changed ordinal. Before audio starts, MCI must
accept the selected output and report its exact ordinal, followed by another
identity check. A paused alias is also checked before Resume. If its ordinal
changed, the alias is closed and a new explicit Play is required. Device loss,
an unconfirmed route, or changed identity never selects automatic output.

Stop invalidates pending reader audio before route changes. An unsuccessful
native Close preserves the alias, old selected identity and cleanup authority
for a later retry. Closing the controls only removes their status subscription;
the reader retains ownership of its player. Late button or queued callbacks do
not access destroyed controls or restart audio.

The checks govern the Windows routing request and reported device identity.
They do not certify a physical speaker, acoustic quality, or hot-plug behavior
inside an already-running Windows driver.

## Qualification

The portable selection includes the existing player transport, A–B/sleep and
reader-action suites, the adopted real-ABI buffer tests, and new route-loss,
resume, cancellation, deadline, duplicate-name and closed-dialog regressions.
Native API boundaries are explicit fixtures in those tests; PCM files and
production player/UI handlers are real.

The installed `scripts/windows_reader_player_smoke.py` retains its measured MCI,
pause/seek/repeat/sleep/file-release checks. It additionally enumerates the
actual Windows inventory and, when an identifiable output exists, uses real
native keyboard selection and the same MCI player to route synthetic silence.
It records loaded-module hashes and a separate `selected_mci_output` outcome.
No-device runners report `NOT_RUN_NO_IDENTIFIED_OUTPUT` for that outcome.
Run with `--require-output-device` for a strict selected-output qualification
gate: lack of an identifiable output then fails rather than passing that gate.

Physical listening quality and manual NVDA acceptance require separate tests.
The Linux source run does not establish either those outcomes or actual Windows
output-routing qualification.
