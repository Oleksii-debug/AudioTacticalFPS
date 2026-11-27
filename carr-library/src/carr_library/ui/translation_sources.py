"""Native bounded document/chapter selection for the current translation workspace."""
from threading import Event

from ..assistant.translation_structure import capture_library


class TranslationSourcesDialog:
    def __init__(self, frame, parent, *, on_ready=None):
        if (frame.active is None or getattr(frame, 'working', False)
                or getattr(frame, 'busy', False) or getattr(frame, '_closing', False)):
            raise ValueError('Open a library and finish the current operation before selecting translation sources.')
        import wx
        self.wx, self.frame = wx, frame
        self.library, self.scope = frame.library, frame.active
        self.workspace = self.scope[0]
        self.cancel = Event()
        self.alive, self.busy = True, False
        self.rows, self.selections, self.history = (), [], []
        self.after, self.has_next, self.result = None, False, None
        self.query_value = None
        self.on_ready = on_ready
        self.dialog = wx.Dialog(parent, title='Choose translation documents and chapters',
            size=(920, 760), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.dialog.SetName('Translation document selection')
        layout = wx.BoxSizer(wx.VERTICAL)
        def field(label, factory=wx.TextCtrl, proportion=0, **options):
            layout.Add(wx.StaticText(self.dialog, label=label), 0, wx.ALL, 4)
            control = factory(self.dialog, **options)
            control.SetName(label.replace('&', ''))
            layout.Add(control, proportion, wx.EXPAND | wx.ALL, 4)
            return control
        self.filter = field('Literal title filter; blank shows all available books', value='')
        self.books = field('Available retained editions; 50 per page', wx.ListBox, proportion=1,
                           choices=[], style=wx.LB_SINGLE)
        self.kind = field('Selection type', wx.Choice, choices=['Whole document', 'Inclusive source-location range', 'One actual EPUB chapter'])
        self.kind.SetSelection(0)
        self.first = field('First reading-order source location or EPUB chapter ordinal', value='1')
        self.last = field('Last reading-order source location; required for a range', value='')
        self.selected = field('Ordered translation selections; at most 32', wx.ListBox,
                              proportion=1, choices=[], style=wx.LB_SINGLE)
        self.markdown = wx.CheckBox(self.dialog, label='Preserve tested Markdown ATX headings and literal fenced code')
        self.markdown.SetName('Markdown structure profile')
        layout.Add(self.markdown, 0, wx.ALL, 4)
        self.status = field('Selection status', value='Choose books and add them in the intended order.',
                            style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.buttons = {}
        bar = wx.WrapSizer(wx.HORIZONTAL)
        for key, label, action in (
            ('search', 'Search titles', lambda event: self.load(None, [])),
            ('previous', 'Previous books', self.previous),
            ('next', 'Next books', self.next),
            ('add', 'Add selected document or range', self.add),
            ('remove', 'Remove selected range', self.remove),
            ('up', 'Move selection up', lambda event: self.move(-1)),
            ('down', 'Move selection down', lambda event: self.move(1)),
            ('prepare', 'Capture exact sources', self.capture),
            ('close', 'Cancel selection', self.close)):
            button = wx.Button(self.dialog, id=wx.ID_CLOSE if key == 'close' else wx.ID_ANY, label=label)
            button.SetName(label)
            button.Bind(wx.EVT_BUTTON, action)
            bar.Add(button, 0, wx.ALL, 3)
            self.buttons[key] = button
        layout.Add(bar, 0, wx.EXPAND)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(wx.ID_CLOSE)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.enable()

    def guard(self):
        if (self.frame.library is not self.library or self.frame.active != self.scope
                or getattr(self.frame, '_closing', False)):
            raise ValueError('The source library changed. Open document selection again.')
        if self.cancel.is_set() or getattr(self.frame, 'cancel_event', self.cancel).is_set():
            raise InterruptedError('Translation source selection cancelled.')

    def stopped(self):
        self.guard()
        return False

    def say(self, message):
        if self.alive:
            self.status.ChangeValue(str(message))

    def enable(self):
        if not self.alive:
            return
        for control in (self.filter, self.books, self.kind, self.first, self.last,
                        self.selected, self.markdown):
            control.Enable(not self.busy)
        for key, button in self.buttons.items():
            button.Enable(key == 'close' or not self.busy)
        self.buttons['previous'].Enable(not self.busy and bool(self.history))
        self.buttons['next'].Enable(not self.busy and self.has_next)
        self.buttons['add'].Enable(not self.busy and bool(self.rows) and len(self.selections) < 32)
        self.buttons['prepare'].Enable(not self.busy and bool(self.selections))

    def work(self, label, action, done):
        if not self.alive or self.busy:
            return False
        self.guard()
        self.busy = True
        self.enable()
        def complete(result):
            if not self.alive:
                return
            self.busy = False
            try:
                self.guard()
                done(result)
            except Exception as exc:
                self.say(exc)
            finally:
                self.enable()
        def failed(error):
            if self.alive:
                self.busy = False
                self.say(error)
                self.enable()
        try:
            accepted = self.frame.run_job(label, action, done=complete, failed=failed)
        except Exception:
            self.busy = False
            self.enable()
            raise
        if accepted is False:
            self.busy = False
            self.enable()
            self.say('The current shared operation did not accept source selection.')
            return False
        return True

    def load(self, after, history):
        try:
            self.guard()
            title = self.filter.GetValue().strip() or None
            if after is not None and title != self.query_value:
                raise ValueError('Search the changed title filter before continuing its pages.')
            def read():
                self.guard()
                rows = self.library.browse(self.workspace, after=after, title=title, limit=51)
                self.guard()
                return rows
            def loaded(rows):
                self.rows, self.has_next = tuple(rows[:50]), len(rows) > 50
                self.after, self.history = after, list(history)
                self.query_value = title
                self.books.SetItems([row.title + ' — ' + ', '.join(row.authors) + ' — ' + row.edition_id
                                     for row in self.rows])
                if self.rows:
                    self.books.SetSelection(0)
                self.say(f'{len(self.rows)} editions on this page. '
                         + ('More books are available.' if self.has_next else 'End of the current title selection.'))
            self.work('Loading translation source choices', read, loaded)
        except Exception as exc:
            self.say(exc)

    def previous(self, event=None):
        if self.history and not self.busy:
            self.load(self.history[-1], self.history[:-1])

    def next(self, event=None):
        if self.rows and self.has_next and not self.busy:
            self.load(self.rows[-1].edition_id, self.history + [self.after])

    def refresh_selected(self, index=None):
        self.selected.SetItems([f"{i + 1}. {row['title']} — {row['kind']} — "
            + str(row['first']) + '..' + ('end' if row['last'] is None else str(row['last']))
            for i, row in enumerate(self.selections)])
        if index is not None and 0 <= index < len(self.selections):
            self.selected.SetSelection(index)
        self.enable()

    def add(self, event=None):
        if self.busy:
            return
        try:
            self.guard()
            index = self.books.GetSelection()
            if not 0 <= index < len(self.rows) or len(self.selections) >= 32:
                raise ValueError('Choose an available book within the 32-document batch allowance.')
            kind = self.kind.GetSelection()
            if kind not in (0, 1, 2):
                raise ValueError('Choose a whole document or an explicit source range.')
            first = 1 if kind == 0 else int(self.first.GetValue())
            last = int(self.last.GetValue()) if kind == 1 else None
            if first < 1 or last is not None and last < first:
                raise ValueError('Choose a positive inclusive source-location range.')
            book = self.rows[index]
            self.selections.append({'workspace': self.workspace, 'edition': book.edition_id,
                'kind': ('document', 'range', 'chapter')[kind], 'first': first, 'last': last,
                'title': book.title, 'topic': self.scope[1]})
            self.refresh_selected(len(self.selections) - 1)
        except Exception as exc:
            self.say(exc)

    def remove(self, event=None):
        if self.busy:
            return
        index = self.selected.GetSelection()
        if 0 <= index < len(self.selections):
            self.selections.pop(index)
            self.refresh_selected(min(index, len(self.selections) - 1))

    def move(self, direction):
        if self.busy:
            return
        index = self.selected.GetSelection()
        other = index + direction
        if 0 <= index < len(self.selections) and 0 <= other < len(self.selections):
            self.selections[index], self.selections[other] = self.selections[other], self.selections[index]
            self.refresh_selected(other)

    def capture(self, event=None):
        try:
            self.guard()
            selected = tuple(dict(row) for row in self.selections)
            markdown = self.markdown.GetValue()
            def prepare():
                self.guard()
                result = capture_library(self.library, selected, markdown=markdown, cancelled=self.stopped)
                self.guard()
                return result
            def captured(result):
                self.result = result
                self.alive = False
                self.dialog.EndModal(self.wx.ID_OK)
            self.work('Capturing exact ordered translation sources', prepare, captured)
        except Exception as exc:
            self.say(exc)

    def close(self, event=None):
        if self.alive:
            self.cancel.set()
            self.alive = False
            self.dialog.EndModal(self.wx.ID_CLOSE)

    def show(self):
        try:
            self.wx.CallAfter(self.load, None, [])
            if self.on_ready is not None:
                self.wx.CallAfter(self.on_ready, self)
            self.dialog.ShowModal()
            return self.result
        finally:
            self.alive = False
            self.cancel.set()
            self.dialog.Destroy()


def choose_translation_sources(frame, parent, *, on_ready=None):
    return TranslationSourcesDialog(frame, parent, on_ready=on_ready).show()
