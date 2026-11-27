"""Keyboard-first full-source reader, with page bookmarks and explicit resume.

The native controls display exact source text. Reading does not approve an
extraction or turn a bookmark into quotation evidence. The controller is
independent of wx so navigation and persistence failures can be exercised.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
import stat
import threading

from ..reader.reading_desk import ReadingDesk
from ..reader.reading_state import MAX_BYTES, ReadingState
from ..research.export_file import write_export, export_completion
from .export_destination import confirm_export_destination
from ..research.topic_tree import TopicTree
from .research_actions import NativeDialogs
from .export_destination import confirm_export_destination


def _chosen(index, values):
    if type(index) is not int or not 0 <= index < len(values):
        raise ValueError('Choose one item from the list.')
    return values[index]


@dataclass(frozen=True)
class _Navigation:
    page: object
    history_position: int | None


class ReaderController:
    """Validated navigation; persistence failures do not block reading."""

    def __init__(self, desk, state, guard=lambda: None, cancelled=lambda: False, *, defer_display=False):
        self.desk, self.state, self.guard, self.cancelled = desk, state, guard, cancelled
        self.defer_display = defer_display
        self.current = None
        self.history = []
        self.history_position = -1
        self.save_warning = None

    def check(self):
        self.guard()
        if self.cancelled():
            raise TimeoutError('Reading operation cancelled.')

    def _commit(self, page, *, history_position=None):
        self.check()
        if self.defer_display:
            return _Navigation(page, history_position)
        return self.accept(_Navigation(page, history_position))

    def _stopped(self):
        self.guard()
        self._authorize()
        return self.cancelled()

    def accept(self, navigation):
        """Accept an already displayed page; unseen worker results never save."""
        self.check()
        page, history_position = navigation.page, navigation.history_position
        self.save_warning = None
        try:
            self.state.remember(page.location, cancelled=self._stopped)
        except Exception as exc:
            # Reading remains usable on a full/read-only disk. Do not report
            # successful persistence or erase the earlier resume position.
            self.save_warning = f'The page is open, but its resume position could not be saved: {exc}'
        self.current = page
        if history_position is not None:
            self.history_position = history_position
        elif self.history_position < 0 or self.history[self.history_position] != page.location:
            self.history = self.history[:self.history_position + 1]
            self.history.append(page.location)
            # Session history is deliberately bounded; bookmarks remain durable.
            if len(self.history) > 200:
                self.history.pop(0)
            self.history_position = len(self.history) - 1
        return page

    def start(self):
        self.check()
        location = self.state.resume(self.desk.workspace, self.desk.edition)
        page = self.desk.validate(location) if location is not None else self.desk.page(1)
        return self._commit(page)

    def go(self, ordinal):
        self.check()
        if type(ordinal) is not int or not 1 <= ordinal <= len(self.desk.pages):
            raise ValueError(f'Enter a reading position from 1 to {len(self.desk.pages)}.')
        return self._commit(self.desk.page(ordinal))

    def move(self, direction):
        if self.current is None:
            raise ValueError('Open a reading position first.')
        if type(direction) is not int or direction not in (-1, 1):
            raise ValueError('Choose Previous or Next.')
        return self.go(self.current.ordinal + direction)

    def history_move(self, direction):
        self.check()
        if type(direction) is not int or direction not in (-1, 1):
            raise ValueError('Choose Back or Forward.')
        position = self.history_position + direction
        if not 0 <= position < len(self.history):
            raise ValueError('There is no further location in this reading session.')
        return self._commit(self.desk.validate(self.history[position]), history_position=position)

    def where(self):
        self.check()
        if self.current is None:
            raise ValueError('Open a reading position first.')
        return self.desk.validate(self.current.location)

    def bookmarks(self):
        self.check()
        self._authorize()
        return self.state.bookmarks(self.desk.workspace, self.desk.edition)

    def _authorize(self):
        with self.desk.library.read(self.desk.workspace) as db:
            if self.desk.topic is not None:
                TopicTree(db, self.desk.workspace)._visible(self.desk.topic)

    def add_bookmark(self, label, location):
        self.check()
        if self.current is None or self.current.location != location:
            raise ValueError('The reading position changed. Add the bookmark again.')
        self.desk.validate(location)
        self.check()
        return self.state.add_bookmark(location, label, cancelled=self._stopped)

    def open_bookmark(self, bookmark):
        self.check()
        return self._commit(self.desk.validate(bookmark.location))

    def _unchanged_bookmark(self, bookmark):
        current = next((item for item in self.bookmarks() if item.id == bookmark.id), None)
        if current != bookmark:
            raise ValueError('The bookmark changed. Open the bookmark list again.')

    def rename_bookmark(self, bookmark, label):
        self._unchanged_bookmark(bookmark)
        self.check()
        return self.state.rename_bookmark(self.desk.workspace, self.desk.edition, bookmark.id, label,
                                          cancelled=self._stopped)

    def delete_bookmark(self, bookmark):
        self._unchanged_bookmark(bookmark)
        self.check()
        self.state.delete_bookmark(self.desk.workspace, self.desk.edition, bookmark.id,
                                   cancelled=self._stopped)


def location_text(page):
    return (f'{page.title}; author {page.author or "unknown"}.\n'
            f'Reading position {page.ordinal} of {page.total}. {page.locator.display()}.\n'
            f'{page.status}\n'
            'Resume and bookmarks retain this source page or text segment, '
            'not the screen reader cursor.')


class ReadingDialog:
    """Modal native reader with a cancellable worker and no late UI callbacks."""

    def __init__(self, parent, desk, state, guard):
        import wx

        self.wx, self.parent = wx, parent
        self.dialog = wx.Dialog(parent, title='Read book — ' + desk.title, size=(880, 700))
        self.dialogs = NativeDialogs()
        self.cancelled = threading.Event()
        # Native source validation must observe this dialog's cancel action,
        # independently of the host job that originally created the desk.
        desk.cancelled = self.cancelled.is_set
        self.controller = ReaderController(desk, state, guard, self.cancelled.is_set, defer_display=True)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='reading-desk')
        self.closed = self.busy = False
        self.buttons = []
        self.command_buttons = {}
        self.accelerators = []
        self._accelerator_ids = []
        layout = wx.BoxSizer(wx.VERTICAL)
        self.location = wx.TextCtrl(self.dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.location.SetName('Current source location and verification status')
        self.location.SetMinSize((-1, 90))
        layout.Add(self.location, 0, wx.EXPAND | wx.ALL, 8)
        self.text = wx.TextCtrl(self.dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.text.SetName('Full source page or text segment')
        layout.Add(self.text, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        navigation = wx.WrapSizer(wx.HORIZONTAL)
        self._button(navigation, 'Previous source location', lambda _: self.navigate(-1),
                     command_id='reader.previous')
        self._button(navigation, 'Next source location', lambda _: self.navigate(1),
                     command_id='reader.next')
        self._button(navigation, 'Go to reading position...', self.go_to, command_id='reader.goto')
        self._button(navigation, 'Start at first source location', lambda _: self.go_first(),
                     command_id='reader.first')
        layout.Add(navigation, 0, wx.EXPAND | wx.ALL, 4)
        history = wx.BoxSizer(wx.HORIZONTAL)
        self._button(history, 'Back in reading session', lambda _: self.history(-1),
                     command_id='reader.back')
        self._button(history, 'Forward in reading session', lambda _: self.history(1),
                     command_id='reader.forward')
        self._button(history, 'Where am I?', self.where,
                     command_id='reader.where')
        layout.Add(history, 0, wx.EXPAND | wx.ALL, 4)
        bookmarks = wx.BoxSizer(wx.HORIZONTAL)
        self._button(bookmarks, 'Add named page bookmark...', self.add_bookmark,
                     command_id='reader.bookmark_add')
        self._button(bookmarks, 'Manage page bookmarks...', self.manage_bookmarks,
                     command_id='reader.bookmarks')
        self._button(bookmarks, 'Reader keyboard guide', self.help, command_id='reader.help')
        self._button(bookmarks, 'Find a reader command...', lambda event: self.keyboard.commands(event),
                     command_id='reader.commands')
        self._button(bookmarks, 'Reader keyboard shortcuts...', lambda event: self.keyboard.settings(event),
                     command_id='reader.settings')
        layout.Add(bookmarks, 0, wx.EXPAND | wx.ALL, 4)
        self.status = wx.TextCtrl(self.dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.status.SetName('Reading operation status')
        self.status.SetMinSize((-1, 65))
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        footer = wx.BoxSizer(wx.HORIZONTAL)
        self.cancel = wx.Button(self.dialog, label='Cancel reader operation')
        self.cancel.Bind(wx.EVT_BUTTON, lambda _: self.cancelled.set())
        self.cancel.Disable()
        footer.Add(self.cancel, 0, wx.ALL, 4)
        close = wx.Button(self.dialog, wx.ID_CANCEL, 'Close reader')
        close.Bind(wx.EVT_BUTTON, self.close)
        footer.Add(close, 0, wx.ALL, 4)
        layout.Add(footer, 0, wx.ALIGN_RIGHT)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(wx.ID_CANCEL)
        from .reading_shortcuts import ReaderKeyboardAdapter
        self.keyboard = ReaderKeyboardAdapter(self, getattr(parent, 'keyboard_commands', None))
        self.dialog.Bind(wx.EVT_CLOSE, self.close)

    def _button(self, sizer, label, handler, modifier=None, key=None, *, command_id=None):
        wx = self.wx
        button = wx.Button(self.dialog, label=label)
        button.SetName(label)
        button.Bind(wx.EVT_BUTTON, handler)
        sizer.Add(button, 0, wx.ALL, 3)
        self.buttons.append(button)
        if command_id is not None:
            self.command_buttons[command_id] = (button, handler)
        elif modifier is not None:
            identifier = wx.NewIdRef()
            self._accelerator_ids.append(identifier)
            self.dialog.Bind(wx.EVT_MENU, handler, id=int(identifier))
            self.accelerators.append((modifier, key, int(identifier)))
        return button

    def _error(self, error):
        self.status.ChangeValue(str(error))
        self.status.SetFocus()

    def _run(self, title, operation, done):
        if self.closed:
            return
        if self.busy:
            self._error('Wait for the current reader operation or choose Cancel reader operation.')
            return
        self.busy = True
        self.cancelled.clear()
        for button in self.buttons:
            button.Disable()
        self.cancel.Enable()
        self.status.ChangeValue(title + '…')

        def work():
            self.controller.check()
            return operation()

        def finish(future):
            def apply():
                if self.closed:
                    return
                self.busy = False
                for button in self.buttons:
                    button.Enable()
                self.cancel.Disable()
                try:
                    result = future.result()
                    self.controller.check()
                    done(result)
                except Exception as exc:
                    self._error(exc)
            if not self.closed:
                self.wx.CallAfter(apply)
        try:
            self.executor.submit(work).add_done_callback(finish)
        except Exception as exc:
            self.busy = False
            for button in self.buttons:
                button.Enable()
            self.cancel.Disable()
            self._error(exc)

    def _page(self, navigation):
        self.controller.check()
        page = navigation.page
        self.text.ChangeValue(page.raw_text)
        self.text.SetInsertionPoint(0)
        self.location.ChangeValue(location_text(page))
        # Only this UI-thread acceptance can change resume/history. A worker
        # completed after cancellation or close never reaches this point.
        self.controller.accept(navigation)
        help_shortcut = self.keyboard.shortcut('reader.help') if hasattr(self, 'keyboard') else 'F1'
        self.status.ChangeValue(self.controller.save_warning or
            ('Source location loaded. Page-level resume saved. '
             + (f'Use {help_shortcut} for reader shortcuts.' if help_shortcut else 'Choose Reader keyboard guide for shortcuts.')))
        self.text.SetFocus()

    def navigate(self, direction):
        self._run('Opening source location', lambda: self.controller.move(direction), self._page)

    def go_first(self):
        self._run('Opening first source location', lambda: self.controller.go(1), self._page)

    def history(self, direction):
        self._run('Returning to reading location', lambda: self.controller.history_move(direction), self._page)

    def go_to(self, event=None):
        if self.busy:
            return
        value = self.dialogs.text(self.dialog, 'Go to reading position',
            f'Position from 1 to {len(self.controller.desk.pages)} in source order. '
            'This is not an asserted printed page number.',
            str(self.controller.current.ordinal if self.controller.current else 1))
        if value is None:
            self.text.SetFocus()
            return
        try:
            if not value.strip().isascii() or not value.strip().isdecimal():
                raise ValueError('Enter a whole-number reading position.')
            position = int(value.strip())
            self._run('Opening reading position', lambda: self.controller.go(position), self._page)
        except Exception as exc:
            self._error(exc)

    def where(self, event=None):
        def show(page):
            self.location.ChangeValue(location_text(page))
            self.location.SetInsertionPoint(0)
            self.location.SetFocus()
        self._run('Checking current source location', self.controller.where, show)

    def add_bookmark(self, event=None):
        if self.busy:
            return
        page = self.controller.current
        if page is None:
            self._error('Open a reading position first.')
            return
        label = self.dialogs.text(self.dialog, 'Add page bookmark',
            'Bookmark name. The bookmark saves this source page or text segment.', page.locator.display())
        if label is None:
            self.text.SetFocus()
            return
        self._run('Saving page bookmark', lambda: self.controller.add_bookmark(label, page.location),
                  lambda _: self._message('Page bookmark saved.'))

    def _message(self, value):
        self.status.ChangeValue(value)
        self.status.SetFocus()

    def manage_bookmarks(self, event=None):
        def choose(rows):
            if not rows:
                shortcut = self.keyboard.shortcut('reader.bookmark_add') if hasattr(self, 'keyboard') else 'Ctrl+B'
                self._message('This book has no page bookmarks yet. '
                              + (f'Use {shortcut} to add one.' if shortcut else 'Choose Add named page bookmark.'))
                return
            labels = [f'{item.label}; {item.location.kind.replace("_", " ")} '
                      f'{item.location.index}; source revision {item.location.revision[:12]}' for item in rows]
            index = self.dialogs.choose(self.dialog, 'Page bookmarks', 'Choose a bookmark.', labels)
            if index is None:
                self.text.SetFocus()
                return
            item = _chosen(index, rows)
            action = self.dialogs.choose(self.dialog, 'Bookmark action', item.label,
                                         ('Open bookmark', 'Rename bookmark', 'Delete bookmark'))
            if action is None:
                self.text.SetFocus()
                return
            _chosen(action, (0, 1, 2))
            if action == 0:
                self._run('Opening bookmarked source', lambda: self.controller.open_bookmark(item), self._page)
            elif action == 1:
                label = self.dialogs.text(self.dialog, 'Rename bookmark', 'Bookmark name', item.label)
                if label is not None:
                    self._run('Renaming bookmark', lambda: self.controller.rename_bookmark(item, label),
                              lambda _: self._message('Bookmark renamed.'))
            else:
                confirmation = self.wx.MessageDialog(self.dialog,
                    f'Delete the page bookmark "{item.label}"?', 'Delete bookmark',
                    self.wx.YES_NO | self.wx.NO_DEFAULT | self.wx.ICON_QUESTION)
                try:
                    accepted = confirmation.ShowModal() == self.wx.ID_YES
                finally:
                    confirmation.Destroy()
                if accepted:
                    self._run('Deleting bookmark', lambda: self.controller.delete_bookmark(item),
                              lambda _: self._message('Bookmark deleted. Source text is unchanged.'))
        self._run('Loading page bookmarks', self.controller.bookmarks, choose)

    def help(self, event=None):
        if self.busy:
            return
        shortcuts = (self.keyboard.guide() if hasattr(self, 'keyboard') else
                     'Use the named reader buttons for navigation and bookmarks.')
        self.dialogs.show(self.dialog, 'Reader keyboard guide',
            'Tab and Shift+Tab move among named native controls.\n'
            'The full source text is read-only; use your usual screen-reader text navigation.\n'
            + shortcuts + '\n'
            'Escape: close this reader and return to the library.\n\n'
            'Resume and bookmarks save the current source page or text segment, not the NVDA cursor. '
            'Unknown printed pagination remains unknown. Unverified extracted text remains unverified. '
            'Reading and bookmarks do not approve quotations.\n\n'
            'Reading, Export reading positions and bookmarks saves a separate JSON backup. '
            'The existing library backup does not yet include this reader state.')
        self.text.SetFocus()

    def close(self, event=None):
        if self.closed:
            return
        self.closed = True
        self.cancelled.set()
        if hasattr(self, 'keyboard'):
            self.keyboard.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.dialog.IsModal():
            self.dialog.EndModal(self.wx.ID_CANCEL)

    def show(self):
        try:
            self._run('Resuming source reading', self.controller.start, self._page)
            self.dialog.ShowModal()
        finally:
            self.close()
            self.dialog.Destroy()


@dataclass(frozen=True)
class _Scope:
    library: object
    workspace: str
    topic: str | None

    @property
    def ids(self):
        return self.workspace, self.topic


class ReadingDialogs(NativeDialogs):
    def path(self, parent, *, save):
        import wx
        dialog = wx.FileDialog(parent,
            'Export reading positions and bookmarks' if save else 'Import reading positions and bookmarks',
            defaultFile='reading-bookmarks.json' if save else '',
            wildcard='Reading state JSON (*.json)|*.json',
            style=wx.FD_SAVE if save else (wx.FD_OPEN | wx.FD_FILE_MUST_EXIST))
        try:
            return Path(dialog.GetPath()) if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()


class ReadingActions:
    def __init__(self, frame, *, dialogs=None, desk_factory=ReadingDesk,
                 state_factory=ReadingState, reader_factory=ReadingDialog):
        self.frame = frame
        self.dialogs = dialogs or ReadingDialogs()
        self.desk_factory, self.state_factory, self.reader_factory = desk_factory, state_factory, reader_factory

    def _scope(self):
        frame = self.frame
        if frame.working or getattr(frame, 'busy', False):
            raise ValueError('Wait for the current operation or choose Cancel current operation.')
        if getattr(frame, '_closing', False):
            raise ValueError('The library is closing.')
        if frame.active is None:
            raise ValueError('Open a library first.')
        return _Scope(frame.library, *frame.active)

    def _guard(self, scope):
        if (self.frame.library is not scope.library or self.frame.active != scope.ids
                or getattr(self.frame, '_closing', False)):
            raise ValueError('The library or research topic changed. Open the reader again.')
        with scope.library.read(scope.workspace) as db:
            if scope.topic is not None:
                TopicTree(db, scope.workspace)._visible(scope.topic)

    def _authorize(self, scope):
        self._guard(scope)

    def _completion_guard(self, scope):
        self._guard(scope)
        if self.frame.cancel_event.is_set():
            raise TimeoutError('Reading operation cancelled.')

    def _focus(self, scope, control):
        try:
            self._guard(scope)
            if not self.frame.working:
                control.SetFocus()
        except (ValueError, AttributeError, RuntimeError):
            pass

    def _run(self, scope, operation):
        self._guard(scope)
        if self.frame.cancel_event.is_set():
            raise TimeoutError('Reading operation cancelled.')
        return operation()

    def open_book(self, event=None):
        try:
            scope = self._scope()
            panel = self.frame.book.GetPage(1) if self.frame.book is not None else None
            if panel is not None and (panel.workspace != scope.workspace or not panel.ready):
                raise ValueError('Refresh Books and Collections before reading a book.')
            selection = panel._selection() if panel is not None else (None, None, None)
            edition = selection[2]
            focus = panel.books if panel is not None else self.frame.choice

            def build(selected):
                self._guard(scope)
                self.frame.run_job('Opening source reader',
                    lambda: self._run(scope, lambda: self.desk_factory(scope.library,
                        scope.workspace, selected, topic=scope.topic,
                        cancelled=self.frame.cancel_event.is_set)), opened)

            def opened(desk):
                self._completion_guard(scope)
                # The reader owns cancellation after the initial host job ends.
                reader = self.reader_factory(self.frame, desk, self.state_factory(scope.library.root),
                                             lambda: self._guard(scope))
                try:
                    reader.show()
                finally:
                    self._focus(scope, focus)

            def choose(rows):
                self._completion_guard(scope)
                if not rows:
                    self.frame.report('This library has no books. Import a source first.')
                    return
                labels = [f'{row.title or "Untitled"}; {"; ".join(row.authors) or "unknown author"}; '
                          f'edition {row.edition or "unknown"}; ID {row.edition_id}' for row in rows[:200]]
                index = self.dialogs.choose(self.frame, 'Read a book',
                    'Choose a book.' + (' Showing the first 200 books; use Books and Collections for other books.'
                                        if len(rows) > 200 else ''), labels)
                if index is None:
                    self._focus(scope, focus)
                    return
                build(_chosen(index, rows[:200]).edition_id)

            if edition is None:
                self.frame.run_job('Loading books for reading',
                    lambda: self._run(scope, lambda: scope.library.browse(scope.workspace, limit=201)), choose)
            else:
                if edition not in panel.rows:
                    raise ValueError('The selected book changed. Refresh Books and Collections.')
                build(edition)
        except Exception as exc:
            self.frame.report(f'Reader not opened: {exc}')

    def export_state(self, event=None):
        try:
            scope = self._scope()
            path = self.dialogs.path(self.frame, save=True)
            if path is None:
                return
            approval = confirm_export_destination(self.frame, path, private_root=scope.library.root,
                                                   title='Replace reading positions and bookmarks')
            if approval is None:
                return
            self._guard(scope)

            def save():
                self._authorize(scope)
                state = self.state_factory(scope.library.root)
                content = state.export_state(scope.workspace)
                return self._run(scope, lambda: write_export(path, content,
                    private_root=scope.library.root, approval=approval,
                    cancelled=self.frame.cancel_event.is_set))
            self.frame.run_job('Exporting reading state', lambda: self._run(scope, save),
                lambda receipt: self.frame.report(export_completion(
                    'Reading positions and page bookmarks exported.', receipt)))
        except Exception as exc:
            self.frame.report(f'Reading state export: {exc}')

    def import_state(self, event=None):
        try:
            scope = self._scope()
            path = self.dialogs.path(self.frame, save=False)
            if path is None:
                return
            self._guard(scope)

            def load():
                self._authorize(scope)
                info = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1
                        or bool(getattr(info, 'st_file_attributes', 0) & 0x400)
                        or not 0 < info.st_size <= MAX_BYTES):
                    raise ValueError(f'Choose an ordinary reading-state JSON file no larger than {MAX_BYTES // 1024 // 1024} MiB.')
                with path.open('rb') as source:
                    raw = source.read(MAX_BYTES + 1)
                if len(raw) != info.st_size or len(raw) > MAX_BYTES:
                    raise ValueError('The reading-state file changed while opening it.')
                content = raw.decode('utf-8')
                state, desks = self.state_factory(scope.library.root), {}

                def validate(location):
                    self._guard(scope)
                    if self.frame.cancel_event.is_set():
                        raise TimeoutError('Reading-state import cancelled.')
                    if location.workspace != scope.workspace:
                        raise ValueError('The imported bookmark belongs to another library.')
                    if location.edition not in desks:
                        desks[location.edition] = self.desk_factory(scope.library, scope.workspace,
                            location.edition, topic=scope.topic, cancelled=self.frame.cancel_event.is_set)
                    return desks[location.edition].validate(location)
                def stopped():
                    self._guard(scope)
                    return self.frame.cancel_event.is_set()
                return self._run(scope, lambda: state.import_state(scope.workspace, content, validate,
                                                                  cancelled=stopped))

            self.frame.run_job('Importing and validating reading state', lambda: self._run(scope, load),
                lambda count: self.frame.report(f'Reading state imported: {count} entries. Existing bookmarks were kept.'))
        except Exception as exc:
            self.frame.report(f'Reading state was not imported: {exc}')


def install_reading_desk_actions(frame):
    existing = getattr(frame, 'reading_desk_actions', None)
    if existing is not None:
        return existing
    import wx
    actions = ReadingActions(frame)
    menu = wx.Menu()
    for label, handler in (
            ('&Read selected book...\tCtrl+R', actions.open_book),
            ('&Export reading positions and bookmarks...', actions.export_state),
            ('&Import reading positions and bookmarks...', actions.import_state)):
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, handler, item)
    frame.GetMenuBar().Append(menu, 'Rea&ding')
    frame.reading_desk_actions = actions
    return actions
