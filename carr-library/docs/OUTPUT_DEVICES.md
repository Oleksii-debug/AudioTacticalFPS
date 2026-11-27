# Explicit playback output selection

## Reader and Audio Studio

Choose **Playback output...** in the source or editable live reader, or the
playback-output control in Audio Studio. Review the installed Windows output
devices, choose one, and confirm. **System default output** is an explicit
choice in the same dialog. Escape or Cancel keeps the previous choice.

Reviewing or changing an output does not start reading or synthesize speech.
Start playback with the reader or Studio's existing playback command. The
selected device belongs to that open reader or Studio window for this session;
it is not saved as a machine-wide preference or restored on another computer.

The live reader accepts a change only when its previous operation and audio
owner are fully stopped. Audio Studio first stops its current preview and
requires confirmed closure. A change cannot replace a device whose player
still owns an unclosed native handle. A closed or replaced window cannot apply
a late choice from an already-open native dialog.

## Device identity and failure behavior

The adapter uses the installed Windows WinMM endpoint identity and reported
capabilities. Display names and numeric list positions alone do not authorize a
device. A device can move to a different numeric position before playback if
its endpoint and capabilities still match. The player verifies the current
endpoint immediately before issuing its existing MCI playback command and
requires the native device-index acknowledgement.

An explicitly selected device that disappears, changes identity or becomes
ambiguous is unavailable. The user must review the output selection again.
The application does not substitute another physical output automatically.
The system-default choice continues to use the existing system-default
transport. Driver routing and changes made by Windows itself remain outside
the application's ownership.

Discovery is bounded to 256 outputs and 8192 bytes per endpoint identity.
Opaque endpoint identifiers remain session data and are not included in
normal progress messages or the native probe's public evidence.

## Scope

This control selects the output for the existing PCM playback owner. It does
not configure microphone inputs, Windows' global default device, per-channel
routing, ASIO, virtual mixers or hardware monitoring. Explicit endpoint
selection is supported on Windows; the existing default playback path retains
its platform behavior elsewhere.

## Verification

The source suites exercise fixed-width WinMM buffers, endpoint identity,
renumbering, stale or ambiguous devices, confirmation cancellation, changed
native owners, exact MCI command order, acknowledgement and retained ownership
when closure fails:

- `tests/audio/test_output_devices.py`
- `tests/speech/test_output_device_playback.py`
- `tests/reader/test_live_reading.py`
- `tests/ui/test_output_devices.py`
- `tests/ui/test_live_reading_controls.py`
- `tests/ui/test_studio_playback_actions.py`

`scripts/windows_output_devices_smoke.py` verifies the actual installed module
bytes, reads the real WinMM inventory and drives actual wx controls with native
keyboard events. Its chooser scenarios use labelled synthetic endpoints so
selection, cancellation and owner destruction remain reproducible on a runner
without audio hardware. If a real endpoint is present, a short silent PCM also
exercises the real selected MCI transport. Without one, that transport result
is explicitly NOT_RUN. Physical routing, listening quality and human NVDA are
separate acceptance records. Consult the exact commit's CI evidence; a source
review or a predecessor's passing result does not qualify another build.
