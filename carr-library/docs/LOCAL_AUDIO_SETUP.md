# Explicit local audio preparation

The installed application can keep its transferable payload free of FFmpeg while
preparing a separate private runtime. The setup wrapper reuses
`packaging/prepare_ffmpeg_runtime.py` and the installed
`carr_library.audio.runtime` verifier. It accepts either a selected existing
archive or an explicitly approved download of the same fixed archive. Engine
settings, library preferences and the installed application are not changed.

## Plan and choose the input

Run `SetupLocalAudio.cmd` to print the current pinned URL, size, SHA-256, runtime
inventory, expected disk space, and proposed external destination. The default
performs no writes, network requests or executable checks.

For a chosen local archive, review and confirm the existing offline path:

```text
SetupLocalAudio.cmd --archive C:\Downloads\ffmpeg-pinned.zip --destination C:\LocalRuntimes\audio-new --library-root C:\Research\Library --interactive
```

`--prepare` remains the explicit non-interactive equivalent for that local file.
Its original bytes stay in their original location.

To review a fixed download, omit `--archive` and choose interactive setup:

```text
SetupLocalAudio.cmd --destination C:\LocalRuntimes\audio-new --library-root C:\Research\Library --interactive
```

The displayed plan marks `downloads: true` and includes the exact public archive,
its size/hash, destination, disk reservation and fixed native checks. Only the
literal response `YES` authorizes this action. An empty answer, other answer, or
end of input leaves files and network untouched. For an explicitly approved
unattended setup, `--download` performs that same download/preparation without
another prompt. It cannot be combined with `--archive`, `--prepare` or
`--interactive`.

## Fixed acquisition and preparation

Only the current Gyan FFmpeg 9.1 full-build archive is fetched. Initial requests
use its fixed HTTPS URL; every redirect is checked before a second connection
and must remain HTTPS on `github.com` or
`release-assets.githubusercontent.com`. No response can select a filename,
command, helper, model, voice or runtime setting. Credentials, fragments,
alternate ports, ambiguous/control characters and unapproved hosts are refused.

Acquisition requires an exact complete response, rejects partial ranges or an
altered byte encoding, and checks any Content-Length against the fixed size.
It streams bounded chunks and verifies both the actual byte count and SHA-256.
The existing audio publisher checks the bytes again during its independent copy,
then publishes without replacing an existing file. A competing destination is
preserved. Failure or cancellation removes only the owned partial file.

A transfer has a 600-second elapsed budget checked between bounded operations
and a 30-second socket timeout. A blocked read may therefore finish after the
elapsed deadline, but no further read or publication is admitted. This is a
bounded setup attempt, not a throughput guarantee.

Preparation requires Windows x64 and a new work folder outside the application
and the supplied library root. The default destination is a new folder under
`LOCALAPPDATA\Resonance\runtimes`. The download stays in a private temporary
setup directory and is removed after the existing preparation and final
verification close their input readers. A successful receipt is emitted only
after that cleanup. Its acquisition path describes temporary material; it is not
a retained executable selection.

The unchanged preparer verifies the exact archive and member bytes, retains the
approved publisher notices, executes its fixed version/filter/license checks,
and verifies the completed runtime again. Manual preparation reserves staging
and completed-runtime space plus headroom. A download additionally reserves two
archive copies for the receipt-bound no-replace publisher. If preparation fails,
keep any newly created external work folder for inspection; existing directories
and library files are never adopted as a new destination or deleted.

After success, use the current library's engine settings to select the printed
FFmpeg and FFprobe paths. Setup does not activate them automatically.

## Source and distribution boundary

The canonical BtbN shared build, its source/build-recipe archives and its notices
are different artifacts from this Gyan static full build. This restored download
does not transfer their provenance or qualification to Gyan. It does not acquire
a complete transitive corresponding-source bundle or authorize redistribution.
The existing transfer-inventory guard remains unchanged: an audio-containing
private bundle cannot become a transferable application payload by changing a
manifest flag.

The bundle builder ships the fixed source tools
`tools/audio-setup/setup_local_audio.py` and
`tools/audio-setup/prepare_ffmpeg_runtime.py`, with `SetupLocalAudio.cmd` beside
the payload's `.venv`. Both helpers belong to the sealed inventory. A standard
generation contains that same payload-relative layout. There is no alternate
audio runtime or installation layout.

## Evidence

The new controlled-transport and interactive tests exercise the actual urllib
redirect/error pipeline with a replaced HTTPS I/O boundary, exact byte/hash
publication, temporary-file ownership, cancellation, user consent and the real
preparer with a tiny synthetic archive. They make no external-network or native
FFmpeg claim. Existing manual-setup tests remain selected.

Actual acquisition and Windows preparation are to be qualified by the existing
main FFmpeg preparation step calling this same wrapper with `--download`.
The step must retain its raw setup receipt and the independently read final
runtime manifest. A timeout, missing receipt, failed native probe or cleanup
failure remains a failure; source review alone cannot mark it successful.
