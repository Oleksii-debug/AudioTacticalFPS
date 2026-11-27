# Synchronized publication reading

The native **Library → Read synchronized EPUB or DAISY** command opens the
current Resonance producer's measured EPUB3/DAISY 2.02 profiles. The existing
general EPUB reader remains available for ordinary imported books. This
adapter does not claim arbitrary third-party EPUB/DAISY interoperability.

The reader captures the selected original without modifying it, verifies the
existing publisher's member inventory, text/SMIL graph, provenance and embedded
MP3 identity, and uses the selected local FFmpeg owner to prepare bounded PCM.
Original, encoded and decoded byte sizes share the selected staging budget;
the default is 2 GiB and 60 minutes, with a 64 MiB free-space reserve. Resource
admission, cancellation and owned native-child cleanup use the existing
application services. No decoder or engine is downloaded.

Preparation and chapter/interval/time navigation never start playback.
Playback uses the existing WavePlayer/WinMM path with an exact verified PCM
reader held through the delayed media handoff and native use. Native observed
driver frames or completed buffers determine the highlighted measured
interval. This is interval-level synchronization; word timing is not claimed.
The optional external ffplay path does not report a playhead, so its selected
text position is retained and the limitation is shown.

Native choice lists contain at most 200 rows. Previous/next chapter and
interval controls reach all bounded windows. The text remains read-only.
Packaged source mappings are visible but do not authorize original quotations.

Stop records the observed position before resetting the native player.
Publication positions bind the complete publication SHA-256, kind and exact
duration; they contain no original paths, source text, executable settings or
audio. The typed reading-publications.json sidecar participates in complete
backup and restore. A different publication cannot inherit a saved cursor.
Loading another book releases the old document after successful preparation
and retains the shared native player. Closing cancels preparation, waits for
the actual worker, settles native/file owners, then removes only private PCM.
A cleanup refusal preserves the document and offers a later explicit retry.

## Source and execution evidence

The initial local domain implementation executed 18 real-current-producer /
selected-FFmpeg tests before the executor outage. That count does **not**
qualify subsequently reconstructed source bytes. The current exact source
adds bounded ZIP central-directory reads, native-position profiles, typed
resume/backup, and the native dialog/lifecycle tests. Their status remains
**NOT_RUN** until the exact required source/installed/native gates finish.

Required suites:

- tests/reader/test_media_overlay.py (actual selected FFmpeg; synthetic PCM).
- tests/reader/test_publication_state.py (typed state, fail-closed I/O, backup).
- tests/ui/test_media_overlay.py (actual workers/verified files; simulated output owner).
- scripts/windows_media_overlay_smoke.py --native (actual installed wx controls,
  current publisher and selected FFmpeg; no physical output device is started).

Physical audio, manual NVDA, independent third-party reader interoperability,
word alignment and per-language listening quality require separate evidence.
The optional engines, original 17/53 dependency profiles, source/citation
rules and reviewed FFmpeg pin are unchanged by this adapter.
