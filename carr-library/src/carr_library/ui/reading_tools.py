"""Native bookmark and paragraph tools over the existing reading document."""
from dataclasses import dataclass

from ..speech.marks import text_digest


@dataclass(frozen=True)
class ReadingToolSnapshot:
    document: object
    signature: tuple
    cursor: int

    @classmethod
    def capture(cls, document, cursor):
        document._range(cursor, cursor)
        return cls(document, document._signature(), cursor)


def create_reading_tools(parent, *, capture, mutate, goto, guard, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class ReadingTools(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Reading bookmarks and paragraph tools', size=(660, 470),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.alive, self.reviewed, self.rows, self.buttons = True, None, (), {}
            layout = wx.BoxSizer(wx.VERTICAL)
            notice = wx.StaticText(self, label=(
                'Bookmarks keep an exact reading-text version. Changed text needs review. '
                'Paragraph changes can be undone; original source text remains unchanged.'))
            notice.Wrap(620)
            layout.Add(notice, 0, wx.ALL | wx.EXPAND, 8)
            layout.Add(wx.StaticText(self, label='Bookmark name'), 0, wx.LEFT | wx.RIGHT, 8)
            self.name = wx.TextCtrl(self, value='Reading bookmark')
            self.name.SetName('Reading bookmark name')
            layout.Add(self.name, 0, wx.ALL | wx.EXPAND, 8)
            self.list = wx.ListBox(self)
            self.list.SetName('Saved bookmarks in this reading copy')
            layout.Add(self.list, 1, wx.ALL | wx.EXPAND, 8)
            self.status = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 65))
            self.status.SetName('Reading text tools status')
            layout.Add(self.status, 0, wx.ALL | wx.EXPAND, 8)
            actions = (
                ('add', 'Add bookmark at current cursor', self.on_add),
                ('goto', 'Go to bookmark and close', self.on_goto),
                ('remove', 'Remove selected bookmark', self.on_remove),
                ('split', 'Split paragraph at current cursor', self.on_split),
                ('join', 'Join next line or paragraph break', self.on_join),
                ('close', 'Close reading text tools', self.on_close),
            )
            for start in (0, 3):
                row = wx.BoxSizer(wx.HORIZONTAL)
                for identity, label, action in actions[start:start + 3]:
                    button = wx.Button(self, label=label)
                    button.SetName(label)
                    button.Bind(wx.EVT_BUTTON, action)
                    self.buttons[identity] = button
                    row.Add(button, 0, wx.ALL, 4)
                layout.Add(row, 0, wx.EXPAND)
            self.SetSizer(layout)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            try:
                self.refresh()
                self.name.SetFocus()
            except BaseException:
                self.alive = False
                self.Destroy()
                raise

        def check(self):
            if not self.alive:
                raise ValueError('These reading tools have closed.')
            guard()

        def notice(self, message):
            if not self.alive:
                return
            try:
                guard()
            except Exception:
                return
            self.status.ChangeValue(str(message))

        def refresh(self, message=''):
            self.check()
            self.reviewed = capture()
            self.rows = self.reviewed.document.bookmarks
            fingerprint = text_digest(self.reviewed.document.text)
            self.list.Set([item.label + '; character ' + str(item.offset + 1)
                           + ('; text changed — review required' if item.text_sha256 != fingerprint else '')
                           for item in self.rows])
            self.status.ChangeValue(message or 'Choose an explicit bookmark or paragraph action.')

        def selected(self):
            index = self.list.GetSelection()
            if not 0 <= index < len(self.rows):
                raise ValueError('Choose a saved bookmark first.')
            return self.rows[index].id

        def action(self, kind, value=None):
            if not self.alive:
                return
            try:
                self.check()
                mutate(kind, value, self.reviewed)
                self.refresh('Reading copy updated. Ctrl+S retains its bookmarks and history; Undo reverses text edits.')
            except Exception as exc:
                self.notice(exc)

        def on_add(self, event=None):
            if self.alive:
                self.action('add', self.name.GetValue())

        def on_remove(self, event=None):
            if not self.alive:
                return
            try:
                self.action('remove', self.selected())
            except Exception as exc:
                self.notice(exc)

        def on_split(self, event=None):
            self.action('split')

        def on_join(self, event=None):
            self.action('join')

        def on_goto(self, event=None):
            if not self.alive:
                return
            try:
                self.check()
                goto(self.selected(), self.reviewed)
                self.on_close()
            except Exception as exc:
                self.notice(exc)

        def on_close(self, event=None):
            if self.alive:
                self.alive = False
                self.EndModal(wx.ID_CANCEL)

    return ReadingTools()


def show_reading_tools(parent, **options):
    dialog = create_reading_tools(parent, **options)
    try:
        dialog.ShowModal()
    finally:
        dialog.alive = False
        dialog.Destroy()
