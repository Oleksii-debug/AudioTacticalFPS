"""Native Word citation review using the desktop's existing single worker."""
from __future__ import annotations

import threading

from ..academic_integration.word_com import WordComBridge
from ..academic_integration.word_fields import WordCitationService, decode_field
from .citation_formatting import (CitationFormattingController, LOCALES, LOCATORS,
                                  reference_label)
from ..academic_integration import available_styles


class WordActions:
    """Worker orchestration kept independent of wx for lifecycle regression tests."""

    def __init__(self, frame, *, bridge=None):
        if (getattr(frame, "_closing", False) or frame.working or getattr(frame, "busy", False)):
            raise ValueError("Wait for the current operation before opening Word review.")
        if frame.active is None:
            raise ValueError("Open a research library before reviewing Word citations.")
        self.frame, self.library, self.scope = frame, frame.library, tuple(frame.active)
        self.cancel_event = threading.Event()
        self.closed = False
        self.pending = None
        self.context = self.plan = None
        self.on_change = lambda: None
        self.on_report = lambda message: None
        self.controller = CitationFormattingController(self.library, self.scope[0])
        self.bridge = WordComBridge() if bridge is None else bridge
        self.service = WordCitationService(self.controller, self.bridge, guard=self.guard)
        self._unregister = frame.executor.register_cleanup(self._close_owned)
        self._cleanup_future = None

    def guard(self):
        if (self.closed or getattr(self.frame, "_closing", False)
                or self.frame.library is not self.library or tuple(self.frame.active or ()) != self.scope):
            raise InterruptedError("The captured library or Word review session is no longer active.")

    def cancelled(self):
        return self.cancel_event.is_set() or self.frame.cancel_event.is_set()

    def _notice(self, text):
        if not self.closed:
            self.on_report(text)

    def _job(self, title, operation, done):
        self.guard()
        if self.pending is not None or self.frame.working or getattr(self.frame, "busy", False):
            self._notice("Wait for the current operation or choose Cancel.")
            return False
        self.cancel_event.clear()
        token = object()
        self.pending = token
        self.on_change()

        def work():
            self.guard()
            return operation()

        def finished(value):
            if self.pending is token:
                self.pending = None
            if self.closed:
                return
            try:
                self.guard()
                done(value)
            except Exception as exc:
                self._notice(str(exc))
            self.on_change()

        def failed(exc):
            if self.pending is token:
                self.pending = None
            if not self.closed:
                self._notice(str(exc))
                self.on_change()

        def discarded(value):
            # No COM object crosses the GUI result boundary. Its native owner
            # is separately retained by the executor's registered finalizer.
            if self.pending is token:
                self.pending = None

        try:
            self.frame.run_job(title, work, finished, failed, discard=discarded)
        except BaseException:
            if self.pending is token:
                self.pending = None
            self.on_change()
            raise
        return True

    def capture(self, ready=lambda context: None):
        self.context = self.plan = None
        def captured(context):
            self.context = context
            ready(context)
            self._notice("Captured the named open Word document. Choose a change and prepare a preview.")
        return self._job("Inspecting open Word and current references",
                         lambda: self.service.capture(cancelled=self.cancelled), captured)

    def preview(self, parameters, ready=lambda plan: None):
        self.guard()
        context = self.context
        if context is None:
            raise ValueError("Inspect Word and the current library first.")
        self.plan = None
        captured = dict(parameters)
        def prepared(plan):
            self.plan = plan
            ready(plan)
            self._notice("Review the exact named document and proposed fields before Apply.")
        return self._job("Preparing the reviewed Word citation change",
            lambda: self.service.preview(context, cancelled=self.cancelled, **captured), prepared)

    def apply(self, expected_plan, *, confirm):
        self.guard()
        if expected_plan is None or expected_plan is not self.plan or self.pending is not None:
            raise ValueError("Prepare a fresh Word preview before applying.")
        if self.cancelled():
            raise InterruptedError("The preview was cancelled. Prepare a fresh one before applying.")
        approved = confirm(expected_plan)
        # A modal return must be checked before run_job clears its cancel event.
        self.guard()
        if self.cancelled() or expected_plan is not self.plan:
            raise InterruptedError("Word approval expired during confirmation.")
        if approved is not True:
            self._notice("Word update declined. No Word write was requested.")
            return False
        self.plan = None
        def applied(receipt):
            self.context = None
            self._notice(receipt.notice + " Document: " + receipt.document_name)
        return self._job("Applying the reviewed CARR Word fields",
            lambda: self.service.apply(expected_plan, cancelled=self.cancelled), applied)

    def invalidate(self):
        self.plan = None
        self.on_change()

    def cancel(self):
        self.cancel_event.set()
        self.plan = None
        self._notice("Cancellation requested. An in-flight Word write remains owned until Word returns; it is never replayed automatically.")

    def _close_owned(self):
        result = self.service.close()
        if result is not True:
            raise RuntimeError("The exact Word automation owner has not confirmed release.")
        unregister, self._unregister = getattr(self, "_unregister", None), None
        if unregister is not None:
            unregister()
        return True

    def close(self):
        if not self.closed:
            self.closed = True
            self.cancel_event.set()
            self.plan = self.context = None
            self.on_change = lambda: None
            self.on_report = lambda message: None
        if self._cleanup_future is not None and not self._cleanup_future.done():
            return
        if getattr(self, "_unregister", None) is None:
            return
        try:
            self._cleanup_future = self.frame.executor.submit(self._close_owned)
        except RuntimeError:
            # Desktop shutdown retains the registered cleanup on this same
            # worker, after any call which is still actually running.
            pass


def _show_word_citations(frame, actions):
    import wx
    dialog = wx.Dialog(frame, title="Review citations in open Word", size=(980, 800),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    owner_id = dialog.GetId()
    destroyed = False
    styles = available_styles()
    references = ()
    field_rows = ()
    preview_parameters = None
    dialog.word_citations = actions
    layout = wx.BoxSizer(wx.VERTICAL)
    notice = wx.TextCtrl(dialog, value=(
        "Open the intended Word document yourself. Inspect only attaches to an existing Word instance. "
        "Review its exact name before applying CARR citations, native endnotes or bibliography. "
        "Only CARR ADDIN results and metadata are changed. Ordinary prose and foreign fields remain untouched. "
        "Word's own AutoSave policy remains in effect; this bridge never calls Save, Undo, Open or Quit."),
        style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 85))
    notice.SetName("Word citation scope and saving policy")
    layout.Add(notice, 0, wx.EXPAND | wx.ALL, 6)
    target = wx.TextCtrl(dialog, value="No Word document inspected.", style=wx.TE_READONLY)
    target.SetName("Captured open Word document")
    layout.Add(target, 0, wx.EXPAND | wx.ALL, 6)
    row = wx.WrapSizer(wx.HORIZONTAL)
    inspect = wx.Button(dialog, label="Inspect open Word and references")
    inspect.SetName("Inspect open Word and references")
    row.Add(inspect, 0, wx.ALL, 4)
    operation = wx.Choice(dialog, choices=[
        "Insert citation at Word cursor", "Edit selected CARR citation",
        "Insert CARR bibliography at Word cursor", "Regenerate existing CARR fields"])
    operation.SetName("Reviewed Word operation")
    operation.SetSelection(0)
    row.Add(operation, 1, wx.ALL, 4)
    layout.Add(row, 0, wx.EXPAND | wx.ALL, 3)
    existing = wx.Choice(dialog, choices=["No CARR citation selected"])
    existing.SetName("Existing stable CARR Word field")
    existing.SetSelection(0)
    layout.Add(existing, 0, wx.EXPAND | wx.ALL, 6)
    choices = wx.CheckListBox(dialog)
    choices.SetName("Academic references for this Word citation or bibliography")
    layout.Add(choices, 1, wx.EXPAND | wx.ALL, 6)
    options = wx.WrapSizer(wx.HORIZONTAL)
    style = wx.Choice(dialog, choices=[item.title for item in styles])
    style.SetName("Pinned CSL style for Word fields")
    style.SetSelection(0)
    locale = wx.Choice(dialog, choices=[label for _, label in LOCALES])
    locale.SetName("Word citation locale")
    locale.SetSelection(0)
    label = wx.Choice(dialog, choices=[title for _, title in LOCATORS])
    label.SetName("Explicit citation locator kind")
    label.SetSelection(0)
    locator = wx.TextCtrl(dialog)
    locator.SetName("Explicit verified source locator; empty for none")
    for control in (style, locale, label, locator):
        options.Add(control, 1, wx.ALL, 4)
    layout.Add(options, 0, wx.EXPAND | wx.ALL, 3)
    verified = wx.CheckBox(dialog, label="I reviewed this locator against the selected source")
    verified.SetName("Explicit source locator review")
    layout.Add(verified, 0, wx.ALL, 6)
    retained_verified = wx.CheckBox(dialog, label="I reviewed all retained Word locators against the current sources")
    retained_verified.SetName("Explicit review of historical Word locators")
    layout.Add(retained_verified, 0, wx.ALL, 6)
    preview = wx.TextCtrl(dialog, style=wx.TE_READONLY | wx.TE_MULTILINE | wx.HSCROLL,
                         size=(-1, 210))
    preview.SetName("Reviewed Word citation changes")
    layout.Add(preview, 1, wx.EXPAND | wx.ALL, 6)
    status = wx.TextCtrl(dialog, value="Inspect Word to begin.",
                        style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 62))
    status.SetName("Word citation operation status")
    layout.Add(status, 0, wx.EXPAND | wx.ALL, 6)
    buttons = wx.WrapSizer(wx.HORIZONTAL)
    prepare = wx.Button(dialog, label="Preview Word changes")
    apply = wx.Button(dialog, label="Apply reviewed Word changes")
    cancel = wx.Button(dialog, label="Cancel Word operation")
    close_button = wx.Button(dialog, wx.ID_CANCEL, "Close Word review")
    for button in (prepare, apply, cancel, close_button):
        button.SetName(button.GetLabel())
        buttons.Add(button, 0, wx.ALL, 4)
    layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 6)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(wx.ID_CANCEL)
    controls = (inspect, operation, existing, choices, style, locale, label, locator, verified, retained_verified)

    def alive():
        return not destroyed and not actions.closed

    def report(message):
        if alive():
            status.ChangeValue(message)
            status.SetInsertionPoint(0)
            status.SetFocus()

    def sync():
        if not alive():
            return
        pending = actions.pending is not None
        for control in controls:
            control.Enable(not pending)
        prepare.Enable(not pending and actions.context is not None)
        apply.Enable(not pending and actions.plan is not None)
        cancel.Enable(pending)

    def parameters():
        selected = operation.GetSelection()
        if not 0 <= selected < 4:
            raise ValueError("Choose one Word operation.")
        current = existing.GetSelection() - 1
        target_id = field_rows[current].id if selected == 1 and 0 <= current < len(field_rows) else None
        if not 0 <= style.GetSelection() < len(styles) or not 0 <= locale.GetSelection() < len(LOCALES):
            raise ValueError("Choose an installed CSL style and locale.")
        if not 0 <= label.GetSelection() < len(LOCATORS):
            raise ValueError("Choose an explicit supported locator kind.")
        refs = tuple(references[index]["id"] for index in choices.GetCheckedItems())
        return dict(operation=("insert", "edit", "bibliography", "refresh")[selected],
            reference_ids=refs if selected != 3 else (), target_id=target_id,
            locator=(locator.GetValue().strip() or None) if selected in (0, 1) else None,
            label=LOCATORS[label.GetSelection()][0], locator_verified=verified.GetValue() if selected in (0, 1) else False,
            review_existing_locators=retained_verified.GetValue(),
            style=styles[style.GetSelection()].id, locale=LOCALES[locale.GetSelection()][0])

    def invalidate(event=None):
        nonlocal preview_parameters
        if alive():
            actions.invalidate()
            preview_parameters = None
            preview.ChangeValue("")

    def reset_locator(event=None):
        verified.SetValue(False)
        invalidate()

    def load_existing(event=None):
        if not alive():
            return
        index = existing.GetSelection() - 1
        verified.SetValue(False)
        if 0 <= index < len(field_rows):
            record = decode_field(field_rows[index])
            wanted = set(record["reference_ids"])
            for number, reference in enumerate(references):
                choices.Check(number, reference["id"] in wanted)
            cluster = record["cluster"]
            value = cluster["items"][0] if cluster is not None and len(cluster["items"]) == 1 else {}
            locator.ChangeValue(value.get("locator", ""))
            if value.get("label", "page") in dict(LOCATORS):
                label.SetSelection([key for key, _ in LOCATORS].index(value.get("label", "page")))
            report("Loaded historical field data. Any locator edit requires a fresh explicit source review.")
        invalidate()

    def captured(context):
        nonlocal references, field_rows
        if not alive():
            return
        references = context.bibliography.references
        field_rows = tuple(row for row in context.document.fields if row.kind == "citation")
        target.ChangeValue(context.document.document_name)
        choices.Set([reference_label(row, index + 1) for index, row in enumerate(references)])
        existing.SetItems(["No CARR citation selected"] +
                          [row.id + " — " + row.text[:100] for row in field_rows])
        existing.SetSelection(0)
        verified.SetValue(False)
        locator.ChangeValue("")
        retained_verified.SetValue(False)
        if context.document.fields:
            first = decode_field(context.document.fields[0])
            if first["style"] in [item.id for item in styles]:
                style.SetSelection([item.id for item in styles].index(first["style"]))
            locale.SetSelection([key for key, _ in LOCALES].index(first["locale"]))
        sync()

    def inspect_word(event=None):
        try:
            actions.capture(captured)
        except Exception as exc:
            report(str(exc))

    def prepare_word(event=None):
        nonlocal preview_parameters
        try:
            captured_parameters = parameters()
            preview_parameters = captured_parameters
            def ready(plan):
                if alive() and parameters() == captured_parameters:
                    preview.ChangeValue(plan.preview)
                    preview.SetInsertionPoint(0)
                    preview.SetFocus()
                else:
                    actions.invalidate()
            actions.preview(captured_parameters, ready)
        except Exception as exc:
            report(str(exc))

    def apply_word(event=None):
        try:
            if preview_parameters != parameters():
                raise ValueError("Word choices changed. Prepare a fresh preview.")
            def confirm(plan):
                answer = wx.MessageBox(
                    "Apply the displayed changes only to CARR fields in:\n"
                    + plan.context.document.document_name
                    + "\n\nWord's own AutoSave policy remains in effect. Library source files are unchanged.",
                    "Apply reviewed Word fields", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, dialog)
                if not alive() or preview_parameters != parameters():
                    return False
                return answer == wx.YES
            actions.apply(actions.plan, confirm=confirm)
        except Exception as exc:
            report(str(exc))

    def close(event=None):
        if destroyed:
            return
        actions.close()
        if dialog.IsModal():
            dialog.EndModal(wx.ID_CANCEL)

    def owner_destroyed(event):
        nonlocal destroyed
        if event.GetId() == owner_id:
            destroyed = True
            actions.close()
        event.Skip()

    actions.on_report, actions.on_change = report, sync
    inspect.Bind(wx.EVT_BUTTON, inspect_word)
    prepare.Bind(wx.EVT_BUTTON, prepare_word)
    apply.Bind(wx.EVT_BUTTON, apply_word)
    cancel.Bind(wx.EVT_BUTTON, lambda event: actions.cancel())
    close_button.Bind(wx.EVT_BUTTON, close)
    dialog.Bind(wx.EVT_CLOSE, close)
    dialog.Bind(wx.EVT_WINDOW_DESTROY, owner_destroyed)
    existing.Bind(wx.EVT_CHOICE, load_existing)
    for control in (operation, style, locale, label):
        control.Bind(wx.EVT_CHOICE, invalidate)
    choices.Bind(wx.EVT_CHECKLISTBOX, reset_locator)
    locator.Bind(wx.EVT_TEXT, reset_locator)
    verified.Bind(wx.EVT_CHECKBOX, invalidate)
    retained_verified.Bind(wx.EVT_CHECKBOX, invalidate)
    sync()
    try:
        inspect.SetFocus()
        dialog.ShowModal()
    finally:
        actions.close()
        if not destroyed:
            dialog.Destroy()


def show_word_citations(frame, *, bridge=None):
    actions = WordActions(frame, bridge=bridge)
    try:
        return _show_word_citations(frame, actions)
    finally:
        actions.close()


def install_word_actions(frame):
    """Call once before the existing KeyboardActions menu capture."""
    if getattr(frame, "_carr_word_actions_installed", False):
        return
    import wx
    bar = frame.GetMenuBar()
    if bar is None:
        raise ValueError("Install the existing native menu bar first.")
    index = bar.FindMenu("Academic")
    menu = bar.GetMenu(index) if index >= 0 else wx.Menu()
    item = menu.Append(wx.ID_ANY, "Review citations in open Word...")
    frame.Bind(wx.EVT_MENU, lambda event: show_word_citations(frame), item)
    if index < 0:
        bar.Append(menu, "&Academic")
    frame._carr_word_actions_installed = True
