"""Accessible, read-only reports for the currently authorized source catalog.

The controller captures the application object as well as workspace/topic IDs.
Dialogs and background jobs never refresh panels, change saved evidence or
grant source approval. Exports use the checked publisher outside private data.
"""
from dataclasses import dataclass
from pathlib import Path

from ..catalog.insights import analyze_catalog, display_value, format_checklist, format_overview, report_collections
from ..catalog.membership import Catalog
from ..research.export_file import write_export, export_completion
from ..research.topic_tree import TopicTree
from .research_actions import NativeDialogs


FORMATS = (
    ('csv', 'CSV table (*.csv)', 'csv', 'CSV files (*.csv)|*.csv'),
    ('markdown', 'Markdown table (*.md)', 'md', 'Markdown files (*.md)|*.md'),
    ('text', 'Plain text (*.txt)', 'txt', 'Text files (*.txt)|*.txt'),
)


class InsightsDialogs(NativeDialogs):
    def focused_control(self, parent):
        import wx

        return wx.Window.FindFocus() or parent.choice

    def destination(self, parent, format):
        import wx

        _, _, extension, wildcard = next(row for row in FORMATS if row[0] == format)
        dialog = wx.FileDialog(parent, 'Save source comparison matrix',
            defaultFile='source-comparison-matrix.' + extension, wildcard=wildcard,
            style=wx.FD_SAVE)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return None
            return Path(dialog.GetPath())
        finally:
            dialog.Destroy()


@dataclass(frozen=True)
class _Scope:
    library: object
    workspace: str
    topic: str | None
    book: object
    cancel_event: object
    focus: object
    request: object

    @property
    def ids(self):
        return self.workspace, self.topic


@dataclass(frozen=True)
class _Collection:
    identity: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class _Report:
    analysis: object
    text: str


@dataclass(frozen=True)
class _ExportReceipt:
    path: Path
    format: str
    cleanup_warning: str | None = None


class CatalogInsightsActions:
    def __init__(self, frame, dialogs=None):
        self.frame = frame
        self.dialogs = dialogs or InsightsDialogs()
        self._request = None

    def _idle(self):
        if getattr(self.frame, '_closing', False):
            raise ValueError('The library is closing.')
        if self.frame.working or getattr(self.frame, 'busy', False):
            raise ValueError('An operation is already running. Wait or choose Cancel current operation.')

    def _capture(self):
        self._idle()
        if self.frame.active is None:
            raise ValueError('Open a library first.')
        self._request = object()
        return _Scope(self.frame.library, *self.frame.active, self.frame.book,
                      self.frame.cancel_event, self.dialogs.focused_control(self.frame), self._request)

    def _context(self, scope, *, cancellable=False):
        # Attribute checks are safe in the worker. Never inspect wx controls
        # there: their lifetime and selection belong to the main thread.
        if (getattr(self.frame, '_closing', False)
                or self.frame.library is not scope.library
                or self.frame.active != scope.ids
                or self.frame.book is not scope.book
                or self.frame.cancel_event is not scope.cancel_event
                or self._request is not scope.request):
            raise ValueError('The selected library or topic changed. Open the command again.')
        if cancellable and scope.cancel_event.is_set():
            raise TimeoutError('The report was cancelled.')

    def _authorize(self, scope, collection=None, *, cancellable=False):
        self._context(scope, cancellable=cancellable)
        with scope.library.read(scope.workspace) as db:
            if scope.topic is not None:
                TopicTree(db, scope.workspace)._visible(scope.topic)
            pairs = report_collections(Catalog(db, scope.workspace))
            if collection is not None and collection.identity is not None:
                if (collection.identity, collection.name) not in pairs:
                    raise ValueError('The selected collection changed or was archived. Choose its scope again.')
        self._context(scope, cancellable=cancellable)
        return pairs

    def _focus(self, scope):
        try:
            self._context(scope)
            self._idle()
        except ValueError:
            return
        try:
            scope.focus.SetFocus()
        except (AttributeError, RuntimeError):
            self.frame.status.SetFocus()

    def _cancel(self, scope):
        if not getattr(self.frame, '_closing', False):
            self.frame.report('Report cancelled. No export file was changed.')
            self._focus(scope)

    def _failed(self, title, error):
        if not getattr(self.frame, '_closing', False):
            self.frame.report(f'{title} not completed: {error}')

    def _choose_scope(self, scope):
        pairs = self._authorize(scope)
        choices = ['All sources in this library'] + [
            f'Collection {number}: {display_value(name)}'
            for number, (_, name) in enumerate(pairs, 1)
        ]
        index = self.dialogs.choose(self.frame, 'Report scope',
            'Choose all sources in the active library or one collection. '
            'The report contains metadata and stored research counts; source text and notes are excluded.',
            choices)
        if index is None:
            self._cancel(scope)
            return None
        if type(index) is not int or not 0 <= index < len(choices):
            raise ValueError('Choose one report scope.')
        collection = _Collection() if index == 0 else _Collection(*pairs[index - 1])
        self._idle()
        self._authorize(scope, collection)
        return collection

    @staticmethod
    def _check_analysis(scope, collection, analysis):
        if (analysis.workspace != scope.workspace
                or analysis.collection_id != collection.identity
                or analysis.collection_name != collection.name):
            raise ValueError('The report does not match the selected library and collection.')

    def _analyze(self, scope, collection):
        self._authorize(scope, collection, cancellable=True)
        analysis = analyze_catalog(scope.library, scope.workspace,
            collection=collection.identity, authorized_topic=scope.topic,
            cancelled=scope.cancel_event.is_set)
        self._check_analysis(scope, collection, analysis)
        self._authorize(scope, collection, cancellable=True)
        return analysis

    def _show_report(self, title, formatter):
        try:
            scope = self._capture()
            collection = self._choose_scope(scope)
            if collection is None:
                return

            def operation():
                analysis = self._analyze(scope, collection)
                text = formatter(analysis)
                self._context(scope, cancellable=True)
                return _Report(analysis, text)

            def done(result):
                self._authorize(scope, collection, cancellable=True)
                if not isinstance(result, _Report) or not isinstance(result.text, str):
                    raise ValueError('The report result is invalid.')
                self._check_analysis(scope, collection, result.analysis)
                self.dialogs.show(self.frame, title, result.text)
                self._focus(scope)

            self.frame.run_job('Preparing ' + title.lower(), operation, done,
                               lambda error: self._failed(title, error))
        except Exception as exc:
            self._failed(title, exc)

    def overview(self, event=None):
        self._show_report('Library overview', format_overview)

    def checklist(self, event=None):
        self._show_report('Source metadata and indexing checklist', format_checklist)

    def export_matrix(self, event=None):
        title = 'Source comparison matrix export'
        try:
            scope = self._capture()
            collection = self._choose_scope(scope)
            if collection is None:
                return
            index = self.dialogs.choose(self.frame, 'Source comparison matrix format',
                'Choose an export format. The matrix includes bibliographic metadata, '
                'stored counts and visible topic labels. Quotations and notes are excluded.',
                [row[1] for row in FORMATS])
            if index is None:
                self._cancel(scope)
                return
            if type(index) is not int or not 0 <= index < len(FORMATS):
                raise ValueError('Choose CSV, Markdown or plain text.')
            self._idle()
            self._authorize(scope, collection)
            format = FORMATS[index][0]
            path = self.dialogs.destination(self.frame, format)
            if path is None:
                self._cancel(scope)
                return
            path = Path(path)
            self._idle()
            self._authorize(scope, collection)
            approval = self.dialogs.approve_export(self.frame, path, private_root=scope.library.root)
            if approval is None:
                self._cancel(scope)
                return

            def operation():
                from ..research.literature_matrix import render_literature_matrix

                analysis = self._analyze(scope, collection)
                text = render_literature_matrix(analysis, format=format, include_trace=False,
                                                cancelled=scope.cancel_event.is_set)
                # Recheck authorization and cancellation immediately before
                # atomic publication. No reads of frame.library occur here.
                self._authorize(scope, collection, cancellable=True)
                receipt = write_export(path, text, private_root=scope.library.root,
                                       cancelled=scope.cancel_event.is_set, approval=approval)
                # Once publication succeeds, cancellation cannot undo it. A
                # receipt lets the callback describe that outcome truthfully.
                return _ExportReceipt(path, format, getattr(receipt, 'cleanup_warning', None))

            def done(result):
                if not isinstance(result, _ExportReceipt) or (result.path, result.format) != (path, format):
                    raise ValueError('The export result does not match its requested destination.')
                try:
                    self._authorize(scope, collection, cancellable=True)
                except Exception:
                    if not getattr(self.frame, '_closing', False):
                        self.frame.report(export_completion(
                            'The matrix file was saved before cancellation or a library change '
                            'was detected. Reopen the command to make a current report.', result))
                    return
                self.frame.report(export_completion(
                    'Source comparison matrix saved to ' + display_value(str(path))
                    + '. Quotations and notes are not included.', result))

            self.frame.run_job('Exporting source comparison matrix', operation, done,
                               lambda error: self._failed(title, error))
        except Exception as exc:
            self._failed(title, exc)


def install_catalog_insights_actions(frame):
    """Add three Research entries once; reuse matching entries when present."""
    import wx

    installed = getattr(frame, 'catalog_insights_actions', None)
    if installed is not None:
        return installed
    index = frame.menu_bar.FindMenu('Research')
    if index < 0:
        raise ValueError('The Research menu is required for catalog reports.')
    actions = CatalogInsightsActions(frame)
    menu = frame.menu_bar.GetMenu(index)
    entries = (
        ('Library &overview...', actions.overview),
        ('Source metadata and indexing &checklist...', actions.checklist),
        ('Export source comparison &matrix...', actions.export_matrix),
    )

    def caption(label):
        return label.split('\t', 1)[0].replace('&', '').rstrip('.').strip().casefold()

    separated = False
    for label, callback in entries:
        matches = [item for item in menu.GetMenuItems()
                   if not item.IsSeparator() and caption(item.GetItemLabel()) == caption(label)]
        if matches:
            item, *duplicates = matches
            frame.Unbind(wx.EVT_MENU, id=item.GetId())
            item.SetItemLabel(label)
            for duplicate in duplicates:
                frame.Unbind(wx.EVT_MENU, id=duplicate.GetId())
                menu.Delete(duplicate.GetId())
        else:
            if not separated:
                menu.AppendSeparator()
                separated = True
            item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, callback, item)
    frame.catalog_insights_actions = actions
    return actions
