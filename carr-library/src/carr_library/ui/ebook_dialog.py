"""Native, text-only EPUB chapter and contents reader.

The modal owns no source files, notes, clipboard approval or saved evidence.
Its callbacks must freshly authorize the captured library, topic and revision.
"""
from .ebook_model import EbookSession, NOTICE, native_position


def show_ebook(parent, load_contents, load_chapter, *, still_current=lambda: True,
               initial_ordinal=1):
    """Read extracted chapters; the caller restores its own focus on return."""
    import wx

    session = EbookSession(load_contents, load_chapter, still_current)
    dialog = wx.Dialog(parent, title='Electronic book — extracted text', size=(900, 760),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    alive = True
    available = False
    try:
        layout = wx.BoxSizer(wx.VERTICAL)

        def labelled(label, factory, *, proportion=0, height=None, **kwargs):
            # Native mnemonic association follows child creation order.
            layout.Add(wx.StaticText(dialog, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
            control = factory(dialog, **kwargs)
            control.SetName(label.replace('&', ''))
            if height is not None:
                control.SetMinSize((-1, height))
            layout.Add(control, proportion, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
            return control

        readonly = wx.TE_READONLY | wx.TE_MULTILINE
        report = labelled('EPUB extraction report', wx.TextCtrl, style=readonly, height=90)
        chapters = labelled('&Chapter (unpaginated)', wx.Choice)
        contents = labelled('Book c&ontents (levels and chapter destinations)', wx.Choice)
        navigation = wx.BoxSizer(wx.HORIZONTAL)
        previous = wx.Button(dialog, label='&Previous chapter')
        following = wx.Button(dialog, label='&Next chapter')
        for button in (previous, following):
            navigation.Add(button, 0, wx.ALL, 6)
        layout.Add(navigation, 0, wx.EXPAND)
        query = labelled('&Find in this chapter (literal text, ignores case)',
                         wx.TextCtrl, style=wx.TE_PROCESS_ENTER)
        find_buttons = wx.BoxSizer(wx.HORIZONTAL)
        find_start = wx.Button(dialog, label='Find from &start')
        find_next = wx.Button(dialog, label='Find ne&xt (F3)')
        for button in (find_start, find_next):
            find_buttons.Add(button, 0, wx.ALL, 6)
        layout.Add(find_buttons, 0, wx.EXPAND)
        status = labelled('Chapter and navigation status', wx.TextCtrl, style=readonly, height=90)
        reader = labelled(NOTICE, wx.TextCtrl, style=readonly | wx.TE_DONTWRAP, proportion=1)
        close = wx.Button(dialog, wx.ID_CANCEL, 'C&lose')
        layout.Add(close, 0, wx.ALL | wx.ALIGN_RIGHT, 6)
        dialog.SetSizer(layout)
        dialog.SetEscapeId(wx.ID_CANCEL)

        def say(message, *, focus=True):
            if not alive:
                return
            status.ChangeValue(message)
            status.SetInsertionPoint(0)
            if focus:
                status.SetFocus()

        def unavailable(error):
            nonlocal available
            if not alive:
                return
            available = False
            session.raw_text = None
            reader.ChangeValue('')
            report.ChangeValue('')
            chapters.Clear()
            contents.Clear()
            for control in (chapters, contents, previous, following, query, find_start, find_next):
                control.Enable(False)
            say(f'Electronic-book text unavailable. {error}\nClose this reader and open the selected EPUB again.')

        def guard():
            if not alive or not available:
                return False
            session.check_current()
            return True

        def position(offset, end=None):
            session.check_current()
            start_position = native_position(reader, session.raw_text, offset)
            end_position = start_position if end is None else native_position(reader, session.raw_text, end)
            session.check_current()
            reader.SetSelection(start_position, end_position)
            reader.ShowPosition(start_position)

        def render(ordinal, *, offset=0, entry_label=None):
            try:
                if not guard():
                    return
                session.visit(ordinal, offset=offset)
                information = session.chapter_status()
                session.check_current()
                # ChangeValue does not emit text-edit events or touch a note.
                reader.ChangeValue(session.raw_text)
                chapters.SetSelection(ordinal - 1)
                if entry_label is None:
                    contents.SetSelection(wx.NOT_FOUND)
                previous.Enable(ordinal > 1)
                following.Enable(ordinal < len(session.chapters))
                for control in (query, find_start, find_next):
                    control.Enable(bool(session.raw_text))
                positioned = True
                try:
                    position(offset)
                except ValueError as exc:
                    # The complete chapter stays readable even if a native
                    # control cannot map a navigation offset exactly.
                    reader.SetInsertionPoint(0)
                    information += f'\nDestination positioning unavailable: {exc}'
                    positioned = False
                    session.check_current()
                if entry_label is not None:
                    information += f'\nContents destination: {entry_label}. '
                    information += ('Tab to read at this destination.' if positioned else
                                    'The complete chapter is available from its beginning.')
                say(information)
            except Exception as exc:
                unavailable(exc)

        def choose_chapter(event):
            try:
                if guard():
                    index = chapters.GetSelection()
                    render(index + 1 if type(index) is int and index >= 0 else None)
            except Exception as exc:
                unavailable(exc)

        def choose_contents(event):
            try:
                if not guard():
                    return
                entry = session.entry(contents.GetSelection())
                if entry.heading_only:
                    say(session.chapter_status() + f'\nContents section heading: {entry.title}. '
                        'This heading has no destination; choose a linked entry below it.')
                    return
                render(entry.ordinal, offset=entry.offset, entry_label=entry.title)
            except Exception as exc:
                unavailable(exc)

        def find(*, again=False):
            try:
                if not guard():
                    return
                value = query.GetValue()
                if not value:
                    say(session.chapter_status() + '\nEnter text to find in this chapter.', focus=False)
                    query.SetFocus()
                    return
                match = session.find(value, next_match=again)
                if match is None:
                    say(session.chapter_status() + '\nText not found in this chapter. '
                        'Find searches literal text and ignores case.')
                    return
                information = (session.chapter_status() +
                    f'\nFound at extracted character {match.start + 1}. '
                    'This is a text position, not a printed-page reference.')
                if match.wrapped:
                    information += '\nSearch wrapped to the beginning of this chapter.'
                try:
                    position(match.start, match.end)
                except ValueError as exc:
                    session.check_current()
                    say(information + f'\nSelection positioning unavailable: {exc}')
                    return
                say(information, focus=False)
                reader.SetFocus()
            except Exception as exc:
                unavailable(exc)

        def keyboard(event):
            key = event.GetKeyCode()
            if key == wx.WXK_F3:
                find(again=True)
            elif event.ControlDown() and key in (ord('F'), ord('f')):
                try:
                    if guard():
                        query.SetFocus()
                        query.SelectAll()
                except Exception as exc:
                    unavailable(exc)
            else:
                event.Skip()

        chapters.Bind(wx.EVT_CHOICE, choose_chapter)
        contents.Bind(wx.EVT_CHOICE, choose_contents)
        previous.Bind(wx.EVT_BUTTON, lambda event: render(session.ordinal - 1))
        following.Bind(wx.EVT_BUTTON, lambda event: render(session.ordinal + 1))
        find_start.Bind(wx.EVT_BUTTON, lambda event: find())
        find_next.Bind(wx.EVT_BUTTON, lambda event: find(again=True))
        query.Bind(wx.EVT_TEXT_ENTER, lambda event: find())
        dialog.Bind(wx.EVT_CHAR_HOOK, keyboard)
        try:
            session.open()
            extraction_report = session.report()
            session.check_current()
            report.ChangeValue(extraction_report)
            chapters.SetItems([chapter.label for chapter in session.chapters])
            contents.SetItems([entry.label for entry in session.toc])
            contents.Enable(bool(session.toc))
            available = True
            render(initial_ordinal)
        except Exception as exc:
            unavailable(exc)
        dialog.ShowModal()
    finally:
        alive = False
        dialog.Destroy()
