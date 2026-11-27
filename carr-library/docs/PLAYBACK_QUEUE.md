# Saved playlists and player-owned live controls

The existing `WavePlayer` can review an ordered playlist of explicitly
chosen local PCM WAV files. Review reads bounded blocks on the existing desktop
worker and records each complete file's SHA-256, frame count and local identity.
A playlist contains at most 256 recordings, at most 4 GiB per file and at most
32 GiB in total, with one two-minute cancellable review/copy/hash budget. The selected library's shared resource policy may impose a lower size allowance; the declared reservation does not measure or cap physical RAM. The standalone
owner retains only these explicitly reviewed bytes through the existing Studio
selected-input transaction. The content-addressed private copies live alongside
Studio inputs; no access is granted to neighbouring files.

Selecting, reordering or replacing the playlist never starts audio. Explicit
**Play queue** arms sequential playback. The existing owner timer reports an
ended item; it does not start another item. The native owner admits the next
item through the same quiet-mode and window/source-lifetime checks used for
manual playback. Stop, Pause, replacement, selection and Close revoke a waiting
queue transition. After Pause, choose Play queue explicitly to rearm subsequent
items. Play queue resumes the same paused item; selecting another item begins
that explicitly chosen recording. The sleep deadline applies across the queue. A–B repeat has precedence
over advancing to another recording. A changed, removed or linked next source
stops the transition without selecting a substitute.

The text reader keeps its own document-bound player. Queue controls belong to
standalone **Audio playback**, so a foreign recording cannot replace a reading
copy's saved cursor or approximate highlighting timeline.


## Saving, restart and complete backup

**Ctrl+S** and **Save playback queue** save the ordered relative media identities,
original labels, selected entry, original PCM frame cursor, skip intervals,
volume/mute/balance and explicitly selected output identity. The queue joins the
existing application **Save All** coordinator. The existing owner timer saves a
changed cursor at five-second intervals; validated queue edits save immediately.
Close flushes the cursor before stopping resets native playback position. A
failed write remains visibly unsaved, Save All remains incomplete, and Close
asks explicitly before discarding those unsaved changes. Compare-and-swap
tokens prevent one open queue from silently replacing a concurrent writer. A visible commit whose directory durability confirmation fails keeps its new token and an explicit pending-confirmation flag; a subsequent Save retries the current snapshot even if it equals the old baseline. A competing create is never reported as this window's completed save.

Loading the JSON reads only passive metadata. **Review saved queue without
playing** hashes the confined retained copies on the existing worker, checks
their exact PCM dimensions and saved identities, then selects the original
cursor silently. It revalidates a saved output interface rather than selecting
a replacement device by ordinal. Missing output or unsupported live controls
fail visibly. The optional **Review saved queue using current controls and
output instead of saved settings** choice is an explicit alternative; it does
not silently discard a saved mute/output choice. No restored queue automatically
plays, arms sequential playback, resumes A–B repeat or restarts a sleep timer.
Press Play or Play queue explicitly after review.

The typed full-library and standalone-state backup adapters include the saved queue and its retained PCM, check every row against actual copied hashes and PCM dimensions, and reject missing or changed media, reserved aliases, and incoming lock files. It never opens an external path from the saved document. Restore uses
relative managed identities in the new library and still requires explicit
review. Original selected files may be removed after retention; their paths are
not required to restore the playlist. Session-native handles and active timers
are never serialized.

## Volume, mute and balance

Windows live controls use a private waveOut transport inside the same
`WavePlayer`. The first explicit application while MCI is open closes the old
alias with acknowledgement before opening the new output at the measured
position. A paused transition remains paused before any PCM is queued. A stopped
level request opens no device and does not play audio. Changing levels afterward
uses only the owned `HWAVEOUT`; no device-ID or system-wide volume call is made.

Volume is a native 0–100 control, mute retains the requested unmuted level, and
balance attenuates the left or right channel without boosting it. These settings
do not alter the recording or a private listening-speed derivative. Unsupported
volume capability or stereo balance is reported explicitly. The POSIX ffplay
fallback retains its existing playback/seek behavior and does not claim live
mixer support.

The private native transport reuses the reviewed fixed-width WinMM buffer ABI without importing or enabling a microphone monitor. It
queues at most four 50 ms PCM blocks, checks the source identity between bounded
reads, reads the driver's returned position unit, and accepts completion only
after the last queued buffer is returned. The existing owner timer pumps it;
there is no second player thread or scheduler. The driver's device calls are
synchronous and cannot provide a hard cancellation deadline.

Reset, every UnprepareHeader and Close must acknowledge release before native
memory or private listening output can be freed. Unconfirmed cleanup retains
the exact handle/buffers, the player and any prepared listening-copy owner; it blocks another live-output instance until cleanup
succeeds or the application exits. A local playback-file selection remains
subject to the same regular-file/link checks used by the reader.

## Evidence and limits

Portable tests use real PCM files and an explicit pointer-owning WinMM double
to cover source mutation, no autoplay, queue cancellation, sleep/repeat priority,
pause/seek, per-instance volume, and failed-close ownership. The native Windows probe is designed to exercise the actual controls and owned output, including two distinct handles on the same Windows output. This composed source has not yet been executed: local source, installed-package and native results remain NOT_RUN until the next exact-head gates run.
Automated native calls do not establish hardware listening quality or NVDA
qualification.

WinMM contracts used here:
- [waveOutSetVolume: handle versus device-ID scope](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutsetvolume)
- [waveOutGetVolume: supported channel fields and exact requested setting](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutgetvolume)
- [waveOutGetPosition: inspect the actual returned time unit](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutgetposition)
- [waveOutReset: return pending playback buffers](https://learn.microsoft.com/en-us/windows/win32/api/mmeapi/nf-mmeapi-waveoutreset)


## Running the focused gates

Portable preservation and new boundary tests:

```text
python -m pytest -q tests/speech/test_wave_output.py tests/speech/test_playlist.py tests/speech/test_playlist_recovery.py tests/ui/test_audio_playback.py tests/ui/test_reader_playback.py tests/speech/test_player_controls.py tests/speech/test_player_transport.py tests/speech/test_listening.py
```

Actual Windows, with the same installed package bytes as the checkout:

```text
python -I -B scripts/windows_audio_player_smoke.py --installed
```

The native probe uses real wx controls, the real Windows file picker and
keyboard, the shared frame worker, real silent PCM, native position/pause and
EOF transitions, actual Ctrl+S and silent saved-queue reopen after moving original files,
and two independent waveOut handles to check volume isolation.
It fails when required native output capabilities are absent and has a 90-second whole-probe watchdog. The actual file picker is preselected by the test and accepted with native keyboard input; this is not manual accessibility or physical listening evidence. The existing
`windows_reader_player_smoke.py` remains a separate MCI/listening preservation
gate. The new `audio.playback` semantic command has no default shortcut; it is
registered with the existing keyboard manager before saved bindings activate.

Reference contracts were retrieved from Microsoft Learn on 2026-11-27. This feature changes player-owned controls only; it does not qualify speakers, acoustic quality, microphone monitoring, streaming models, or NVDA behavior.
