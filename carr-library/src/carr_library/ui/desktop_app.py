"""Native desktop host for the persistent application services.

The existing scoped panels and wx data-view grid supply the main workflows.
Long operations run outside the UI thread; focus returns to readable status.
"""
from .worker_lifetime import DesktopExecutor
from contextlib import contextmanager
from pathlib import Path
import threading

import wx

from ..application import LibraryApp
from ..extract.document_formats import file_dialog_wildcard, supported_formats_text
from ..catalog.read_views import active_collections
from ..citations.anchors import format_citation
from ..research.export_file import write_export, write_binary_export
from .export_destination import confirm_export_destination
from ..reader.source_file import prepare_source_file, launch_source_file
from ..research.saved_topic_query import SavedTopicQueries
from ..research.topic_tree import TopicTree
from ..reader.context import open_context
from .workspace_shell import WorkspaceFrame
from .scope_bindings import ScopeServices, scope_labels
from .paged_search_panel import PagedSearchPanel
from .page_context_dialog import show_page_context
from .search_model import Hit
from .catalog_metadata_dialog import edit_selected_book
from .catalog_tools_dialog import show_catalog_tools
from .desktop_evidence_panel import DesktopEvidencePanel
from .research_actions import install_research_actions
from .source_review import install_source_review_actions
from .academic_actions import install_academic_actions
from .notebook_search import search_saved_research
from .academic_tools import install_academic_actions as install_academic_comparison_actions
from .reading_desk import install_reading_desk_actions
from .catalog_insights_actions import install_catalog_insights_actions
from .ebook_actions import install_ebook_actions
from .federated_search import install_federated_search_actions
from .query_manager import install_query_manager_actions
from .library_assistant import install_library_assistant_actions
from .acquisition_dialog import show_acquisition_dialog


def text_dialog(parent, title, label, value='', *, multiline=False):
    dialog = wx.TextEntryDialog(parent, label, title, value,
        style=wx.OK | wx.CANCEL | (wx.TE_MULTILINE if multiline else 0))
    try:
        return dialog.GetValue() if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()


def show_text(parent, title, text):
    dialog = wx.Dialog(parent, title=title, size=(800, 600))
    try:
        layout = wx.BoxSizer(wx.VERTICAL)
        reader = wx.TextCtrl(dialog, value=text, style=wx.TE_READONLY | wx.TE_MULTILINE)
        reader.SetName(title)
        layout.Add(reader, 1, wx.EXPAND | wx.ALL, 8)
        back = wx.Button(dialog, wx.ID_CANCEL, '&Close')
        layout.Add(back, 0, wx.ALL, 8)
        dialog.SetSizer(layout)
        reader.SetInsertionPoint(0)
        reader.SetFocus()
        dialog.ShowModal()
    finally:
        dialog.Destroy()


def format_topic_results(rows):
    """Readable source slices and notes, without dataclass/debug representations."""
    sections = []
    for number, hit in enumerate(rows,1):
        evidence = hit.evidence
        parts = [f'Result {number}. Match in ' + ('research note.' if hit.matched_field == 'note' else 'quotation.')]
        for anchor in evidence.anchors:
            parts.extend(['Source: ' + format_citation(anchor), 'Exact quotation:', anchor.quote])
        parts.extend(['Research note:', evidence.note or '(No note)'])
        sections.append('\n\n'.join(parts))
    return '\n\n'.join(sections) if sections else 'No matching saved evidence.'


class DesktopSearchPanel(PagedSearchPanel):
    MODES = ('phrase', 'word', 'terms', 'thematic', 'semantic', 'advanced')

    def __init__(self, parent, frame, services):
        self.frame = frame
        self.bound_library = frame.library
        self.bound_workspace,self.bound_topic = services.workspace,services.topic
        self.results = {}
        self._search_page = None
        self._source_history = []
        super().__init__(parent, services.search, services.load_context,
                         verified_copy=services.verified_copy)
        self.retrieval.SetItems(['Exact phrase', 'Whole word', 'All words on one page',
                                 'Thematic (local keywords)', 'Semantic (local model)',
                                 'Advanced (AND, OR, NOT, NEAR)'])
        self.retrieval.SetSelection(0)
        # Source presentation is independent from retrieval. These offsets also
        # identify the quotation used by copying, saved evidence and More.
        self.filters = {}
        form = wx.FlexGridSizer(cols=2, hgap=8, vgap=4)
        form.AddGrowableCol(1)
        self.collection_ids = [None]
        self.collection = wx.Choice(self, choices=['All books in this library'])
        for identity, name in frame.library.collections(services.workspace):
            self.collection_ids.append(identity)
            self.collection.Append(f'Collection: {name}')
        self.collection.SetSelection(0)
        self.collection.SetName('Search collection')
        form.Add(wx.StaticText(self, label='Search collection'))
        form.Add(self.collection, 0, wx.EXPAND)
        for key, label in [('author', 'Author contains'), ('title', 'Title contains'),
                           ('language', 'Language code'), ('edition', 'Edition')]:
            control = wx.TextCtrl(self)
            control.SetName(label)
            self.filters[key] = control
            form.Add(wx.StaticText(self, label=label))
            form.Add(control, 0, wx.EXPAND)
        self.GetSizer().Insert(0, form, 0, wx.EXPAND | wx.ALL, 4)
        self.collection.MoveBeforeInTabOrder(self.query)
        previous = self.collection
        for control in self.filters.values():
            control.MoveAfterInTabOrder(previous)
            previous = control
        self.query.MoveAfterInTabOrder(previous)
        self.advanced_builder = wx.Button(self, label='Build &advanced query')
        self.advanced_builder.Bind(wx.EVT_BUTTON, self.on_build_query)
        self.GetSizer().Insert(3, self.advanced_builder, 0, wx.ALL, 4)
        self.advanced_builder.MoveAfterInTabOrder(self.query)
        self.retrieval.MoveAfterInTabOrder(self.advanced_builder)
        navigation = wx.BoxSizer(wx.HORIZONTAL)
        self.search_navigation = {}
        for key, caption in (('result_previous', 'Previous result page'), ('result_next', 'Next result page'),
                             ('source_previous', 'Previous source window'), ('source_next', 'Next source window')):
            button = wx.Button(self, label=caption)
            button.SetName(caption)
            button.Bind(wx.EVT_BUTTON, lambda event, key=key: self.on_search_navigation(key))
            button.Disable()
            self.search_navigation[key] = button
            navigation.Add(button, 0, wx.ALL, 3)
        self.GetSizer().Add(navigation, 0, wx.EXPAND)
        budget_row = wx.BoxSizer(wx.HORIZONTAL)
        budget_row.Add(wx.StaticText(self, label='Search time budget in seconds'), 0, wx.ALL, 3)
        self.search_timeout = wx.TextCtrl(self, value='300')
        self.search_timeout.SetName('Search time budget in seconds')
        self.search_timeout.Bind(wx.EVT_TEXT, self.on_options_changed)
        budget_row.Add(self.search_timeout, 0, wx.ALL, 3)
        self.GetSizer().Add(budget_row, 0, wx.EXPAND)
        save = wx.Button(self, label='Save selected quotation to &topic')
        save.Bind(wx.EVT_BUTTON, self.save_selected)
        self.GetSizer().Add(save, 0, wx.ALL, 4)
        review = wx.Button(self,label='Record independent review of selected physical page')
        review.Bind(wx.EVT_BUTTON, self.review_selected)
        self.GetSizer().Add(review,0,wx.ALL,4)
        original = wx.Button(self, label='Open &original source file')
        original.Bind(wx.EVT_BUTTON, self.open_original)
        self.GetSizer().Add(original, 0, wx.ALL, 4)
        for control in (self.retrieval, self.result_mode, self.collection):
            control.Bind(wx.EVT_CHOICE, self.on_options_changed)
        for control in (self.query, *self.filters.values()):
            control.Bind(wx.EVT_TEXT, self.on_options_changed)

    def on_build_query(self, event=None):
        if getattr(self.frame, '_closing', False):
            return
        if self.frame.working:
            self.frame.report('An operation is already running. Wait or choose Cancel current operation.')
            return
        workspace = self.model.workspace
        if not workspace or workspace != self.bound_workspace:
            self.status.ChangeValue('Open a library before building a search query.')
            self.query.SetFocus()
            return
        from carr_library.ui.advanced_search import show_query_builder
        current, epoch = self.query.GetValue(), self._context_epoch
        active = getattr(self.frame, 'active', None)
        query = show_query_builder(self, current_query=current)
        if getattr(self.frame, '_closing', False):
            return
        if (self.model.workspace != workspace or self.frame.library is not self.bound_library
                or getattr(self.frame, 'active', None) != active):
            self.frame.report('The library changed. Open its Search section again.')
            return
        if query is None:
            self.query.SetFocus()
            return
        if (self._context_epoch != epoch or self.query.GetValue() != current
                or self.frame.working or getattr(self.frame, '_closing', False)):
            self.frame.report('Search options changed while the query builder was open. Open it again.')
            return
        self.on_options_changed()
        self.retrieval.SetSelection(self.MODES.index('advanced'))
        self.query.ChangeValue(query)
        self.status.ChangeValue('Advanced query ready. Press Enter in the query field to search the selected library.')
        self.query.SetFocus()

    def on_options_changed(self, event=None):
        """Discard old results when query, filters or presentation change."""
        self._search_page = None
        self._source_history = []
        self.refresh_search_navigation()
        self._context_epoch += 1
        self.model.clear()
        self.results.clear()
        self.grid.refresh()
        self.details.ChangeValue('')
        self.status.ChangeValue('Search options changed. Choose Search to load matching source excerpts.')
        if event is not None:
            event.Skip()

    def refresh_collections(self):
        index = self.collection.GetSelection()
        selected = self.collection_ids[index] if 0 <= index < len(self.collection_ids) else None
        pairs = tuple(self.frame.library.collections(self.model.workspace))
        self.collection_ids = [None] + [identity for identity, name in pairs]
        self.collection.SetItems(['All books in this library'] +
                                 [f'Collection: {name}' for identity, name in pairs])
        self.collection.SetSelection(self.collection_ids.index(selected) if selected in self.collection_ids else 0)
        self._context_epoch += 1
        self.model.clear()
        self.results.clear()
        self.grid.refresh()
        self.details.ChangeValue('')
        self._search_page = None
        self._source_history = []
        self.refresh_search_navigation()
        self.status.ChangeValue('Collections refreshed. Search again using the current selection.' if selected in self.collection_ids else
                                'The selected collection is no longer available. Choose a collection and search again.')

    def on_search(self, event=None, *, cursor=None, offset=0, continuing=False):
        if getattr(self.frame, '_closing', False):
            return
        if self.frame.working:
            self.frame.report('An operation is already running. Wait or choose Cancel current operation.')
            return
        workspace = self.model.workspace
        query = self.query.GetValue().strip()
        if not workspace or workspace != self.bound_workspace or not query:
            self.status.ChangeValue('Select a library and enter a query.')
            self.query.SetFocus()
            return
        library, active = self.frame.library, getattr(self.frame, 'active', None)
        if library is not self.bound_library or (active is not None
                and active != (workspace, self.bound_topic)):
            self.frame.report('The library changed. Open its Search section again.')
            return
        data_identity = library.root, library.database, library.assets_dir, library.assets.root
        retrieval = self.retrieval.GetSelection()
        if not 0 <= retrieval < len(self.MODES):
            self.status.ChangeValue('Choose a search mode first.')
            self.retrieval.SetFocus()
            return
        mode = self.MODES[retrieval]
        if mode == 'advanced':
            from carr_library.search.advanced import compile_query
            try:
                compile_query(query)
            except ValueError as exc:
                self.status.ChangeValue(f'Invalid advanced query: {exc}')
                self.status.SetInsertionPoint(0)
                self.status.SetFocus()
                return
        presentation = self.result_mode.GetSelection()
        if not 0 <= presentation < 2:
            self.status.ChangeValue('Choose Short quotations or Thematic excerpts first.')
            self.result_mode.SetFocus()
            return
        result_mode = ('short', 'thematic')[presentation]
        options = {key: control.GetValue().strip() or None for key, control in self.filters.items()}
        index = self.collection.GetSelection()
        if not 0 <= index < len(self.collection_ids):
            self.status.ChangeValue('Choose a search collection first.')
            self.collection.SetFocus()
            return
        options['collection'] = self.collection_ids[index]
        try:
            allowance = float(self.search_timeout.GetValue())
            from ..search.paging import MAX_TIMEOUT_SECONDS
            if not 0 < allowance <= MAX_TIMEOUT_SECONDS:
                raise ValueError('Choose a search time budget greater than zero and at most 86400 seconds.')
        except (ValueError, TypeError) as exc:
            self.status.ChangeValue(str(exc))
            self.status.SetFocus()
            return
        if not continuing:
            self._source_history = []
        self._search_page = None
        self.refresh_search_navigation()
        self._context_epoch += 1
        epoch = self._context_epoch
        self.model.clear()
        self.results.clear()
        self.grid.refresh()
        self.details.ChangeValue('')
        notes = []

        def current_scope():
            return (self._context_epoch == epoch and self.model.workspace == workspace
                    and self.frame.library is library and getattr(self.frame, 'active', None) == active
                    and not getattr(self.frame, '_closing', False)
                    and (library.root, library.database, library.assets_dir, library.assets.root) == data_identity)

        def stopped():
            return self.frame.cancel_event.is_set() or not current_scope()

        def done(page):
            if stopped():
                return
            rows = page.results
            self._search_page = page
            self.refresh_search_navigation()
            self.results = {r.ref: r for r in rows}
            self.model.replace(workspace, (Hit(r.workspace, r.ref, r.revision, r.title,
                r.author, r.edition, r.page, r.quote, r.citation) for r in rows))
            self.grid.refresh()
            info = f'{len(rows)} results.' if rows else 'No results. Change the query or source filters.'
            if rows:
                info += (' Short source quotations.' if result_mode == 'short' else
                         ' Expanded source excerpts from the matching page or text segment.')
                info += f' Result page {offset // 100 + 1} within {page.source_pages} source pages.'
            info += ' Relevance is local to this bounded source window.'
            info += (' More matching results are available in this window.' if page.has_more_results else '')
            info += (' Next source window continues the remaining books or segments.'
                     if page.next_source_cursor is not None else ' All source windows have been visited.')
            if notes:
                info += ' ' + ' '.join(dict.fromkeys(notes))
            if mode == 'advanced':
                info += (' Conditions are checked on one page or text segment at a time. '
                         'A short excerpt may show only part of the matching text; More opens that source page.')
            if any(not r.verified for r in rows):
                info += ' Some extracted texts need independent review before quotation or export.'
            self.status.ChangeValue(info)
            if rows:
                self.grid.select_rows([0])
                self.grid.focus_row(0)
                self._refresh_details()
            else:
                self.status.SetFocus()

        def load():
            if stopped():
                raise InterruptedError('The search query, library, or data folder changed before searching.')
            return library.search_page(workspace, query, mode=mode, result_mode=result_mode,
                cursor=cursor, offset=offset, timeout=allowance,
                cancelled=stopped, authorized_topic=self.bound_topic, on_status=notes.append, **options)

        self.frame.run_job('Searching the selected library', load, done)


    def refresh_search_navigation(self):
        page = self._search_page
        flags = {'result_previous': bool(page and page.offset > 0),
                 'result_next': bool(page and page.has_more_results),
                 'source_previous': bool(page and self._source_history),
                 'source_next': bool(page and page.next_source_cursor is not None)}
        for key, button in self.search_navigation.items():
            button.Enable(flags[key] and not getattr(self.frame, '_closing', False))

    def on_search_navigation(self, key):
        if self.frame.working or self._search_page is None:
            return
        page = self._search_page
        cursor, offset = page.cursor, page.offset
        if key == 'result_previous' and offset > 0:
            offset = max(0, offset - 100)
        elif key == 'result_next' and page.has_more_results:
            offset += 100
        elif key == 'source_next' and page.next_source_cursor is not None:
            self._source_history.append((cursor, offset))
            del self._source_history[:-128]
            cursor, offset = page.next_source_cursor, 0
        elif key == 'source_previous' and self._source_history:
            cursor, offset = self._source_history.pop()
        else:
            return
        self.on_search(cursor=cursor, offset=offset, continuing=True)

    def on_more(self, event):
        if self.frame.working:
            self.frame.report('An operation is already running. Wait or choose Cancel current operation.')
            return
        try:
            selected = self.selected()
            if selected.workspace != self.bound_workspace or selected.workspace != self.model.workspace:
                raise ValueError('Select one result in the active library.')
            result = self.results.get(selected.ref)
            unverified = result is not None and not result.verified
            epoch = self._context_epoch
            identity = selected.workspace,selected.ref,selected.revision

            def current_selection():
                try:
                    if (self._context_epoch == epoch and self.model.workspace == selected.workspace and
                            self.selected() == selected):
                        return identity
                except Exception:
                    pass
                return None

            def load():
                if self.frame.cancel_event.is_set():
                    raise TimeoutError('Context cancelled.')
                value = (self.frame.library.preview(*identity,authorized_topic=self.bound_topic)
                         if unverified else self._load_context(*identity))
                if self.frame.cancel_event.is_set():
                    raise TimeoutError('Context cancelled.')
                return value

            def done(value):
                if current_selection() != identity or self.frame.cancel_event.is_set():
                    return
                if unverified:
                    show_text(self,'Extracted source text — review required',value)
                else:
                    show_page_context(self,identity,current_selection,self._load_context,
                                      initial_context=value,run_job=self.frame.run_job)
                if current_selection() == identity:
                    self.grid.control.SetFocus()
                    self._refresh_details()

            self.frame.run_job('Loading original source context',load,done)
        except Exception as exc:
            self.status.ChangeValue(str(exc))
            self.status.SetFocus()

    def copy(self,field):
        if self.frame.working:
            self.frame.report('An operation is already running. Wait or choose Cancel current operation.')
            return
        try:
            hit = self.selected()
            if field not in ('quote','citation') or hit.workspace != self.bound_workspace or hit.workspace != self.model.workspace:
                raise ValueError('Select one result in the active library.')
            epoch = self._context_epoch

            def verify():
                if self.frame.cancel_event.is_set():
                    raise TimeoutError('Copy cancelled. Nothing copied.')
                value = self.verified_copy_service(hit.workspace,hit.ref,hit.revision,field)
                if self.frame.cancel_event.is_set():
                    raise TimeoutError('Copy cancelled. Nothing copied.')
                return value

            def done(value):
                if self._context_epoch != epoch or self.model.workspace != hit.workspace:
                    return
                if self.frame.cancel_event.is_set():
                    self.status.ChangeValue('Copy cancelled. Nothing copied.')
                    self.status.SetFocus()
                    return
                try:
                    if self.selected() != hit or not isinstance(value,str) or not value or value != getattr(hit,field):
                        raise ValueError('Verified source or selection changed. Nothing copied.')
                    if not wx.TheClipboard.Open():
                        raise RuntimeError('Clipboard is busy. Nothing copied.')
                    try:
                        if not wx.TheClipboard.SetData(wx.TextDataObject(value)):
                            raise RuntimeError('Clipboard rejected data. Nothing copied.')
                        persisted = wx.TheClipboard.Flush()
                    finally:
                        wx.TheClipboard.Close()
                    self.status.ChangeValue('Copied ' + field if persisted else
                        'Copied ' + field + '; paste before closing because availability after exit is not confirmed.')
                except Exception as exc:
                    self.status.ChangeValue(f'Copy failed: {exc}')
                self.status.SetFocus()

            self.frame.run_job('Verifying quotation for copy',verify,done)
        except Exception as exc:
            self.status.ChangeValue(f'Nothing copied: {exc}')
            self.status.SetFocus()

    def open_original(self, event):
        if self.frame.working:
            self.frame.report('An operation is already running. Wait or choose Cancel current operation.')
            return
        try:
            hit = self.selected()
            library = self.bound_library
            topic = self.bound_topic
            active = (self.bound_workspace, topic)
            if (self.frame.library is not library or self.frame.active != active or
                    hit.workspace != self.bound_workspace or hit.workspace != self.model.workspace):
                raise ValueError('Select one result in the active library and research topic.')
            selection = (hit.workspace, hit.ref, hit.revision)
            epoch = self._context_epoch
            cancelled = self.frame.cancel_event.is_set
        except Exception as exc:
            self.status.ChangeValue(f'Original not opened: {exc}')
            self.status.SetFocus()
            return

        def check_context():
            if cancelled():
                raise InterruptedError('Opening the original was cancelled.')
            if self.frame.library is not library or self.bound_library is not library:
                raise ValueError('The library folder changed. Select the source again.')
            if (self.frame.active != active or self.bound_workspace != selection[0] or
                    self.bound_topic != topic):
                raise ValueError('The library or research topic changed. Select the source again.')
            if self._context_epoch != epoch or self.model.workspace != selection[0]:
                raise ValueError('The selection changed. Select the source again.')

        def check_selection():
            check_context()
            current = self.selected()
            if (current.workspace, current.ref, current.revision) != selection:
                raise ValueError('The selection changed. Select the source again.')

        def prepare():
            # Read only captured scope here; native selection controls belong to
            # the UI thread. The service rechecks topic access in its transaction.
            check_context()
            return prepare_source_file(library, *selection, authorized_topic=topic,
                                       cancelled=cancelled)

        def launch(path):
            # Source validation may take time. Check scope again immediately
            # before handing any path to the user's external viewer.
            check_selection()
            return wx.LaunchDefaultApplication(path)

        def ready(source):
            try:
                check_selection()
                if (source.identity.workspace, source.ref, source.identity.revision) != selection:
                    raise ValueError('The prepared source changed. Select the source again.')
                launch_source_file(library, source, launch, authorized_topic=topic,
                                   cancelled=cancelled)
                self.status.ChangeValue(f'Opened a viewing copy of {source.title}. '
                    f'Source location: {source.locator.display()}. '
                    'Use the external viewer to navigate to this location. '
                    'The retained original is unchanged.')
            except Exception as exc:
                self.status.ChangeValue(f'Original not opened: {exc}')
            self.status.SetFocus()

        self.frame.run_job('Preparing original source file', prepare, ready)

    def save_selected(self, event):
        if self.frame.working:
            self.frame.report('An operation is already running. Wait or choose Cancel current operation.')
            return
        try:
            hit = self.selected()
            if hit.workspace != self.bound_workspace or hit.workspace != self.model.workspace:
                raise ValueError('Select one result in the active library.')
            topics = tuple(self.frame.library.topics(hit.workspace))
            if not topics:
                self.status.ChangeValue('Create a research topic in Topics and Sections first.')
                self.status.SetFocus()
                return
            selected_topic = self.frame.active[1] if self.frame.active else None
            if selected_topic is None:
                labels = scope_labels((hit.workspace, t.id, t.name) for t in topics)
                dialog = wx.SingleChoiceDialog(self, 'Choose the research topic for this quotation.',
                                              'Save quotation', labels)
                try:
                    if dialog.ShowModal() != wx.ID_OK:
                        return
                    index = dialog.GetSelection()
                    if type(index) is not int or not 0 <= index < len(topics):
                        raise ValueError('Choose a research topic for this quotation.')
                    selected_topic = topics[index].id
                finally:
                    dialog.Destroy()
            epoch = self._context_epoch

            def save():
                if self.frame.cancel_event.is_set():
                    raise TimeoutError('Saving quotation cancelled.')
                return self.frame.library.save_hit(hit.workspace,selected_topic,hit.ref,hit.revision,
                    authorized_topic=self.bound_topic,cancelled=self.frame.cancel_event.is_set)

            def done(evidence_id):
                if self._context_epoch != epoch or self.model.workspace != hit.workspace:
                    return
                if self.frame.evidence is not None and not self.frame._dirty():
                    self.frame.evidence.refresh(evidence_id=evidence_id)
                self.status.ChangeValue('Quotation saved. Open its topic to add research notes.')
                self.status.SetFocus()

            self.frame.run_job('Saving verified quotation',save,done)
        except Exception as exc:
            self.status.ChangeValue(f'Quotation not saved: {exc}')
            self.status.SetFocus()

    def review_selected(self,event):
        self.frame.source_review_actions.review(event)


class DesktopFrame(WorkspaceFrame):
    def __init__(self, library, remember_data_dir=None):
        self.library = library
        self.remember_data_dir = remember_data_dir
        self.executor = DesktopExecutor()
        self.cancel_event = threading.Event()
        self.working = False
        self._closing = False
        super().__init__(None, library.list_scopes, self._bind)
        self.SetTitle('Resonance')
        self.SetMinSize((760, 650))
        self._menus()
        if self.scopes:
            self.choice.SetSelection(0)
            self.open_selected()
        else:
            self.status.SetValue('Welcome. Choose Library, New library to begin. All books stay on this computer.')
        if getattr(library,'semantic_notice',None):
            self.report(library.semantic_notice)

    def ocr_language_models(self, event):
        if self.working:
            self.report('Wait for the current operation or cancel it before changing OCR models.')
            return
        from .ocr_model_manager import show_ocr_model_manager
        show_ocr_model_manager(self)

    def _menus(self):
        self.menu_bar = wx.MenuBar()
        menus = [('&Library', [('New library\tCtrl+N', self.new_library),
            ('Open existing library folder',self.open_library_folder),
            ('Rename or describe library', self.rename_library), ('Archive library', self.archive_library),
            ('Restore archived library', self.unarchive_library),
            ('Import books\tCtrl+I', self.import_books),
            ('Import Center: files, folders and queues', self.import_center),
            ('OCR language models: Fast and Best...', self.ocr_language_models),
            ('Resume interrupted import',self.resume_import),
            ('Review pending imported page',self.review_pending_import),
            ('Add original text\tCtrl+Shift+N', self.add_text),
            ('Edit selected book metadata\tCtrl+Shift+M',self.edit_metadata),
            ('Library tools\tCtrl+Shift+L', lambda e: show_catalog_tools(self)),
            ('Back up all libraries', self.backup), ('Restore backup to a new folder', self.restore_backup)]),
            ('&Research', [('Export active topic\tCtrl+E', self.export_topic),
                ('Search saved research\tCtrl+Shift+F', lambda event: search_saved_research(self, event)),
                ('Build advanced query\tCtrl+Alt+Q', self.build_advanced_query),
                ('Edit selected evidence tags',self.edit_tags),
                ('Save a topic search', self.save_query), ('Run a saved topic search', self.run_saved_query),
                ('Refresh libraries and topics\tF5', self.refresh_scopes)]),
            ('&Academic', [('Academic library...\tCtrl+Shift+B', self.open_academic_library),
                ('DOI lookup and open-access sources...', lambda event: show_acquisition_dialog(self)),
                ('Add selected book to academic library', self.add_selected_book_reference)]),
            ('&Help', [('Keyboard guide\tF1', self.help),
                ('Document formats', lambda event: show_text(self, 'Document formats', supported_formats_text())),
                ('Check this installation', self.check_installation)])]
        for label, actions in menus:
            menu = wx.Menu()
            for title, handler in actions:
                item = menu.Append(wx.ID_ANY, title)
                self.Bind(wx.EVT_MENU, handler, item)
                if label == '&Research' and title.startswith('Build advanced query'):
                    from .command_registry import CommandSpec
                    from .keyboard_settings import register_menu_command
                    register_menu_command(self, CommandSpec(
                        'research.advanced_query', 'Build advanced query', 'main', 'Ctrl+Alt+Q'), item)
            self.menu_bar.Append(menu, label)
        self.SetMenuBar(self.menu_bar)
        cancel = wx.Button(self.host, label='Cancel current operation')
        cancel.Bind(wx.EVT_BUTTON, self.cancel_current_operation)
        self.layout.Insert(0, cancel, 0, wx.ALL, 3)
        self.cancel_button = cancel
        cancel.Disable()
        install_research_actions(self)
        install_source_review_actions(self)
        install_academic_actions(self)
        install_academic_comparison_actions(self)
        install_reading_desk_actions(self)
        from .ebook_reader import install_ebook_reader
        install_ebook_reader(self)
        install_ebook_actions(self)
        install_catalog_insights_actions(self)
        install_federated_search_actions(self)
        install_query_manager_actions(self)
        from .speech_dialog import install_speech_actions
        from .studio_dialog import install_studio_actions
        from .recording_dialog import install_recording_actions
        from .assistant_dialog import install_assistant_actions
        from .save_actions import install_save_actions
        install_speech_actions(self)
        install_studio_actions(self)
        from .audio_playback import install_audio_playback
        install_audio_playback(self)
        from .book_audio_dialog import install_book_audio_actions
        install_book_audio_actions(self)
        install_recording_actions(self)
        install_assistant_actions(self)
        install_save_actions(self)
        from .citation_formatting import install_citation_actions
        install_citation_actions(self)
        install_library_assistant_actions(self)
        from .translation_dialog import install_translation_actions
        install_translation_actions(self)
        from .transcription_dialog import install_transcription_actions
        install_transcription_actions(self)
        from .document_conversion import install_conversion_actions
        from .language_matrix import install_language_actions
        install_conversion_actions(self)
        install_language_actions(self)
        from .incremental_backup_dialog import install_incremental_backup
        install_incremental_backup(self)
        from .portable_sync import install_portable_sync_actions
        install_portable_sync_actions(self)
        from .trust_status import install_trust_actions
        install_trust_actions(self)
        from .operations_dialog import install_operations_actions
        install_operations_actions(self)
        from .keyboard_settings import install_keyboard_settings
        install_keyboard_settings(self)
        from .progress_panel import install_progress_panel
        install_progress_panel(self)
        from .local_schedules import install_local_schedules
        self.local_schedules = install_local_schedules(self)
        from .desktop_integration import install_desktop_integration
        install_desktop_integration(self)

    def build_advanced_query(self, event=None):
        if self.working:
            self.report('An operation is already running. Wait or choose Cancel current operation.')
            return
        if self.book is not None and self.active is not None:
            for index in range(self.book.GetPageCount()):
                panel = self.book.GetPage(index)
                if isinstance(panel, DesktopSearchPanel):
                    # Showing Search keeps any unsaved research note intact.
                    self.book.ChangeSelection(index)
                    panel.on_build_query(event)
                    return
        self.report('Open a library or research topic before building a query.')

    def make_evidence_panel(self,parent,services):
        return DesktopEvidencePanel(parent,self,services.workspace,services.topic,services.evidence_tx)

    def _workspace(self):
        if self.active is None:
            raise ValueError('Open a library first.')
        return self.active[0]

    def _topic(self):
        if self.active is None or self.active[1] is None:
            raise ValueError('Open a research topic using Library and research topic first.')
        return self.active

    def _bind(self, workspace, topic):
        library = self.library

        def visible(db):
            if topic is not None:
                TopicTree(db,workspace)._visible(topic)

        with library.read(workspace) as db:
            visible(db)

        @contextmanager
        def catalog_tx():
            with library.catalog_transaction(workspace) as service:
                visible(service.db)
                yield service

        @contextmanager
        def topics_tx():
            with library.topic_transaction(workspace) as service:
                visible(service.db)
                yield service

        @contextmanager
        def evidence_tx():
            with library.evidence_transaction(workspace,cancelled=self.cancel_event.is_set) as service:
                service.tree._visible(topic)
                yield service

        def search(requested, query, retrieval, result_mode, limit):
            if requested != workspace:
                raise ValueError('Library changed.')
            rows = library.search(workspace, query,mode='thematic' if retrieval == 'thematic' else 'phrase',
                                  limit=limit,authorized_topic=topic,result_mode=result_mode)
            return tuple(Hit(r.workspace, r.ref, r.revision, r.title, r.author,
                             r.edition, r.page, r.quote, r.citation) for r in rows)

        @contextmanager
        def reader(requested):
            if requested != workspace:
                raise ValueError('Library changed.')
            with library.read(workspace) as db:
                visible(db)
                yield db

        def context(requested,ref,revision):
            with reader(requested) as db:
                anchor,load,_ = library._anchor(db,workspace,ref,revision,cancelled=self.cancel_event.is_set)
                return open_context(workspace,(anchor,),load)

        def copy(requested,ref,revision,field):
            if field not in ('quote','citation'):
                raise ValueError('Choose quotation or citation.')
            with reader(requested) as db:
                anchor,load,_ = library._anchor(db,workspace,ref,revision,cancelled=self.cancel_event.is_set)
                return open_context(workspace,(anchor,),load).clipboard(workspace,0,field,load)

        return ScopeServices(workspace, topic, search, context, copy,
                             catalog_tx, active_collections, topics_tx,
                             evidence_tx if topic is not None else None)

    def make_search_panel(self, parent, services):
        return DesktopSearchPanel(parent, self, services)

    def refresh_scopes(self, event=None):
        if getattr(self,'busy',False):
            return
        super().refresh_scopes(event)
        if self.book is not None:
            try:
                self.book.GetPage(0).refresh_collections()
            except Exception as exc:
                self.report(f'Search collections unavailable: {exc}')

    def report(self, text):
        self.status.SetValue(text)
        self.status.SetFocus()

    def register_worker_cleanup(self, callback):
        """Retain idempotent resource release on the existing serial worker."""
        return self.executor.register_cleanup(callback)

    def cancel_current_operation(self, event=None):
        self.cancel_event.set()
        progress = getattr(self, 'progress_panel', None)
        if progress is not None:
            progress.request_cancel()

    def run_job(self, title, operation, done=None, failed=None, *, progress_total=None,
                progress_unit='units', with_progress=False):
        if self._closing:
            return
        if self.working:
            self.report('An operation is already running. Wait or choose Cancel current operation.')
            return
        progress = getattr(self, 'progress_panel', None)
        task = None
        progress_error = None

        def observe(callback):
            nonlocal progress_error
            try:
                return callback()
            except Exception as exc:
                progress_error = exc

        if progress is not None:
            task = observe(lambda: progress.begin(title, total=progress_total, unit=progress_unit))
        self.working = self.busy = True
        self.cancel_event.clear()
        self.cancel_button.Enable()
        self.choice.Disable()
        if self.book is not None:
            self.book.Disable()
        if progress is not None:
            observe(lambda: progress.set_busy(True))
        else:
            for i in range(self.menu_bar.GetMenuCount()):
                self.menu_bar.EnableTop(i, False)
        self.report(title + '…')

        def finish_progress(state, message=''):
            if progress is None:
                return
            try:
                if progress_error is not None:
                    raise progress_error
                progress.finish(task, state, message)
            except Exception as error:
                # Optional observation cannot reverse an already committed
                # operation or invoke its failure handler after successful done.
                result = {'succeeded': 'completed', 'failed': 'failed', 'cancelled': 'cancelled',
                          'paused': 'needs attention', 'unavailable': 'returned; see the operation results'}[state]
                warning = f'{title}: {result}. Progress reporting is unavailable: {error}'
                if task is not None:
                    try:
                        if not task.snapshot().terminal:
                            message = ''.join(character if (ord(character) >= 32 and ord(character) != 127)
                                              or character == '\n' else ' ' for character in warning)[:1000]
                            task.unavailable(message)
                    except Exception:
                        pass  # An already lost observer cannot alter the job result.
                try:
                    unavailable = getattr(progress, 'reporting_unavailable', None)
                    if callable(unavailable):
                        unavailable(task, state, warning)
                    else:
                        progress.notice = warning
                        progress.refresh()
                except Exception:
                    # Keep result-selected focus even when a native observer
                    # was destroyed or its notification provider failed.
                    try:
                        self.status.SetValue(warning)
                    except Exception:
                        pass

        def report_progress(completed=None, total=None, phase=None):
            if task is not None and progress_error is None:
                observe(lambda: task.update(completed=completed, total=total, phase=phase))

        def operation_result():
            return operation(report_progress) if with_progress else operation()

        def work():
            if task is None:
                return operation_result()
            from ..progress import bind_progress
            with bind_progress(task):
                return operation_result()

        try:
            future = self.executor.submit(work)
        except Exception as exc:
            self._finish_job_controls()
            finish_progress('failed', str(exc))
            self.report(f'{title}: could not start: {exc}')
            if failed is not None:
                failed(exc)
            return

        def finish(future):
            if self._closing:
                return
            def apply():
                if self._closing:
                    return
                self._finish_job_controls()
                try:
                    result = future.result()
                    cancellation_arrived = self.cancel_event.is_set()
                    from ..progress import operation_outcome
                    status = getattr(self, 'status', None)
                    before = observe(status.GetValue) if status is not None else None
                    # Result callbacks may still validate or publish an artifact
                    # and own the selected source control and detailed status.
                    if done is not None:
                        done(result)
                    if self._closing:
                        return
                    outcome = observe(lambda: operation_outcome(result)) or 'unavailable'
                    if not self.working and (status is None or observe(status.GetValue) == before):
                        label = {'succeeded': 'complete', 'paused': 'needs attention',
                                 'failed': 'failed', 'cancelled': 'cancelled',
                                 'unavailable': 'returned; see the operation results'}[outcome]
                        summary = title + ': ' + label + '.'
                        if progress_error is not None:
                            summary += ' Progress reporting is unavailable.'
                        if done is not None and status is not None:
                            # The callback may focus a panel's detailed status,
                            # results or reader without changing frame.status.
                            # Keep that focus while recording the job summary.
                            observe(lambda: status.SetValue(summary))
                        else:
                            self.report(summary)
                except Exception as exc:
                    from concurrent.futures import CancelledError
                    cancelled = isinstance(exc, (InterruptedError, CancelledError))
                    finish_progress('cancelled' if cancelled else 'failed', str(exc))
                    self.report(f'{title}: {exc}')
                    if failed is not None:
                        failed(exc)
                else:
                    finish_progress(outcome,
                        'Work completed before cancellation could stop it.'
                        if cancellation_arrived and outcome == 'succeeded' else
                        ('Work needs attention; completed portions were retained.' if outcome == 'paused' else ''))
            wx.CallAfter(apply)
        future.add_done_callback(finish)
        return future

    def _finish_job_controls(self):
        self.working = self.busy = False
        self.cancel_button.Disable()
        self.choice.Enable()
        if self.book is not None:
            self.book.Enable()
        progress = getattr(self, 'progress_panel', None)
        if progress is not None:
            try:
                progress.set_busy(False)
            except Exception:
                pass  # A detached observer cannot suppress a durable result.
        else:
            for i in range(self.menu_bar.GetMenuCount()):
                self.menu_bar.EnableTop(i, True)

    def _open_workspace(self, workspace, topic=None):
        self.refresh_scopes()
        if (workspace, topic) in self.scopes:
            self.choice.SetSelection(self.scopes.index((workspace, topic)))
            self.open_selected()

    def _open_data_root(self,path):
        if self.working:
            raise RuntimeError('Wait for the current operation to finish or cancel it before changing library folders.')
        path = Path(path)
        if not (path / 'library.sqlite').is_file():
            raise ValueError('Choose an existing research-library data folder containing library.sqlite.')
        if not self._confirm():
            return False
        candidate = LibraryApp(path,semantic_ranker=self.library.semantic_ranker)
        if self.remember_data_dir is not None:
            self.remember_data_dir(candidate.root)
        self._drop()
        self.library = candidate
        schedule_notice = ''
        if getattr(self, 'local_schedules', None) is not None:
            try:
                self.local_schedules.change_root()
            except Exception as exc:
                schedule_notice = ' Local schedules are disarmed; native timer cleanup needs attention: ' + str(exc)
        if getattr(self, 'keyboard_commands', None) is not None:
            self.keyboard_commands.change_root(candidate.root)
        if getattr(self, 'progress_panel', None) is not None:
            self.progress_panel.change_root(candidate.root)
        desktop_notice = ''
        if getattr(self, 'desktop_integration', None) is not None:
            try:
                self.desktop_integration.change_root()
            except Exception as error:
                desktop_notice = ' Desktop integration needs attention: ' + str(error)
        self.list_scopes = candidate.list_scopes
        self.refresh_scopes()
        if self.scopes:
            self.choice.SetSelection(0)
            self.open_selected()
        self.report(f'Opened library folder: {candidate.root}' + desktop_notice + schedule_notice)
        return True

    def open_library_folder(self,event):
        dialog = wx.DirDialog(self,'Choose an existing research-library data folder')
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = Path(dialog.GetPath())
        finally:
            dialog.Destroy()
        try:
            self._open_data_root(path)
        except Exception as exc:
            self.report(f'Library folder was not opened: {exc}')

    def new_library(self, event):
        name = text_dialog(self, 'New library', 'Library name')
        if name is not None:
            description = text_dialog(self, 'New library', 'Library description (optional)',
                                      multiline=True)
            if description is None:
                return
            try:
                self._open_workspace(self.library.create_workspace(name, description))
            except Exception as exc:
                self.report(str(exc))

    def rename_library(self, event):
        try:
            workspace = self._workspace()
            current = next(w for w in self.library.workspaces() if w.id == workspace)
            name = text_dialog(self, 'Rename library', 'Library name', current.name)
            if name is not None:
                description = text_dialog(self, 'Describe library', 'Library description (optional)',
                                          current.description, multiline=True)
                if description is None:
                    return
                self.library.rename_workspace(workspace, name, description)
                self.refresh_scopes()
        except Exception as exc:
            self.report(str(exc))

    def archive_library(self, event):
        try:
            workspace = self._workspace()
            name = next(w.name for w in self.library.workspaces() if w.id == workspace)
            if wx.MessageBox(f'Archive library "{name}"? Its sources and notes will be preserved.',
                'Archive library', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
                return
            if not self._confirm():
                return
            self.library.archive_workspace(workspace)
            self.refresh_scopes()
        except Exception as exc:
            self.report(str(exc))

    def unarchive_library(self, event):
        rows = [w for w in self.library.workspaces(include_archived=True) if w.archived]
        if not rows:
            self.report('There are no archived libraries.')
            return
        dialog = wx.SingleChoiceDialog(self, 'Choose a library to restore.', 'Archived libraries', [w.name for w in rows])
        try:
            if dialog.ShowModal() == wx.ID_OK:
                workspace = rows[dialog.GetSelection()].id
                self.library.restore_workspace(workspace)
                self._open_workspace(workspace)
        finally:
            dialog.Destroy()

    def import_center(self, event=None):
        try:
            from .import_center import show_import_center
            show_import_center(self, self._workspace())
        except Exception as exc:
            self.report(str(exc))

    def import_books(self, event):
        try:
            workspace = self._workspace()
        except ValueError as exc:
            self.report(str(exc))
            return
        dialog = wx.FileDialog(self, 'Import locally owned books',
            wildcard=file_dialog_wildcard(),
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST | wx.FD_MULTIPLE)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            paths = dialog.GetPaths()
        finally:
            dialog.Destroy()
        from .ocr_options import choose_ocr_config
        try:
            config = choose_ocr_config(self,paths)
        except Exception as exc:
            self.report(f'Import not started: {exc}')
            return
        if config is None:
            self.report('Import cancelled.')
            return
        def done(rows):
            completed = sum(r['state'] == 'complete' for r in rows)
            unattempted = max(0, len(paths) - len(rows))
            self.report(f'{completed} of {len(paths)} selected books imported. ' +
                ' '.join(f'{r["file"]}: {r["state"].replace("_", " ")}. ' +
                         (r.get('message') or r.get('error') or '')
                         for r in rows if r['state'] != 'complete') +
                (f' {unattempted} remaining selected files were not attempted.' if unattempted else '') +
                (' Choose Library, Review pending imported page to check quarantined OCR text.'
                 if any(r['state'] == 'quarantined' for r in rows) else ''))
            if self.book is not None:
                self.book.GetPage(1).refresh()
            if any(row.get('warnings') for row in rows):
                from .import_center import row_details
                report = '\n\n'.join(row_details(dict(file=row['file'], state=row['state'],
                    message=row.get('message', ''), result=row)) for row in rows)
                show_text(self, 'Import details', report)
        library = self.library
        self.run_job('Importing books', lambda measured: library.import_files(workspace, paths,
            cancelled=self.cancel_event.is_set, config=config, progress=measured), done, with_progress=True)

    def add_text(self, event):
        try:
            workspace = self._workspace()
            title = text_dialog(self, 'Add original text', 'Source title')
            if title is None:
                return
            text = text_dialog(self, 'Original source text', 'Enter the original text exactly. Notes belong in Evidence and Notes.', multiline=True)
            if text is None:
                return
            self.run_job('Saving original text', lambda: self.library.add_text(workspace, title, text),
                         lambda r: self.book.GetPage(1).refresh() if self.book else None)
        except Exception as exc:
            self.report(str(exc))

    def resume_import(self,event):
        try:
            workspace = self._workspace()
            rows = tuple(row for row in self.library.import_requests(workspace)
                         if row['state'] not in ('complete','superseded'))
            if not rows:
                self.report('There are no interrupted imports in this library.')
                return
            dialog = wx.SingleChoiceDialog(self,'Choose an import to resume from its retained original.',
                'Resume import',[f'{i}. {row["file"]}: {row["state"].replace("_"," ")}' for i,row in enumerate(rows,1)])
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                selected = rows[dialog.GetSelection()]
            finally:
                dialog.Destroy()
            def done(result):
                state = result['state'].replace('_',' ')
                self.report(f'Import {state}. ' + (result.get('message') or result.get('error') or '') +
                    (' Choose Library, Review pending imported page to check this page.'
                     if result['state'] == 'quarantined' else ''))
                if self.book is not None:
                    self.book.GetPage(1).refresh()
            self.run_job('Resuming import',lambda: self.library.resume_import(workspace,
                selected['request_id'],cancelled=self.cancel_event.is_set),done)
        except Exception as exc:
            self.report(str(exc))

    def review_pending_import(self,event):
        try:
            workspace = self._workspace()
            rows = tuple(row for row in self.library.import_requests(workspace)
                         if row['state'] not in ('complete','superseded'))
            if not rows:
                self.report('There are no pending imported pages in this library.')
                return
            dialog = wx.SingleChoiceDialog(self,'Choose the import whose current page you want to review.',
                'Review imported page',[f'{i}. {row["file"]}: {row["state"].replace("_"," ")}'
                                        for i,row in enumerate(rows,1)])
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                selected = rows[dialog.GetSelection()]
            finally:
                dialog.Destroy()
            request_id = selected['request_id']

            def prepared(page):
                if page is None:
                    self.report('This import has no page awaiting review. Choose Resume interrupted import to continue it.')
                    return
                show_text(self,'Imported page to compare with the original',
                    f'{selected["file"]}\nPhysical page {page["physical"]}\n\n'
                    'Compare the entire extracted text below with the same physical page or image frame of the original source.\n\n' +
                    page['raw_text'])
                if wx.MessageBox('Have you independently compared all the extracted text with this original physical page or image frame? '
                        'Confirm only after that comparison. Printed page numbers remain unverified.',
                        'Confirm independent imported-page review',
                        wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,self) != wx.YES:
                    return
                reviewer = text_dialog(self,'Independent page review','Reviewer name')
                if reviewer is None:
                    return

                def approve_and_resume():
                    self.library.confirm_import_review(workspace,request_id,reviewer,
                        confirmed=True,expected_checksum=page['checksum'])
                    return self.library.resume_import(workspace,request_id,
                        cancelled=self.cancel_event.is_set)

                def resumed(result):
                    if result['state'] == 'complete':
                        message = 'Page review saved. The imported book is ready to search.'
                    elif result['state'] == 'quarantined':
                        message = ('Page review saved. Another page needs independent review. '
                                   'Choose Library, Review pending imported page again.')
                    else:
                        message = ('Page review saved. Import ' + result['state'].replace('_',' ') + '. ' +
                                   (result.get('message') or result.get('error') or '') +
                                   ' Choose Resume interrupted import when ready.')
                    self.report(message)
                    if self.book is not None:
                        self.book.GetPage(1).refresh()

                self.run_job('Saving page review and resuming import',approve_and_resume,resumed)

            self.run_job('Preparing imported page for review',
                lambda: self.library.pending_review(workspace,request_id),prepared)
        except Exception as exc:
            self.report(f'Imported page review was not started: {exc}')

    def edit_metadata(self, event):
        edit_selected_book(self)

    def edit_tags(self,event):
        try:
            workspace,topic = self._topic()
            if self.evidence is None or self.book.GetCurrentPage() is not self.evidence:
                raise ValueError('Open Evidence and Notes and select the evidence to tag.')
            row = self.evidence._selected()
            if row is None or row != self.evidence.displayed:
                raise ValueError('Select a saved evidence item first.')
            current = self.library.evidence_tags(workspace,topic,row.evidence_id)
            value = text_dialog(self,'Evidence tags','One tag per line. Leave empty to remove tags.',
                                '\n'.join(current),multiline=True)
            if value is None:
                return
            names = tuple(line.strip() for line in value.splitlines() if line.strip())
            self.run_job('Saving evidence tags',lambda: self.library.set_evidence_tags(
                workspace,topic,row.evidence_id,names),lambda result: self.report('Evidence tags saved. Your research note is unchanged.'))
        except Exception as exc:
            self.report(str(exc))

    def export_topic(self, event):
        try:
            workspace, topic = self._topic()
            library = self.library
            formats = ('markdown', 'text', 'ris', 'bibtex', 'csv', 'docx', 'html')
            extensions = ('.md', '.txt', '.ris', '.bib', '.csv', '.docx', '.html')
            dialog = wx.FileDialog(self, 'Export topic', defaultFile='research-topic',
                wildcard='Markdown (*.md)|*.md|Plain text (*.txt)|*.txt|RIS (*.ris)|*.ris|BibTeX (*.bib)|*.bib|CSV (*.csv)|*.csv|Word document (*.docx)|*.docx|HTML document (*.html)|*.html',
                style=wx.FD_SAVE)
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                path = Path(dialog.GetPath())
                selected = dialog.GetFilterIndex()
                if not 0 <= selected < len(formats):
                    raise ValueError('Choose an export format.')
                format, extension = formats[selected], extensions[selected]
            finally:
                dialog.Destroy()
            if not path.suffix:
                # Some native save dialogs append the filter's extension and
                # others do not. Capture and confirm the actual final target.
                path = path.with_suffix(extension)
            elif format in ('docx', 'html') and path.suffix.lower() != extension:
                raise ValueError(f'The selected format needs a {extension} filename. '
                                 'Export again with the matching filename or file type.')
            approval = confirm_export_destination(self, path, private_root=library.root, wx_module=wx)
            if approval is None:
                return

            def selected_scope():
                if self.library is not library or self.active != (workspace, topic):
                    raise ValueError('The selected library or topic changed. Export again from the intended topic.')

            def export():
                selected_scope()
                content = library.export_topic(workspace,topic,format=format,
                                               cancelled=self.cancel_event.is_set)
                selected_scope()
                if self.cancel_event.is_set():
                    raise TimeoutError('Export cancelled; the destination was not changed.')
                writer = write_binary_export if format == 'docx' else write_export
                return writer(path,content,private_root=library.root,
                       cancelled=self.cancel_event.is_set, approval=approval)

            def done(result):
                if format in ('ris','bibtex'):
                    message = 'Bibliography exported. Quotations and research notes are not included.'
                elif format in ('docx', 'html'):
                    message = ('Research document exported with navigable headings, source citations, '
                               'and quotations kept separate from research notes.')
                else:
                    message = 'Topic exported with quotations and research notes kept separate.'
                warning = getattr(result, 'cleanup_warning', None)
                self.report(message + ('\n' + warning if isinstance(warning, str) and warning else ''))

            self.run_job('Exporting verified topic',export,done,
                         lambda error: self.report(f'Export not completed: {error}'))
        except Exception as exc:
            self.report(f'Export not completed: {exc}')

    def save_query(self, event):
        try:
            workspace, topic = self._topic()
            name = text_dialog(self, 'Save topic search', 'Name for this saved search')
            if name is None:
                return
            query = text_dialog(self, 'Save topic search', 'Words to search in saved quotations')
            if query is not None:
                self.library.save_query(workspace, topic, name, query)
                self.report('Topic search saved.')
        except Exception as exc:
            self.report(str(exc))

    def run_saved_query(self, event):
        try:
            workspace, topic = self._topic()
            with self.library.evidence_transaction(workspace) as store:
                rows = SavedTopicQueries(store).list_queries(topic)
            if not rows:
                self.report('No saved searches in this topic.')
                return
            dialog = wx.SingleChoiceDialog(self, 'Choose a saved search.', 'Topic searches', [r.name for r in rows])
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                query = rows[dialog.GetSelection()]
            finally:
                dialog.Destroy()
            def done(result):
                if self.active != (workspace, topic):
                    return
                show_text(self, query.name, format_topic_results(result))
                self.status.SetFocus()
            self.run_job('Searching saved quotations', lambda: self.library.run_query(
                workspace, topic, query.id, cancelled=self.cancel_event.is_set), done)
        except Exception as exc:
            self.report(str(exc))

    def backup(self, event):
        dialog = wx.DirDialog(self, 'Choose a parent folder for a backup of completed libraries; unfinished imports are excluded')
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            parent = Path(dialog.GetPath())
        finally:
            dialog.Destroy()
        from datetime import datetime
        destination = parent / ('ResearchLibrary-backup-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
        def done(checksum):
            show_text(self, 'Backup complete', f'Backup folder: {destination}\n\n'
                'All completed libraries, original files, saved evidence, notes, tags, searches, '
                'reading positions and bookmarks are included. '
                'Unfinished imports and their pending reviews are excluded.\n\n'
                f'Keep this checksum separately; it is needed to verify a restore:\n{checksum}')
        self.run_job('Backing up all libraries', lambda: self.library.backup(destination), done)

    def restore_backup(self, event):
        dialog = wx.DirDialog(self, 'Select the backup folder')
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            source = Path(dialog.GetPath())
        finally:
            dialog.Destroy()
        checksum = text_dialog(self, 'Verify backup', 'Paste the manifest checksum saved when this backup was created.')
        if checksum is None:
            return
        dialog = wx.DirDialog(self, 'Choose a parent folder for the restored libraries')
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            parent = Path(dialog.GetPath())
        finally:
            dialog.Destroy()
        from datetime import datetime
        from ..library_backup import restore_backup
        destination = parent / ('ResearchLibrary-restored-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
        def done(path):
            if wx.MessageBox('Backup restored successfully. Open the restored libraries now?',
                'Restore complete',wx.YES_NO | wx.ICON_QUESTION,self) == wx.YES:
                self._open_data_root(path)
            else:
                show_text(self,'Restore complete',f'Restored library folder: {path}\n\n'
                    'You can open it later through Library, Open existing library folder.')
        self.run_job('Restoring backup', lambda: restore_backup(source, destination, expected_manifest_sha256=checksum.strip()),
            done)

    def check_installation(self, event):
        from ..__main__ import self_test
        self.run_job('Checking this installation', self_test,
            lambda result: show_text(self, 'Installation check', '\n'.join(result['checks']) + '\n\nAll checks passed.'))

    def open_academic_library(self, event):
        from .academic_library_dialog import open_academic_library
        open_academic_library(self)

    def add_selected_book_reference(self, event):
        from .academic_library_dialog import add_selected_book_reference
        add_selected_book_reference(self)

    def help(self, event):
        if getattr(self, 'keyboard_commands', None) is not None:
            show_text(self, 'Keyboard guide', self.keyboard_commands.help_text())
            return
        show_text(self, 'Keyboard guide',
            'Use Alt+L for Library, Alt+R for Research, Alt+A for Academic, Alt+B for Bibliography, '
            'Alt+S for Source, Alt+D for Reading, Alt+T for Tools, and Alt+H for Help.\n'
            'Tools, Local library workflow assistant previews and runs selected local library operations with saved recovery checkpoints.\n'
            'Ctrl+Alt+M compares saved sources in the active topic. Academic also exports filtered outlines, reviews RIS metadata and checks the catalog.\n'
            'Ctrl+N creates a library. Ctrl+I imports books. Ctrl+Shift+N adds original text.\n'
            'Library, Import Center opens folder scans, duplicate checks and saved import queues.\n'
            'Help, Document formats explains supported text, ebook, email, presentation and spreadsheet formats and their limits.\n'
            'Ctrl+R opens the selected book with page or segment resume, bookmarks and reading history.\n'
            'Ctrl+Shift+R opens EPUB section navigation with chapter titles and separate image descriptions.\n'
            'Ctrl+Shift+O opens contents, headings, chapter search and an extraction report for the selected EPUB.\n'
            'Ctrl+Shift+I resumes an interrupted import in the selected library.\n'
            'Ctrl+Alt+Q opens the advanced query builder with All, Any, Exclude and Nearby conditions.\n'
            'Ctrl+Shift+M edits the book selected in Books and Collections. Saved citations keep their original metadata.\n'
            'Ctrl+Shift+B opens the academic bibliography and reading list for the current library.\n'
            'Inside the Reading Desk dialog, Ctrl+Shift+B manages page bookmarks instead.\n'
            'Academic, Add selected book links an imported book to an editable bibliographic reference.\n'
            'Academic references have reading status, priority, rating, progress, tags and personal notes.\n'
            'Use Apply filters, Next page and Previous page to browse every matching reference.\n'
            'Academic library imports are reviewed before saving. Its exports ask for all active references or selected only; notes are excluded by default.\n'
            'Ctrl+Shift+L opens Library tools: catalog reports, collection bibliography, source inventory and literature-review matrix.\n'
            'Ctrl+Alt+F opens Research Search Center for explicitly selected libraries and source metadata filters.\n'
            'Research, Manage saved topic searches creates, edits, archives or restores searches of quotations and notes in the active topic.\n'
            'Ctrl+Shift+T edits tags for selected evidence. Ctrl+Alt+T finds evidence by a tag in the active topic.\n'
            'Ctrl+Shift+F searches saved quotations and notes across topics in the current library.\n'
            'F1 inside the reader explains page navigation and bookmarks.\n'
            'Tab and Shift+Tab move between controls. Arrow keys choose list items.\n'
            'Ctrl+Tab switches sections. Enter searches from the query field.\n'
            'F5 refreshes library and topic choices. Ctrl+E exports the active topic.\n\n'
            'Ctrl+Shift+E exports the active topic together with its nonarchived subtopics.\n'
            'Bibliography exports a library or collection as CSL-JSON, RIS or BibTeX.\n'
            'Select a book, then Bibliography, Review reference metadata to review a local RIS or CSL-JSON file.\n'
            'Use the named reference buttons to replace chosen fields, then Save metadata.\n\n'
            'In Export topic, choose Word or HTML for a readable research document with heading navigation.\n'
            'The Research menu also opens Library overview, Source metadata and indexing checklist, '
            'and Export source comparison matrix for this library or one of its collections.\n'
            'Choose a library or topic, then Open selected scope. The Search tab stays inside that library.\n'
            'Research Search Center searches only the libraries explicitly checked in its dialog.\n'
            'Its Build advanced query command also uses Ctrl+Alt+Q and keeps checked libraries and filters.\n'
            'All words and Advanced searches offer Previous and Next result pages; changing options starts a new search.\n'
            'Search mode chooses how matches are found. Result mode chooses Short quotations or Thematic excerpts.\n'
            'Advanced mode supports uppercase AND, OR, NOT, parentheses, quoted phrases and NEAR/5.\n'
            'NEAR/5 means at most five intervening words, in either order, within one page or text segment.\n'
            'Use the builder for operator words or phrases containing quotation marks. Other search modes keep their own meaning.\n'
            'Both modes show exact source text. Thematic excerpts includes more context on the same page or segment.\n'
            'Original quotations cannot be edited. Put your commentary in Evidence and Notes.\n'
            'TXT quotations are checked against original bytes. PDF pages may require independent review.\n'
            'DOCX and EPUB extraction remains unverified. Reading or bookmarking does not approve quotations.\n'
            'The Source menu records or withdraws an independent review of the selected PDF page.\n'
            'For quarantined OCR imports, use Library, Review pending imported page; each page needs explicit independent comparison.\n'
            'Complete backups include libraries, pending import inputs, application state, reading positions and bookmarks.\n'
            'Reading-menu export and import can also transfer bookmarks separately. Keep the backup manifest checksum.\n'
            'Printed pages are shown only when verified; otherwise physical PDF pages or unpaginated segments are identified.')

    def on_close(self, event):
        if self.working:
            if event.CanVeto():
                event.Veto()
                self.report('An operation is running. Cancel it or wait before closing.')
                return
        super().on_close(event)
        if not event.GetVeto():
            if getattr(self, 'local_schedules', None) is not None:
                try:
                    self.local_schedules.close()
                except Exception as exc:
                    self.report(f'Local schedules are disarmed; native timer cleanup needs attention: {exc}')
                    if event.CanVeto():
                        event.Veto()
                        return
            controller = getattr(self, 'desktop_integration', None)
            if controller is not None:
                try:
                    controller.close()
                except Exception as error:
                    if event.CanVeto():
                        event.Veto()
                        self.report(str(error))
                        return
                    # Forced destruction revokes dispatch and retains native
                    # owners in the controller; do not interrupt worker cleanup.
            self._closing = True
            self.cancel_event.set()
            self.executor.shutdown(wait=False, cancel_futures=True)
            if getattr(self, 'progress_panel', None) is not None:
                self.progress_panel.close()

