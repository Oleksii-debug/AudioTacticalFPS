# Studio output choices and saved revisions

Audio Studio keeps the final format and the choice to export separate stems with
each production job. The WAV master is retained for every supported final format.

Choose **Render / resume** to continue a job with its saved choices. Changing the
format or the separate-stems checkbox requires a **new production job** and an
explicit output-folder selection. Canceling that folder selection restores the
previous job's visible output choices. It leaves the saved job and its files intact.

The saved-job chooser displays the format, whether separate stems were requested,
and the job identity. Selecting a job restores those exact controls and its saved
project revision. When that revision predates the current editable project,
saving over the newer project is refused. A new output from the historical job
uses an independent project copy with its original cues, effects and source
bindings.

Recovery drafts retain their original revision and fingerprint. A draft from an
older version of the application that has no revision identity remains readable.
If its text differs from the current saved project, saving creates a separate
project. Backup and restore preserve those draft identities without granting
permission to overwrite the current project or resume an old output automatically.

This change reuses the existing project store, production worker, source guards,
Save All, cache and publication rules. It does not install voices, select another
runtime or alter output-device settings.

## Evidence

Source reconciliation reuses PR199 at
`36af770a4b80c8545f4a732ee1dc886666b5cbcc` on P3
`10b9a4e36b6415cdf9a21fbc36bcd9ef2b72420a`, preserving the current rich-project
and fingerprint compare-and-save behavior. The passive backup hooks compose with
the independently reviewed production-operation identity change.

Required focused suites:

- `tests/ui/test_studio_output_settings.py`
- `tests/storage/test_studio_draft_backup.py`
- Existing `tests/studio/test_studio_controller.py`,
  `tests/studio/test_clip_editing.py`,
  `tests/ui/test_save_all_access.py` and
  `tests/ui/test_studio_progress_isolation.py`.

New source and tests are **NOT_RUN** at this checkpoint. Native keyboard,
installed rendering and human listening results must be reported separately.
