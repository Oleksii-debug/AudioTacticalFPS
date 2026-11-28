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

## Inspect long silences in completed audio

In **Audio Studio → Scene projects and saved scene jobs**, choose **Saved jobs**
and explicitly select the completed production. Choose **Inspect long silences...**.
The criteria dialog starts at **−50 dBFS** and **2 seconds**. It starts work only
after **Inspect completed audio** is confirmed. Enter a finite threshold from
−192 through 0 dBFS and a positive minimum duration of at most 31,536,000 seconds.

The inspection measures the retained integer-PCM WAV/RF64 master. A frame belongs
to a candidate interval only when every channel is at or below the selected
absolute-sample threshold. The minimum duration rounds up to a whole sample
frame. The result lists start, end and duration in seconds and retains exact
frame bounds and source/PCM hashes in its structured report.

These intervals are warnings for review: an intentional pause may match.
An empty result means that no interval met the chosen settings. It does not
certify intelligibility, source-word accuracy, pronunciation, clipping or
listening quality. The list retains at most 4,096 intervals and reports both the
total and any omitted count. The inspector does not start playback, synthesize
another voice, rewrite the completed job or replace an audio artifact.

During a Scene inspection, the existing F8 button is named **Cancel inspection
(F8)**. F8 or Close cancels the inspection; it does not pause a saved production.
The ordinary **Pause (F8)** label returns after work finishes. If the selected
job, project, editor, library or window changes, the stale result is discarded.
A restored job requires a separately authorized new output to complete before
inspection; this operation does not reactivate its former destination.

The selected local FFmpeg/ffprobe runtime is required. Inspection uses the shared
resource broker and existing owned-process cleanup. Original source validation,
the completed-artifact checks and the job lock remain in effect throughout.
Optional music models, voice cloning and ASR are not needed for this measurement.

### Book parts and primary Studio productions

For a Book part, open **Audio Studio → Book audio: split and render...**, choose
its saved Book plan and **Inspect long silences...**. The **Book part for silence
inspection** chooser lists the saved parts by their one-based number and label.
Select a completed part, then confirm the same criteria. The Book source and
saved plan are revalidated; this action needs no new output folder. During this
operation the existing Book Pause button is named **Cancel inspection**.
Book inspection has no F8 shortcut.

For a primary Director production, open **Audio Studio → Synchronized EPUB3
publication...**. Choose **Load jobs / receipts**, explicitly select the completed
Studio job, then choose **Inspect long silences...**. The same action also accepts
the ordinary Scene jobs in this picker. It reads neither publication destination
nor creator, language or publisher fields. **Cancel operation** cancels the work.
The report retains the local job identity and separately binds it to the exact
selected source family; it creates no EPUB/DAISY file or publication receipt.

For a completed Scene job, the existing command-line entry point is:

```text
research-library studio --data-dir "C:\ResonanceData" inspect-silence JOB_ID --threshold-dbfs -50 --minimum-seconds 2
```

The command writes a structured JSON result to standard output and phase messages
to standard error. Ctrl+C cancels this read-only operation. Its process waits for
any already-owned native cleanup before exiting. A source-bound Book job requires
its Book source guard through the Book workflow.

### Inspection acceptance to record separately

The source change and its tests are **SOURCE_REVIEWED / NOT_RUN** at this
composition checkpoint. The following manual checks start at **NOT_RUN**; native
widget automation is recorded separately from human NVDA and listening.

| Scenario | Expected behavior | Human Windows 11/NVDA |
| --- | --- | --- |
| Open criteria and cancel | Named controls receive keyboard focus; no inspection starts | NOT_RUN |
| Invalid threshold or duration | Readable validation remains in the criteria dialog | NOT_RUN |
| Confirm a completed job | The existing background worker reports progress and leaves the interface responsive | NOT_RUN |
| Read matching intervals | Start, end and duration are readable in seconds; the result is read-only | NOT_RUN |
| No matching interval | The report states the selected measurement, without a quality verdict | NOT_RUN |
| More than 4,096 matches | The report states total, displayed and omitted counts | NOT_RUN |
| F8 during Scene inspection | The action is named Cancel inspection; no production pause is written | NOT_RUN |
| Close or change the owner | Work is cancelled or the stale result is discarded; focus does not jump to a former window | NOT_RUN |
| Review the retained output | Completed audio and job receipts are unchanged; playback starts only through an explicit playback action | NOT_RUN |
| Inspect a saved Book part | The exact numbered part and current original source remain bound; Cancel inspection is readable without an F8 claim | NOT_RUN |
| Inspect a primary Studio job | The selected source family remains bound; publication fields are unnecessary and Cancel operation remains usable | NOT_RUN |
