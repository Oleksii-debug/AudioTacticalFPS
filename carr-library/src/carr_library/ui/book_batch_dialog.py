"""Native ordered book reviews over current managed Studio jobs."""
from dataclasses import asdict
from pathlib import Path
import threading

from ..audio.book_audio import BookAudioService
from ..audio.book_batch import BookBatchService, select_books, validate_plans
from ..audio.book_plan import SplitOptions, capture_plan, MODES, FORMATS
from ..audio.models import Role


HELP = (
    'Select a retained source in Books and Collections, then use Add selected book. '
    'Each row freezes its own literal source, split, voice and format. Move up/down sets exact execution order. '
    'Changing the settings below affects only the next captured review. Replace selected review recaptures that book.\n\n'
    'Preview shows all exact output names and starts no speech. Prepare retains ordinary Studio projects. '
    'Run is separate: completed production receipts and files are rechecked before reuse. '
    'Pause or Cancel stops this invocation; existing managed parts remain available for an explicit later Run. '
    'No output permission is retained in a saved batch. Choose and review the destination again after reopening.\n\n'
    'Limits: 32 books, 256 parts, eight million selected characters in total; two million per book. '
    'Character/page/heading boundaries are exact source ranges, not duration estimates. '
    'A source-bound part stays immutable; use a separate reading copy for performance edits. '
    'Complete library backup retains book plans, originals and managed audio together.'
)


class BookWorkspace:
    """Portable review policy; native controls never act through a stale row index."""
    def __init__(self, service):
        self.service = service
        self.rows = []
        self.title = 'Book audio batch'
        self.batch = self.reviewed = None
        self.generation = 0

    def invalidate(self, *, selection_changed=False):
        self.generation += 1
        self.reviewed = None
        if selection_changed:
            self.batch = None

    def set_title(self, title):
        if title != self.title:
            self.title = title
            self.invalidate(selection_changed=True)

    def add(self, plan, *, replace_id=None):
        if replace_id is None:
            proposed = [*self.rows, {'plan': plan, 'output': None}]
        else:
            index = self.index(replace_id)
            proposed = [dict(row) for row in self.rows]
            proposed[index] = {'plan': plan, 'output': proposed[index]['output']}
        select_books(tuple(row['plan'] for row in proposed), title=self.title)
        self.rows = proposed
        self.invalidate(selection_changed=True)

    def index(self, identity):
        matches = [index for index, row in enumerate(self.rows) if row['plan'].id == identity]
        if len(matches) != 1:
            raise ValueError('The selected reviewed book changed. Select it again.')
        return matches[0]

    def move(self, identity, offset):
        if offset not in (-1, 1):
            raise ValueError('Move one reviewed book up or down.')
        index = self.index(identity)
        target = index + offset
        if not 0 <= target < len(self.rows):
            return
        self.rows[index], self.rows[target] = self.rows[target], self.rows[index]
        self.invalidate(selection_changed=True)

    def remove(self, identity):
        del self.rows[self.index(identity)]
        self.invalidate(selection_changed=True)

    def output(self, path, identity=None):
        if identity is None:
            for row in self.rows:
                row['output'] = Path(path)
        else:
            self.rows[self.index(identity)]['output'] = Path(path)
        self.invalidate()

    def snapshot(self):
        if not self.rows or any(row['output'] is None for row in self.rows):
            raise ValueError('Add reviewed books and explicitly choose every output parent first.')
        plans = tuple(row['plan'] for row in self.rows)
        if self.batch is None:
            self.batch = select_books(plans, title=self.title)
        validate_plans(self.batch, plans)
        return self.generation, self.batch, plans, tuple(row['output'] for row in self.rows)

    def accept_review(self, snapshot, reviewed):
        generation, batch, plans, outputs = snapshot
        if generation != self.generation or self.snapshot()[1:] != snapshot[1:]:
            raise ValueError('The selection changed while reviewing. Preview the current order again.')
        if reviewed.batch_sha256 != batch.fingerprint:
            raise ValueError('The returned review belongs to another ordered selection.')
        self.reviewed = reviewed

    def load(self, identity):
        batch = self.service.load(identity)
        plans = self.service.plans(batch)
        self.rows = [{'plan': plan, 'output': None} for plan in plans]
        self.title, self.batch = batch.title, batch
        self.invalidate()


def review_text(batch, plans, reviewed):
    lines = ['PREPARED — Run is a separate action.' if reviewed.prepared else 'PREVIEW — no audio has started.',
             batch.title, 'Review SHA-256: ' + reviewed.fingerprint]
    for number, (plan, review) in enumerate(zip(plans, reviewed.books), 1):
        lines += ['', '%d. %s' % (number, plan.source['title']),
                  'Voice: %s; language: %s; format: %s; rate: %s' %
                  (plan.role.voice, plan.role.language, plan.format, plan.sample_rate),
                  'Speech rate: %s; pitch: %s; volume: %s; pan: %s; effects: %s' %
                  (plan.role.rate, plan.role.pitch, plan.role.volume, plan.role.pan, len(plan.role.effects)),
                  'Split: %s; exact parts: %d' % (plan.options.mode, len(plan.parts))]
        for index, (part, destination) in enumerate(zip(plan.parts, review.destinations), 1):
            lines.append('%d. Characters %d..%d; %s' % (index, part.start, part.end, destination))
        lines += plan.source['warnings']
    return '\n'.join(lines)


class BookBatchDialog:
    def __init__(self, frame, *, service=None, wx_module=None):
        if wx_module is None:
            import wx as wx_module
        wx = self.wx = wx_module
        self.frame, self.library, self.scope = frame, frame.library, frame.active
        if self.scope is None:
            raise ValueError('Select an existing library first.')
        self.books = service or BookAudioService(self.library, self.scope[0])
        self.groups = BookBatchService(self.books)
        self.state = BookWorkspace(self.groups)
        self.cancel = threading.Event()
        self.alive, self.busy, self.closing = True, False, False
        self.action = 'pause'
        self.operation_token = 0
        self.displayed = ()
        self.voices = ()
        self.dialog = wx.Dialog(frame, title='Reviewed books — ordered audio batch', size=(980, 850),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.dialog.SetName('Reviewed multi-book audio workspace')
        self.dialog_id = self.dialog.GetId()
        self.body = wx.ScrolledWindow(self.dialog, style=wx.VSCROLL | wx.TAB_TRAVERSAL)
        self.body.SetScrollRate(0, 12)
        layout = wx.BoxSizer(wx.VERTICAL)
        def field(label, factory=wx.TextCtrl, **kwargs):
            layout.Add(wx.StaticText(self.body, label=label), 0, wx.ALL, 3)
            item = factory(self.body, **kwargs)
            item.SetName(label.replace('&', ''))
            layout.Add(item, 0, wx.EXPAND | wx.ALL, 3)
            return item
        self.title = field('Batch title', value=self.state.title)
        self.selected = field('Reviewed books in execution order', wx.ListBox, choices=[])
        self.voice = field('Explicit local voice for the next review', wx.Choice, choices=[])
        self.language = field('Language for the next review', value='uk')
        self.voice_rate = field('Speech rate from -10 to 10', value='0')
        self.pitch = field('Speech pitch from -10 to 10', value='0')
        self.volume = field('Speech volume from 0 to 1', value='1')
        self.pan = field('Stereo position from -1 left to 1 right', value='0')
        self.mode = field('Split mode for the next review', wx.Choice, choices=list(MODES))
        self.mode.SetSelection(0)
        self.characters = field('Characters per part when using characters mode', value='10000')
        self.pattern = field('Isolated regular expression when using regex mode', value='')
        self.pattern.SetMaxLength(1000)
        self.ignore_case = field('Ignore case for regex', wx.CheckBox, label='Explicit case-insensitive matching')
        self.template = field('Part filename template', value='{index:04d} - {book} - {part}')
        self.format = field('Output format for the next review', wx.Choice, choices=list(FORMATS))
        self.format.SetSelection(0)
        self.rate = field('Sample rate for the next review', wx.Choice,
                          choices=['8000', '16000', '22050', '24000', '44100', '48000', '96000'])
        self.rate.SetSelection(5)
        self.first = field('First source position; blank means first', value='')
        self.last = field('Last source position; blank means last', value='')
        self.report_box = field('Exact review, progress and retained result', style=wx.TE_MULTILINE | wx.TE_READONLY,
                                value=HELP)
        self.report_box.SetMinSize((-1, 230))
        self.buttons = {}
        for definitions in (
                [('voices', 'Refresh local voices'), ('add', 'Add selected library book'),
                 ('replace', 'Replace selected review')],
                [('up', 'Move up'), ('down', 'Move down'), ('remove', 'Remove reviewed book'),
                 ('output', 'Choose output for selected book'), ('all_outputs', 'Choose output for all books')],
                [('preview', 'Preview exact batch'), ('prepare', 'Prepare managed parts'),
                 ('run', 'Run or resume'), ('pause', 'Pause'), ('cancel', 'Cancel invocation')],
                [('saved', 'Open saved batch'), ('new', 'New batch'), ('studio', 'Open selected part in Audio Studio'),
                 ('help', 'Help'), ('close', 'Close')]):
            row = wx.WrapSizer(wx.HORIZONTAL)
            for key, label in definitions:
                button = wx.Button(self.body, wx.ID_CANCEL if key == 'close' else wx.ID_ANY, label)
                button.SetName(label)
                button.Bind(wx.EVT_BUTTON, lambda event, key=key: self.dispatch(key))
                self.buttons[key] = button
                row.Add(button, 0, wx.ALL, 3)
            layout.Add(row, 0, wx.EXPAND)
        self.body.SetSizer(layout)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(self.body, 1, wx.EXPAND)
        self.dialog.SetSizer(outer)
        self.title.Bind(wx.EVT_TEXT, self.changed_title)
        self.dialog.Bind(wx.EVT_CLOSE, self.close)
        self.dialog.Bind(wx.EVT_WINDOW_DESTROY, self.destroyed)
        self.controls()

    def guard(self):
        if (not self.alive or getattr(self.frame, '_closing', False)
                or self.frame.library is not self.library or self.frame.active != self.scope):
            raise ValueError('The active library changed. Reopen the book review in its original scope.')

    def stopped(self):
        return (self.cancel.is_set() or not self.alive or self.closing
                or self.frame.cancel_event.is_set() or getattr(self.frame, '_closing', False)
                or self.frame.library is not self.library or self.frame.active != self.scope)

    def report(self, message):
        if self.alive and not getattr(self.frame, '_closing', False):
            self.report_box.ChangeValue(str(message))

    def controls(self):
        if not self.alive:
            return
        for key, button in self.buttons.items():
            button.Enable(not self.busy or key in ('pause', 'cancel', 'close'))
        self.buttons['prepare'].Enable(not self.busy and self.state.reviewed is not None)
        self.buttons['run'].Enable(not self.busy and self.state.reviewed is not None and self.state.reviewed.prepared)
        for key in ('pause', 'cancel'):
            self.buttons[key].Enable(self.busy)
        for item in (self.title, self.selected, self.voice, self.language, self.voice_rate, self.pitch, self.volume,
                     self.pan, self.mode, self.characters,
                     self.pattern, self.ignore_case, self.template, self.format, self.rate, self.first, self.last):
            item.Enable(not self.busy)

    def selection(self):
        index = self.selected.GetSelection()
        if not 0 <= index < len(self.displayed):
            raise ValueError('Choose a reviewed book.')
        identity = self.displayed[index]
        self.state.index(identity)
        return identity

    def render(self, keep=None):
        self.displayed = tuple(row['plan'].id for row in self.state.rows)
        self.selected.SetItems(['%d. %s; %s; %s; %d parts%s' %
            (index, row['plan'].source['title'], row['plan'].role.voice, row['plan'].format,
             len(row['plan'].parts), '; output not reviewed' if row['output'] is None else '')
            for index, row in enumerate(self.state.rows, 1)])
        self.selected.SetSelection(self.displayed.index(keep) if keep in self.displayed else self.wx.NOT_FOUND)
        self.controls()

    def changed_title(self, event=None):
        if not self.busy:
            self.state.set_title(self.title.GetValue())
            self.controls()
        if event is not None:
            event.Skip()

    def begin(self, label, operation, done, *, accept_after_stop=False):
        self.guard()
        if self.busy or self.frame.working or getattr(self.frame, 'busy', False):
            raise ValueError('Wait for or stop the current operation first.')
        self.busy, self.action = True, 'pause'
        self.operation_token += 1
        self.cancel.clear()
        self.controls()
        def work():
            self.guard()
            if self.stopped():
                raise InterruptedError('Book operation stopped before starting.')
            return operation()
        def settled(value=None, error=None):
            self.busy = False
            if not self.alive:
                return
            if self.closing:
                self.dispose()
                return
            try:
                self.guard()
                if error is not None:
                    self.report(error)
                elif accept_after_stop or not self.stopped():
                    done(value)
                else:
                    self.report('The operation stopped. Existing managed parts and accepted exports are retained.')
            except Exception as exc:
                self.report(exc)
            self.controls()
            if self.alive:
                self.report_box.SetFocus()
        try:
            accepted = self.frame.run_job(label, work, done=lambda value: settled(value),
                                          failed=lambda error: settled(error=error))
        except BaseException:
            self.busy = False
            self.controls()
            raise
        if accepted is False:
            self.busy = False
            self.controls()

    def _progress(self, token, message):
        if (self.alive and self.busy and token == self.operation_token
                and self.frame.library is self.library and self.frame.active == self.scope):
            self.report(message)

    def _choices(self, title, labels):
        wx = self.wx
        prompt = wx.SingleChoiceDialog(self.dialog, 'Choose an existing reviewed item.', title, labels)
        try:
            if prompt.ShowModal() != wx.ID_OK:
                return None
            selected = prompt.GetSelection()
        finally:
            prompt.Destroy()
        self.guard()
        return selected if 0 <= selected < len(labels) else None

    def settings(self):
        index = self.voice.GetSelection()
        if not 0 <= index < len(self.voices):
            raise ValueError('Refresh local voices and explicitly select one before capture.')
        voice = self.voices[index]
        role = Role('Narrator', voice=voice['id'], language=self.language.GetValue().strip(),
                    rate=int(self.voice_rate.GetValue()), pitch=int(self.pitch.GetValue()),
                    volume=float(self.volume.GetValue()), pan=float(self.pan.GetValue()))
        options = SplitOptions(mode=self.mode.GetStringSelection(), characters=int(self.characters.GetValue()),
            pattern=self.pattern.GetValue(), ignore_case=self.ignore_case.GetValue(),
            name_template=self.template.GetValue())
        def position(control):
            return None if not control.GetValue().strip() else int(control.GetValue())
        return dict(options=options, role=role, format=self.format.GetStringSelection(),
                    sample_rate=int(self.rate.GetStringSelection()), first=position(self.first), last=position(self.last))

    def capture(self, replace=False):
        settings = self.settings()
        identity = self.selection() if replace else None
        if replace:
            plan = self.state.rows[self.state.index(identity)]['plan']
            anchor = plan.source['source_anchor']
            edition, revision, topic = anchor['edition'], anchor['revision'], plan.source['source_topic']
        else:
            panel = self.frame.book.GetPage(1) if self.frame.book is not None else None
            if panel is None or panel.workspace != self.scope[0] or not panel.ready:
                raise ValueError('Select a retained source in Books and Collections first.')
            selected = panel._selection()
            edition = selected[2]
            if edition is None:
                raise ValueError('Select a retained source first.')
            revision, topic = None, None
        generation = self.state.generation
        def create(bookmark_ids=None):
            def work():
                from ..operations import ResourceBroker, ResourceRequest
                broker = ResourceBroker.for_app(self.library.root, cancelled=self.stopped)
                with broker.reserve(ResourceRequest(cpu_slots=1, memory_bytes=256 * 1024**2),
                                    operation_id='document:' + __import__('uuid').uuid4().hex,
                                    cancelled=self.stopped, timeout=60) as lease:
                    return capture_plan(self.library, self.scope[0], edition, **settings,
                        revision=revision, topic=topic, bookmark_ids=bookmark_ids,
                        cancelled=lease.cancelled, guard=self.guard)
            def accepted(plan):
                if self.state.generation != generation:
                    raise ValueError('The reviewed order changed during source capture.')
                if not replace and (self.frame.book.GetPage(1) is not panel or panel._selection() != selected):
                    raise ValueError('The selected book changed during capture. Select and review it again.')
                self.state.add(plan, replace_id=identity)
                self.render(plan.id)
                self.report('Retained source preview: %d exact parts. Choose an output parent and Preview entire batch.' % len(plan.parts))
            self.begin('Capturing exact retained book source and split', work, accepted)
        if settings['options'].mode != 'bookmarks':
            create()
            return
        from ..audio.book_landmarks import available_bookmarks
        def choose(rows):
            wx = self.wx
            valid = [row['bookmark'] for row in rows if row['current_revision']]
            if not valid:
                raise ValueError('This source has no current reading bookmarks.')
            prompt = wx.MultiChoiceDialog(self.dialog, 'Choose exact current source bookmarks.',
                'Book split bookmarks', [item.label for item in valid])
            try:
                if prompt.ShowModal() != wx.ID_OK:
                    return
                indices = tuple(prompt.GetSelections())
            finally:
                prompt.Destroy()
            self.guard()
            if not indices:
                return
            create([valid[index].id for index in indices])
        self.begin('Reading current bookmark choices',
            lambda: available_bookmarks(self.library, self.scope[0], edition, topic=topic,
                                        revision=revision, cancelled=self.stopped), choose)

    def preview(self, prepare=False, run=False):
        snapshot = self.state.snapshot()
        _, batch, plans, outputs = snapshot
        approved = self.state.reviewed
        if (prepare or run) and approved is None:
            raise ValueError('Preview the exact current batch first.')
        def completed(value):
            if run:
                reused = sum(bool(item.get('retained_completion')) for row in value['books']
                             for item in row['items'])
                self.report('Invocation ' + value['status'] + '. Completed parts keep their verified receipts.\n' +
                            ('Retained completions reused: %d; these are not new synthesis or engine qualification.\n' % reused) +
                            str(value['stored']))
            else:
                self.state.accept_review(snapshot, value)
                self.report(review_text(batch, plans, value))
        if run:
            token = self.operation_token + 1
            operation = lambda: self.groups.run(batch.id, outputs, approval=approved.fingerprint,
                cancelled=self.stopped, cancel_action=lambda: self.action, guard=self.guard,
                progress=lambda message: self.wx.CallAfter(self._progress, token, message))
        elif prepare:
            operation = lambda: self.groups.prepare(batch, outputs, plans=plans, approval=approved.fingerprint,
                                                    cancelled=self.stopped, guard=self.guard)
        else:
            operation = lambda: self.groups.review(batch, outputs, plans=plans,
                                                   cancelled=self.stopped, guard=self.guard)
        self.begin('Running reviewed book batch' if run else 'Preparing managed book parts' if prepare
                   else 'Reviewing exact ordered book destinations', operation, completed, accept_after_stop=run)

    def dispatch(self, key):
        try:
            if key == 'close':
                self.close()
                return
            if key in ('pause', 'cancel'):
                self.action = key
                self.cancel.set()
                self.report('Stop requested; waiting for the owned worker and current publication to settle.')
                return
            self.guard()
            if self.busy:
                raise ValueError('Wait for or stop the current book operation.')
            if key == 'help':
                self.report(HELP)
            elif key in ('add', 'replace'):
                self.capture(replace=key == 'replace')
            elif key == 'voices':
                def loaded(values):
                    self.voices = tuple(value for value in values if value.get('available', True))
                    self.voice.SetItems([value['name'] + ' — ' + value['id'] for value in self.voices])
                    self.voice.SetSelection(self.wx.NOT_FOUND)
                    self.report('Explicitly choose a local voice and language. No first voice is selected automatically.')
                from .audio_studio import StudioController
                controller = StudioController(private_root=self.library.root, registry=self.books.engine)
                def discover():
                    try:
                        return controller.voices(cancelled=self.stopped)
                    finally:
                        if controller._owns_registry:
                            cleanup = getattr(controller.registry, 'close', None)
                            if callable(cleanup):
                                cleanup()
                self.begin('Discovering current local voice profiles', discover, loaded)
            elif key in ('up', 'down', 'remove'):
                identity = self.selection()
                if key == 'remove':
                    self.state.remove(identity)
                else:
                    self.state.move(identity, -1 if key == 'up' else 1)
                self.render(identity)
            elif key in ('output', 'all_outputs'):
                identity = self.selection() if key == 'output' else None
                prompt = self.wx.DirDialog(self.dialog, 'Choose an existing external export parent',
                                            style=self.wx.DD_DEFAULT_STYLE | self.wx.DD_DIR_MUST_EXIST)
                try:
                    if prompt.ShowModal() != self.wx.ID_OK:
                        return
                    path = prompt.GetPath()
                finally:
                    prompt.Destroy()
                self.guard()
                from ..audio.book_export import output_parent
                parent, _ = output_parent(path, self.library.root)
                self.state.output(parent, identity)
                self.render(identity)
            elif key in ('preview', 'prepare', 'run'):
                self.preview(prepare=key == 'prepare', run=key == 'run')
            elif key == 'saved':
                def chosen(values):
                    index = self._choices('Saved ordered book batches', [value.title for value in values])
                    if index is not None:
                        identity = values[index].id
                        self.begin('Opening retained book plans',
                            lambda: (self.groups.load(identity), self.groups.plans(self.groups.load(identity))),
                            self._opened)
                self.begin('Listing retained book batch reviews',
                           lambda: self.groups.saved(cancelled=self.stopped), chosen)
            elif key == 'new':
                self.state = BookWorkspace(self.groups)
                self.title.ChangeValue(self.state.title)
                self.render()
                self.report(HELP)
            elif key == 'studio':
                plan = self.state.rows[self.state.index(self.selection())]['plan']
                index = self._choices('Managed book part', [part.label for part in plan.parts])
                if index is not None:
                    _, document, _ = self.books.managed.paths(plan.job_id(index + 1))
                    from .audio_studio import StudioController, AudioStudioDialog
                    def opened(controller):
                        AudioStudioDialog(self.frame, controller).show()
                    self.begin('Opening the source-bound managed part',
                        lambda: StudioController(private_root=self.library.root).open(document), opened)
        except Exception as exc:
            self.report(exc)

    def _opened(self, values):
        batch, plans = values
        self.state.rows = [{'plan': plan, 'output': None} for plan in plans]
        self.state.title, self.state.batch = batch.title, batch
        self.state.invalidate()
        self.title.ChangeValue(batch.title)
        self.render()
        self.report('Saved reviews opened. Explicitly choose new output parents and Preview before Run.')

    def close(self, event=None):
        if self.busy:
            self.closing = True
            self.cancel.set()
            self.report('Closing after the owned book operation settles; existing receipts are retained.')
            if event is not None and event.CanVeto():
                event.Veto()
                return
            if event is not None:
                event.Skip()
            return
        self.dispose()

    def dispose(self):
        if self.alive:
            self.alive = False
            self.cancel.set()
            self.books.close()
            self.dialog.Destroy()

    def destroyed(self, event):
        if event.GetId() == self.dialog_id:
            self.alive = False
            self.cancel.set()
            self.books.close()
        event.Skip()

    def show(self):
        self.dialog.Show()
        self.selected.SetFocus()
        return self


class BookBatchActions:
    def __init__(self, frame):
        self.frame = frame
        self.views = []

    def open(self, event=None):
        try:
            if getattr(self.frame, '_closing', False) or self.frame.working or getattr(self.frame, 'busy', False):
                raise ValueError('Finish the current desktop operation first.')
            view = BookBatchDialog(self.frame).show()
            self.views = [item for item in self.views if item.alive] + [view]
            return view
        except Exception as exc:
            self.frame.report('Book batch could not open: ' + str(exc))


def install_book_batch_actions(frame):
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    existing = getattr(frame, 'book_batch_actions', None)
    if existing is not None:
        return existing
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                 if frame.menu_bar.GetMenuLabel(index).replace('&', '').casefold() in ('audio', 'audio studio')), None)
    if menu is None:
        raise ValueError('Install the shared Audio menu before book batch actions.')
    actions = BookBatchActions(frame)
    item = menu.Append(wx.ID_ANY, 'Review multiple books for audio...')
    frame.Bind(wx.EVT_MENU, actions.open, item)
    register_menu_command(frame, CommandSpec('audio.book-batch', 'Review multiple books for audio', 'main'), item)
    frame.book_batch_actions = actions
    return actions
