# Resonance composite V3 integration

This candidate reconciles the accepted platform source at
184dd83d43f285ce27b6f351e82fee0bbaf19323 with the reviewed integration
checkpoint b54b89703d3318f101a68646e27ff054f3a5c5c5. The current
UNIVERSAL_SCOPE_V3_FINALIZATION.md remains authoritative. This record is not
full-platform acceptance or distribution clearance.

## Preserved contracts

- Existing library and research schemas, immutable originals, source and
  quotation identities, Unicode managed OCR and native result-focus behavior
  are retained.
- Saved reading derivatives retain their document/source anchors and distinguish
  inserted inter-page separators from exact source spans. Text equality alone
  cannot authorize recovery of an unbound or different-library reading draft.
- Existing-file export approval includes a streamed content digest as well as
  file/parent identity. Equal-size writes with unchanged visible Windows
  timestamps revoke that approval. Initially absent paths publish without
  replacing a competing file, including for previous writer callers. The
  bibliography window captures the snapshot on its existing cancellable worker
  before requesting overwrite approval on the UI thread.
- One main-window menu system and shortcut manager exposes the accepted reader,
  production, translation, conversion, citation and progress workflows.
  Previous keymaps are read only when the current preference file is absent.
  Unknown commands and collisions are reported; the old file is retained.
- Save, note-draft recovery and standard Edit commands share one menu.
  Ctrl+Alt+P remains Audio Studio; Ctrl+Alt+G queries operation progress.
- Existing studio project formats remain distinct and readable. Scene projects
  have an explicit entry in Audio Studio. No silent project-format conversion
  or change to source text is performed.
- The two accepted provider APIs use the same pinned-address transport.
  Credential namespaces stay distinct. Restored plans and usage reservations
  require review before external work; viewing a saved plan does not require
  the old source directory to exist.
- Provider cancellation and deadlines use the request's own nonblocking socket
  loop through connect, TLS handshake, send and response parsing. They do not
  depend on another thread's shutdown interrupting a Windows buffered read.
  Standard HTTP parsing and TLS certificate/hostname validation remain.
- Both accepted music API schemas remain available. The actual procedural,
  MIDI and sound-effect generators are shared. Different optional neural SDK
  manifests are not silently converted or treated as executed inference.
- Historical backups 1, 2, both strict version-3 shapes and version 4 remain
  readable. Composite version 5 adds typed sidecars to canonical state and
  pending imports. The manifest distinguishes inputs, optional generated
  artifacts and external/re-downloadable engine assets.
- Import completion is reconciled against a request-bound publication intent after
  the child has fully stopped. A committed revision remains complete after a late
  cancellation, timeout or lost reply; uncommitted and stale replay controls
  remain stopped. Restoring an import clears the previous machine's attempt and
  publication authorization before rebinding its data directory.
- Progress observer failures cannot reverse a durable result. Selected local
  voice notices respect playback, mute and library-switch boundaries.
- Restored credentials are detached, provider profiles disabled, external jobs
  marked for review, and old production destinations cannot be resumed without
  a new explicit output choice. Typed engine registrations require revalidation.

## Additional workflows prepared against the updated scope

The existing acquisition service is extended with the reviewed PR100
`d8db53a7ee288c7dff708d639d59b3b0ce612cef` identifier implementation.
DOI requests can use OpenAlex; ISBN-10/ISBN-13 requests use one exact
Open Library record. Provider metadata remains unverified source evidence.
ISBN equivalence reuses the academic catalogue; source text and historical
quotations are preserved. Every metadata redirect and isolated-worker response
remains bound to the originally selected provider host. No live provider
execution is claimed by synthetic transport tests.

The command palette reads the current menu/keyboard registry and invokes the
original handler after the native modal closes. Existing user shortcut choices
keep ownership. New source and installed Windows gates cover the palette.
The exact DOI-to-DOI/ISBN command rename preserves saved shortcuts through the
existing registry migration; conflicting aliases fail closed without rewriting
the source preferences. Named typed workflows reuse the current plan/job store:
saving does not execute, and reuse removes previous scope and authority before
a fresh preview and explicit Run. Diagnostics reuse the isolated synthetic
self-test and expose only allowlisted status/version data, with native preview
and exact create-only export.

Synchronized EPUB authoring reuses PR112's measured Studio publisher and the
existing two production journals. An explicit adapter retains the primary
Director's source spans, reading-copy mapping, project fingerprint, accepted
cache receipts and measured frame identities; it does not rewrite projects.
Publication receipts are included in typed recovery and restored outputs
require explicit selection. Pinned EPUBCheck 5.3.0 is a test-only tool, never a
production dependency. The bounded reader verifies the same publication graph,
supports chapter/utterance navigation and explicit playback, and reuses the
existing FFmpeg/WavePlayer path. It checks the RIFF32 decoded-frame bound before
conversion and verifies actual PCM output; it does not claim arbitrary EPUB or
unbounded long-form playback support. Package conformance, actual audio-device
playback, human NVDA, DAISY interoperability and Braille remain separate
acceptance items.

These additions were prepared during an executor interruption. Their earlier
peer results do not establish this composite's execution. The new exact-head
source, native and installed Windows gates are required before qualification.

## Packaging

Core and full Windows CPython 3.11 locks contain 15 and 51 normalized unique
dependency pins respectively, with matching CycloneDX component inventories.
Both the source wheel and the installed payload must bind to their exact
source commit.

Long audio mix graphs use an owned temporary graph file and bounded Windows
process arguments. The actual reviewed FFmpeg option is selected for the local
runtime. Model inventory reads fresh no-follow file metadata and still rejects
hard links and reparse points.

The complete source-locked FFmpeg pack is the production build default.
Earlier compact private packs retain a distinct, strictly verified layout:
two executables, publisher LICENSE/README and their exact provenance file.
They do not claim ffplay support. A bundle containing both layouts is rejected.
Explicit setup APIs are retained; no setup download is started implicitly.

The installed Windows gate retains only its verified public application source
wheel and exact commit/tree/hash proof. Private delivery reuses those tested
application bytes. Native runtimes, models, private ASR code and user inputs
are not uploaded by public CI. The private refresher checks parent and new
committed native build inputs before reusing a sealed launcher.

The optional native ResearchLibrary.exe is bound to reviewed C source and
PE/build evidence. It remains an unsigned private candidate. Package and
data-directory names are preserved for compatibility; visible desktop titles
use Resonance.

## Acceptance boundary

The source was recovered across executor interruptions and verified again on
the current Python 3.12/Linux checkout. Completed portable gates include:

- 43 actual CSL formatting/UI/document cases with the shared 512 MiB QuickJS
  context, and 180 assistant/provider cases, all without skips.
- 107 DOI gateway/worker cases, including actual isolated-child bytecode
  suppression and bounded cancellation; paid providers were not invoked.
- 80 import publication/replay/progress cases using real PDF/DOCX children and
  SQLite commit boundaries, all without skips.
- 171 ASR protocol, pending import, sidecar, composite and state backup cases,
  including copied-source rehash and restore authorization revocation.
- 74 managed OCR/model-store cases using actual local Tesseract and the pinned
  official model bytes.
- 49 compact audio/native launcher/bundle cases and 21 private-refresh cases.
- Targeted native-controller, engine-review and progress gates are recorded
  separately; overlapping runs are not added into one aggregate count.

The initial failing compact-hardlink, packaging-guide, pending-format and CSL
memory cases were corrected and rerun. Completed portable checks are not
Windows execution evidence. The pull-request workflows must establish the
exact new commit's source, installed-package, native-control and sealed-bundle
results. Skipped/XFAIL cases do not count as a successful strict gate.

The first composite Windows candidate, 7af7d9f2, passed both native launcher
toolchains, the transcription controls/protocol and the strict bibliography
workflow. Its wider Windows gates found concrete failures; those successful
component runs do not qualify the successor. Corrections include the occupied
keyboard fixture, large audio command, fresh Windows model metadata, cancellable
provider transport, real OCR prerequisites, content-bound export and accepted
locale/process-cleanup fixtures. Further main-workflow corrections use the
supported native TaskDialog confirmation message, bounded OCR test IDs,
canonical MIME base64 validation across Python versions, restored acquisition
workflow tests, explicit XLSX test dependencies and direct-format backup
identity validation. Source suites retain individual strict verdicts
and isolated temporary directories when run concurrently. Complete aggregate
Windows success and its exact retained application wheel remain mandatory for
the private application delivery.

The supplied private Whisper Transcribe V4.3 source is kept outside this public
repository. The public bridge now validates explicit worker capabilities,
local model inventory, request-bound receipts, each requested output, timed
segments and word metadata. It refuses the incompatible original worker
before starting recognition. Its repair/addon is a separate private artifact;
synthetic protocol and native-control tests do not qualify acoustic inference.
No actual ASR model/runtime acceptance is claimed in this source record.

Owner voice/cloning runtime acceptance, authorized reference recordings,
per-language quality for all 135 target entries, unrestricted distribution
clearance, publisher signing, and signed update/entitlement services remain
explicit open requirements. This candidate does not remove them.
