"""Explicit, keyboard-first search across selected local research libraries.

The dialog owns its result handles. It never changes the desktop's library,
topic, selection cache, or notebook, including an unsaved evidence note.
Native wx controls are imported only when the desktop command is opened.
"""
from dataclasses import dataclass, replace

from ..search.federated import FederatedSearch, _workspaces
from ..search.paging import LIBRARIES_PER_PAGE, MAX_TIMEOUT_SECONDS, SearchResources
from .research_actions import NativeDialogs
from .scope_bindings import scope_labels


MODES = ('phrase', 'word', 'terms', 'thematic', 'semantic', 'advanced')
PRESENTATIONS = ('short', 'thematic')
LIMITS = (10, 25, 50, 100)
PAGED_MODES = MODES


@dataclass(frozen=True)
class SearchOptions:
    workspaces: tuple[str, ...]
    query: str
    mode: str = 'phrase'
    result_mode: str = 'short'
    title: str | None = None
    author: str | None = None
    language: str | None = None
    edition: str | None = None
    limit_per_library: int = 50
    offset_per_library: int = 0
    library_offset: int = 0
    source_cursors: tuple = ()
    timeout: float = 300.0
    memory_bytes: int = 64 * 1024**2
    disk_bytes: int = 2 * 1024**3
    ranking_scope: str | None = None

    @property
    def effective_ranking_scope(self):
        return self.ranking_scope or ('all' if self.mode in ('thematic', 'semantic') else 'window')

    def validate(self):
        if (not isinstance(self.workspaces, tuple) or not self.workspaces
                or len(set(self.workspaces)) != len(self.workspaces)):
            raise ValueError('Explicitly check one or more distinct libraries to search.')
        resources = SearchResources(self.timeout, self.memory_bytes, self.disk_bytes)
        _workspaces(self.workspaces, lambda: None, maximum_bytes=resources.memory_bytes // 8)
        if type(self.library_offset) is not int or not 0 <= self.library_offset < len(self.workspaces):
            raise ValueError('Choose an existing selected-library page.')
        if type(self.timeout) not in (int, float) or not 0 < self.timeout <= MAX_TIMEOUT_SECONDS:
            raise ValueError('Choose a search time budget from more than zero to 86400 seconds.')
        if (type(self.source_cursors) is not tuple
                or any(type(row) not in (tuple, list) or len(row) != 2
                       or row[0] not in self.workspaces for row in self.source_cursors)
                or len({row[0] for row in self.source_cursors}) != len(self.source_cursors)):
            raise ValueError('Source navigation must stay in the checked libraries.')
        if not isinstance(self.query, str) or not self.query.strip():
            raise ValueError('Enter a search query.')
        if self.ranking_scope not in (None, 'window', 'all') or (self.effective_ranking_scope == 'all' and self.mode not in ('thematic', 'semantic')):
            raise ValueError('Complete relevance ranking requires a ranked retrieval method.')
        if self.mode not in MODES or self.result_mode not in PRESENTATIONS:
            raise ValueError('Choose a retrieval method and result presentation.')
        if self.mode == 'advanced':
            from ..search.advanced import compile_query
            try:
                compile_query(self.query.strip())
            except ValueError as exc:
                raise ValueError(f'Invalid advanced query: {exc}') from exc
        if type(self.limit_per_library) is not int or not 1 <= self.limit_per_library <= 100:
            raise ValueError('Choose between 1 and 100 results per library.')
        if type(self.offset_per_library) is not int or self.offset_per_library < 0:
            raise ValueError('The result-page offset must be a nonnegative whole number.')
        if self.offset_per_library and self.mode not in PAGED_MODES:
            raise ValueError('Choose a supported retrieval mode before navigating results.')
        return self


def default_libraries(libraries, active):
    """An empty/currently unavailable scope never silently means all libraries."""
    current = active[0] if active is not None else None
    return tuple(row.id for row in libraries if row.id == current)


def result_label(hit, library_label=None):
    row = hit.result
    status = 'Verified source' if row.verified is True else 'Unverified extraction'
    # Native list rows stay one line; the details preserve the complete values.
    return ' | '.join(' '.join(str(value).split()) for value in
                      (hit.library_name if library_label is None else library_label,
                       row.title, row.author, row.edition or 'Unknown edition', row.page, status))


def result_details(hit):
    row = hit.result
    status = ('Verified source text. Quote and citation actions recheck the original.'
              if row.verified is True else
              'UNVERIFIED EXTRACTION — independent review required. '
              'Verified quotation copying and saving are unavailable.')
    return '\n\n'.join((f'Library: {hit.library_name}\nLibrary identifier: {row.workspace}',
        status, f'Title: {row.title}\nAuthor: {row.author}\nEdition: {row.edition or "Unknown"}'
        f'\nSource location: {row.page}', 'Source excerpt:\n' + row.quote,
        'Citation:\n' + row.citation, 'Retrieval information:\n' + (row.reason or 'Source match')))


def _topic_labels(topics, workspace):
    by_id = {topic.id: topic for topic in topics}
    labels = []
    for topic in topics:
        names, seen, current = [], set(), topic
        while current is not None:
            if current.id in seen:
                raise ValueError('The topic hierarchy changed. Load the topic list again.')
            seen.add(current.id)
            names.append(current.name)
            current = by_id.get(current.parent)
        labels.append(' / '.join(reversed(names)))
    return tuple(scope_labels((workspace, topic.id, label)
                              for topic, label in zip(topics, labels)))


class SearchCenter:
    """Headless workflow controller; all source work uses the frame's worker."""

    def __init__(self, frame, view, *, service=None, dialogs=None):
        self.frame, self.view = frame, view
        self.library, self.active = frame.library, frame.active
        self.data_identity = self._data_identity()
        self.service = service or FederatedSearch(self.library)
        self.dialogs = dialogs or NativeDialogs()
        self.rows = ()
        self.options = None
        self.has_more = False
        self.next_source_cursors = ()
        self.source_history = []
        self.epoch = 0
        self.running = False
        self.alive = True
        self._clear_when_idle = False

    def _data_identity(self):
        app = self.frame.library
        return app.root, app.database, app.assets_dir, app.assets.root

    def _scope_current(self):
        return (self.alive and self.frame.library is self.library
                and self.frame.active == self.active and self._data_identity() == self.data_identity
                and not getattr(self.frame, '_closing', False))

    def _ready(self):
        if not self._scope_current():
            raise ValueError('The active library, topic, or data folder changed. Reopen Research Search Center.')
        if self.running or self.frame.working or getattr(self.frame, 'busy', False):
            raise ValueError('An operation is running. Wait or choose Cancel operation.')

    def _cancelled(self):
        # Only ordinary Python scope attributes are read from the worker.
        # A programmatic scope change must withdraw the pending operation just
        # as an explicit Cancel does, even though native selectors are disabled.
        return self.frame.cancel_event.is_set() or not self._scope_current()

    def _clear_rows(self):
        self.epoch += 1
        self.rows = ()
        self.options = None
        self.has_more = False
        self.next_source_cursors = ()
        if self.alive:
            self.view.clear_results()
            self._refresh_navigation()

    def _refresh_navigation(self):
        options = self.options
        enabled = (not self.running and self._scope_current() and options is not None
                   and options.mode in PAGED_MODES)
        if self.alive:
            self.view.set_navigation(
                bool(enabled and options.offset_per_library > 0),
                bool(enabled and self.has_more))
            if hasattr(self.view, 'set_scope_navigation'):
                self.view.set_scope_navigation(
                    bool(enabled and self.source_history),
                    bool(enabled and any(cursor is not None for _, cursor in self.next_source_cursors)),
                    bool(enabled and options.library_offset > 0),
                    bool(enabled and options.library_offset + LIBRARIES_PER_PAGE < len(options.workspaces)))

    def options_changed(self):
        self.source_history.clear()
        self._clear_rows()
        if self.running:
            # clear() also takes the service action lock. Do not block the UI
            # behind a source verifier that is responding to cancellation.
            self.frame.cancel_event.set()
            self._clear_when_idle = True
        else:
            self.service.clear()
        if self.alive:
            self.view.report('Search options changed. Choose Search to load new results.')

    def selected(self, *, verified=False):
        self._ready()
        index = self.view.selection()
        if type(index) is not int or not 0 <= index < len(self.rows):
            raise ValueError('Select one result first.')
        hit = self.rows[index]
        if verified and hit.result.verified is not True:
            raise ValueError('This extraction needs independent review before copying or saving a quotation.')
        return hit

    def build_query(self, builder=None):
        """Apply a reviewed expression only to the still-current search form."""
        try:
            self._ready()
            if builder is None:
                from .advanced_search import show_query_builder
                builder = show_query_builder
            epoch, current = self.epoch, self.view.query_value()
            query = builder(self.view.parent, current_query=current)
            if query is None:
                if self._scope_current():
                    self.view.focus_query()
                return
            self._ready()
            if self.epoch != epoch or self.view.query_value() != current:
                raise ValueError('Search options changed while the query builder was open. Open it again.')
            from ..search.advanced import compile_query
            compile_query(query)
            # ChangeValue and SetSelection do not emit editing events. Withdraw
            # old source handles before the reviewed query becomes visible.
            self.options_changed()
            self.view.set_advanced_query(query)
            self.view.report('Advanced query ready. Press Enter in the query field to search the checked libraries.')
            self.view.focus_query()
        except Exception as exc:
            self._report_error(exc)

    def navigate(self, direction):
        """Read a new result page using the exact last accepted form settings."""
        try:
            self._ready()
            options = self.options
            if type(direction) is not int or direction not in (-1, 1):
                raise ValueError('Choose the previous or next result page.')
            if options is None or options.mode not in PAGED_MODES:
                raise ValueError('Search before changing result pages.')
            if (direction == 1 and not self.has_more) or (direction == -1 and not options.offset_per_library):
                raise ValueError('There is no further result page in that direction.')
            offset = max(0, options.offset_per_library + direction * options.limit_per_library)
            self.search(replace(options, offset_per_library=offset), continuing=True)
        except Exception as exc:
            self._report_error(exc)


    def navigate_scope(self, kind, direction):
        """Continue selected libraries or source windows on the same worker."""
        try:
            self._ready()
            options = self.options
            if options is None or kind not in ('sources', 'libraries') or direction not in (-1, 1):
                raise ValueError('Choose a completed search and an available navigation direction.')
            if kind == 'libraries':
                offset = options.library_offset + direction * LIBRARIES_PER_PAGE
                if not 0 <= offset < len(options.workspaces):
                    raise ValueError('There is no further selected-library page.')
                self.source_history.clear()
                following = replace(options, library_offset=offset, source_cursors=(), offset_per_library=0)
            elif direction == -1:
                if not self.source_history:
                    raise ValueError('No previous source window is retained. Search restarts at the first window.')
                following = self.source_history.pop()
            else:
                if not any(cursor is not None for _, cursor in self.next_source_cursors):
                    raise ValueError('All sources in this selected-library page have been visited.')
                self.source_history.append(options)
                # Navigation history is bounded; total forward traversal is not.
                del self.source_history[:-128]
                following = replace(options, source_cursors=self.next_source_cursors, offset_per_library=0)
            self.search(following, continuing=True)
        except Exception as exc:
            self._report_error(exc)

    def selection_changed(self):
        if not self.alive:
            return
        if self.running:
            self.options_changed()
            return
        try:
            hit = self.selected()
            self.view.show_details(result_details(hit), verified=hit.result.verified is True)
        except Exception:
            self.view.show_details('', verified=False, selected=False)

    def _same_result(self, hit, epoch):
        if not self._scope_current() or self.epoch != epoch:
            return False
        index = self.view.selection()
        return type(index) is int and 0 <= index < len(self.rows) and self.rows[index] is hit

    def _report_error(self, error):
        if self.alive:
            if not self._scope_current():
                self.options_changed()
            self.view.report(str(error), focus=True)

    def _job(self, title, operation, done, *, epoch, hit=None, saved=False):
        self._ready()
        self.running = True
        self.view.set_busy(True)
        self._refresh_navigation()
        self.view.report(title + '…', focus=True)

        def finish():
            self.running = False
            if self._clear_when_idle:
                self.service.clear()
                self._clear_when_idle = False
            if self.alive:
                self.view.set_busy(False)
                self._refresh_navigation()
            elif hasattr(self.view, 'finish_disposal'):
                self.view.finish_disposal()

        def complete(value):
            finish()
            if not self.alive:
                return
            if not self._scope_current() or self.epoch != epoch or (
                    hit is not None and not self._same_result(hit, epoch)):
                self._clear_rows()
                self.view.report('The search selection or library changed. Search again.', focus=True)
                return
            if self.frame.cancel_event.is_set() and not saved:
                self._clear_rows()
                self.view.report('Operation cancelled. Search again to load results.', focus=True)
                return
            try:
                done(value)
            except Exception as exc:
                failed(exc)

        def failed(error):
            finish()
            if self.alive:
                self._clear_rows()
                message = ('Operation cancelled. Search again to load results.'
                           if self.frame.cancel_event.is_set() else
                           f'Operation not completed: {error}. Search again when ready.')
                self.view.report(message, focus=True)

        def guarded_operation():
            if self._cancelled() or self.epoch != epoch:
                raise InterruptedError('The search selection or library changed before the operation started.')
            return operation()

        try:
            self.frame.run_job(title, guarded_operation, complete, failed)
        except (Exception, KeyboardInterrupt) as exc:
            failed(exc)

    def search(self, options, *, continuing=False):
        try:
            self._ready()
            if not continuing:
                self.source_history.clear()
            self._clear_rows()
            options.validate()
            epoch = self.epoch
            notices = []

            def report(message):
                notices.append(message)
                del notices[:-16]
                from ..progress import current_progress
                task = current_progress()
                if task is not None:
                    task.update(phase='Searching selected sources', message=message)

            def done(session):
                expected = options.workspaces[options.library_offset:options.library_offset + LIBRARIES_PER_PAGE]
                if session.workspaces != expected:
                    raise ValueError('The completed search differs from the selected libraries')
                if session.offset_per_library != options.offset_per_library:
                    raise ValueError('The completed result page differs from the requested page')
                self.rows = session.hits
                current_cursors = tuple(getattr(session, 'source_cursors', options.source_cursors))
                self.options = replace(options, source_cursors=current_cursors)
                self.has_more = session.has_more
                if getattr(session, 'library_offset', options.library_offset) != options.library_offset:
                    raise ValueError('The completed selected-library page changed.')
                self.next_source_cursors = tuple(getattr(session, 'next_source_cursors', ()))
                labels = scope_labels((group.workspace, None, group.library_name)
                                      for group in session.groups)
                names = dict(zip(session.workspaces, labels))
                self.view.show_results(tuple(
                    f'{number}. {result_label(hit, names[hit.workspace])}'
                    for number, hit in enumerate(self.rows, 1)))
                counts = '; '.join(f'{names[group.workspace]}: {len(group.hits)}'
                                   for group in session.groups)
                text = (f'{len(self.rows)} results. {counts}. '
                        f'Up to {options.limit_per_library} results per library. '
                        'Results are grouped by selected library. '
                        f'Libraries {options.library_offset + 1}–{options.library_offset + len(expected)} '
                        f'of {len(options.workspaces)} selected. Time budget: {options.timeout:g} seconds.')
                text += (' Relevance uses the complete selected scope within each library.'
                         if options.effective_ranking_scope == 'all' else
                         ' Exact matches follow source order within the current bounded window.')
                if options.mode in PAGED_MODES:
                    text += (f' Page {options.offset_per_library // options.limit_per_library + 1}. '
                             'Previous and Next resolve fresh results with the same search options; a changed library requires restarting search.')
                    if not session.has_more:
                        text += ' No further matching result page in this search scope.'
                if self.next_source_cursors:
                    windows = getattr(session, 'source_windows', ())
                    text += f' Inspected {sum(row[1] for row in windows)} selected source pages.'
                    text += (' Next source window continues the remaining material.'
                             if any(cursor is not None for _, cursor in self.next_source_cursors)
                             else ' All source windows in this selected-library page are complete.')
                if any(hit.result.verified is not True for hit in self.rows):
                    text += ' Unverified extraction is labelled and cannot be saved as a quotation.'
                if notices:
                    text += '\n' + '\n'.join(dict.fromkeys(notices))
                self.view.report(text, focus=not self.rows)
                self._refresh_navigation()
                if self.rows:
                    self.view.select(0)
                    self.selection_changed()
                    self.view.focus_results()

            self._job('Searching selected libraries', lambda: self.service.search(
                options.workspaces, options.query.strip(), mode=options.mode,
                result_mode=options.result_mode, title=options.title, author=options.author,
                language=options.language, edition=options.edition,
                limit_per_library=options.limit_per_library,
                offset_per_library=options.offset_per_library, timeout=options.timeout,
                paged=True, library_offset=options.library_offset,
                source_cursors=dict(options.source_cursors),
                resources=SearchResources(options.timeout, options.memory_bytes, options.disk_bytes),
                ranking_scope=options.effective_ranking_scope,
                cancelled=self._cancelled, on_status=report), done, epoch=epoch)
        except Exception as exc:
            self._report_error(exc)

    def context(self):
        try:
            hit = self.selected()
            epoch = self.epoch
            verified = hit.result.verified is True

            def load():
                method = self.service.context if verified else self.service.preview
                return method(hit, cancelled=self._cancelled)

            def done(value):
                header = f'Library: {hit.library_name}\nLibrary identifier: {hit.result.workspace}\n\n'
                if verified:
                    # Each original page stays a distinct labelled section;
                    # source text is never stitched into a synthetic quotation.
                    sections = [f'Original context page {number}\n{page.citation}\n\n{page.raw_text}'
                                for number, page in enumerate(value.pages, 1)]
                    text = header + '\n\n'.join(sections)
                    title = 'Verified original source context'
                else:
                    title = 'Unverified extracted source text'
                    text = (header + 'UNVERIFIED EXTRACTION — independent review required. '
                            'Verified quotation copying and saving are unavailable.\n\n' + value)
                self.dialogs.show(self.view.parent, title, text)
                if self._same_result(hit, epoch):
                    self.view.focus_results()
                else:
                    self.options_changed()

            self._job('Loading fresh source context', load, done, epoch=epoch, hit=hit)
        except Exception as exc:
            self._report_error(exc)

    def copy(self, field):
        try:
            if field not in ('quote', 'citation'):
                raise ValueError('Choose quotation or citation.')
            hit = self.selected(verified=True)
            epoch = self.epoch

            def done(text):
                persisted = self.view.copy_text(text)
                suffix = '' if persisted else ' Keep the application open while pasting.'
                self.view.report(f'{field.capitalize()} copied from {hit.library_name}.' + suffix)
                self.view.focus_results()

            self._job('Verifying selected ' + field, lambda: self.service.copy(
                hit, field, cancelled=self._cancelled), done, epoch=epoch, hit=hit)
        except Exception as exc:
            self._report_error(exc)

    def save(self):
        try:
            hit = self.selected(verified=True)
            epoch = self.epoch

            def choose(topics):
                if not topics:
                    self.view.report(f'No active topics in {hit.library_name}. '
                                     'Create a topic in that library before saving.', focus=True)
                    return
                index = self.dialogs.choose(self.view.parent, 'Save quotation to a topic',
                    f'Choose a topic in the source library: {hit.library_name}',
                    _topic_labels(topics, hit.workspace))
                if index is None:
                    self.view.report('Saving the quotation was cancelled.')
                    self.view.focus_results()
                    return
                if not self._same_result(hit, epoch):
                    raise ValueError('The selected result or library changed')
                if type(index) is not int or not 0 <= index < len(topics):
                    raise ValueError('Choose a topic from the source library')
                topic = topics[index]
                note = self.dialogs.text(self.view.parent, 'Quotation note',
                    f'Optional research note for {hit.library_name} / {topic.name}', multiline=True)
                if note is None:
                    self.view.report('Saving the quotation was cancelled.')
                    self.view.focus_results()
                    return
                if not self._same_result(hit, epoch):
                    raise ValueError('The selected result or library changed')

                def write():
                    fresh = self.service.topics(hit, cancelled=self._cancelled)
                    if not any(current == topic for current in fresh):
                        raise ValueError('The selected topic changed or is no longer available')
                    return self.service.save_hit(hit, topic.id, note,
                                                 cancelled=self._cancelled)

                def saved(_):
                    text = f'Quotation saved to {hit.library_name} / {topic.name}.'
                    if self.frame.cancel_event.is_set():
                        text += ' Cancellation arrived after saving finished.'
                    self.view.report(text + ' Open or refresh that topic to view it.')
                    self.view.focus_results()

                self._job('Saving verified quotation', write, saved, epoch=epoch, hit=hit, saved=True)

            self._job('Loading topics from the source library', lambda: self.service.topics(
                hit, cancelled=self._cancelled), choose, epoch=epoch, hit=hit)
        except Exception as exc:
            self._report_error(exc)

    def cancel(self):
        if self.running:
            self.frame.cancel_event.set()
            if self.alive:
                self.view.report('Cancellation requested. Wait for the operation to finish.', focus=True)

    def request_close(self):
        if self.running:
            self.cancel()
            return False
        self.dispose()
        return True

    def dispose(self):
        self.alive = False
        self.epoch += 1
        self.rows = ()
        self.options, self.has_more = None, False
        if self.running:
            self.frame.cancel_event.set()
            self._clear_when_idle = True
        else:
            self.service.clear()


class NativeSearchCenter:
    """Standard native controls; no custom rendering or assumed NVDA result."""

    def __init__(self, frame, libraries):
        import wx
        self.wx = wx
        self.window = self.parent = wx.Dialog(frame, title='Research Search Center', size=(980, 850))
        self._window_id = self.window.GetId()
        self._window_alive = True
        self._deferred_destroy = False
        self.window.SetMinSize((800, 700))
        self.libraries = tuple(libraries)
        self.viewport = wx.ScrolledWindow(self.window, style=wx.VSCROLL)
        self.viewport.SetScrollRate(0, 20)
        self.viewport.SetMinSize((600, 400))
        parent = self.viewport
        layout = wx.BoxSizer(wx.VERTICAL)
        explanation = wx.StaticText(parent, label=
            'Check the libraries to search. Space checks or unchecks a focused library. '
            'Results retain their source library; quotations can be saved only to its topics.')
        explanation.Wrap(700)
        layout.Add(explanation, 0, wx.EXPAND | wx.ALL, 8)
        form = wx.FlexGridSizer(cols=2, hgap=8, vgap=5)
        form.AddGrowableCol(1)

        def field(label, control):
            control.SetName(label)
            form.Add(wx.StaticText(parent, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            form.Add(control, 0, wx.EXPAND)
            return control

        choices = scope_labels((row.id, None, row.name) for row in self.libraries)
        self.libraries_control = field('Libraries to search (Space selects)',
            wx.CheckListBox(parent, choices=choices, size=(-1, 95)))
        selected = default_libraries(self.libraries, frame.active)
        for index, row in enumerate(self.libraries):
            self.libraries_control.Check(index, row.id in selected)
        self.query = field('Search query (Enter starts search)',
                           wx.TextCtrl(parent, style=wx.TE_PROCESS_ENTER))
        self.retrieval = field('Retrieval method', wx.Choice(parent, choices=[
            'Exact phrase', 'Whole word', 'All words on one page',
            'Thematic (local keywords)', 'Semantic (local model)',
            'Advanced (AND, OR, NOT, NEAR)']))
        self.presentation = field('Result presentation', wx.Choice(parent,
            choices=['Short quotations', 'Thematic excerpts']))
        self.limit = field('Results per library on this result page', wx.Choice(parent,
            choices=[str(value) for value in LIMITS]))
        self.retrieval.SetSelection(0)
        self.presentation.SetSelection(0)
        self.limit.SetSelection(2)
        self.timeout = field('Cooperative search time budget in seconds', wx.TextCtrl(parent, value='300'))
        self.memory = field('Search working-memory allowance in MiB', wx.TextCtrl(parent, value='64'))
        self.disk = field('Search temporary-disk allowance in MiB', wx.TextCtrl(parent, value='2048'))
        self.filters = {key: field(label, wx.TextCtrl(parent)) for key, label in (
            ('author', 'Author contains'), ('title', 'Title contains'),
            ('language', 'Language code (exact)'), ('edition', 'Edition (exact)'))}
        layout.Add(form, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        commands = wx.BoxSizer(wx.HORIZONTAL)
        self.search_button = wx.Button(parent, label='&Search')
        self.builder_button = wx.Button(parent, label='Build &advanced query')
        self.builder_button.SetName('Build advanced query; Ctrl+Alt+Q')
        self.cancel_button = wx.Button(parent, label='Cancel &operation')
        commands.Add(self.search_button, 0, wx.ALL, 5)
        commands.Add(self.builder_button, 0, wx.ALL, 5)
        commands.Add(self.cancel_button, 0, wx.ALL, 5)
        layout.Add(commands, 0, wx.EXPAND)
        layout.Add(wx.StaticText(parent, label='Results — grouped by source library'),
                   0, wx.LEFT | wx.RIGHT, 8)
        pages = wx.BoxSizer(wx.HORIZONTAL)
        self.previous_button = wx.Button(parent, label='&Previous result page')
        self.next_button = wx.Button(parent, label='&Next result page')
        self.previous_button.SetName('Previous result page within current sources')
        self.next_button.SetName('Next result page within current sources')
        for button in (self.previous_button, self.next_button):
            pages.Add(button, 0, wx.ALL, 5)
        layout.Add(pages, 0, wx.EXPAND)
        source_pages = wx.FlexGridSizer(cols=2, hgap=5, vgap=5)
        source_pages.AddGrowableCol(0)
        source_pages.AddGrowableCol(1)
        self.scope_buttons = {}
        for key, label, kind, direction in (
            ('source_previous', 'Previous source window', 'sources', -1),
            ('source_next', 'Next source window', 'sources', 1),
            ('library_previous', 'Previous selected libraries', 'libraries', -1),
            ('library_next', 'Next selected libraries', 'libraries', 1)):
            button = wx.Button(parent, label=label)
            button.SetName(label)
            self.scope_buttons[key] = button
            source_pages.Add(button, 0, wx.EXPAND | wx.ALL, 5)
            button.Bind(wx.EVT_BUTTON, lambda event, kind=kind, direction=direction:
                        self.controller.navigate_scope(kind, direction))
        layout.Add(source_pages, 0, wx.EXPAND)
        self.results = wx.ListBox(parent, style=wx.LB_SINGLE, size=(-1, 130))
        self.results.SetName('Search results with library and verification status')
        layout.Add(self.results, 1, wx.EXPAND | wx.ALL, 8)
        layout.Add(wx.StaticText(parent, label='Selected source details'), 0, wx.LEFT | wx.RIGHT, 8)
        self.details = wx.TextCtrl(parent, style=wx.TE_READONLY | wx.TE_MULTILINE,
                                   size=(-1, 115))
        self.details.SetName('Selected source details, excerpt and verification status')
        layout.Add(self.details, 1, wx.EXPAND | wx.ALL, 8)
        actions = wx.FlexGridSizer(cols=2, hgap=5, vgap=5)
        actions.AddGrowableCol(0)
        actions.AddGrowableCol(1)
        self.context_button = wx.Button(parent, label='View source conte&xt')
        self.quote_button = wx.Button(parent, label='Copy &quotation')
        self.citation_button = wx.Button(parent, label='Copy ci&tation')
        self.save_button = wx.Button(parent, label='Save quotation to a to&pic')
        for button in (self.context_button, self.quote_button, self.citation_button, self.save_button):
            actions.Add(button, 0, wx.EXPAND | wx.ALL, 5)
        layout.Add(actions, 0, wx.EXPAND)
        self.status = wx.TextCtrl(parent, style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 65))
        self.status.SetName('Search Center status')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        self.close_button = wx.Button(parent, label='&Close')
        layout.Add(self.close_button, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        self.viewport.SetSizer(layout)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(self.viewport, 1, wx.EXPAND)
        self.window.SetSizer(outer)
        self.window.SetEscapeId(self.close_button.GetId())
        self.controller = SearchCenter(frame, self)
        self._bind()
        self.set_navigation(False, False)
        self.set_scope_navigation(False, False, False, False)
        self.clear_results()
        self.set_busy(False)
        self.report('Only the current library is checked initially. Choose libraries and enter a query.'
                    if selected else 'Check at least one library and enter a query.')
        (self.query if selected else self.libraries_control).SetFocus()

    def _bind(self):
        wx, controller = self.wx, self.controller
        self.search_button.Bind(wx.EVT_BUTTON, self._search)
        self.builder_button.Bind(wx.EVT_BUTTON, lambda event: controller.build_query())
        self.query.Bind(wx.EVT_TEXT_ENTER, self._search)
        self.cancel_button.Bind(wx.EVT_BUTTON, lambda event: controller.cancel())
        self.previous_button.Bind(wx.EVT_BUTTON, lambda event: controller.navigate(-1))
        self.next_button.Bind(wx.EVT_BUTTON, lambda event: controller.navigate(1))
        self.context_button.Bind(wx.EVT_BUTTON, lambda event: controller.context())
        self.quote_button.Bind(wx.EVT_BUTTON, lambda event: controller.copy('quote'))
        self.citation_button.Bind(wx.EVT_BUTTON, lambda event: controller.copy('citation'))
        self.save_button.Bind(wx.EVT_BUTTON, lambda event: controller.save())
        self.results.Bind(wx.EVT_LISTBOX, lambda event: controller.selection_changed())
        self.results.Bind(wx.EVT_LISTBOX_DCLICK, lambda event: controller.context())
        self.libraries_control.Bind(wx.EVT_CHECKLISTBOX, lambda event: controller.options_changed())
        for control in (self.query, self.timeout, self.memory, self.disk, *self.filters.values()):
            control.Bind(wx.EVT_TEXT, lambda event: controller.options_changed())
        for control in (self.retrieval, self.presentation, self.limit):
            control.Bind(wx.EVT_CHOICE, lambda event: controller.options_changed())
        self.close_button.Bind(wx.EVT_BUTTON, self._close)
        self.window.Bind(wx.EVT_CLOSE, self._close)
        self.window.Bind(wx.EVT_CHAR_HOOK, self._key)
        self.window.Bind(wx.EVT_WINDOW_DESTROY, self._destroyed)

    def _destroyed(self, event):
        if (event.GetEventObject() is self.window
                or event.GetId() == self._window_id):
            self._window_alive = False
            self._deferred_destroy = False
            self.controller.dispose()
        event.Skip()

    def _search(self, event=None):
        if not self._window_alive:
            return
        try:
            indices = (self.retrieval.GetSelection(), self.presentation.GetSelection(), self.limit.GetSelection())
            if not all(0 <= index < len(values) for index, values in
                       zip(indices, (MODES, PRESENTATIONS, LIMITS))):
                raise ValueError('Choose retrieval, presentation, and result limit before searching.')
            options = SearchOptions(
                tuple(row.id for index, row in enumerate(self.libraries)
                      if self.libraries_control.IsChecked(index)), self.query.GetValue(),
                mode=MODES[indices[0]], result_mode=PRESENTATIONS[indices[1]],
                limit_per_library=LIMITS[indices[2]], timeout=float(self.timeout.GetValue()),
                memory_bytes=int(self.memory.GetValue()) * 1024**2,
                disk_bytes=int(self.disk.GetValue()) * 1024**2,
                **{key: control.GetValue().strip() or None for key, control in self.filters.items()})
            self.controller.search(options)
        except Exception as exc:
            self.report(str(exc), focus=True)

    def _key(self, event):
        if event.GetKeyCode() == self.wx.WXK_ESCAPE:
            self._close()
        elif event.ControlDown() and event.AltDown() and event.GetKeyCode() in (ord('Q'), ord('q')):
            self.controller.build_query()
        else:
            event.Skip()

    def _close(self, event=None):
        if not self._window_alive:
            return
        if self.controller.request_close():
            self.window.EndModal(self.wx.ID_CANCEL)
        elif event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
            event.Veto()

    def set_busy(self, busy):
        for control in (self.libraries_control, self.query, self.retrieval, self.presentation,
                        self.limit, self.timeout, self.memory, self.disk, *self.filters.values(), self.search_button, self.results,
                        self.builder_button, self.close_button):
            control.Enable(not busy)
        self.cancel_button.Enable(busy)
        if busy:
            for control in (self.context_button, self.quote_button, self.citation_button, self.save_button):
                control.Disable()
        else:
            self.controller.selection_changed()

    def report(self, text, *, focus=False):
        self.status.ChangeValue(text)
        self.status.SetInsertionPoint(0)
        if focus:
            self.status.SetFocus()

    def query_value(self):
        return self.query.GetValue()

    def set_advanced_query(self, query):
        self.query.ChangeValue(query)
        self.retrieval.SetSelection(MODES.index('advanced'))

    def focus_query(self):
        self.query.SetFocus()

    def set_navigation(self, previous, next_page):
        self.previous_button.Enable(previous)
        self.next_button.Enable(next_page)

    def set_scope_navigation(self, source_previous, source_next, library_previous, library_next):
        for key, enabled in zip(('source_previous', 'source_next', 'library_previous', 'library_next'),
                                (source_previous, source_next, library_previous, library_next)):
            self.scope_buttons[key].Enable(enabled)

    def clear_results(self):
        self.results.Clear()
        self.show_details('', verified=False, selected=False)

    def show_results(self, labels):
        self.results.Set(list(labels))

    def selection(self):
        return self.results.GetSelection()

    def select(self, index):
        self.results.SetSelection(index)

    def focus_results(self):
        self.results.SetFocus()

    def show_details(self, text, *, verified, selected=True):
        self.details.ChangeValue(text)
        self.details.SetInsertionPoint(0)
        self.context_button.Enable(selected)
        for control in (self.quote_button, self.citation_button, self.save_button):
            control.Enable(selected and verified)

    def copy_text(self, text):
        wx = self.wx
        if not wx.TheClipboard.Open():
            raise ValueError('The clipboard is busy. Try again.')
        try:
            if not wx.TheClipboard.SetData(wx.TextDataObject(text)):
                raise ValueError('The clipboard did not accept the verified text.')
            return wx.TheClipboard.Flush()
        finally:
            wx.TheClipboard.Close()

    def show(self):
        if not self._window_alive:
            return
        try:
            self.window.ShowModal()
        finally:
            # Normal close paths refuse dismissal while a job is running.
            # If the windowing system ends the modal loop unexpectedly, avoid
            # destroying controls that an active completion still owns.
            if not self._window_alive:
                self.controller.dispose()
            elif self.controller.running:
                self._deferred_destroy = True
                self.controller.dispose()
                self.window.Hide()
            else:
                self.controller.dispose()
                self.window.Destroy()

    def finish_disposal(self):
        """Destroy an unexpectedly dismissed modal only after its worker ends."""
        if self._deferred_destroy:
            self._deferred_destroy = False
            if self._window_alive:
                self.window.Destroy()


class FederatedSearchActions:
    def __init__(self, frame):
        self.frame = frame

    def open(self, event=None):
        import wx
        frame = self.frame
        if frame.working or getattr(frame, 'busy', False) or getattr(frame, '_closing', False):
            frame.report('Wait for the current operation before opening Research Search Center.')
            return
        library, active = frame.library, frame.active
        focus = wx.Window.FindFocus()
        try:
            libraries = library.workspaces()
            if not libraries:
                frame.report('Create a library and import a source before using Research Search Center.')
                return
            NativeSearchCenter(frame, libraries).show()
        except Exception as exc:
            frame.report(f'Research Search Center could not open: {exc}')
        finally:
            if frame.library is library and frame.active == active and not frame.working:
                try:
                    (focus or frame.status).SetFocus()
                except (AttributeError, RuntimeError):
                    frame.status.SetFocus()


def install_federated_search_actions(frame):
    """Append one research command without replacing existing menu handlers."""
    existing = getattr(frame, 'federated_search_actions', None)
    if existing is not None:
        return existing
    import wx
    index = frame.menu_bar.FindMenu('Research')
    if index < 0:
        raise ValueError('The Research menu is required for Research Search Center.')
    menu = frame.menu_bar.GetMenu(index)
    actions = FederatedSearchActions(frame)
    menu.AppendSeparator()
    item = menu.Append(wx.ID_ANY, 'Research Search &Center...\tCtrl+Alt+F')
    frame.Bind(wx.EVT_MENU, actions.open, item)
    frame.federated_search_actions = actions
    return actions
