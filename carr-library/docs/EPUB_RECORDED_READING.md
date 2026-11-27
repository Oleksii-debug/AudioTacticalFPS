# Reading an EPUB with synchronized recorded audio

Import the EPUB through the existing library workflow, open it in **Read book**,
and choose **EPUB synchronized recorded audio…** for the current source section.
The dialog checks the retained original and its local SMIL index in a bounded
background process. Opening it never starts audio.

Select a recorded fragment, then choose **Play from selected segment**. Playback
follows the authored fragments through the current source section. The exact
corresponding source text is selected in a read-only native control. **Pause**,
**Resume**, **Stop**, **Previous**, **Next**, and **Where am I** are named native
buttons reachable with Tab and Shift+Tab; Space activates a focused button.
Escape closes this audio dialog and returns to the Reading Desk. Choose another
source section in the Reading Desk to continue there.

Changing the segment stops playback; it does not automatically play the newly
selected segment. An interruption by an owned audio operation retains the same
selected segment and requires explicit Play. Stop, close, a changed source
selection, or lost source access invalidates pending playback. Errors remain
visible and focus the playback status. Device closure must be confirmed before
the dialog releases its prepared WAV or closes.

## Source and timing boundaries

The reader reuses the retained EPUB and the Reading Desk's exact extraction,
source revision, topic and library access checks. It never rewrites the EPUB,
marks extracted text as verified, or creates quotation approval. Repeated
phrases are located by their actual XHTML fragment identifiers and the same
text normalizer used during import. Word-level alignment is available only if
the EPUB itself supplies word-level fragments; no new alignment is inferred.

The supported subset is EPUB 3 local XHTML with recorded SMIL `seq`/`par`
overlays, one text/audio pair per segment, and standard `clipBegin`/`clipEnd`
times. This includes EPUBs exported by the application's Studio publication
workflow. MP3, MP4 audio, AAC, Ogg, WAV and FLAC resources must be uniquely
declared in the package. Local FFmpeg/ffprobe convert each selected interval to
24 kHz stereo PCM; the resulting frame count must match the authored interval.
Full source and prepared-audio hashes are checked in the worker. Native start
and resume recheck source access and verified file identities without reading
hundreds of megabytes on the GUI thread.

Remote resources, encrypted or obfuscated EPUBs, executable markup, additional
SMIL timing/conditions, missing or ambiguous text fragments, and sections
without a recorded overlay fail explicitly. There is no fallback to synthetic
speech. The existing 64 MiB source and ZIP/XML limits remain in force, with at
most 2,048 recorded segments per section, 32 MiB per embedded audio member, one
hour per selected interval, and a 512 MiB prepared-audio allowance. These are
supported-input limits, not a claim of complete EPUB reading-system conformance.

The standards defining the supported mapping are [EPUB 3.3 media overlays](https://www.w3.org/TR/epub-33/#sec-media-overlays)
and [EPUB Reading Systems 3.3](https://www.w3.org/TR/epub-rs-33/#sec-media-overlays).

## Acceptance

`tests/reader/test_ebook_audio.py` uses real retained EPUB imports, an isolated
parser child, actual FFmpeg decoding, and a real Studio publication → import →
SMIL → measured PCM roundtrip. Transport-boundary doubles are identified as
such; they do not establish native audio-device or screen-reader acceptance.

Run the native probe using the installed Windows interpreter:

```powershell
& $env:CARR_PYTHON -I -B scripts/windows_epub_reading_smoke.py
```

The probe checks installed source identity, actual DesktopFrame/ReadingDialog
integration, named controls and foreground focus, native key down/up, actual
WavePlayer pause/resume/stop, source access invalidation, and unchanged original
bytes. Its audio fixture is a synthetic tone produced through real Studio and
FFmpeg. Outside Windows it exits 77 with `NOT_RUN`. A source/portable pass does
not substitute for this Windows gate. NVDA, human listening quality and
third-party reader compatibility remain separately reported acceptance tasks.

Local validation of this source slice: **163 passed, 0 failed, 0 skipped** —
55 overlay/roundtrip/transport regressions and 108 existing Reading Desk,
ebook-reader and native-handler tests. The two independent lifecycle findings
were also replayed against the prior session/transport code: an external close
incorrectly advanced to segment 2, and native start synchronously invoked the
full source check. Both corresponding tests pass with the corrected code.
Python AST validation passed for all five changed/added Python files. The
Windows probe has only its explicit Linux `NOT_RUN` result at this checkpoint;
that result is not native acceptance.
