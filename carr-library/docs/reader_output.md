# Reader output device selection

The voice reader can explicitly select one Windows waveform output for the
current reader window. It uses the existing WavePlayer and MCI transport.
Changing this selection does not change the Windows default device, mixer
volume, the speech engine's voice, NVDA, another reader, or progress speech.

## Accessible workflow

1. Open the voice reader and activate **Refresh outputs**. Discovery reads
   WinMM capabilities and interface identities; it does not play sound.
2. Focus **Reader output device**, choose a device with the arrow keys, then
   activate **Use selected output**. Selecting an item alone does not apply it.
3. Read normally, or use the reader's Pause/resume action to continue retained
   audio. Its standard binding is F7; configured reader bindings remain valid.
   Stop is F8 by default. Every new playback admission still uses the shared
   quiet policy.

Applying a different output stops the old native route and retains an active
or paused recording's position in a paused state. It clears A/B repeat and the
sleep timer and invalidates queued automatic restarts. It never briefly opens
or plays the new output. Resume/read is a separate explicit action.

**System default output** is an explicit choice. The initial reader behavior
continues to use that mapper until the user applies a different device.
Refresh never silently selects the first discovered device. If a previously
selected device is missing, the chooser requires a fresh explicit selection.

The choice belongs only to this reader session. Reading-project and recovery
formats are unchanged; restoring or reopening a reading draft does not retain
device authority or start audio.

On a non-Windows ffplay backend these controls are disabled with an explanation.
The existing default playback remains available. This implementation does not
claim cross-platform device selection, live input monitoring or a hardware
compatibility matrix.

## Identity and lifecycle

Friendly names are presentation only. The selection retains the exact opaque
WinMM/WDM device-interface identity. Full inventory and reordering validation
run on the reader's existing background worker. Each MCI open rechecks the
prepared numeric index, applies it to the private MCI alias, verifies the
reported output index and rechecks the native interface before playback.
A retained paused native route is checked again before resume.

Device removal, replacement, an index that changed after preparation, an
ambiguous interface, a native query failure, or rejection of the selected
format stops the operation with an actionable error. There is no automatic
fallback to another named output or to System default. Refresh and Apply
explicitly resolve an existing identity after a reorder.

Inventory is bounded to 64 devices and a 64 KiB interface buffer per device.
A device without a verifiable interface cannot be explicitly selected through
this flow. Default mapper playback retains its original capability boundary.
No endpoint installation, driver change, network call or system-default setter
is introduced.

Close, cancellation, changed source/library and a new playback session invalidate
pending chooser work. Only the current UI completion can apply a verified
selection. The controller reuses the existing single reader executor and adds
no timer, job store or alternate audio backend.

## Qualification

Portable source coverage is in
[output identity and transport tests](../tests/speech/test_output_devices.py)
and [native-controller lifecycle models](../tests/ui/test_reader_output.py).
These tests use actual PCM files with controlled WinMM/MCI protocols; they do
not establish Windows device or human listening qualification. The existing
A/B/sleep suite remains part of the same regression gate.

[The native output probe](../scripts/windows_reader_output_smoke.py) requires
actual Windows, wx controls and an explicitly supplied available WinMM output
index. It uses real keyboard events in the actual reader, real MCI playback of
independently generated PCM, and actual MCI output-status queries. It checks
selection, pause, explicit default and selected-route transitions, seek, Stop,
source preservation, saving and cleanup.

From the package directory, the source lane provides its source PYTHONPATH:

~~~text
python scripts/windows_reader_output_smoke.py --device-index 0 --report output-source.json
~~~

The isolated installed lane uses the exact installed application:

~~~text
python -I -B scripts/windows_reader_output_smoke.py --device-index 0 --report output-installed.json --expect-installed
~~~

The index in these examples is a caller choice for a previously inspected
qualification machine, not a default device discovery policy. A missing
selected output is a failed probe, not a skip. The probe writes an initial
FAILED receipt and has an independent 110-second watchdog, including cleanup.
It neither installs an endpoint nor changes the system's default routing.

At implementation checkpoint the portable gate passed; actual Windows/native
installed execution is **NOT_RUN until the corresponding CI receipt exists**.
Physical multi-device acceptance, NVDA and human listening remain **NOT_RUN**.
A successful synthetic one-device probe does not qualify every physical output.

## Primary API references

- Microsoft [waveaudio set command](https://learn.microsoft.com/en-us/windows/win32/multimedia/set)
  and [status command](https://learn.microsoft.com/en-us/windows/win32/multimedia/status).
- Microsoft [obtaining a device interface name](https://learn.microsoft.com/en-us/windows-hardware/drivers/audio/obtaining-a-device-interface-name).
- Microsoft [waveOutMessage](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutmessage)
  and [DRV_QUERYDEVICEINTERFACESIZE](https://learn.microsoft.com/en-us/windows-hardware/drivers/audio/drv-querydeviceinterfacesize).
- Microsoft WinSDK [mmddk.h](https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/um/mmddk.h)
  supplies the two query constants. Existing MCI string routing avoids new
  packed MCI command structures.
