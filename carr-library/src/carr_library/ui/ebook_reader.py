"""Keyboard-first EPUB reader using the existing background-job controller.

All source reads run off the UI thread. A section is loaded again under fresh
authorization on each visit; changing the active library invalidates callbacks.
The dialog never approves, edits or exports extracted text as a quotation.
"""
import sqlite3

from ..reader.ebook import EbookSection, list_ebooks, read_ebook_section
from ..research.topic_tree import TopicTree
from .research_actions import NativeDialogs


def _authorized_scope(library, workspace, topic):
    """Reject an archived scope even after a worker has returned its text."""
    try:
        with library.read(workspace) as db:
            if topic is not None:
                TopicTree(db, workspace)._visible(topic)
        return True
    except (OSError, ValueError, sqlite3.Error):
        return False


class EbookSession:
    def __init__(self, book, current_scope):
        self.book = book
        self.current_scope = current_scope
        self.position = 1
        self.chapters = None

    def accept(self, section, position):
        if (not self.current_scope() or not isinstance(section, EbookSection)
                or type(position) is not int or not 1 <= position <= section.count
                or section.position != position or section.verified
                or (section.identity.workspace, section.identity.edition_id, section.identity.revision)
                   != (self.book.workspace, self.book.edition_id, self.book.revision)
                or section.count != self.book.section_count or len(section.chapters) != section.count
                or (self.chapters is not None and section.chapters != self.chapters)):
            raise ValueError('The EPUB section or library changed. Reopen the book.')
        self.chapters = section.chapters
        self.position = position
        return section


def show_ebook_reader(parent, book, current_scope, load_section, run_job, initial_section):
    import wx

    session = EbookSession(book, current_scope)
    dialog = wx.Dialog(parent, title='Read EPUB book', size=(820, 680))
    alive = True
    loading = False
    try:
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(wx.StaticText(dialog, label='EPUB section'), 0, wx.ALL, 6)
        chooser = wx.Choice(dialog)
        chooser.SetName('EPUB section')
        layout.Add(chooser, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        layout.Add(wx.StaticText(dialog, label='Section information'), 0, wx.ALL, 6)
        status = wx.TextCtrl(dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
        status.SetName('Section information')
        status.SetMinSize((-1, 100))
        layout.Add(status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        layout.Add(wx.StaticText(dialog, label='Extracted section text — unverified'), 0, wx.ALL, 6)
        text = wx.TextCtrl(dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
        text.SetName('Extracted section text — unverified')
        layout.Add(text, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        layout.Add(wx.StaticText(dialog, label='Authored image descriptions — separate from quotation text'),
                   0, wx.ALL, 6)
        alternatives = wx.TextCtrl(dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
        alternatives.SetName('Authored image descriptions — separate from quotation text')
        alternatives.SetMinSize((-1, 65))
        layout.Add(alternatives, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        previous = wx.Button(dialog, label='&Previous section')
        following = wx.Button(dialog, label='&Next section')
        back = wx.Button(dialog, wx.ID_CANCEL, '&Back')
        for button in (previous, following, back):
            buttons.Add(button, 0, wx.ALL, 6)
        layout.Add(buttons, 0)
        dialog.SetSizer(layout)
        dialog.SetEscapeId(wx.ID_CANCEL)

        def unavailable(error=None):
            nonlocal loading
            if not alive:
                return
            loading = False
            text.ChangeValue('')
            alternatives.ChangeValue('')
            chooser.Enable(False)
            previous.Enable(False)
            following.Enable(False)
            status.ChangeValue('The EPUB section is unavailable or changed. Return to the library and reopen the book.')
            status.SetInsertionPoint(0)
            status.SetFocus()

        def display(section, position):
            nonlocal loading
            if not alive:
                return
            loading = False
            try:
                value = session.accept(section, position)
            except Exception:
                unavailable()
                return
            chooser.SetItems([f'{c.position}. {c.title}' + (' (supplementary)' if not c.linear else '')
                              for c in value.chapters])
            chooser.SetSelection(position - 1)
            chooser.Enable(True)
            information = (f'{value.book_title}\nEPUB section {position} of {value.count}: {value.title}\n'
                           'Extracted text is unverified. Printed pages are unknown.')
            if not value.linear:
                information += '\nThis is supplementary content from the reading order.'
            if value.warnings:
                information += '\nReading notes: ' + '; '.join(w.replace('_', ' ') for w in value.warnings)
            status.ChangeValue(information)
            text.ChangeValue(value.raw_text)
            text.SetInsertionPoint(0)
            alternatives.ChangeValue('\n\n'.join(f'Image {number}: {description}'
                                                  for number, description in value.alternatives)
                                     or 'No authored image descriptions in this section.')
            alternatives.SetInsertionPoint(0)
            previous.Enable(position > 1)
            following.Enable(position < value.count)
            status.SetInsertionPoint(0)
            status.SetFocus()

        def navigate(position):
            nonlocal loading
            if not alive or loading:
                return
            if (not current_scope() or type(position) is not int
                    or not 1 <= position <= book.section_count):
                unavailable()
                return
            loading = True
            chooser.Enable(False)
            previous.Enable(False)
            following.Enable(False)
            text.ChangeValue('')
            alternatives.ChangeValue('')
            status.ChangeValue('Loading the selected EPUB section…')
            status.SetFocus()
            run_job('Loading EPUB section', lambda: load_section(position),
                    lambda value: display(value, position), unavailable)

        chooser.Bind(wx.EVT_CHOICE, lambda event: navigate(chooser.GetSelection() + 1))
        previous.Bind(wx.EVT_BUTTON, lambda event: navigate(session.position - 1))
        following.Bind(wx.EVT_BUTTON, lambda event: navigate(session.position + 1))
        display(initial_section, 1)
        dialog.ShowModal()
    finally:
        alive = False
        dialog.Destroy()


class EbookReaderActions:
    def __init__(self, frame, *, dialogs=None, show=show_ebook_reader):
        self.frame = frame
        self.dialogs = dialogs or NativeDialogs()
        self.show = show

    def read(self, event=None):
        frame = self.frame
        if frame.working:
            frame.report('An operation is already running. Wait or choose Cancel current operation.')
            return
        try:
            workspace = frame._workspace()
            scope = frame.active
            topic = scope[1]
            library = frame.library

            def current_scope():
                return (not frame._closing and frame.library is library and frame.active == scope
                        and _authorized_scope(library, workspace, topic))

            def chosen(books):
                if not current_scope() or frame.cancel_event.is_set():
                    return
                if not books:
                    frame.report('This library has no imported EPUB books. Choose Library, Import books first.')
                    return
                index = self.dialogs.choose(frame, 'Read EPUB book', 'Choose a book to read by section.',
                    [f'{i}. {book.title} — {book.author}' + (f' — {book.edition}' if book.edition else '')
                     for i, book in enumerate(books, 1)])
                if index is None or not current_scope():
                    return
                if type(index) is not int or not 0 <= index < len(books):
                    raise ValueError('Choose an EPUB book from the list.')
                book = books[index]

                def load(position):
                    if not current_scope():
                        raise ValueError('The active library changed. Reopen the EPUB book.')
                    return read_ebook_section(library, workspace, book.edition_id, book.revision, position,
                        authorized_topic=topic, cancelled=frame.cancel_event.is_set)

                def opened(section):
                    if not current_scope() or frame.cancel_event.is_set():
                        return
                    self.show(frame, book, current_scope, load, frame.run_job, section)
                    if current_scope() and not frame.working and frame.choice.IsEnabled():
                        frame.choice.SetFocus()

                frame.run_job('Opening EPUB book', lambda: load(1), opened)

            frame.run_job('Listing EPUB books', lambda: list_ebooks(library, workspace,
                authorized_topic=topic, cancelled=frame.cancel_event.is_set), chosen)
        except Exception as exc:
            frame.report(f'EPUB reader: {exc}')


def install_ebook_reader(frame):
    import wx

    index = frame.menu_bar.FindMenu('Library')
    if index < 0:
        raise ValueError('The Library menu is required for EPUB reading.')
    actions = EbookReaderActions(frame)
    item = frame.menu_bar.GetMenu(index).Append(wx.ID_ANY, 'Read EPUB book\tCtrl+Shift+R')
    frame.Bind(wx.EVT_MENU, actions.read, item)
    frame.ebook_reader_actions = actions
