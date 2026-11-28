# Reading recorded audio in an EPUB

Open an imported EPUB in the existing **Reading Desk**. On the current source
section, choose **EPUB synchronized recorded audio...**. The same action is
available in the reader command palette and keyboard settings; it has no new
default shortcut.

This feature reads recorded audio already authored into that EPUB. It does not
synthesize missing audio or infer word timings. Supported local SMIL
`seq`/`par` fragments can correspond to a word, sentence, or larger passage,
according to the publication. The highlighted text remains the exact retained
source section, and unverified extraction remains unverified.

## Controls and lifecycle

Opening the window parses and checks the selected retained archive on the
application's existing background worker. It starts no playback. Choose a
recorded segment, then **Play from selected segment**. **Pause**, **Resume**,
**Stop**, previous/next segment, and **Where am I** are explicit native controls.
Selecting a segment does not start it. Natural completion advances within this
source section; an external quiet/recording stop does not impersonate natural
completion and does not restart audio.

The existing WavePlayer owns the native transport. Progress and recording
quiet rules apply before playback. A source, library, research-scope or section
change invalidates pending work and stops playback. Native dispatch performs a
fresh authorization and file-identity check; full byte verification stays on
the background worker.

Close requests cancellation and retains pending results until the actual
worker returns. If native cleanup is uncertain, the existing cleanup owner
keeps the affected temporary files and quiet reservation until silence is
confirmed. Closing this child never shuts down the application's executor.
The UI makes an unconfirmed native close visible rather than treating it as a
successful stop.

## Local input and runtime boundaries

The retained original EPUB supplies its own SMIL and recorded audio. Remote
resources, encrypted content, executable markup, unsafe archive aliases,
unsupported synchronization structures, missing fragments and invalid timing
fail explicitly. No original document, stored source approval or quotation
authority is changed.

The isolated parser uses the existing bounded child-process runner and its
owned Windows Job option. Preparation uses
`runtime_settings.audio_runtime`, including sealed bundled-tool checks and
explicit previously selected executable settings. It measures audio duration
and verifies the exact prepared PCM interval. No codec, model, voice or runtime
is downloaded.

Parsing and preparation use the application's existing shared resource
coordinator. The parser requests one CPU slot and a declared 512 MiB memory
allowance before reading the retained source. It reserves temporary capacity
for the bounded archive and reply logs. The decoder requests its actual
configured worker-thread count, a declared memory estimate of at least
512 MiB or twice the FFmpeg per-allocation limit, and temporary capacity for
one bounded recorded member, the selected PCM interval, logs and the configured
free-space reserve. If the authored end is absent, the existing one-hour
interval limit bounds the PCM estimate until inspection supplies its duration.
These are cooperative admission allowances, not measured peak memory or a
machine-performance benchmark; the existing child/output limits still apply.

Waiting can be cancelled before any parser or decoder is launched. Source and
selection authority are checked again after admission. An explicit resource
policy which cannot admit the configured decoder fails visibly; the reader
does not silently reduce its thread count or increase the policy. If native
cleanup is pending, the operation worker holds the reservation until the
existing cleanup owner confirms that its child has stopped. The GUI does not
perform that wait. A prepared clip keeps its verified temporary files for
playback but holds no active parser/decoder CPU reservation.

Current bounds include the existing EPUB source/archive budgets, at most 2,048
recorded cues in a source section, 32 MiB per extracted recorded member, one
hour per selected interval, and a 512 MiB prepared-output allowance. An authored
interval that exceeds its measured recording fails rather than being silently
extended.

## Backup and restore

There is no separate saved playback database. The original EPUB, including its
recorded media, is already part of the canonical retained-source backup.
Reading positions and bookmarks continue using their existing typed producer.
Parsed indexes and prepared PCM files are temporary derivatives.

After a normal verified library restore, reopen the retained EPUB through the
Reading Desk and choose the recorded-audio action again. Preparation uses the
restored source. It does not open the old external import path, old library,
old temporary PCM path, an external voice model, or a provider account.

## Evidence required for this composition

- `tests/reader/test_ebook_audio.py`: actual retained imports, isolated parsing,
  actual FFmpeg intervals, source/metadata/authorization changes, malformed
  archives/SMIL, real-child cleanup with held resource reservations, cancelled
  admission and stale queued selections, unchanged quotation authority, publication
  roundtrip and canonical backup/delete/restore without old-source reads.
- `tests/ui/test_ebook_audio_lifetime.py`: the real host executor, queued scope
  changes, result ownership, callback omission during close, refused admission,
  retained uncertain native-cleanup fixtures, and no child-owned executor.
- `scripts/windows_epub_reading_smoke.py --audio-directory <pinned-directory>
  --audio-lock packaging/windows-audio.lock.json`: installed module hashes,
  explicitly pinned local FFmpeg, actual native source highlighting and
  WavePlayer playback, keyboard Pause/Resume/Stop/navigation, archive rejection
  and cleanup through the current shared ProgressHub.

These new sources and tests are **NOT_RUN at the composition checkpoint**.
The native test uses clearly labelled synthetic tones. It can establish native
transport and timing behavior, not speech quality, human listening, manual
NVDA acceptance, every publisher's EPUB support, or third-party reader
conformance. Existing publisher EPUBCheck evidence remains a separate gate.
