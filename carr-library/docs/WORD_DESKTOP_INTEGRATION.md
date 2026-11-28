# Desktop Word citation working copies

CARR can connect its current reviewed bibliography to desktop Microsoft Word on Windows. The connection is optional: the ordinary offline formatter, editable DOCX exports and DOCX regeneration work without Word or an Office account. The existing Windows package supplies the pinned comtypes runtime; Word itself must be installed separately.

## Open a reviewed working copy

1. In the current library and workspace, open the bibliography/citation formatter. Review the reference metadata and any source locators. Choose the style and locale and format the current citations.
2. Export that result as a citation DOCX. Open the exported file in desktop Word, save it, and leave that document active. Keep ordinary prose and the CARR citation metadata intact.
3. In the formatter's Preview tab, choose **Open Word controls...**. The formatter closes before the **Word citation working copy** window opens.
4. Choose **Connect and create working copy...** and select a new local `.docx` filename. The destination must be absent. CARR copies the exact reviewed original, opens that new copy in Word, and binds its citation controls. It does not overwrite the original.
5. Keep that same working-copy Word window active for every subsequent action. A different document, changed library/workspace, revised source metadata, or altered managed citation data requires a fresh review rather than silent rebinding.

Use one Word session for this workflow. Word's running-object registration may require switching focus away from Word once after opening it. If CARR reports that Word is unavailable, leave the saved document open, return to CARR and retry only after the previous action has finished. CARR refuses a COM runtime whose first initialization cannot be assigned to the current desktop main thread; in that case restart CARR and open Word controls before another COM-based action.

## Insert, edit and regenerate

**Insert at Word caret** inserts the selected current reference at a collapsed caret in ordinary managed body text. For a pinpoint, enter the exact locator, select its type and tick **I checked this exact locator in the selected source**. Changing the reference, locator or locator type clears that confirmation. A saved metadata flag alone never approves a new locator.

**Edit selected citation** replaces the selected stable citation occurrence using those same current controls. **Refresh bibliography** regenerates the managed citations and bibliography with the current reviewed CSL result. Note styles use native Word endnotes. Occurrence and reference identifiers remain editable in the embedded current CARR data.

The visible text remains subject to review. Formatting a citation does not establish that its quotation is exact, that a page contains the claimed passage, or that missing metadata is known. Enter and review missing facts yourself. Source changes invalidate the captured formatting result.

Save the working copy explicitly in Word. CARR does not automatically save, close or quit Word. A completed mutation uses a named Word Undo record; use Word's normal Undo control if you deliberately want to undo it, then **Inspect working copy** before continuing. Live Word Undo and custom-XML behavior require actual Office qualification; controlled protocol tests do not prove those host behaviors.

## Cancellation and uncertain host results

The same desktop worker handles the operation. Closing the controls requests cancellation and waits for the actual Word call to return. An external COM call may remain blocked by a Word dialog; dismiss that dialog in Word. CARR does not terminate Word or claim that cancellation rolled back an already completed edit.

If the result is uncertain, editing controls stay blocked. Choose **Inspect working copy** to reconcile the actual managed data with a previously issued state. Do not repeat an uncertain Insert. An unknown foreign state requires a new explicit review; it is not adopted automatically. The original exported file remains the recovery copy even if opening or binding the newly created copy was interrupted.

A working-copy capability lasts only for the dialog that issued it. Restoring files or reopening the application does not restore permission to write to an arbitrary Word document. Re-export/review and connect a new copy when a prior capability is no longer available.

## Qualification and scope

The automated source and isolated-installed suites exercise the current library, source binding, bundled CSL processor, real DOCX files, create-only publication, typed requests and worker lifetime. Their external Word boundary is controlled and is reported as such.

The separate Windows desktop-controls probe uses actual wx windows, keyboard events and the existing desktop executor while controlling only Word and picker answers. It is not a real Office or real picker-dialog test.

The completed [native component run 46126241176](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46126241176)
qualified commit `429a5e60685b4c4ac38efd204d8fb4d57a6604c5` with **128 required
native assertions passed**. Its before/after proof covered all 408 package Python
files, 20 CSL/runtime/notice assets and 19 named module origins. The retained
record is `8264ac4a6aa227d0e86b7d7041fe16476de1125d`. Office was not installed
on that runner; the separate optional Office probe reported `NOT_RUN` with zero
checks and the explicit availability reason. This result belongs to the recorded
component head and does not qualify a later composed application.

The optional actual Office command is:

```text
python -I -B -X utf8 scripts/windows_word_citations_smoke.py
python -I -B -X utf8 scripts/windows_word_citations_smoke.py --require-word --seconds 240
```

It must run on an otherwise idle Windows desktop with no existing Word process. It creates new synthetic documents in its own new Word instance and refuses to close any unowned document. It never kills Word. Its final JSON distinguishes `PASS`, `FAIL` and `NOT_RUN`; missing Office yields zero completed checks and `NOT_RUN`, while `--require-word` makes that a nonzero exit. The timeout is cooperative between COM calls, not an interruption guarantee. Unconfirmed cleanup retains the synthetic files for inspection.

That probe uses its directly owned COM instance, so it reports production running-instance/worker attachment separately from actual host mutation. Neither native probe establishes NVDA behavior, physical speech, arbitrary third-party add-in compatibility, or compatibility with every Word release.

## Interface references

- [Microsoft Word UndoRecord](https://learn.microsoft.com/en-us/office/vba/api/word.undorecord)
- [Microsoft Word Window.Hwnd](https://learn.microsoft.com/en-us/office/vba/api/word.window.hwnd)
- [Microsoft CustomXMLPart.LoadXML](https://learn.microsoft.com/en-us/office/vba/api/office.customxmlpart.loadxml)
- [Microsoft Office automation server behavior](https://learn.microsoft.com/en-us/troubleshoot/microsoft-365-apps/office-suite-issues/getobject-createobject-behavior)
- [comtypes 1.5.0 initialization source](https://github.com/enthought/comtypes/blob/1.5.0/comtypes/__init__.py)

These links describe host interfaces. They are not execution evidence for this build. Actual Office, keyboard and accessibility results must be recorded against the exact tested build.
