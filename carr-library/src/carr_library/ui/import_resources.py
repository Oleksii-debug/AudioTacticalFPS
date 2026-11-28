"""Explicit resource choices for future Import Center plans only."""
from ..ingest.resource_budget import ImportBudget, MIB, checked_budget, effective_budget

_FIELDS = (
    ('source_mib', 'Maximum original size (MiB)', 'source_bytes', MIB),
    ('segment_mib', 'Maximum individual TXT record (MiB)', 'segment_bytes', MIB),
    ('segments', 'Maximum source segments or physical pages', 'segments', 1),
    ('steps', 'Checkpoint and publication steps per work slice', 'steps', 1),
    ('seconds', 'Native time allowance per work slice (seconds)', 'seconds', 1),
    ('memory_mib', 'Declared worker memory reservation (MiB)', 'memory_bytes', MIB),
    ('library_mib', 'Library growth allowance per work slice (MiB)', 'library_bytes', MIB),
    ('temporary_mib', 'Temporary worker reservation (MiB)', 'temporary_bytes', MIB),
)


def profile_values(selected=None):
    policy = effective_budget(selected)
    values = {}
    for key, _, field, unit in _FIELDS:
        value = getattr(policy, field)
        values[key] = str(value if field == 'seconds' else (value + unit - 1) // unit)
    return values


def profile_from_values(values):
    if type(values) is not dict or set(values) != {row[0] for row in _FIELDS}:
        raise ValueError('Fill every reviewed import resource value.')
    selected = {}
    for key, label, field, unit in _FIELDS:
        text = values[key]
        if type(text) is not str or not text.strip() or len(text) > 32:
            raise ValueError(label + ' requires one bounded number.')
        try:
            number = float(text) if field == 'seconds' else int(text)
        except ValueError as exc:
            raise ValueError(label + ' requires a valid number.') from exc
        selected[field] = number * unit
    return ImportBudget(**selected)


def profile_summary(selected=None):
    checked_budget(selected)
    if selected is None:
        return 'Current default import limits: 64 MiB originals and 120-second work slices.'
    return (f'Reviewed originals up to {selected.source_bytes / MIB:g} MiB; '
            f'{selected.segments} segments/pages; {selected.steps} steps and '
            f'{selected.seconds:g} seconds per explicit work slice. '
            f'Declared memory {selected.memory_bytes / MIB:g} MiB; '
            f'library growth {selected.library_bytes / MIB:g} MiB; '
            f'temporary space {selected.temporary_bytes / MIB:g} MiB.')


def choose_import_resources(parent, selected=None):
    """Return (accepted, profile); Cancel never changes the current choice."""
    import wx
    checked_budget(selected)

    class ResourceDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Import resource allowances', size=(760, 650),
                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.selected = selected
            self.fields = {}
            body = wx.ScrolledWindow(self, style=wx.VSCROLL | wx.TAB_TRAVERSAL)
            body.SetScrollRate(0, 20)
            layout = wx.BoxSizer(wx.VERTICAL)
            introduction = wx.StaticText(body, label=(
                'These choices are frozen into new import plans. Existing saved pages keep their '
                'reviewed values. Larger original sizes apply to streamed TXT and PDF only. '
                'Individual records and other formats retain their bounded decoders. '
                'Memory and disk reservations are checked against the shared operation settings.'))
            introduction.Wrap(660)
            layout.Add(introduction, 0, wx.ALL | wx.EXPAND, 8)
            form = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
            form.AddGrowableCol(1)
            values = profile_values(selected)
            for key, label, _, _ in _FIELDS:
                field = wx.TextCtrl(body, value=values[key])
                field.SetName(label)
                field.SetMaxLength(32)
                self.fields[key] = field
                form.Add(wx.StaticText(body, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
                form.Add(field, 0, wx.EXPAND)
            layout.Add(form, 0, wx.ALL | wx.EXPAND, 8)
            self.status = wx.TextCtrl(body, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 90))
            self.status.SetName('Import resource validation')
            self.status.SetValue(profile_summary(selected))
            layout.Add(self.status, 0, wx.ALL | wx.EXPAND, 8)
            body.SetSizer(layout)
            shell = wx.BoxSizer(wx.VERTICAL)
            shell.Add(body, 1, wx.EXPAND)
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            apply = wx.Button(self, wx.ID_OK, '&Use reviewed allowances')
            defaults = wx.Button(self, label='Use current &defaults')
            cancel = wx.Button(self, wx.ID_CANCEL, '&Cancel')
            for button, label in ((apply, 'Use reviewed import allowances'),
                                  (defaults, 'Use current default import limits'),
                                  (cancel, 'Cancel resource changes')):
                button.SetName(label)
                buttons.Add(button, 0, wx.ALL, 5)
            apply.Bind(wx.EVT_BUTTON, self.accept)
            defaults.Bind(wx.EVT_BUTTON, self.defaults)
            shell.Add(buttons, 0, wx.ALIGN_RIGHT)
            self.SetSizer(shell)
            self.SetMinSize((650, 460))
            self.SetEscapeId(wx.ID_CANCEL)
            self.fields['source_mib'].SetFocus()

        def accept(self, event=None):
            try:
                chosen = profile_from_values({key: field.GetValue() for key, field in self.fields.items()})
            except (TypeError, ValueError, OverflowError) as exc:
                self.status.SetValue(str(exc))
                self.status.SetFocus()
                return
            self.selected = chosen
            self.EndModal(wx.ID_OK)

        def defaults(self, event=None):
            self.selected = None
            self.EndModal(wx.ID_OK)

    dialog = ResourceDialog()
    try:
        accepted = dialog.ShowModal() == wx.ID_OK
        return (True, dialog.selected) if accepted else (False, selected)
    finally:
        try:
            if dialog and not dialog.IsBeingDeleted():
                dialog.Destroy()
        except RuntimeError:
            pass  # Its real native parent already completed destruction.
