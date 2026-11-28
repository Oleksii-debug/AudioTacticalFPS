# Local voice packages and reference recordings

Open **Local voice and music engines**, connect an existing reviewed dordiai-voices installation, then choose **Manage reviewed voice packages and references**. Wait for the current operation to finish before opening management. The window uses the application's existing background worker.

## Register and choose a backend

Choose **Register reviewed local manifest**, select the manifest belonging to the connected installation, and read the confirmation. **No** is the default. Registration checks the selected local files; it does not download a model or generate speech.

Choose the installed backend and its existing model folders. Save the configuration before inspecting a package. Changing backend or model choices clears earlier production-language approvals. A different reference directory requires a new explicit manifest registration and ownership review.

The application keeps the existing local-engine settings and voice preferences. The parent engine window reloads a completed management change. A stale parent snapshot cannot overwrite a newer management choice.

## Inspect, approve and activate

Choose **Inspect data-only package**, or enter an installed profile ID and version and choose **Review selected installed version**. Read the profile identity, provenance, file roles and declared languages.

The permission checkbox begins unchecked. Check it only when you have permission to use the exact references shown. Enter the production languages you have reviewed; an empty field leaves declared languages as candidates. Choosing **Apply reviewed profile** installs or activates only that reviewed data.

A package's declared languages do not establish pronunciation, similarity or synthesis quality. Open a new Reader or Audio Studio window for a separate, explicit audition. Management never starts inference or replaces a live speech provider.

## Audition the explicitly selected backend

Holos retains its existing path. A managed OmniVoice package requires the selected local OmniVoice model directory and its reviewed `use_onnx.py` entry, plus the package's exact audio and transcript roles. It returns one completed PCM reply per request, so loading and chunk-streaming progress are not advertised.

A managed OpenVoice package uses the reviewed owner's OpenVoice V2 converter and encoder under `models/OpenVoiceV2`, plus the selected Ukrainian or English Piper carrier and its configuration. OpenVoice V1 is not advertised by this adapter. Other declared package languages remain unavailable through this particular OpenVoice route. Missing resources refuse the pair; a package label alone cannot make it runnable.

The worker fingerprint, complete local manifest and explicit permission remain required. A different reviewed worker hash is an explicit configuration choice; existing installations are not silently upgraded. Changing a backend, activating another version, revoking permission or deleting references invalidates the previous reviewed selection. Reopen the voice list and select the approved current voice before auditioning or preparing another saved plan.

The managed process holds the existing package lock until its real worker closes. Each PCM delivery rechecks current authority, and cancellation remains a failure rather than a completed partial audition. Package-management tests and synthetic PCM tests do not establish model inference, language quality or similarity.

## Create a local OmniVoice reference package

Reference creation is available only for the saved OmniVoice configuration. After creating, inspecting and approving that package, open a fresh Reader or Audio Studio to choose its current managed voice. The existing speech provider has separate reviewed OmniVoice and OpenVoice V2 protocol paths; this does not download a model or establish acoustic quality.

Choose a local standard 16-bit mono or stereo PCM WAV, enter its exact transcript, profile ID, version, display name, language and provenance, and confirm your permission. **Open existing recorder** opens the application's recorder only after that explicit action; the recorder must return a verified saved recording.

Choose **Review reference input**, read the measurements, and review the recording and matching transcript in the existing audio tools. The separate recording/text checkbox also begins unchecked. After reviewing it, choose **Create local reference package** and a fresh destination.

Creation writes a local unencrypted data package. It does not train a model, install a profile or approve production use. The package then receives a fresh inspection in the parent window; installation still needs its own permission and language review. Original recordings and transcripts remain unchanged.

## Export and protected packages

An installed version must be reviewed again before export. Unencrypted export and password-protected export have separate permission checkboxes.

Unencrypted export creates a local data package containing the reviewed version and references. Engine code and shared model files are excluded. Use the displayed SHA-256 to identify the resulting file.

Protected export uses the application's existing encrypted-file format. Enter and confirm a password of at least 12 characters. Keep it separately: the application cannot recover it. Passwords are used for that operation, are not saved in settings, and are cleared from the dialog and queued operation when it finishes or is cancelled.

Protected import first authenticates and inspects the package. Applying it asks for the password again and requires a fresh permission and language review. Authentication alone never authorizes installation.

## Deactivate or delete

Enter the profile ID and select **Deactivate and keep reference files** or **Delete all installed versions and reference files**. Choose **Review profile removal**, then type the exact confirmation shown in the review. The confirmation begins empty.

Deactivation revokes the current production approval and keeps the reference files. Deletion removes only the reviewed profile's installed versions and references. Both retain checks on other profiles and shared models. Reusing a deactivated profile requires a new explicit review.

## Cancel, close and recovery

**Cancel operation** asks the current owned operation to stop at its next checkpoint. Closing a child window cancels queued work and prevents a late result from changing its controls. The shared host retains responsibility for running work and cleanup.

A completed local change remains completed when cancellation or cleanup failure arrives later. A saved-settings receipt is retained and the window explains that cleanup needs attention. Reopen management and revalidate the current installation before another change; do not assume that an interrupted operation made no changes.

Complete library backups preserve the local configuration as passive restored state. They do not copy an external voice runtime or its model/reference files. After restore, explicitly revalidate the local installation before using its management authority.

## Keyboard and evidence boundary

Every field, permission, status and action has a native label. Tab moves between controls, Space activates the focused button or checkbox, and Escape closes the package or reference dialog. The forms scroll while the status and close controls remain available.

The automated native management check uses the actual Windows host, controls, shared worker, broker and isolated metadata child with a purpose-written synthetic owner protocol. That check covers the interface and lifecycle boundaries; it does not establish real-model inference, voice similarity, recording quality, NVDA behavior or permission to use another person's voice.
