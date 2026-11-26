"""Bind the native EPUB reader to the current catalog selection and scope."""
from dataclasses import dataclass

from ..reader.ebook import open_ebook, read_chapter
from .ebook_dialog import show_ebook


@dataclass(frozen=True)
class _BookSelection:
    library: object
    workspace: str
    topic: str | None
    notebook: object
    panel: object
    selection: tuple
    row: object

    @property
    def edition(self):
        return self.selection[2]


class EbookActions:
    def __init__(self, frame, *, reader=show_ebook):
        self.frame = frame
        self.reader = reader

    def _selected(self):
        frame = self.frame
        if getattr(frame, '_closing', False):
            raise ValueError('The library is closing.')
        if frame.working or getattr(frame, 'busy', False):
            raise ValueError('An operation is already running. Wait or choose Cancel current operation.')
        if frame.active is None:
            raise ValueError('Open a library first.')
        if frame.book is None:
            raise ValueError('Open Books and Collections and select an imported EPUB first.')
        workspace, topic = frame.active
        panel = frame.book.GetPage(1)
        if panel.workspace != workspace or not panel.ready:
            raise ValueError('Refresh Books and Collections before opening an electronic book.')
        selection = panel._selection()
        row = panel.rows.get(selection[2])
        if row is None:
            raise ValueError('Select an imported EPUB in Books and Collections first.')
        return _BookSelection(frame.library, workspace, topic, frame.book, panel, selection, row)

    def _current(self, selected):
        frame = self.frame
        try:
            return (not getattr(frame, '_closing', False)
                    and frame.library is selected.library
                    and frame.active == (selected.workspace, selected.topic)
                    and frame.book is selected.notebook
                    and frame.book.GetPage(1) is selected.panel
                    and selected.panel.workspace == selected.workspace and selected.panel.ready
                    and selected.panel._selection() == selected.selection
                    and selected.panel.rows.get(selected.edition) == selected.row)
        except (AttributeError, RuntimeError, IndexError):
            return False

    def _guard(self, selected):
        if not self._current(selected):
            raise ValueError('The selected book, library or topic changed. Open the reader again.')

    def _restore_focus(self, selected):
        if not self._current(selected) or self.frame.working or getattr(self.frame, 'busy', False):
            return
        try:
            if selected.notebook.GetSelection() == 1:
                selected.panel.books.SetFocus()
            else:
                self.frame.status.SetFocus()
        except (AttributeError, RuntimeError):
            self.frame.status.SetFocus()

    def open(self, event=None):
        try:
            selected = self._selected()
            cancelled = self.frame.cancel_event.is_set
            self._guard(selected)

            def load():
                # This runs on the worker. Captured identities, not wx controls
                # or a later active scope, reach the authorization service.
                return open_ebook(selected.library, selected.workspace, selected.edition,
                                  authorized_topic=selected.topic, cancelled=cancelled)

            def ready(contents):
                self._guard(selected)
                if cancelled():
                    raise InterruptedError('Reading the electronic book was cancelled.')
                revision = contents['revision']

                def load_contents():
                    self._guard(selected)
                    return contents

                def load_chapter(ordinal):
                    self._guard(selected)
                    return read_chapter(selected.library, selected.workspace, selected.edition,
                                        revision, ordinal, authorized_topic=selected.topic,
                                        cancelled=cancelled)

                try:
                    self.reader(self.frame, load_contents, load_chapter,
                                still_current=lambda: self._current(selected))
                finally:
                    self._restore_focus(selected)

            self.frame.run_job('Loading electronic-book contents', load, ready)
        except Exception as exc:
            self.frame.report(f'Electronic-book reader not opened: {exc}')


def install_ebook_actions(frame):
    """Add one Library menu command and retain its handler for the frame."""
    existing = getattr(frame, 'ebook_actions', None)
    if existing is not None:
        return existing
    import wx

    index = frame.menu_bar.FindMenu('Library')
    if index < 0:
        raise ValueError('The Library menu is required for electronic-book reading.')
    actions = EbookActions(frame)
    menu = frame.menu_bar.GetMenu(index)
    menu.AppendSeparator()
    item = menu.Append(wx.ID_ANY, 'Read electronic book contents\tCtrl+Shift+O')
    frame.Bind(wx.EVT_MENU, actions.open, item)
    frame.ebook_actions = actions
    return actions
