"""Native metadata editing; the existing application owns revision publication."""
import wx


def metadata_values(title, authors, language, edition, volume):
    """Validate the form without changing author order or source text."""
    title = title.strip()
    if not title or len(title) > 512 or '\x00' in title:
        raise ValueError('Enter a title of 1 to 512 characters.')
    names = tuple(line.strip() for line in authors.splitlines() if line.strip())
    if len(names) > 100 or any(len(name) > 512 or '\x00' in name for name in names):
        raise ValueError('Enter at most 100 authors, one per line, up to 512 characters each.')
    result = dict(title=title, authors=names)
    for key, value in (('language', language), ('edition', edition), ('volume', volume)):
        value = value.strip()
        if len(value) > 4000 or '\x00' in value:
            raise ValueError('Language, edition and volume must be at most 4000 characters each.')
        result[key] = value or None
    return result


class MetadataDialog(wx.Dialog):
    """Keep typed edits on failure and run the atomic save off the UI thread."""
    def __init__(self, parent, current, *, save, executor):
        super().__init__(parent, title='Edit book metadata', size=(620, 650))
        self.save = save
        self.executor = executor
        self.pending = False
        self.fields = {}
        layout = wx.BoxSizer(wx.VERTICAL)
        explanation = wx.StaticText(self, label=(
            'Title and authors apply to every edition of this work.\n'
            'Language, edition and volume apply to the selected edition.\n'
            'Original texts and previously saved quotations are preserved.'))
        layout.Add(explanation, 0, wx.EXPAND | wx.ALL, 8)
        fields = (
            ('title', 'Title', current.title or '', False),
            ('authors', 'Authors, one per line, in source order', '\n'.join(current.authors), True),
            ('language', 'Language; leave blank if unknown', current.language or '', False),
            ('edition', 'Edition; leave blank if unknown', current.edition or '', False),
            ('volume', 'Volume; leave blank if unknown', current.volume or '', False),
        )
        for key, label, value, multiline in fields:
            layout.Add(wx.StaticText(self, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
            control = wx.TextCtrl(self, value=value, style=wx.TE_MULTILINE if multiline else 0)
            control.SetName(label)
            if multiline:
                control.SetMinSize((-1, 110))
            self.fields[key] = control
            layout.Add(control, 1 if multiline else 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.status = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.status.SetName('Metadata save status')
        self.status.SetMinSize((-1, 64))
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.save_button = wx.Button(self, wx.ID_SAVE, '&Save metadata')
        self.save_button.SetDefault()
        self.cancel_button = wx.Button(self, wx.ID_CANCEL, '&Cancel')
        self.save_button.Bind(wx.EVT_BUTTON, self.on_save)
        buttons.Add(self.save_button, 0, wx.ALL, 8)
        buttons.Add(self.cancel_button, 0, wx.ALL, 8)
        layout.Add(buttons, 0, wx.ALIGN_RIGHT)
        self.SetSizer(layout)
        self.SetMinSize((520, 590))
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.fields['title'].SetFocus()

    def _enabled(self, enabled):
        for control in (*self.fields.values(), self.save_button, self.cancel_button):
            control.Enable(enabled)

    def on_save(self, event=None):
        if self.pending:
            return
        try:
            values = metadata_values(**{key: control.GetValue() for key, control in self.fields.items()})
        except (ValueError, UnicodeError) as exc:
            self.status.SetValue(str(exc))
            self.status.SetFocus()
            return
        self.pending = True
        self._enabled(False)
        self.status.SetValue('Saving metadata. Please wait.')
        self.status.SetFocus()
        try:
            future = self.executor.submit(self.save, **values)
        except Exception as exc:
            self.pending = False
            self._enabled(True)
            self.status.SetValue(f'Metadata was not saved: {exc}')
            return

        def completed(result):
            # A metadata dialog cannot close while the atomic save is running.
            # SQLite connections are created by the existing application service.
            def show_result():
                self.pending = False
                self._enabled(True)
                try:
                    result.result()
                except Exception as exc:
                    self.status.SetValue(f'Metadata was not saved: {exc}\nYour edits remain in this form.')
                    self.status.SetFocus()
                else:
                    self.EndModal(wx.ID_OK)
            wx.CallAfter(show_result)
        future.add_done_callback(completed)

    def on_close(self, event):
        if self.pending:
            if event.CanVeto():
                event.Veto()
            self.status.SetValue('Metadata is being saved. Wait for the result before closing.')
            self.status.SetFocus()
            return
        event.Skip()


def edit_selected_book(frame):
    """Use the existing Books selection and retain that selection after editing."""
    dialog = None
    panel = None
    try:
        if frame.working or frame.busy:
            raise ValueError('Wait for the current operation before editing book metadata.')
        workspace = frame._workspace()
        if frame.book is None:
            raise ValueError('Open a library and select a book in Books and Collections.')
        panel = frame.book.GetPage(1)
        if panel.workspace != workspace or not panel.ready:
            raise ValueError('Refresh Books and Collections before editing a book.')
        selection = panel._selection()
        identity = selection[2]
        if identity is None:
            raise ValueError('Select a book in Books and Collections first.')
        current = frame.library.edition_metadata(workspace, identity)

        def save(**values):
            return frame.library.update_metadata(workspace, identity,
                expected_metadata=current, **values)

        dialog = MetadataDialog(frame, current, save=save, executor=frame.executor)
        if dialog.ShowModal() == wx.ID_OK:
            if panel.refresh(selection=selection):
                panel.status.SetValue('Book metadata saved. Search again to see the updated results.')
            else:
                panel.status.SetValue('Book metadata saved. Refresh the catalog to view it.')
            panel.status.SetFocus()
        else:
            panel.books.SetFocus()
    except Exception as exc:
        frame.report(str(exc))
    finally:
        if dialog is not None:
            dialog.Destroy()
