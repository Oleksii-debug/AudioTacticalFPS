"""Native More dialog adapter for the verified CARR-08 Context contract.

Host supplies a fresh authorized Context for the selected hit on each visit.
No text extraction, source resolution, citation formatting or clipboard logic here.
"""


class PageSession:
    def __init__(self, selection, current_selection, load_context):
        self.selection = selection
        self.current_selection = current_selection
        self.load_context = load_context
        self.position = 0
        self.anchors = None

    def visit(self, position):
        from ..reader.context import Context

        if self.current_selection() != self.selection:
            raise ValueError('Selection changed')
        workspace, ref, revision = self.selection
        context = self.load_context(workspace, ref, revision)
        if self.current_selection() != self.selection or not isinstance(context, Context):
            raise ValueError('Context unavailable or selection changed')
        anchors = tuple(p.anchor for p in context.pages)
        if not anchors or any(a.identity.workspace != workspace or a.identity.revision != revision
                              for a in anchors):
            raise ValueError('Context identity mismatch')
        if self.anchors is not None and anchors != self.anchors:
            raise ValueError('Context anchors changed')
        page = context.page(position)
        self.anchors = anchors
        self.position = position
        return page, len(anchors)


def show_page_context(parent, selection, current_selection, load_context):
    """Return after modal dismissal; host restores focus only if selection is unchanged."""
    import wx

    session = PageSession(selection, current_selection, load_context)
    dialog = wx.Dialog(parent, title='Original source context', size=(760, 550))
    try:
        layout = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(dialog, label='Page information')
        layout.Add(label, 0, wx.ALL, 4)
        status = wx.TextCtrl(dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
        status.SetName('Page information')
        layout.Add(status, 0, wx.EXPAND | wx.ALL, 4)
        layout.Add(wx.StaticText(dialog, label='Original page text'), 0, wx.ALL, 4)
        reader = wx.TextCtrl(dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
        reader.SetName('Original page text')
        layout.Add(reader, 1, wx.EXPAND | wx.ALL, 4)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        previous = wx.Button(dialog, label='&Previous page')
        following = wx.Button(dialog, label='&Next page')
        back = wx.Button(dialog, wx.ID_CANCEL, '&Back')
        for button in (previous, following, back):
            buttons.Add(button, 0, wx.ALL, 4)
        layout.Add(buttons, 0, wx.EXPAND)
        dialog.SetSizer(layout)

        def render(position):
            try:
                page, count = session.visit(position)
            except Exception:
                reader.ChangeValue('')
                status.ChangeValue('Context unavailable or changed. Return to search and try again.')
                previous.Enable(False)
                following.Enable(False)
                status.SetInsertionPoint(0)
                status.SetFocus()
                return
            status.ChangeValue(f'Context page {position + 1} of {count}\n{page.citation}')
            reader.ChangeValue(page.raw_text)
            reader.SetInsertionPoint(0)
            previous.Enable(position > 0)
            following.Enable(position + 1 < count)
            # Move to the page information so keyboard users can read the new citation,
            # then Tab to original text. No native announcement is assumed.
            status.SetInsertionPoint(0)
            status.SetFocus()

        previous.Bind(wx.EVT_BUTTON, lambda event: render(session.position - 1))
        following.Bind(wx.EVT_BUTTON, lambda event: render(session.position + 1))
        render(0)
        dialog.ShowModal()
    finally:
        dialog.Destroy()
