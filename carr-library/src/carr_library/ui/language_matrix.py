"""Read-only canonical languages, capability evidence and separate local voices."""
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..language_qualification import build_capability_matrix, load_catalog


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
    "candidate": "Declared candidate", "partial": "Partial candidate", "verified": "Verified",
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
    registry = voice_registry(Path(library.root) / "state", cancelled=cancelled)
    try:
        if cancelled():
            raise InterruptedError("Installed voice inventory cancelled after the bounded adapter probe.")
        voices = tuple(registry.voices())
        notices = tuple(registry.notices)
        if len(voices) > 10_000 or len(notices) > 1000 or any(not isinstance(item, str) or len(item) > 4000 for item in notices):
            raise ValueError("The local voice inventory exceeds its metadata limits.")
        return InstalledVoiceInventory(voices, notices, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    finally:
        registry.close()


class LanguageMatrixController:
    """Constructing or filtering the canonical view never discovers voices."""

    def __init__(self):
        self.catalog = load_catalog()
        self.matrix = build_capability_matrix(catalog=self.catalog)
        self.inventory = None
        self.selected_id = self.catalog.entries[0].id

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
        matrix = build_capability_matrix(catalog=self.catalog, voices=inventory.voices, bindings=())
        self.inventory = inventory
        self.matrix = matrix

    def summary(self):
        values = self.matrix.summary()
        local = "Not enumerated; use Refresh installed voices" if self.inventory is None else str(len(self.inventory.voices))
        return (
            f"Canonical selected entries: {values['selected_entries']}. "
            f"Qualified language entries: {values['qualified_entries']}.\n"
            f"Separate upstream inventory: {values['upstream_reported_resources']} resource entries; "
            f"Windows/NVDA acoustic tests reported there: {values['upstream_reported_acoustic_tests']}.\n"
            f"Voices enumerated on this installation: {local}. "
            "Local voices are not automatically assigned to the selected languages."
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
            self.filter()
            self.search.SetFocus()

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

        def refresh(self, event=None):
            if self.pending:
                return
            self.pending = True
            self.refresh_button.Disable()
            self.cancel_button.Enable()
            self.status.ChangeValue("Enumerating configured local adapters. The canonical language selection is unchanged.")

            def completed(inventory):
                if not self._alive:
                    return
                self.pending = False
                self.refresh_button.Enable()
                self.cancel_button.Disable()
                self.summary.ChangeValue(self.controller.summary())
                self.inventory.ChangeValue(self.controller.inventory_text())
                self.inventory.SetInsertionPoint(0)
                self.filter()
                self.status.ChangeValue(f"Local inventory updated: {len(inventory.voices)} voices. "
                    "Use Ctrl+Tab for details. Qualified language entries remain zero; no exact bindings or acoustic tests were added.")

            def failed(error):
                if self._alive:
                    self.pending = False
                    self.refresh_button.Enable()
                    self.cancel_button.Disable()
                    self.status.ChangeValue("Voice inventory was not updated. " + str(error))

            try:
                request_voice_inventory(self.GetParent(), self.controller, completed=completed, failed=failed)
            except Exception as error:
                failed(error)

        def cancel(self, event=None):
            if self.pending:
                self.GetParent().cancel_event.set()
                self.status.ChangeValue("Cancellation requested. The current bounded adapter probe must finish or reach its time limit.")

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
        controller = LanguageMatrixController()
        dialog = _native_dialog_type()(frame, controller)
        dialog.ShowModal()
    except Exception as error:
        frame.report("Language evidence view could not open. " + str(error))
    finally:
        if dialog is not None:
            dialog.shutdown()
            dialog.Destroy()


def install_language_actions(frame):
    """Add the read-only evidence command to the native Voice menu."""
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
    frame._carr_language_actions_installed = True
