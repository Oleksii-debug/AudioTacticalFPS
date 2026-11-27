# Explicit Windows playback output

The Voice reader's **Playback navigation** dialog can select a Windows output
for the current reader session. Open the dialog, choose **Reload available
outputs**, select an enumerated output, and activate **Stop audio and use
selected output**. The action stops the existing reader operation, clears its
repeat/sleep controls and queued seeks, and changes the route for the next
explicit Play or Read. Selecting a row or reloading the list does not play,
stop or restart audio.

The default entry preserves the current player's automatic routing. It is
described as the playback backend's automatic output; it is not a promise about
which Windows communications or multimedia default the driver will select.
The choice is deliberately session-only. Backups and reopened readers do not
silently restore authority to hardware whose identity may have changed.

## One existing player

`speech/playback_devices.py` enumerates the installed Windows `winmm` waveOut
devices. It loads that DLL from System32 only, calls the Unicode capabilities
API and asks the driver for its opaque device interface identity. Those strings
are compared as identities; they are never opened as paths. Two complete,
bounded inventories must agree. Native errors, duplicate interfaces, malformed
UTF-16, excessive counts and changing inventories are visible failures.

The existing `WavePlayer` remains the only transport. A selected device is
resolved again by its exact interface and capabilities at playback admission.
An index may move, but a similarly named replacement is not substituted. After
opening its owned MCI waveaudio handle, the player sets the selected output and
requires the reported output number to agree before Play. A final inventory
check rejects an index change during that admission. Any failure closes the
owned handle and does not play through an automatic fallback.

An output without a stable interface cannot be explicitly selected. If a
selected output disappears, reloading shows no selected replacement. The user
can explicitly choose another output or the automatic entry. Other platforms
retain their existing automatic backend and report Windows-specific selection
as unavailable.

The controls use native widgets inside a scrolling panel, with Close outside
the scrolling area. Their names explain the explicit Stop/apply action. Reader
destruction compares the captured native window ID, so a detached MSW Python
wrapper cannot leave the navigation timer or its callbacks active.

## Evidence and remaining qualification

Portable source verification currently passes **72 cases with zero failures or
skips** across `test_playback_devices.py`, `test_playback_navigation.py` and
`test_playback_reader_hook.py`. The new tests exercise the actual ctypes memory
layout with controlled API callbacks, routing command order, fail-closed
admission, native-handler dispatch, queued-operation revocation and detached
owner destruction. The WAV fixtures contain synthetic PCM.

These tests do **not** enumerate a physical Windows sound card, prove acoustic
output, validate arbitrary drivers, or constitute an NVDA review. Actual
installed-Windows enumeration, selected-device playback, unplug/replug behavior
and human accessibility/listening are separate qualification gates. No new
audio engine, native binary, device driver, network service or persistent
hardware authorization is installed by this feature.

The existing installed native navigation probe now has explicit output-control
phases using two declared synthetic interface fixtures, with real keyboard
selection, Stop/apply, disappearance, no fallback and explicit automatic routing.
It separately calls the actual Windows inventory API and, only if the runner
has an identified output, plays synthetic silence through the real selected
MCI route and stops it. A runner without such hardware records routing NOT_RUN.
This extension has parsed and returned the expected non-Windows exit 77 locally;
its actual Windows execution is still pending. Synthetic UI routing and a
conditional real native route remain distinct fields in its evidence.

## Reviewed primary contracts

- [Microsoft: waveOutGetDevCapsW](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutgetdevcapsw)
- [Microsoft: WAVEOUTCAPSW](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/ns-mmeapi-waveoutcapsw)
- [Microsoft: waveOutMessage](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutmessage)
- [Microsoft: obtaining a device interface name](https://learn.microsoft.com/en-us/windows-hardware/drivers/audio/obtaining-a-device-interface-name)
- [Microsoft: MCI set command](https://learn.microsoft.com/en-us/windows/win32/multimedia/set)
- [Reviewed Microsoft SDK constants](https://github.com/microsoft/win32metadata/blob/b07213e28bcc48221c155024f9c5e0e1a92c6497/generation/WinSDK/RecompiledIdlHeaders/um/mmddk.h)
- [wxPython: native scrolling controls](https://docs.wxpython.org/wx.Scrolled.html)

The SDK header revision pins the two interface-query constants; it does not
claim that a Windows runtime or driver at that revision was tested here.
