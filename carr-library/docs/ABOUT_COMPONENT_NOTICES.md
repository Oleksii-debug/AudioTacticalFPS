# About Resonance and component notices

**Product:** Resonance, by Sceneria Innovative Technologies.

Help → **About Resonance and component notices** is one native command with
semantic ID help.about_components. It participates in the existing keyboard
command registry. Its readonly text controls, component selector, Read, Refresh
and Close controls have explicit accessible names. Escape closes the dialog.
Actual NVDA qualification remains a separate native acceptance activity.

The dialog uses the desktop's existing job executor and progress binding.
Metadata discovery, inventory parsing and notice reads never run on the GUI
thread. Closing cancels its bounded reads; late callbacks cannot display data
under a different library object/root or a closing window. This flow does not
change project data, entitlement status, or user preferences.

## Existing sources of truth

The installed interpreter's purelib and platlib directories provide actual
distribution metadata. Python's importlib.metadata.PathDistribution locates
each selected dist-info/egg-info; bounded metadata bytes provide the original
name, version, declared licence expression/text and License-File headers.
No optional component is imported to discover it. The integrated profile's
pywin32, TUF and securesystemslib are therefore listed when actually installed,
as are the other installed components. The component count is not a fixed
claim about which optional profile was installed.

The existing runtime bundle discovery and manifest validator provide retained
Python, dependency-wheel, OCR, FFmpeg, native C++ and semantic-model notices.
The native launcher's retained build record is labelled as provenance, with no
separate licence inferred. The viewer does not hash model or executable payloads
or authenticate a publisher. It reads only recognized notice paths already
listed in the existing local bundle inventory.

There is no second licence database, online lookup, entitlement decision,
universal licence assignment or redistribution-clearance claim. Copyrights,
mixed licences and the selected upstream text are preserved. A missing bundle
or metadata entry in development is explicitly reported as unavailable.

## Bounded selected-file reading

Notice files are read only after choosing a component and a specific file.
The inventory accepts local canonical relative paths, refuses parent traversal,
drive/UNC/device aliases, symlinks and reparse directories, and never follows
unrelated script paths from RECORD. A present RECORD must actually list a
declared file. Selected RECORD or bundle hashes and sizes are checked when
available. Older metadata without RECORD is labelled as an unhashed local notice.

Metadata and inventory identity are captured and checked again before/after
payload reading; a changed file or manifest requires reopening the inventory.
Selected files must remain regular local files with a stable descriptor/path
identity. Actual bytes are bounded and cancellation is checked between chunks.
UTF-8 and BOM-marked UTF-16 plain text are supported. Retained binary licence
documents are not rendered as invented text.

Limits are 1,024 installed component entries, 8,192 entries per interpreter
directory, 256 KiB per METADATA and 8 MiB aggregate metadata, 4 MiB / 50,000 rows
per selected RECORD, 256 notice entries per selected component, and 2 MiB for
one displayed notice. The existing bundle manifest retains its own bounds.

## Validation status

The source tests use actual synthetic installed distribution metadata, RECORD
hashes and local licence files, real worker threads, and controlled native
widget boundaries. They cover original text, lazy reads, traversal/device
paths, inventory mutation, byte limits, cancellation, same-library callbacks,
busy/admission errors and semantic command installation. These controlled
boundaries are not a claim of native Windows or NVDA execution.

Component acceptance passed at commit `8c51cb28f8410d01e4675876a703fcf558a424e5`
in [run 46117569608](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46117569608):
41 source and 41 isolated installed cases on each of Windows and Linux, with
zero skipped or expected-failure cases. Both actual Windows core and semantic
profiles passed all 19 required native keyboard, installed-notice, worker and
window-lifetime checks. All four jobs bound 408 Python files to the same
source inventory before and after execution. Evidence blob:
`4395289883c6de3d4f4b77a82bd6dce684e0ba84`.

These results qualify that component commit. The final combined package must
rerun its maintained installed and native checks. The final integration also
checks that the Slovak preparation component's own shipped COPYING is exposed
through the actual distribution License-File/RECORD inventory. Native NVDA,
physical-device and redistribution-clearance qualifications are not inferred.

Primary metadata reference:
https://docs.python.org/3.11/library/importlib.metadata.html
