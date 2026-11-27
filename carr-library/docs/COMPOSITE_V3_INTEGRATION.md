# Resonance composite V3 integration

This candidate reconciles the accepted platform source at
962297eef228870b6eaab3f46163124db8f5045c with the reviewed integration
checkpoint b54b89703d3318f101a68646e27ff054f3a5c5c5. The current
UNIVERSAL_SCOPE_V3_FINALIZATION.md remains authoritative. This record is not
full-platform acceptance or distribution clearance.

## Preserved contracts

- Existing library and research schemas, immutable originals, source and
  quotation identities, Unicode managed OCR and native result-focus behavior
  are retained.
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

## Packaging

Core and full Windows CPython 3.11 locks contain 15 and 51 normalized unique
dependency pins respectively, with matching CycloneDX component inventories.
Both the source wheel and the installed payload must bind to their exact
source commit.

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
