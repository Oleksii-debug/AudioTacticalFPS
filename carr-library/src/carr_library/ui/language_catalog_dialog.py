"""Read-only native view of selected languages and the recorded evidence scope."""
from ..language_catalog import CatalogError, MAX_QUERY, REVISION, load_catalog


def entry_text(entry):
    """Make every capability inspectable without implying installed support."""
    from ..language_catalog import CAPABILITIES
    inventory = entry.resource_inventory
    lines = [entry.name_uk, 'Selected catalog identity: ' + entry.id, '', 'Geography:']
    lines.extend(place.region + ': ' + ', '.join(place.countries) for place in entry.geography)
    lines.extend(('', 'Recorded DEV2 resource status: ' + inventory.status,
                  'Recorded resource identifier: ' + inventory.runtime_code,
                  'Recorded backend: ' + inventory.backend,
                  'Identity review: ' + entry.identity_review))
    if inventory.standard_note:
        lines.append('Qualification: ' + inventory.standard_note)
    lines.extend(('', 'Capability qualification:'))
    lines.extend(name.replace('_', ' ') + ': not assessed' for name in CAPABILITIES)
    lines.extend(('', 'This historical resource inventory does not establish current local installation, '
                  'voice compatibility, pronunciation, or Windows/NVDA acceptance.',
                  'No voice is selected or substituted by this catalog. Inspect installed voices in Audio Studio '
                  'and installed recognition models in OCR settings.',
                  'Final language qualification follows core features and secure delivery. '
                  'Standalone transcription is deferred.',
                  'Pinned upstream source revision: ' + REVISION))
    return '\n'.join(lines)


class LanguageCatalogActions:
    def refresh(self, event=None):
        index = self.languages.GetSelection()
        selected = self.rows[index].id if 0 <= index < len(self.rows) else None
        try:
            self.rows = self.catalog.matching(self.filter.GetValue())
        except CatalogError as exc:
            self.rows = ()
            self.languages.SetItems([])
            self.details.SetValue(str(exc))
            self.status.SetValue('The catalog filter could not be applied.')
            return
        self.languages.SetItems([entry.name_uk + '; ' + entry.id + '; '
            + entry.resource_inventory.status for entry in self.rows])
        selected_index = next((i for i, entry in enumerate(self.rows) if entry.id == selected), 0)
        self.languages.SetSelection(selected_index if self.rows else -1)
        self.selection_changed()

    def selection_changed(self, event=None):
        index = self.languages.GetSelection()
        self.status.SetValue(f'{len(self.rows)} matching selected languages out of '
            f'{len(self.catalog.entries)}. Resource presence is not voice or language acceptance.')
        self.details.SetValue(entry_text(self.rows[index]) if 0 <= index < len(self.rows)
            else 'No selected languages match this filter. Search a name, country, region or recorded identifier.')


def create_language_catalog_dialog(parent):
    import wx
    catalog = load_catalog()

    class LanguageCatalogDialog(wx.Dialog, LanguageCatalogActions):
        def __init__(self):
            super().__init__(parent, title='Language capability catalog', size=(920, 760),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.catalog, self.rows = catalog, ()
            layout = wx.BoxSizer(wx.VERTICAL)
            explanation = wx.StaticText(self, label='The selected catalog has 135 unique entries. '
                'Recorded resource presence and verified language capability are separate.')
            explanation.Wrap(850)
            layout.Add(explanation, 0, wx.ALL | wx.EXPAND, 8)
            layout.Add(wx.StaticText(self, label='Filter by language, country, region or identifier'),
                       0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
            self.filter = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
            self.filter.SetName('Filter selected languages by name, country, region or identifier')
            self.filter.SetMaxLength(MAX_QUERY)
            layout.Add(self.filter, 0, wx.ALL | wx.EXPAND, 8)
            self.languages = wx.ListBox(self)
            self.languages.SetName('Selected languages and recorded resource status')
            layout.Add(self.languages, 1, wx.ALL | wx.EXPAND, 8)
            self.details = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE)
            self.details.SetName('Selected language geography, capabilities and evidence limits')
            layout.Add(self.details, 2, wx.ALL | wx.EXPAND, 8)
            self.status = wx.TextCtrl(self, style=wx.TE_READONLY)
            self.status.SetName('Language catalog filter status')
            layout.Add(self.status, 0, wx.ALL | wx.EXPAND, 8)
            close = wx.Button(self, wx.ID_CANCEL, 'Close')
            layout.Add(close, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
            self.SetEscapeId(wx.ID_CANCEL)
            self.SetSizer(layout)
            self.SetMinSize((640, 480))
            self.filter.Bind(wx.EVT_TEXT, self.refresh)
            self.filter.Bind(wx.EVT_TEXT_ENTER, lambda event: self.languages.SetFocus())
            self.languages.Bind(wx.EVT_LISTBOX, self.selection_changed)
            self.refresh()
            self.filter.SetFocus()

    return LanguageCatalogDialog()


def show_language_catalog(parent, *, on_ready=None):
    import wx
    dialog = None
    try:
        dialog = create_language_catalog_dialog(parent)
        if on_ready is not None:
            wx.CallAfter(on_ready, dialog)
        dialog.ShowModal()
    except (CatalogError, OSError) as exc:
        parent.report('The installed language catalog could not be verified: ' + str(exc))
    finally:
        if dialog is not None:
            dialog.Destroy()
