# EPUB and DAISY from completed Audio Studio productions

The Audio menu publishes an existing, completed Audio Studio production as
EPUB 3 with media overlays or a DAISY 2.02 ZIP. It uses the current renderer,
selected local FFmpeg, resource broker, project lock and measured frame
timeline. It does not create a second Studio, provider, library or job system.

## Publish and verify

1. Render the intended script or literal narration in Audio Studio and keep
   its output directory. A complete.json marker, saved plan, timeline and
   exact recorded artifacts must agree.
2. Choose Audio → Publish completed audio as EPUB or DAISY. Select the saved
   output directory and the exact completed production.
3. Choose the format and enter the language tag and optional creator.
   DAISY also requires your publisher metadata. These fields do not infer a
   rights holder or permission to distribute someone else's material.
4. Select a new filename outside both the private library and the audio
   project directory. An existing file is never replaced. The selected
   directory is checked again immediately before publication.
5. Wait for the saved-and-verified result. Cancel before publication leaves
   no final book. Cancellation arriving after the atomic file publication
   does not relabel the complete artifact.
6. Use Audio → Verify a saved audio publication to select its receipt and
   existing exported file explicitly. Verification rechecks the actual bytes,
   internal document graph and source provenance. It does not render again.

Both choices use native labelled dialogs and the existing background-job
host. Keyboard, screen-reader and third-party reading-system acceptance are
separate tests; choosing a format does not establish those results.

## What synchronization means

The publication retains the original source, project/plan identity, hashes of
every completed source artifact, the original integer frame rate and measured
speech-line frame intervals. EPUB/SMIL clock values use six decimal places.
They are line-level timing, not word alignment or acoustic recognition.

Sound or music outside scripted lines remains an explicitly untranscribed
audio interval. Headings, role labels and the source/provenance page are not
claimed to be spoken. A minimum valid 0.001-second Pause is retained; the
production model still refuses a zero-second Pause.

Literal narration stays literal, including bracketed text, CRLF and form-feed
page boundaries. Form feeds cannot appear directly in XML text. The source
page therefore uses a reversible JSON-string presentation when needed, while
the exact source remains in provenance. This presentation does not rewrite
the retained narration draft or grant it approved-library-evidence authority.

A completed MP3 master is copied byte-for-byte. Other completed output
formats use the verified retained WAV master and the existing local MP3
encoder. Actual ffprobe codec, duration, rate and channel measurements must
match the completed timeline. MP3 may include encoder padding.

DAISY output transports the complete NCC, content, source, SMIL and MP3 set.
Extract the whole ZIP into a new directory for a compatible reading system.
The first heading reference is text-only and has zero duration; no spoken
heading is invented.

## Integrity, interruption and resource limits

The current completed-render marker adds plan_sha256. Existing markers remain
readable only when their old run key can be reconstructed from the exact saved
plan, retained assets and original recorded processing identities. A changed
title, role or plan cannot acquire the identity of an older render.

The publisher reattests every source artifact before publication. The existing
renderer deliberately retains some sealed chapter PCM through hardlinks;
these are permitted only as hashed source inputs. Receipt, staging and final
publication files retain the normal unlinked-file boundary.

Export uses the current heavy-media broker reservation and project lock.
The native probe uses the existing bounded operations.process runner. Its
platform cleanup contract is unchanged; this feature does not claim a new
Windows process-memory sandbox. Publication defaults to a 32 GiB maximum,
a 900-second operation budget and a 128 MiB free-space reserve. ZIP directory,
member, XML byte, depth and node counts are checked before unbounded expansion.

A prepared receipt is saved before the create-only atomic file publication.
If final receipt persistence is interrupted, the complete exported bytes are
kept. Explicit verification can finish that receipt without replacing the
book or starting another renderer. A process crash before the prepared
receipt is durable is not claimed to be recoverable through this receipt.

## Audio project snapshots

The separate Audio Studio project snapshot includes typed
publications/<UUID>.json receipts whether or not generated audio is included.
It validates their passive schema before snapshot and after restore under
the existing aggregate byte/file limits and project lock.

External exported books are not copied into a project snapshot automatically.
Keep the EPUB/DAISY file separately. A receipt restored to another project
directory requires you to select the exported file explicitly before it can
be verified. Recorded paths never authorize a provider, engine, library
import, microphone or background job.

## Reused source and validation evidence

The format serializers, graph checks, conformance-tool pins and negative
controls were selectively adapted from public PR152,
d4f9fe061c237c2e0f36ae23498ee19c70249d79. The old Studio/AudioRuntime, project
schema, provider registry and renderer were not imported. The present adapter
uses current audio/production.py and its actual completed output.

At d150f2088dc406076ef61c9dcd0b3e33b8f805eb, Actions 45952503396,
job 140351356102 passed five Linux suites: 36 publication, 12 publication UI,
17 existing production, 15 existing project-backup and 17 existing Studio
cases. Total: 97 passed, zero failed and zero skipped, on CPython 3.11.18 with
actual FFmpeg/ffprobe 6.1.1. Fixtures use public synthetic PCM; no private voice,
microphone, model, provider, quotation or document is used.

This source proof includes real rendering/export, unchanged source files,
literal pages, exact MP3 reuse, legacy plan binding, create-only races,
cancellation, post-publication receipt recovery and passive snapshot restore.
It does not by itself qualify an external reader or the installed Windows UI.

The external tools remain CI-only and are not bundled application dependencies:

- EPUBCheck 5.3.0, exact archive SHA-256
  6c07e68584b2e2ce2f89fe06e1246dfead3eb36b46b340e7d93524f29dcff6c5,
  with its BSD-3-Clause and third-party notices retained during verification.
- The exact five offline SMIL/XHTML DTD/entity inputs listed in
  packaging/daisy-validation-schemas.json. lxml validates only those pinned
  local resources; arbitrary external entity resolution is refused.

Run the explicit setup-only acquisition and independent checks from
carr-library, with the current application installed and actual FFmpeg
selected. Existing tool-cache files are reused only if their hashes match:

    python scripts/prepare_publication_tools.py --output VALIDATOR_CACHE --download
    python scripts/epubcheck_acceptance.py --archive VALIDATOR_CACHE/epubcheck-5.3.0.zip --java ABSOLUTE_JAVA --report NEW_EPUB_REPORT.json --require-installed
    python scripts/daisy_schema_acceptance.py --schemas VALIDATOR_CACHE/daisy-schemas --report NEW_DAISY_REPORT.json --require-installed

Each independent proof renders four actual current productions: WAV narration,
completed MP3 reuse, literal source pages and untranscribed retained sound.
EPUBCheck must report zero errors and warnings and reject a deliberately
invalid package. The DAISY check validates all four XML documents against
the pinned DTDs and must reject invalid XHTML. These validators do not prove
full DAISY rules, reader interoperability, spoken quality, WCAG or NVDA.

At b5a5d47c9052e82e4a6ffd3fbdfd8f5f1743a5dd, Actions 45953559624,
job 140354734331 passed all six source suites: 109 tests, zero failed or skipped.
The actual installed application then produced all four EPUB cases. Pinned
EPUBCheck 5.3.0 reported zero fatal errors, errors and warnings for each, and
rejected the invalid MIME control with one error. The four installed DAISY
cases passed all 16 pinned-DTD document checks and rejected all four invalid
XHTML controls. OpenJDK 17.0.21.1 and the selected real FFmpeg/ffprobe 6.1.1 ran
on Linux. Exact JSON reports are retained in docs/evidence.

The installed Windows native-menu probe is separate:

    .venv/Scripts/python.exe -I -B scripts/windows_publication_smoke.py --bundle BUNDLE --output NEW_NATIVE_REPORT.json

It requires the selected installed package and exact source bytes, preserves
the sealed manifest, drives the actual Audio menu and verifies both real
exports, both receipt-verification actions, the native Cancel button, source
immutability and completed/cancelled progress. Only chooser answers and public
synthetic PCM are controlled. Its Windows result remains NOT_RUN until the
exact installed gate executes. Physical keyboard, pickers, microphone, speaker,
third-party reading systems, speech quality and NVDA remain manual NOT_RUN
items. Historical PR152 reports remain separate component-reuse evidence.
