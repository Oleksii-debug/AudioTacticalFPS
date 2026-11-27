"""Academic workflows composed from the existing scoped application services.

Long reads and exports use the desktop job runner. Modal choices never authorize
another library or topic, and exports never trust the displayed matrix cache.
"""
from dataclasses import dataclass
from pathlib import Path

from ..research.export_file import write_export
from .export_destination import confirm_export_destination
from ..research.topic_literature_matrix import build_literature_matrix, export_matrix
from .literature_matrix import make_matrix_dialog


@dataclass(frozen=True)
class AcademicScope:
    library: object
    workspace: str
    topic: str | None

    @property
    def ids(self):
        return self.workspace, self.topic


class AcademicDialogs:
    def matrix(self, frame, snapshot, *, include_descendants=True, tag_filter=None):
        dialog = make_matrix_dialog(frame, snapshot, include_descendants=include_descendants,
                                    tag_filter=tag_filter)
        try:
            dialog.ShowModal()
            return dialog.command
        finally:
            dialog.Destroy()

    def save_path(self, frame, *, kind, format):
        import wx

        extension, wildcard = {
            'markdown': ('md', 'Markdown (*.md)|*.md'),
            'text': ('txt', 'Plain text (*.txt)|*.txt'),
            'csv': ('csv', 'CSV (*.csv)|*.csv'),
        }[format]
        name = 'literature-review'
        dialog = wx.FileDialog(frame, 'Save ' + name.replace('-', ' '),
            defaultFile=f'{name}.{extension}', wildcard=wildcard,
            style=wx.FD_SAVE)
        try:
            return Path(dialog.GetPath()) if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()


class AcademicActions:
    def __init__(self, frame, dialogs=None):
        self.frame = frame
        self.dialogs = dialogs or AcademicDialogs()

    def _scope(self, *, topic=False):
        frame = self.frame
        if frame.working or getattr(frame, 'busy', False):
            raise ValueError('Wait for the current operation or choose Cancel current operation.')
        if getattr(frame, '_closing', False):
            raise ValueError('The library is closing.')
        if frame.active is None:
            raise ValueError('Open a library first.')
        if topic and frame.active[1] is None:
            raise ValueError('Open a research topic using Library and research topic first.')
        return AcademicScope(frame.library, *frame.active)

    def _current(self, scope):
        return (not getattr(self.frame, '_closing', False)
                and self.frame.library is scope.library and self.frame.active == scope.ids)

    def _require_current(self, scope):
        if not self._current(scope):
            raise ValueError('The selected library or topic changed. Open the command again.')

    def compare_sources(self, event=None, *, include_descendants=True, tag_filter=None):
        try:
            scope = self._scope(topic=True)

            def load():
                return build_literature_matrix(scope.library, scope.workspace, scope.topic,
                    include_descendants=True, cancelled=self.frame.cancel_event.is_set)

            def done(snapshot):
                if not self._current(scope) or self.frame.cancel_event.is_set():
                    return
                try:
                    command = self.dialogs.matrix(self.frame, snapshot,
                        include_descendants=include_descendants, tag_filter=tag_filter)
                    if command is None:
                        self.frame.report('Source comparison closed. Saved research is unchanged.')
                        return
                    self._require_current(scope)
                    if command.kind == 'open_evidence':
                        self.open_evidence(scope, command.evidence)
                    elif command.kind == 'refresh':
                        self.compare_sources(include_descendants=command.include_descendants,
                                             tag_filter=command.tag_filter)
                    elif command.kind in ('matrix_csv', 'matrix_markdown'):
                        self._export(scope, kind='matrix', format=command.kind.removeprefix('matrix_'),
                                     include_descendants=command.include_descendants, tag_filter=command.tag_filter)
                    else:
                        raise ValueError('Unknown source comparison action.')
                except Exception as exc:
                    self.frame.report(f'Source comparison: {exc}')

            self.frame.run_job('Comparing saved sources', load, done)
        except Exception as exc:
            self.frame.report(f'Source comparison unavailable: {exc}')

    def open_evidence(self, scope, evidence):
        """Use the current evidence editor, including its unsaved-note guard."""
        self._require_current(scope)
        if evidence is None:
            raise ValueError('Select a saved evidence item first.')
        wanted = scope.workspace, evidence.topic_id
        if self.frame.active != wanted:
            self.frame._open_workspace(*wanted)
        if self.frame.library is not scope.library or self.frame.active != wanted:
            # The existing host may retain an unsaved note instead of switching.
            return
        panel, book = getattr(self.frame, 'evidence', None), self.frame.book
        if panel is None or book is None or (panel.workspace, panel.topic) != wanted:
            raise ValueError('The saved evidence editor is no longer available.')
        for number in range(book.GetPageCount()):
            if book.GetPage(number) is panel:
                book.SetSelection(number)
                break
        else:
            raise ValueError('The saved evidence section is unavailable.')
        if not panel.refresh(evidence_id=evidence.evidence_id, focus=True):
            panel.note.SetFocus()

    def export_comparison(self, *, format='markdown', include_descendants=True, tag_filter=None):
        try:
            scope = self._scope(topic=True)
            self._export(scope, kind='matrix', format=format,
                         include_descendants=include_descendants, tag_filter=tag_filter)
        except Exception as exc:
            self.frame.report(f'Source comparison export unavailable: {exc}')

    def _export(self, scope, *, kind, format, include_descendants, tag_filter):
        self._require_current(scope)
        destination = self.dialogs.save_path(self.frame, kind=kind, format=format)
        if destination is None:
            self.frame.report('Export cancelled. No file was written.')
            return
        approval = confirm_export_destination(self.frame, destination, private_root=scope.library.root)
        if approval is None:
            self.frame.report('Export cancelled. No file was written.')
            return
        self._require_current(scope)
        if kind != 'matrix':
            raise ValueError('Unknown source comparison export.')

        def save():
            self._require_current(scope)
            text = export_matrix(scope.library, scope.workspace, scope.topic, format=format,
                include_descendants=include_descendants, tag_filter=tag_filter,
                cancelled=self.frame.cancel_event.is_set)
            if self.frame.cancel_event.is_set():
                raise InterruptedError('Export cancelled. No file was written.')
            self._require_current(scope)
            write_export(destination, text, private_root=scope.library.root,
                         cancelled=self.frame.cancel_event.is_set, approval=approval)
            return destination

        def done(path):
            if self._current(scope):
                self.frame.report(f'Export saved: {path}')

        self.frame.run_job('Exporting source comparison', save, done)


def edit_catalog_edition(frame, workspace, edition_id):
    """Open the existing native metadata form for a quality-review selection."""
    actions = AcademicActions(frame)
    try:
        scope = actions._scope()
        if scope.workspace != workspace:
            raise ValueError('The selected library changed. Run the catalog check again.')

        def done(current):
            if not actions._current(scope) or frame.cancel_event.is_set():
                return
            import wx
            from .catalog_metadata_dialog import MetadataDialog

            def save(**values):
                actions._require_current(scope)
                return scope.library.update_metadata(scope.workspace, edition_id,
                    expected_metadata=current, **values)

            dialog = MetadataDialog(frame, current, save=save, executor=frame.executor)
            try:
                saved = dialog.ShowModal() == wx.ID_OK
            finally:
                dialog.Destroy()
            if saved and actions._current(scope):
                if frame.book is not None:
                    frame.book.GetPage(1).refresh()
                    frame.book.GetPage(0).refresh_collections()
                frame.report('Book metadata saved. Run the catalog quality check again to review the updated record.')

        frame.run_job('Loading book metadata', lambda: scope.library.edition_metadata(workspace, edition_id), done)
    except Exception as exc:
        frame.report(f'Metadata editor unavailable: {exc}')


def install_academic_comparison_actions(frame):
    import wx
    from .catalog_quality import open_catalog_quality

    actions = AcademicActions(frame)
    academic_index = frame.menu_bar.FindMenu('Academic')
    menu = (frame.menu_bar.GetMenu(academic_index)
            if academic_index >= 0 else wx.Menu())
    commands = (
        ('Compare sources in active topic', actions.compare_sources),
        ('Check catalog quality', lambda event: open_catalog_quality(frame,
            edit_edition=lambda workspace, edition: edit_catalog_edition(frame, workspace, edition))),
    )
    for title, handler in commands:
        item = menu.Append(wx.ID_ANY, title)
        frame.Bind(wx.EVT_MENU, handler, item)
    # Preserve existing menus and their handlers, including post-merge fixes.
    if academic_index < 0:
        help_index = frame.menu_bar.FindMenu('Help')
        frame.menu_bar.Insert(help_index if help_index >= 0 else frame.menu_bar.GetMenuCount(), menu, '&Academic')
    frame.academic_comparison_actions = actions
    return actions
