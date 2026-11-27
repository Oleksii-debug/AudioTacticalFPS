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

## Independent recorded playback controls

Speed, pitch, volume and stereo balance are prepared as a private PCM derivative
through the existing FFmpeg filter graph and owned recording_process runner.
The original publication and decoded source stay unchanged. Default controls
reuse the original decoded PCM. Changing controls requires Stop; paused output
resumes the same controls. Speed or pitch processing labels the mapping back to
the original measured text as estimated_after_time_stretch. This is a source-time
estimate, never a new word-alignment measurement.

The native window retains the exact library object, physical root identity and
configured trust controller through workers and delayed Play callbacks.
Reading existing recorded audio uses protected data.read; it does not request
a fresh audio.render entitlement. Cleanup remains available after that window
authority is revoked. Existing live speech, SAPI controls, recording pause,
assistant playback ownership and batch conversion remain their current services.

## Source and execution evidence

This component ports selected reader files from owner
f8d5f85619b61fb52df24c7eb2f5dd4d1febc18f onto clean root
c5e37ec798cca301e8767577550ebf33e142abe4. The owner is provenance, not an
additional parent. Its capture_process/capture_types dependencies are adapted
to the retained recording_process.CaptureBudget/run_process and cleanup APIs.
The fixed publication MP3 decoder uses that existing owned process boundary,
holding input, runtime, staging and resource ownership until cleanup settles.

Current evidence is **NOT_RUN** until this exact component and its two integration
hunks complete source, installed and native qualification. Earlier owner and
R6/R7 results are not transferred. The public exact function/native-label oracle
and focused workflow retain the actual raw receipts on failure.

Required suites cover actual selected-FFmpeg playback transforms, current
publication producers, typed resume/backup, cancellation/cleanup, native-position
profiles, late UI actions and no automatic playback. The native probe executes
installed wx controls and actual FFmpeg preparation; it deliberately holds
the existing media handoff before opening a physical output device.

Physical audio, manual NVDA, independent third-party reader interoperability,
word alignment and per-language listening quality require separate evidence.
The retained player/WinMM implementation, current live speech controls, reviewed
FFmpeg pin and integrated 56-pin dependency profile remain unchanged.


## Exact qualification gate

The focused workflow is .github/workflows/carr-media-overlay.yml, enabled for
carr/media-overlay-* and carr/v3-acceptance-* candidates. It uses the unchanged
reviewed integrated56 lock and selected pinned FFmpeg archive.

| Required lane | Exact population |
| --- | ---: |
| Source functions and parameter multiplicities | 100 |
| Isolated installed functions and parameter multiplicities | 100 |
| Installed native reader checks | 45 |
| Installed implementation origins in the native receipt | 12 |

scripts/media_overlay_oracles.json declares the complete Counters. The shared
audio_batch_acceptance harness retains its observed-bootstrap fixes and
complete tracked-checkout/test/wheel identity checks. The reader adapter adds
the exact native contract for both EPUB and DAISY: 8,000 stereo PCM frames at
16,000 Hz, a 4,000-frame independent-control derivative, and byte identities
measured from the actual synthetic outputs.

Each source/installed child remains bounded at 1,200 seconds and each step at
22 minutes. The native probe has a 180-second watchdog; the whole job is
bounded at 75 minutes. Timeout, incomplete collection, changed counters,
skips/XFAIL/XPASS, failed cleanup or altered source evidence is FAIL. Raw textual
reports and bounded failure log tails remain available on every outcome.
No private books, PCM payloads, models or executables are uploaded.
