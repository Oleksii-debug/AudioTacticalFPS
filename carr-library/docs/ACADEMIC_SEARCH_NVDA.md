# Academic search: Windows and NVDA acceptance

This is a reproducible manual acceptance protocol, not an execution result.
Record the tested source commit, installed package hash, Windows version, wxPython
version, NVDA version, keyboard layout and display scaling before starting.
Use synthetic books and notes only.

The automated native helper exercises real wx controls, workers, source checks,
clipboard and topic saving. It does not establish what NVDA announces or whether
a keyboard-only user can complete the workflow.

## Fixture

Create two libraries containing small synthetic TXT books, each with a topic.
Use identical library names in one pass to check that stable IDs distinguish
them. Give the books different titles and authors. Include this sentence in both:

> Railway libraries support academic research.

Add an unreviewed PDF or EPUB containing the same sentence to one library.
Its extracted text must retain
an unverified state. Keep a separate unsaved note in the main notebook.

## Search Center

1. Open a library and press Ctrl+Shift+F. Verify that Research Search Center is
   announced, focus is inside it, and Tab/Shift+Tab reach every enabled control.
2. At the library checklist, verify that only the current library starts checked.
   Arrow keys move focus; Space toggles the selected library. NVDA must announce
   the accessible name, library identity, and checked/unchecked state.
3. Select both libraries, enter Railway, and use the query field's Enter action.
   During work, verify that Cancel operation remains usable and disabled source
   actions cannot be activated.
4. Verify each result's originating library, source title and verification state
   without relying on color. Test author and language filters separately.
5. Open verified context. Confirm that the original source text and location are
   readable with normal text-caret commands. Close it; focus must return to the
   selected result.
6. Copy the short quotation and paste it into a temporary text editor. Compare
   it exactly with the fixture sentence. Copying must not include a research
   note. Confirm result focus is restored after the action.
7. Save a result from the second library. The topic chooser must offer that
   library's topics, with distinguishable labels. Enter a separate note; reopen
   the saved evidence and compare source text and note independently.
8. Open the unreviewed result. Its extraction preview must be clearly announced
   as unverified; Copy and Save must not grant verified-evidence actions.
9. Change a query or filter. Old result actions must become unavailable. Start
   another search, then cancel or attempt to close while work remains pending.
   No cancelled result may be published. If the fixture completes too quickly
   to exercise this state, record NOT_EXERCISED rather than PASS.
10. Close the Search Center. Confirm that the original library, notebook and
    unsaved note remain intact. Reopen the dialog and repeat navigation at the
    user's normal display scaling; check for unreachable or clipped controls.

## Saved topic searches

1. Open a research topic and choose Research, Manage saved topic searches.
   The chooser must announce each definition's name, field, limit and archive
   state. Definitions with the same name must remain distinguishable.
2. Create criteria for Research notes. Verify the name/query text controls,
   search-field choice, numeric result limit and OK/Cancel buttons have useful
   spoken names. Cancel one creation and confirm no definition was created.
3. Run the saved definition. After the operation, read the result text and
   confirm that original quotations and research notes are kept separate.
4. Change a note, then rerun the same definition. Confirm it evaluates current
   evidence rather than displaying a cached result set.
5. Edit the criteria, duplicate the definition, archive the original and restore
   it. Verify each state is announced and persists after reopening the program.
6. Escape from the manager and each subordinate dialog. Confirm useful focus
   returns to the desktop and no keyboard trap or hidden mutation occurs.

## Record the outcome

For each step record PASS, FAIL, NOT_RUN or NOT_EXERCISED, with actual spoken
output or a short observation. Do not infer speech from a control's SetName or
from a passing headless handler test. Include a failure's exact steps, focus
position and source commit. Keep speech logs restricted to synthetic material.

NVDA's current-focus command (NVDA+Tab) and optional Speech Viewer can help record
what the screen reader actually reports. These are observation aids, not
substitutes for completing the keyboard workflow.

Official references:
- [NVDA User Guide](https://download.nvaccess.org/documentation/en/userGuide.html)
- [NV Access: accessibility testing with NVDA](https://www.nvaccess.org/get-help/accessibility-testing-with-nvda-free-sample/)
