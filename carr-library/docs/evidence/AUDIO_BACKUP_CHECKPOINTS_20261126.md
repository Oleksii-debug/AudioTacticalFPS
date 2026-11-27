# Audio snapshot checkpoint and archive verification

Date: 2026-11-26. This follow-up is based on the PR99 literal-narration update
`d59cc82e3be7320386eaaba6af3c83da83c27678` and the separately committed encoded
export and publication fixes. It changes the Audio Studio ZIP snapshot only;
the full-library backup format and its sidecar registry are unchanged.

## Preserved recoverable work

With `include_cache=True`, a project snapshot includes accepted music chunks
from `music-jobs/<job>/cue.music-work`, their exact `state.json`, and completed
cue audio, provenance and procedural MIDI when present. Settings fingerprints,
typed settings, continuation-parent hashes, contiguous accepted chunk records,
PCM frame counts and chunk SHA256 values are checked before copying and again
before publishing the snapshot. Restore repeats the semantic checks against
the extracted bytes; recomputing an outer archive checksum cannot make an
invalid chunk record valid.

Partial waveforms, generated filter scripts, temporary playlists, lock files,
unknown files and quarantined results are excluded. Installed model assets and
credentials remain outside this snapshot. `include_cache=False` explicitly
excludes music resume state as well as the ordinary cache. A damaged accepted
checkpoint is refused; the snapshot operation does not repair or silently omit
it. Users can resume the original render to let its existing recovery logic
repair the checkpoint before taking a complete snapshot.

Inventory uses streamed directory enumeration, with file, byte and metadata
budgets checked while collecting known paths. Music checksums are streamed in
bounded blocks, observe cancellation, and verify source-file identity before
and after reading. Existing unknown source files remain untouched.

## Bounded archive opening

Before constructing `ZipFile`, restore validates the single-disk, comment-free
ZIP/ZIP64 end records, caps central-directory bytes, and scans the actual central
records. The declared count must match those records exactly. Names and extra
fields have bounded lengths. This prevents a small declared entry count from
causing an unexpectedly large allocation of `ZipInfo` objects. The opened source
descriptor and source path retain their identities through extraction.

The ordinary manifest inventory, path allowlist, selected-content flags, byte
budgets, CRC handling and each file's SHA256 are still checked. Restore publishes
only to a new destination after these validations.

## Executed verification

Linux x86-64, CPython 3.12.14, pytest 9.0.2 and installed FFmpeg 7.1.5.

```sh
python scripts/ci_check.py \
  tests/audio/test_project_archive_bounds.py \
  tests/audio/test_music_snapshot_resume.py \
  tests/audio/test_project_backup.py
```

| Suite | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| Central-directory preflight | 8 | 0 | 0 |
| Actual music snapshot/resume | 12 | 0 | 0 |
| Existing project snapshots | 12 | 0 | 0 |
| **Total** | **32** | **0** | **0** |

The music regression creates a real 12-second procedural composition, stops
after the first four-second chunk, snapshots and restores the project, and
finishes the composition. The restored chunk's bytes and modification time
remain unchanged, and the generated provenance records one reused chunk.
Other cases cover completed cues, corruption, forged accepted hashes despite
recomputed outer records, unaccepted extra chunks, nested invalid provenance,
file/byte budgets and explicit cache exclusion.

The ZIP64 test uses the real Python writer with a reduced ZIP64 threshold and
a small synthetic payload. The malformed-directory cases prove `ZipFile` is
never constructed before rejection. These are archive-format and procedural
audio checks; no multihour neural inference, native Windows or NVDA acceptance
is claimed.
