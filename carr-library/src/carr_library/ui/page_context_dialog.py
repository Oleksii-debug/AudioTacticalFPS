"""Native More dialog for verified context and explicitly unverified source preview.

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
        self.citation_verified = None

    def visit(self, position):
        from ..reader.context import Context
        from ..reader.source_preview import SourcePreview

        if self.current_selection() != self.selection:
            raise ValueError('Selection changed')
        workspace, ref, revision = self.selection
        context = self.load_context(workspace, ref, revision)
        if self.current_selection() != self.selection or not isinstance(context, (Context, SourcePreview)):
            raise ValueError('Context unavailable or selection changed')
        verified = isinstance(context, Context)
        if verified:
            anchors = tuple(p.anchor for p in context.pages)
            identities = tuple(a.identity for a in anchors)
        else:
            anchors = tuple((p.identity, p.locator, p.raw_sha256, p.start, p.end, p.quote, p.citation)
                            for p in context.pages)
            identities = tuple(p.identity for p in context.pages)
        if not anchors or any(identity.workspace != workspace or identity.revision != revision
                              for identity in identities):
            raise ValueError('Context identity mismatch')
        if self.anchors is not None and (anchors != self.anchors or verified != self.citation_verified):
            raise ValueError('Context anchors changed')
        page = context.page(position)
        self.anchors = anchors
        self.citation_verified = verified
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
        text_label = wx.StaticText(dialog, label='Source text')
        layout.Add(text_label, 0, wx.ALL, 4)
        reader = wx.TextCtrl(dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
        reader.SetName('Source text')
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
            if session.citation_verified:
                dialog.SetTitle('Original source context')
                text_label.SetLabel('Original page text')
                reader.SetName('Original page text')
                message = f'Context page {position + 1} of {count}\n{page.citation}'
            else:
                dialog.SetTitle('Unverified extraction preview')
                text_label.SetLabel('Unverified extracted text')
                reader.SetName('Unverified extracted text')
                message = (f'Unverified extraction preview — page {position + 1} of {count}\n'
                           'Source review is required before copying citations or saving verified evidence.\n'
                           + page.citation)
            status.ChangeValue(message)
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
