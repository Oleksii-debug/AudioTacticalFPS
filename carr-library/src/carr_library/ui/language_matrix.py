"""Canonical languages, passive evidence and scoped recorded human assessments."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..language_qualification import build_capability_matrix, load_catalog
from ..language_qualification.evidence import (
    EvidenceSnapshot, EvidenceStore, PersonProvenance, REPORT_SCHEMA,
    EvidenceSaveCommittedError, assessment_detail, import_observation,
)
from ..durable_state import atomic_bytes, encode, local_path, read_json_snapshot, regular
import json
import os


CAPABILITY_LABELS = {
    "ui_translation": "Interface translation", "menu_translation": "Menu translation",
    "help_translation": "Help translation", "unicode_text": "Unicode text",
    "rtl_layout": "Right-to-left layout", "vertical_layout": "Vertical layout",
    "ocr": "Optical character recognition", "tts": "Speech synthesis",
    "phonemization": "Phonemization", "stress": "Word stress", "tone": "Tone",
    "punctuation": "Punctuation", "segmentation": "Text segmentation",
    "lexical_retrieval": "Lexical retrieval", "semantic_retrieval": "Semantic retrieval",
    "translation": "Translation", "accessible_reading": "Accessible reading",
    "keyboard_input": "Keyboard input", "number_rules": "Number rules",
    "date_rules": "Date rules", "model_availability": "Model availability",
    "voice_availability": "Voice availability",
}
STATUS_LABELS = {
    "untested": "Untested", "unsupported": "No confirmed local support",
    "candidate": "Declared candidate", "partial": "Partial assessment",
    "verified": "Verified for the recorded configuration",
    "failed": "Reported failure or rejected assessment", "revoked": "Revoked assessment",
}


@dataclass(frozen=True)
class InstalledVoiceInventory:
    voices: tuple
    notices: tuple[str, ...]
    enumerated_at: str


def collect_installed_voices(library, *, cancelled=lambda: False):
    """Explicit local registry enumeration only; no synthesis or tag matching."""
    if cancelled():
        raise InterruptedError("Installed voice inventory cancelled.")
    from ..runtime_settings import voice_registry
    registry = voice_registry(Path(library.root) / "state")
    try:
        if cancelled():
            raise InterruptedError("Installed voice inventory cancelled after the bounded adapter probe.")
        voices = tuple(registry.voices())
        notices = tuple(registry.notices)
        if len(voices) > 10_000 or len(notices) > 1000 or any(not isinstance(item, str) or len(item) > 4000 for item in notices):
            raise ValueError("The local voice inventory exceeds its metadata limits.")
        return InstalledVoiceInventory(voices, notices, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    finally:
        close = getattr(registry, "close", None)
        if callable(close):
            close()


class LanguageMatrixController:
    """Constructing or filtering the canonical view never discovers voices."""

    def __init__(self, library_root=None, *, guard=lambda: None):
        self.catalog = load_catalog()
        self.matrix = build_capability_matrix(catalog=self.catalog)
        self.inventory = None
        self.selected_id = self.catalog.entries[0].id
        self.store = EvidenceStore(library_root, guard=guard) if library_root is not None else None
        self.evidence, self.evidence_token = EvidenceSnapshot(), None
        self.recorded_context = None
        self.evidence_loaded = False

    def filter(self, query="", *, region=None, capability=None, status=None):
        return self.matrix.search(query, region=region, capability=capability, status=status)

    def select(self, language_id):
        self.catalog.get(language_id)
        self.selected_id = language_id

    def selection_index(self, rows):
        return next((index for index, row in enumerate(rows) if row.language.id == self.selected_id), 0 if rows else -1)

    def set_inventory(self, inventory):
        if not isinstance(inventory, InstalledVoiceInventory):
            raise ValueError("Use an explicitly enumerated installed voice inventory.")
        # No bindings are guessed from names, ISO-like tags, countries or the
        # separate upstream inventory. All enumerated voices remain unmatched.
        if self.store is not None:
            self.store.guard()
        matrix = build_capability_matrix(catalog=self.catalog, voices=inventory.voices, bindings=(),
            evidence=self.evidence, recorded_context=self.recorded_context)
        self.inventory = inventory
        self.matrix = matrix

    def _require_store(self):
        if self.store is None:
            raise ValueError("Open language evidence from a current library.")
        self.store.guard()
        return self.store

    def load_evidence(self, *, cancelled=lambda: False):
        return self._require_store().load(cancelled=cancelled)

    def accept_evidence(self, result):
        snapshot, token = result
        if type(snapshot) is not EvidenceSnapshot:
            raise ValueError("Use an exact checked evidence snapshot.")
        if self.recorded_context not in {row.fingerprint for row in snapshot.contexts()}:
            self.recorded_context = None
        self.evidence, self.evidence_token = snapshot, token
        self.evidence_loaded = True
        self._rebuild()

    def _rebuild(self):
        self.matrix = build_capability_matrix(catalog=self.catalog,
            voices=self.inventory.voices if self.inventory is not None else (), bindings=(),
            evidence=self.evidence, recorded_context=self.recorded_context)

    def choose_configuration(self, identity):
        if self.store is not None:
            self.store.guard()
        if identity is not None and identity not in {row.fingerprint for row in self.evidence.contexts()}:
            raise ValueError("Choose one exact recorded configuration.")
        self.recorded_context = identity
        self._rebuild()

    def read_observation(self, path, *, cancelled=lambda: False):
        store = self._require_store()
        result = import_observation(path, cancelled=cancelled)
        store.guard()
        return result

    def evidence_details(self, identity):
        observation = self.evidence.get(identity)
        reviews = [(index, row) for index, row in enumerate(self.evidence.reviews, 1)
                   if row.observation_sha256 == identity]
        last = reviews[-1] if reviews else None
        detail = assessment_detail(observation, last[1] if last else None,
                                   bool(last and last[0] > self.evidence.dormant_through))
        return (self.catalog.get(observation.language_id).label_uk + " — "
                + CAPABILITY_LABELS[observation.capability] + "\n" + detail
                + "\n\nComplete passive observation\n"
                + json.dumps(observation.to_dict(), ensure_ascii=False, indent=2)
                + f"\n\nReview history: last {min(32, len(reviews))} of {len(reviews)}; "
                "the report retains the full history.\n" + json.dumps(
                    [{"sequence": index, "active_after_restore": index > self.evidence.dormant_through,
                      **asdict(row)} for index, row in reviews[-32:]], ensure_ascii=False, indent=2))

    def export_report(self, path, *, expected_token, cancelled=lambda: False):
        store = self._require_store()
        snapshot, token = store.load(cancelled=cancelled)
        if token != expected_token:
            raise ValueError("Language evidence changed. Reload before exporting its report.")
        context = self.recorded_context
        matrix = build_capability_matrix(catalog=self.catalog, evidence=snapshot,
                                          recorded_context=context)
        payload = encode({"schema": REPORT_SCHEMA, "qualification":
            "Recorded human assessments of exact configurations; no automatic execution, "
            "authenticated reviewer identity or current-device qualification.",
            "evidence": snapshot.to_dict(), "matrix": matrix.to_dict()})
        destination = local_path(path)
        if destination.is_relative_to(store.root):
            raise ValueError("Choose a new report outside the library's private state.")
        regular(local_path(destination.parent, exists=True), directory=True)
        store._check(cancelled)
        # No callback after this fresh exact-token check and before create-only write.
        if os.path.lexists(store.path):
            _, latest = read_json_snapshot(store.path)
        else:
            latest = None
        store._validate_scope()
        if latest != token:
            raise ValueError("Language evidence changed before report publication.")
        atomic_bytes(destination, payload, replace=False)
        return destination

    def summary(self):
        values = self.matrix.summary()
        local = "Not enumerated; use Refresh installed voices" if self.inventory is None else str(len(self.inventory.voices))
        return (
            f"Canonical selected entries: {values['selected_entries']}. "
            f"Qualified language entries: {values['qualified_entries']} "
            "(all 22 capabilities in the selected recorded configuration only).\n"
            f"Separate upstream inventory: {values['upstream_reported_resources']} resource entries; "
            f"Windows/NVDA acoustic tests reported there: {values['upstream_reported_acoustic_tests']}.\n"
            f"Voices enumerated on this installation: {local}. "
            "Local voices are not automatically assigned to the selected languages. "
            "Human assessments are recorded claims, not authenticated identities or current-device tests."
        )

    def details(self, qualification):
        entry = self.catalog.get(qualification.language.id)
        regions = {row.id: row.label_uk for row in self.catalog.regions}
        lines = [f"{entry.order}. {entry.label_uk}", "Selection identity: " + entry.scope,
                 "Canonical locale: not supplied by the source", "Canonical voice ID: not supplied by the source", "",
                 "Regions and countries"]
        for association in entry.associations:
            lines.append(regions[association.region_id] + ": " + ", ".join(association.countries))
        if entry.identity_notes:
            lines.extend(("", "Identity review notes", *entry.identity_notes))
        lines.extend(("", f"Capability evidence: {len(qualification.capabilities)} categories"))
        for capability in qualification.capabilities:
            lines.extend(("", CAPABILITY_LABELS.get(capability.name, capability.name.replace("_", " ")) + ": "
                          + STATUS_LABELS[capability.status], capability.detail))
        lines.extend(("", "Canonical source", self.catalog.source.url))
        for association in entry.associations:
            lines.extend((f"Source line {association.source_line}", association.source_text))
        return "\n".join(lines)

    def inventory_text(self):
        if self.inventory is None:
            return ("Installed voices have not been enumerated. Choose Refresh installed voices to inspect only the "
                    "configured local adapters. This view does not synthesize text or download a runtime or model.")
        lines = [f"Local inventory retrieved: {self.inventory.enumerated_at}",
                 f"Enumerated voices: {len(self.inventory.voices)}.",
                 "Adapter declarations only. Pronunciation, language coverage and NVDA listening are not qualified by enumeration.",
                 "No automatic language bindings were made."]
        for number, voice in enumerate(self.inventory.voices, 1):
            lines.extend(("", f"{number}. {voice.name}", f"Engine: {voice.engine}",
                          f"Adapter-declared language: {voice.language}",
                          "Declared controls: " + (", ".join(voice.capabilities) or "None"),
                          "License notice: " + voice.license))
        if self.inventory.notices:
            lines.extend(("", "Adapter notices", *self.inventory.notices))
        if not self.inventory.voices:
            lines.extend(("", "No voices were returned by the configured local adapters."))
        return "\n".join(lines)


def request_voice_inventory(frame, controller, *, completed, failed):
    """Schedule only an explicitly requested inventory using the frame worker."""
    if getattr(frame, "_closing", False):
        raise ValueError("The application is closing.")
    if getattr(frame, "working", False) or getattr(frame, "busy", False):
        raise ValueError("Wait for the current operation or cancel it first.")
    library = frame.library

    def done(inventory):
        if getattr(frame, "_closing", False) or frame.library is not library:
            raise ValueError("The library folder changed while local voices were enumerated. Refresh again.")
        controller.set_inventory(inventory)
        completed(inventory)

    frame.run_job("Enumerating configured local voices", lambda: collect_installed_voices(
        library, cancelled=frame.cancel_event.is_set), done, failed)


class EvidenceDeliveryScopeError(ValueError):
    """Retain an old-library outcome without applying it to the current view."""
    def __init__(self, scope_error, *, completed_result=None, original_error=None):
        self.scope_error = scope_error
        self.completed_result = completed_result
        self.original_error = original_error
        if isinstance(original_error, EvidenceSaveCommittedError):
            detail = "A change was saved in the original library; its receipt is retained. "
        elif completed_result is not None:
            detail = "The completed operation result for the original library is retained. "
        else:
            detail = "The original operation error is retained. "
        super().__init__("The library changed before the language evidence outcome could be shown. "
                         + detail + "Reload that library before repeating a change.")


def request_evidence_operation(frame, controller, operation, *, completed, failed):
    """Use the existing worker and exact current-library guard for passive I/O."""
    if getattr(frame, "_closing", False):
        raise ValueError("The application is closing.")
    if getattr(frame, "working", False) or getattr(frame, "busy", False):
        raise ValueError("Wait for the current operation or cancel it first.")
    library = frame.library
    root_spelling = str(library.root)
    store = controller._require_store()

    def check():
        if (getattr(frame, "_closing", False) or frame.library is not library
                or str(library.root) != root_spelling):
            raise ValueError("The library changed during language evidence work.")
        store.guard()

    def work():
        check()
        return operation(frame.cancel_event.is_set)

    def done(result):
        try:
            check()
        except Exception as scope_error:
            raise EvidenceDeliveryScopeError(scope_error, completed_result=result) from scope_error
        completed(result)

    def failure(error):
        if isinstance(error, EvidenceDeliveryScopeError):
            failed(error)
            return
        try:
            check()
        except Exception as scope_error:
            retained = EvidenceDeliveryScopeError(scope_error, original_error=error)
            retained.__cause__ = error
            failed(retained)
            return
        failed(error)

    frame.run_job("Language qualification evidence", work, done, failure)


def _native_dialog_type():
    import wx

    def labelled(parent, layout, label, control, proportion=0):
        layout.Add(wx.StaticText(parent, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 7)
        control.SetName(label.replace("&", ""))
        layout.Add(control, proportion, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 7)
        return control

    class LanguageMatrixDialog(wx.Dialog):
        def __init__(self, parent, controller):
            super().__init__(parent, title="Languages and capability evidence", size=(960, 760),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.controller = controller
            self.rows = ()
            self._alive = True
            self.pending = False
            layout = wx.BoxSizer(wx.VERTICAL)
            self.summary = labelled(self, layout, "Selection, upstream report and local evidence", wx.TextCtrl(self,
                value=controller.summary(), style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 85)))
            self.tabs = wx.Notebook(self)
            catalog_page = wx.Panel(self.tabs)
            self.tabs.AddPage(catalog_page, "Selected languages and capabilities")
            catalog_layout = wx.BoxSizer(wx.VERTICAL)
            self.search = labelled(catalog_page, catalog_layout, "&Search selected languages or countries", wx.TextCtrl(catalog_page,
                style=wx.TE_PROCESS_ENTER))
            filters = wx.BoxSizer(wx.HORIZONTAL)
            filters.Add(wx.StaticText(catalog_page, label="&Region"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            self.regions = wx.Choice(catalog_page, choices=["All regions"] + [row.label_uk for row in controller.catalog.regions])
            self.regions.SetName("Region filter")
            self.regions.SetSelection(0)
            filters.Add(self.regions, 1, wx.ALL, 5)
            filters.Add(wx.StaticText(catalog_page, label="&Capability"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            self.capability_ids = tuple(row.name for row in controller.matrix.entries[0].capabilities)
            self.capabilities = wx.Choice(catalog_page, choices=["All capabilities"] + [CAPABILITY_LABELS[key] for key in self.capability_ids])
            self.capabilities.SetName("Capability filter")
            self.capabilities.SetSelection(0)
            filters.Add(self.capabilities, 1, wx.ALL, 5)
            filters.Add(wx.StaticText(catalog_page, label="&Status"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            self.status_ids = tuple(STATUS_LABELS)
            self.status_filter = wx.Choice(catalog_page, choices=["All statuses"] + list(STATUS_LABELS.values()))
            self.status_filter.SetName("Evidence status filter")
            self.status_filter.SetSelection(0)
            filters.Add(self.status_filter, 1, wx.ALL, 5)
            catalog_layout.Add(filters, 0, wx.EXPAND)
            columns = wx.BoxSizer(wx.HORIZONTAL)
            left, right = wx.BoxSizer(wx.VERTICAL), wx.BoxSizer(wx.VERTICAL)
            self.languages = labelled(catalog_page, left, "Languages in canonical order", wx.ListBox(catalog_page), 1)
            self.details = labelled(catalog_page, right, "Selected language and all capability evidence", wx.TextCtrl(catalog_page,
                style=wx.TE_READONLY | wx.TE_MULTILINE), 1)
            columns.Add(left, 1, wx.EXPAND)
            columns.Add(right, 2, wx.EXPAND)
            catalog_layout.Add(columns, 1, wx.EXPAND)
            catalog_page.SetSizer(catalog_layout)
            inventory_page = wx.Panel(self.tabs)
            self.tabs.AddPage(inventory_page, "Installed voice inventory")
            inventory_layout = wx.BoxSizer(wx.VERTICAL)
            self.inventory = labelled(inventory_page, inventory_layout, "Separately enumerated local voices and adapter notices",
                wx.TextCtrl(inventory_page, value=controller.inventory_text(), style=wx.TE_READONLY | wx.TE_MULTILINE), 1)
            inventory_page.SetSizer(inventory_layout)
            evidence_page = wx.Panel(self.tabs)
            self.tabs.AddPage(evidence_page, "Recorded qualification evidence")
            evidence_layout = wx.BoxSizer(wx.VERTICAL)
            self.contexts = labelled(evidence_page, evidence_layout,
                "Recorded application and environment; does not inspect this device", wx.Choice(evidence_page))
            self.contexts.Bind(wx.EVT_CHOICE, self.choose_context)
            self.evidence_rows = ()
            self.evidence_list = labelled(evidence_page, evidence_layout,
                "Observations for the selected language", wx.ListBox(evidence_page), 1)
            self.evidence_list.Bind(wx.EVT_LISTBOX, self.select_evidence)
            self.evidence_text = labelled(evidence_page, evidence_layout,
                "Exact observation, configuration and recorded review history",
                wx.TextCtrl(evidence_page, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 220)), 2)
            evidence_buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.load_button = wx.Button(evidence_page, label="&Load recorded evidence")
            self.import_button = wx.Button(evidence_page, label="&Import passive observation...")
            self.review_button = wx.Button(evidence_page, label="Record human &assessment...")
            self.export_button = wx.Button(evidence_page, label="Export evidence &report...")
            for button, callback in ((self.load_button, self.load_evidence),
                    (self.import_button, self.import_evidence), (self.review_button, self.record_review),
                    (self.export_button, self.export_evidence)):
                button.SetName(button.GetLabel().replace("&", ""))
                button.Bind(wx.EVT_BUTTON, callback)
                evidence_buttons.Add(button, 0, wx.ALL, 5)
            evidence_layout.Add(evidence_buttons, 0, wx.EXPAND)
            evidence_page.SetSizer(evidence_layout)
            layout.Add(self.tabs, 1, wx.EXPAND | wx.ALL, 6)
            self.status = labelled(self, layout, "View status", wx.TextCtrl(self, style=wx.TE_READONLY))
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.refresh_button = wx.Button(self, label="&Refresh installed voices")
            self.refresh_button.SetName("Explicitly refresh configured installed voices")
            self.refresh_button.Bind(wx.EVT_BUTTON, self.refresh)
            buttons.Add(self.refresh_button, 0, wx.ALL, 6)
            self.cancel_button = wx.Button(self, label="Cancel &voice scan")
            self.cancel_button.Disable()
            self.cancel_button.Bind(wx.EVT_BUTTON, self.cancel)
            buttons.Add(self.cancel_button, 0, wx.ALL, 6)
            close = wx.Button(self, wx.ID_CANCEL, "C&lose")
            close.Bind(wx.EVT_BUTTON, self.close)
            buttons.Add(close, 0, wx.ALL, 6)
            layout.Add(buttons, 0, wx.ALIGN_RIGHT)
            self.SetSizer(layout)
            self.SetMinSize((740, 540))
            self.SetEscapeId(wx.ID_CANCEL)
            self.Bind(wx.EVT_CLOSE, self.close)
            self.search.Bind(wx.EVT_TEXT, self.filter)
            self.search.Bind(wx.EVT_TEXT_ENTER, self.filter)
            for control in (self.regions, self.capabilities, self.status_filter):
                control.Bind(wx.EVT_CHOICE, self.filter)
            self.languages.Bind(wx.EVT_LISTBOX, self.select)
            self.update_evidence()
            self.filter()
            self.search.SetFocus()
            if controller.store is not None:
                wx.CallAfter(self.load_evidence)

        def filter(self, event=None):
            try:
                r, c, s = self.regions.GetSelection(), self.capabilities.GetSelection(), self.status_filter.GetSelection()
                self.rows = self.controller.filter(self.search.GetValue(),
                    region=self.controller.catalog.regions[r - 1].id if r > 0 else None,
                    capability=self.capability_ids[c - 1] if c > 0 else None,
                    status=self.status_ids[s - 1] if s > 0 else None)
                self.languages.Set([f"{row.language.order}. {row.language.label_uk}" for row in self.rows])
                index = self.controller.selection_index(self.rows)
                if index >= 0:
                    self.languages.SetSelection(index)
                    self.details.ChangeValue(self.controller.details(self.rows[index]))
                    self.details.SetInsertionPoint(0)
                else:
                    self.details.ChangeValue("No selected entries match these filters. No language or voice substitution is made.")
                self.status.ChangeValue(f"{len(self.rows)} of {len(self.controller.catalog.entries)} selected entries shown. "
                    "Canonical order retained. Ctrl+Tab changes sections.")
                self.update_evidence()
                # Deliberately do not call SetFocus here: typing, region
                # selection and repeated filter updates keep their input focus.
            except Exception as error:
                self.status.ChangeValue("Could not apply language filters. " + str(error))

        def select(self, event=None):
            index = self.languages.GetSelection()
            if 0 <= index < len(self.rows):
                self.controller.select(self.rows[index].language.id)
                self.details.ChangeValue(self.controller.details(self.rows[index]))
                self.details.SetInsertionPoint(0)
                self.update_evidence()

        def refresh(self, event=None):
            if self.pending:
                return
            self._set_pending(True)
            self.status.ChangeValue("Enumerating configured local adapters. The canonical language selection is unchanged.")

            def completed(inventory):
                if not self._alive:
                    return
                self._set_pending(False)
                self.summary.ChangeValue(self.controller.summary())
                self.inventory.ChangeValue(self.controller.inventory_text())
                self.inventory.SetInsertionPoint(0)
                self.filter()
                self.status.ChangeValue(f"Local inventory updated: {len(inventory.voices)} voices. "
                    "Use Ctrl+Tab for details. Enumeration adds no qualification or exact language binding.")

            def failed(error):
                if self._alive:
                    self._set_pending(False)
                    self.status.ChangeValue("Voice inventory was not updated. " + str(error))

            try:
                request_voice_inventory(self.GetParent(), self.controller, completed=completed, failed=failed)
            except Exception as error:
                failed(error)

        def _set_pending(self, value):
            self.pending = value
            self.refresh_button.Enable(not value)
            self.cancel_button.Enable(value)
            usable = self.controller.store is not None and not value
            self.load_button.Enable(usable)
            self.contexts.Enable(usable and self.controller.evidence_loaded)
            for button in (self.import_button, self.review_button, self.export_button):
                button.Enable(usable and self.controller.evidence_loaded)

        def _run_evidence(self, operation, completed):
            if self.pending:
                return
            self._set_pending(True)

            def done(result):
                if not self._alive:
                    return
                self._set_pending(False)
                completed(result)

            def failed(error):
                if self._alive:
                    self._set_pending(False)
                    if isinstance(error, EvidenceSaveCommittedError):
                        self.controller.accept_evidence((error.snapshot, error.token))
                        self.update_evidence()
                        self.filter()
                    self.status.ChangeValue(str(error))

            try:
                request_evidence_operation(self.GetParent(), self.controller, operation,
                                           completed=done, failed=failed)
            except Exception as error:
                failed(error)

        def update_evidence(self):
            controller = self.controller
            contexts = controller.evidence.contexts()
            self.context_ids = (None,) + tuple(row.fingerprint for row in contexts)
            self.contexts.Set(["No recorded configuration selected"] + [
                row.application.name + " " + row.application.version + " | " + row.platform
                + " | " + row.fingerprint[:16] for row in contexts])
            self.contexts.SetSelection(self.context_ids.index(controller.recorded_context))
            selected = self.languages.GetSelection()
            language_id = self.rows[selected].language.id if 0 <= selected < len(self.rows) else None
            self.evidence_rows = tuple(row for row in controller.evidence.observations
                                       if row.language_id == language_id)
            self.evidence_list.Set([
                CAPABILITY_LABELS[row.capability] + " | " + row.language_tag
                + " | reported " + row.test.outcome + " | " + row.fingerprint[:16]
                for row in self.evidence_rows])
            if self.evidence_rows:
                self.evidence_list.SetSelection(0)
                self.select_evidence()
            else:
                self.evidence_text.ChangeValue("No saved observation for this selected language. "
                    "Importing a claim cannot verify it. Reviewer identities are recorded assertions.")
            self.summary.ChangeValue(controller.summary())
            self._set_pending(self.pending)

        def choose_context(self, event=None):
            try:
                index = self.contexts.GetSelection()
                if not 0 <= index < len(self.context_ids):
                    raise ValueError("Choose an exact recorded configuration.")
                self.controller.choose_configuration(self.context_ids[index])
                self.filter()
                self.summary.ChangeValue(self.controller.summary())
            except Exception as error:
                self.status.ChangeValue(str(error))

        def select_evidence(self, event=None):
            index = self.evidence_list.GetSelection()
            if 0 <= index < len(self.evidence_rows):
                self.evidence_text.ChangeValue(
                    self.controller.evidence_details(self.evidence_rows[index].fingerprint))
                self.evidence_text.SetInsertionPoint(0)

        def _accepted_evidence(self, result):
            self.controller.accept_evidence(result)
            self.update_evidence()
            self.filter()
            self.status.ChangeValue("Recorded evidence loaded. Choose its exact application/environment "
                "to view assessments; this does not qualify the current device.")

        def load_evidence(self, event=None):
            self._run_evidence(lambda cancelled: self.controller.load_evidence(cancelled=cancelled),
                               self._accepted_evidence)

        def import_evidence(self, event=None):
            if self.pending or not self.controller.evidence_loaded:
                return
            token = self.controller.evidence_token
            picker = wx.FileDialog(self, "Choose one passive qualification observation",
                wildcard="JSON observation (*.json)|*.json", style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            try:
                if picker.ShowModal() != wx.ID_OK:
                    return
                path = picker.GetPath()
                self.controller._require_store()
            finally:
                picker.Destroy()

            def read(result):
                observation, source_digest = result
                question = wx.MessageDialog(self,
                    "Save this passive observation?\n\n"
                    + self.controller.catalog.get(observation.language_id).label_uk + " — "
                    + CAPABILITY_LABELS[observation.capability] + "\nReported outcome: "
                    + observation.test.outcome + "\nObservation SHA-256: " + observation.fingerprint
                    + "\nImported file SHA-256: " + source_digest
                    + "\n\nThis action records a claim only. It does not verify a capability.",
                    "Import passive language observation", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
                try:
                    if question.ShowModal() != wx.ID_YES:
                        return
                    store = self.controller._require_store()
                finally:
                    question.Destroy()
                self._run_evidence(lambda cancelled: store.add(observation, expected_token=token,
                    cancelled=cancelled), self._accepted_evidence)

            self._run_evidence(lambda cancelled: self.controller.read_observation(path, cancelled=cancelled), read)

        def record_review(self, event=None):
            if self.pending or not self.controller.evidence_loaded:
                return
            index = self.evidence_list.GetSelection()
            if not 0 <= index < len(self.evidence_rows):
                self.status.ChangeValue("Select the exact saved observation first.")
                return
            observation = self.evidence_rows[index]
            token = self.controller.evidence_token
            dialog = wx.Dialog(self, title="Record a human language assessment", size=(900, 820),
                               style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            layout = wx.BoxSizer(wx.VERTICAL)
            labelled(dialog, layout, "Exact observation and prior reviews; no current-device test",
                wx.TextCtrl(dialog, value=self.controller.evidence_details(observation.fingerprint),
                            style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 250)), 1)
            fields = {}
            for name, label in (("identity", "Reviewer identity"), ("source", "Reviewer provenance reference"),
                                ("role", "Reviewer role"), ("statement", "Assessment and limitations")):
                fields[name] = labelled(dialog, layout, label,
                    wx.TextCtrl(dialog, style=wx.TE_MULTILINE if name == "statement" else 0))
            decision = labelled(dialog, layout, "Recorded decision", wx.Choice(dialog,
                choices=["Accept recorded test assessment", "Reject assessment", "Revoke assessment"]))
            decision.SetSelection(0)
            independent = wx.CheckBox(dialog, label="I record an independent assessment; identities are not authenticated")
            exact = wx.CheckBox(dialog, label="This decision concerns the exact observation SHA-256 displayed above")
            independent.SetName("Recorded independent assessment acknowledgement")
            exact.SetName("Confirm the exact displayed observation")
            layout.Add(independent, 0, wx.ALL, 7)
            layout.Add(exact, 0, wx.ALL, 7)
            layout.Add(dialog.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALIGN_RIGHT | wx.ALL, 7)
            dialog.SetSizer(layout)
            dialog.SetEscapeId(wx.ID_CANCEL)
            try:
                self.controller._require_store()
                if dialog.ShowModal() != wx.ID_OK:
                    return
                if not exact.GetValue():
                    raise ValueError("Confirm the exact displayed observation before recording an assessment.")
                choice = decision.GetSelection()
                if not 0 <= choice < 3:
                    raise ValueError("Choose a recorded review decision.")
                selected = ("accept", "reject", "revoke")[choice]
                reviewer = PersonProvenance(fields["identity"].GetValue(),
                    fields["source"].GetValue(), fields["role"].GetValue())
                statement = fields["statement"].GetValue()
                acknowledged = bool(independent.GetValue())
                store = self.controller._require_store()
            except Exception as error:
                self.status.ChangeValue(str(error))
                return
            finally:
                dialog.Destroy()
            self._run_evidence(lambda cancelled: store.review(observation.fingerprint, reviewer,
                decision=selected, independent_assessment=acknowledged, statement=statement,
                confirmation=selected.upper() + " " + observation.fingerprint,
                expected_token=token, cancelled=cancelled), self._accepted_evidence)

        def export_evidence(self, event=None):
            if self.pending or not self.controller.evidence_loaded:
                return
            token = self.controller.evidence_token
            picker = wx.FileDialog(self, "Create a new passive evidence report outside the library",
                wildcard="JSON report (*.json)|*.json", style=wx.FD_SAVE)
            try:
                if picker.ShowModal() != wx.ID_OK:
                    return
                path = picker.GetPath()
                self.controller._require_store()
            finally:
                picker.Destroy()
            self._run_evidence(lambda cancelled: self.controller.export_report(path,
                expected_token=token, cancelled=cancelled),
                lambda destination: self.status.ChangeValue(
                    "Passive evidence report created. It cannot be imported as review authority."))

        def cancel(self, event=None):
            if self.pending:
                self.GetParent().cancel_event.set()
                self.status.ChangeValue("Cancellation requested. Current bounded work will settle before another operation.")

        def close(self, event=None):
            self.shutdown()
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)

        def shutdown(self):
            self._alive = False
            if self.pending:
                self.GetParent().cancel_event.set()

    return LanguageMatrixDialog


def show_language_matrix(frame):
    dialog = None
    try:
        if getattr(frame, "_closing", False):
            raise ValueError("The application is closing.")
        library = frame.library
        root_spelling = str(library.root)

        def guard():
            if (getattr(frame, "_closing", False) or frame.library is not library
                    or str(library.root) != root_spelling):
                raise ValueError("The selected library changed. Reopen language evidence.")

        controller = LanguageMatrixController(library.root, guard=guard)
        dialog = _native_dialog_type()(frame, controller)
        dialog.ShowModal()
    except Exception as error:
        frame.report("Language evidence view could not open. " + str(error))
    finally:
        if dialog is not None:
            dialog.shutdown()
            dialog.Destroy()


def install_language_actions(frame):
    """Add the existing canonical matrix and passive evidence workflow to Voice."""
    if getattr(frame, "_carr_language_actions_installed", False):
        return
    import wx
    bar = frame.GetMenuBar()
    if bar is None:
        raise ValueError("Install the native menu bar before language actions.")
    menu = None
    for title in ("Voice", "Help"):
        for index in range(bar.GetMenuCount()):
            if bar.GetMenuLabel(index).replace("&", "") == title:
                menu = bar.GetMenu(index)
                break
        if menu is not None:
            break
    if menu is None:
        menu = wx.Menu()
        bar.Append(menu, "Languages")
    elif menu.GetMenuItemCount():
        menu.AppendSeparator()
    item = menu.Append(wx.ID_ANY, "Languages and capability evidence...")
    frame.Bind(wx.EVT_MENU, lambda event: show_language_matrix(frame), item)
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    register_menu_command(frame, CommandSpec("voice.languages", "Languages and capability evidence"), item)
    frame._carr_language_actions_installed = True
