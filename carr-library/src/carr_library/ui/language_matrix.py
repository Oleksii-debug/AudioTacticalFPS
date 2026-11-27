"""Read-only canonical languages and separately observed voice/OCR resources."""
from pathlib import Path
from threading import Event

from ..language_qualification import build_capability_matrix, load_catalog
from ..language_qualification.runtime_inventory import (
    InstalledOcrInventory, InstalledVoiceInventory, collect_installed_ocr,
    collect_installed_voices,
)


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


class LanguageMatrixController:
    """Constructing or filtering the canonical view never discovers voices."""

    def __init__(self):
        self.catalog = load_catalog()
        self.matrix = build_capability_matrix(catalog=self.catalog)
        self.inventory = None
        self.ocr_inventory = None
        self._inventory_generation = 0
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
        if (type(inventory.voices) is not tuple or type(inventory.notices) is not tuple
                or len(inventory.notices) > 1000
                or any(not isinstance(item, str) or len(item) > 4300 for item in inventory.notices)
                or not isinstance(inventory.enumerated_at, str) or len(inventory.enumerated_at) > 80):
            raise ValueError("Use a bounded immutable installed voice snapshot.")
        # No bindings are guessed from names, ISO-like tags, countries or the
        # separate upstream inventory. All enumerated voices remain unmatched.
        matrix = build_capability_matrix(catalog=self.catalog, voices=inventory.voices, bindings=())
        self.inventory = inventory
        self.matrix = matrix

    def set_ocr_inventory(self, inventory):
        if not isinstance(inventory, InstalledOcrInventory) or inventory.profile != 'installed':
            raise ValueError('Use an explicitly enumerated installed OCR inventory.')
        from .ocr_selection import selectable_models
        if (type(inventory.identifiers) is not tuple or type(inventory.text_models) is not tuple
                or not isinstance(inventory.enumerated_at, str) or len(inventory.enumerated_at) > 80
                or selectable_models('installed', inventory.identifiers) != inventory.text_models):
            raise ValueError('The OCR inventory labels do not match the existing model catalogue.')
        self.ocr_inventory = inventory

    def summary(self):
        values = self.matrix.summary()
        local = "Not enumerated; use Refresh installed voices" if self.inventory is None else str(len(self.inventory.voices))
        return (
            f"Canonical selected entries: {values['selected_entries']}. "
            f"Qualified language entries: {values['qualified_entries']}.\n"
            f"Separate upstream inventory: {values['upstream_reported_resources']} resource entries; "
            f"Windows/NVDA acoustic tests reported there: {values['upstream_reported_acoustic_tests']}.\n"
            f"Voices enumerated on this installation: {local}. "
            "Local voices are not automatically assigned to the selected languages.\n"
            + ('Installed OCR models: not enumerated.' if self.ocr_inventory is None else
               f'Installed OCR model identifiers: {len(self.ocr_inventory.identifiers)}; '
               'no OCR language qualification is inferred.')
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
                          "Adapter-declared languages: " + (', '.join(voice.languages) or 'None'),
                          "Adapter availability: " + ('Available' if voice.available else 'Unavailable'),
                          "Adapter revision: " + (voice.revision or 'Not supplied'),
                          "Declared controls: " + (", ".join(voice.controls) or "None"),
                          "License notice: " + voice.license))
            if not voice.available and voice.reason:
                lines.append('Availability detail: ' + voice.reason)
        if self.inventory.notices:
            lines.extend(("", "Adapter notices", *self.inventory.notices))
        if not self.inventory.voices:
            lines.extend(("", "No voices were returned by the configured local adapters."))
        return "\n".join(lines)

    def ocr_inventory_text(self):
        if self.ocr_inventory is None:
            return ('Installed OCR models have not been enumerated. Choose Refresh installed OCR models '
                    'to inspect the current local engine through the existing OCR picker. No OCR is run, '
                    'no model is installed and no language is inferred from an OCR model identifier.')
        inventory = self.ocr_inventory
        lines = [f'Local OCR inventory retrieved: {inventory.enumerated_at}',
                 f'Installed model identifiers: {len(inventory.identifiers)}.',
                 'Model and script names come from the existing OCR model catalogue.',
                 'Enumeration does not qualify recognition accuracy, layout or a selected language.',
                 'Each actual import verifies its selected runtime and model bytes separately.', '']
        lines.extend(label for _identifier, label in inventory.text_models)
        text_ids = {identifier for identifier, _label in inventory.text_models}
        auxiliary = tuple(identifier for identifier in inventory.identifiers if identifier not in text_ids)
        if auxiliary:
            lines.extend(('', 'Auxiliary models, not recognition languages: ' + ', '.join(auxiliary)))
        if not inventory.identifiers:
            lines.append('No models were returned by the current OCR runtime.')
        return '\n'.join(lines)


def _host_scope(frame):
    """Capture Python owner state and the data-folder inode without GUI calls."""
    library = frame.library
    root = Path(library.root).absolute()
    info = root.stat()
    identity = info.st_dev, info.st_ino

    def current():
        try:
            now = root.stat()
            return (not getattr(frame, '_closing', False)
                    and getattr(frame, 'library', None) is library
                    and Path(library.root).absolute() == root
                    and (now.st_dev, now.st_ino) == identity)
        except (OSError, ValueError, AttributeError):
            return False
    return library, current


class InventoryRequest:
    """Cancellation belongs to this scan, not a later host operation."""
    def __init__(self, frame, controller, *, alive):
        self.library, self.scope_current = _host_scope(frame)
        self._host_cancel = frame.cancel_event
        self._cancel = Event()
        self._alive = alive
        self._controller = controller
        controller._inventory_generation += 1
        self._generation = controller._inventory_generation

    def cancel(self):
        self._cancel.set()

    def cancelled(self):
        if (self._cancel.is_set() or self._host_cancel.is_set() or not self._alive()
                or not self.scope_current()
                or self._controller._inventory_generation != self._generation):
            self._cancel.set()
        return self._cancel.is_set()

    def check(self):
        if self.cancelled():
            raise InterruptedError('Local inventory discarded because it was cancelled or its window or library changed.')


def request_inventory(frame, controller, *, kind, completed, failed, alive=lambda: True,
                      registry=None):
    """Schedule an explicit root-bound scan on the existing host worker."""
    if kind not in ('voices', 'ocr'):
        raise ValueError('Choose the local voice or OCR inventory.')
    if getattr(frame, '_closing', False):
        raise ValueError('The application is closing.')
    if getattr(frame, 'working', False) or getattr(frame, 'busy', False):
        raise ValueError('Wait for the current operation or cancel it first.')
    request = InventoryRequest(frame, controller, alive=alive)

    def operation():
        request.check()
        result = (collect_installed_voices(request.library, registry=registry, cancelled=request.cancelled)
                  if kind == 'voices' else collect_installed_ocr(cancelled=request.cancelled))
        request.check()
        return result

    def done(inventory):
        request.check()
        if kind == 'voices':
            controller.set_inventory(inventory)
        else:
            controller.set_ocr_inventory(inventory)
        completed(inventory)

    def error(exc):
        if (alive() and request.scope_current()
                and controller._inventory_generation == request._generation):
            failed(exc)

    frame.run_job('Enumerating configured local ' + ('voices' if kind == 'voices' else 'OCR models'),
                  operation, done, error)
    return request


def request_voice_inventory(frame, controller, *, completed, failed, alive=lambda: True, registry=None):
    """Schedule only an explicitly requested inventory using the frame worker."""
    return request_inventory(frame, controller, kind='voices', completed=completed,
                             failed=failed, alive=alive, registry=registry)


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
            # Retain the native identity while the derived window is alive.
            # A destruction event can expose a different Python wrapper.
            self._window_id = self.GetId()
            self.controller = controller
            self.rows = ()
            self._alive = True
            self._window_alive = True
            self.request = None
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
            ocr_page = wx.Panel(self.tabs)
            self.tabs.AddPage(ocr_page, "Installed OCR model inventory")
            ocr_layout = wx.BoxSizer(wx.VERTICAL)
            self.ocr_inventory = labelled(ocr_page, ocr_layout, "Separately enumerated OCR models and scripts",
                wx.TextCtrl(ocr_page, value=controller.ocr_inventory_text(), style=wx.TE_READONLY | wx.TE_MULTILINE), 1)
            ocr_page.SetSizer(ocr_layout)
            layout.Add(self.tabs, 1, wx.EXPAND | wx.ALL, 6)
            self.status = labelled(self, layout, "View status", wx.TextCtrl(self, style=wx.TE_READONLY))
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.refresh_button = wx.Button(self, label="&Refresh installed voices")
            self.refresh_button.SetName("Explicitly refresh configured installed voices")
            self.refresh_button.Bind(wx.EVT_BUTTON, self.refresh)
            buttons.Add(self.refresh_button, 0, wx.ALL, 6)
            self.ocr_button = wx.Button(self, label="Refresh installed &OCR models")
            self.ocr_button.SetName("Explicitly refresh installed OCR models")
            self.ocr_button.Bind(wx.EVT_BUTTON, lambda event: self.refresh(event, kind='ocr'))
            buttons.Add(self.ocr_button, 0, wx.ALL, 6)
            self.cancel_button = wx.Button(self, label="Cancel &resource scan")
            self.cancel_button.SetName("Cancel current resource scan")
            self.cancel_button.Disable()
            self.cancel_button.Bind(wx.EVT_BUTTON, self.cancel)
            buttons.Add(self.cancel_button, 0, wx.ALL, 6)
            close = wx.Button(self, wx.ID_CANCEL, "C&lose")
            close.SetName("Close language evidence view")
            close.Bind(wx.EVT_BUTTON, self.close)
            buttons.Add(close, 0, wx.ALL, 6)
            layout.Add(buttons, 0, wx.ALIGN_RIGHT)
            self.SetSizer(layout)
            self.SetMinSize((740, 540))
            self.SetEscapeId(wx.ID_CANCEL)
            self.Bind(wx.EVT_CLOSE, self.close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.destroyed)
            self.search.Bind(wx.EVT_TEXT, self.filter)
            self.search.Bind(wx.EVT_TEXT_ENTER, self.filter)
            for control in (self.regions, self.capabilities, self.status_filter):
                control.Bind(wx.EVT_CHOICE, self.filter)
            self.languages.Bind(wx.EVT_LISTBOX, self.select)
            self.filter()
            self.search.SetFocus()

        def filter(self, event=None):
            if not self._alive or not self._window_alive:
                return
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
            if not self._alive or not self._window_alive:
                return
            index = self.languages.GetSelection()
            if 0 <= index < len(self.rows):
                self.controller.select(self.rows[index].language.id)
                self.details.ChangeValue(self.controller.details(self.rows[index]))
                self.details.SetInsertionPoint(0)

        def refresh(self, event=None, *, kind='voices'):
            if not self._alive or not self._window_alive or self.pending:
                return
            self.pending = True
            self.refresh_button.Disable()
            self.ocr_button.Disable()
            self.cancel_button.Enable()
            self.status.ChangeValue("Enumerating configured local adapters. The canonical language selection is unchanged.")

            def completed(inventory):
                if not self._alive or not self._window_alive:
                    return
                self.pending = False
                self.request = None
                self.refresh_button.Enable()
                self.ocr_button.Enable()
                self.cancel_button.Disable()
                self.summary.ChangeValue(self.controller.summary())
                self.inventory.ChangeValue(self.controller.inventory_text())
                self.inventory.SetInsertionPoint(0)
                self.ocr_inventory.ChangeValue(self.controller.ocr_inventory_text())
                self.ocr_inventory.SetInsertionPoint(0)
                self.filter()
                count = len(inventory.voices) if kind == 'voices' else len(inventory.identifiers)
                self.status.ChangeValue(f"Local inventory updated: {count} {'voices' if kind == 'voices' else 'OCR model identifiers'}. "
                    "Use Ctrl+Tab for details. Qualified language entries remain zero; no exact bindings or acoustic tests were added.")

            def failed(error):
                if self._alive and self._window_alive:
                    self.pending = False
                    self.request = None
                    self.refresh_button.Enable()
                    self.ocr_button.Enable()
                    self.cancel_button.Disable()
                    self.status.ChangeValue("Local inventory was not updated. " + str(error))

            try:
                request = request_inventory(self.GetParent(), self.controller, kind=kind,
                    completed=completed, failed=failed, alive=lambda: self._alive and self._window_alive)
                # A rejected host executor reports failure synchronously.
                self.request = request if self.pending else None
            except Exception as error:
                failed(error)

        def cancel(self, event=None):
            if self._alive and self._window_alive and self.pending and self.request is not None:
                self.request.cancel()
                self.status.ChangeValue("Cancellation requested. The current bounded adapter probe must finish or reach its time limit.")

        def close(self, event=None):
            if not self._window_alive:
                return
            self.shutdown()
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            else:
                self.Destroy()

        def shutdown(self):
            self._alive = False
            if self.request is not None:
                self.request.cancel()

        def destroyed(self, event):
            if event.GetId() == self._window_id:
                self._window_alive = False
                self.shutdown()
            event.Skip()

    return LanguageMatrixDialog


def show_language_matrix(frame, *, on_ready=None):
    dialog = None
    try:
        if getattr(frame, "_closing", False):
            raise ValueError("The application is closing.")
        controller = LanguageMatrixController()
        dialog = _native_dialog_type()(frame, controller)
        if on_ready is not None:
            import wx
            wx.CallAfter(lambda: on_ready(dialog) if dialog._alive and dialog._window_alive else None)
        dialog.ShowModal()
    except Exception as error:
        if not getattr(frame, '_closing', False):
            try:
                frame.report("Language evidence view could not open. " + str(error))
            except RuntimeError:
                pass  # Native parent destruction can race a modal failure.
    finally:
        if dialog is not None:
            dialog.shutdown()
            if dialog._window_alive:
                dialog.Destroy()


def install_language_actions(frame):
    """Reuse the existing native menu and shared command registry."""
    if getattr(frame, "_carr_language_actions_installed", False):
        return frame._carr_language_menu_item
    import wx
    bar = frame.GetMenuBar()
    if bar is None:
        raise ValueError("Install the native menu bar before language actions.")
    menu = None
    for title in ("Audio", "Voice", "Help"):
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
    register_menu_command(frame, CommandSpec('language.capabilities', 'Languages and capability evidence'), item)
    frame._carr_language_menu_item = item
    frame._carr_language_actions_installed = True
    return item
