"""Academic workflows composed from the existing scoped application services.

Long reads and exports use the desktop job runner. Modal choices never authorize
another library or topic, and exports never trust the displayed matrix cache.
"""
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from ..research.export_file import write_export
from ..research.topic_tree import TopicTree
from ..research.topic_literature_matrix import build_literature_matrix, export_matrix
from ..research.outline_export import export_topic_outline as export_outline
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

    def outline_options(self, frame, *, include_descendants=True, tag_filter=None):
        import wx

        dialog = wx.Dialog(frame, title='Export research outline', size=(570, 340))
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            description = wx.StaticText(dialog, label=(
                'Export the active research topic as an ordered outline. '
                'Source quotations and your research notes have separate labels.'))
            description.Wrap(520)
            layout.Add(description, 0, wx.EXPAND | wx.ALL, 8)
            descendants = wx.CheckBox(dialog, label='Include &subtopics')
            descendants.SetName('Include subtopics in research outline')
            descendants.SetValue(include_descendants)
            layout.Add(descendants, 0, wx.ALL, 8)
            layout.Add(wx.StaticText(dialog, label='Only this exact tag; blank includes all'),
                       0, wx.LEFT | wx.RIGHT, 8)
            tag = wx.TextCtrl(dialog, value=tag_filter or '')
            tag.SetName('Exact tag for outline; blank includes all tags')
            layout.Add(tag, 0, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.StaticText(dialog, label='Export format'), 0, wx.LEFT | wx.RIGHT, 8)
            kind = wx.Choice(dialog, choices=['Markdown (.md)', 'Plain text (.txt)'])
            kind.SetName('Research outline export format')
            kind.SetSelection(0)
            layout.Add(kind, 0, wx.EXPAND | wx.ALL, 8)
            layout.Add(dialog.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL),
                       0, wx.ALIGN_RIGHT | wx.ALL, 8)
            dialog.SetSizer(layout)
            dialog.SetEscapeId(wx.ID_CANCEL)
            descendants.SetFocus()
            if dialog.ShowModal() != wx.ID_OK:
                return None
            return descendants.GetValue(), tag.GetValue().strip() or None, ('markdown', 'text')[kind.GetSelection()]
        finally:
            dialog.Destroy()

    def save_path(self, frame, *, kind, format):
        import wx

        extension, wildcard = {
            'markdown': ('md', 'Markdown (*.md)|*.md'),
            'text': ('txt', 'Plain text (*.txt)|*.txt'),
            'csv': ('csv', 'CSV (*.csv)|*.csv'),
        }[format]
        name = 'research-outline' if kind == 'outline' else 'literature-review'
        dialog = wx.FileDialog(frame, 'Save ' + name.replace('-', ' '),
            defaultFile=f'{name}.{extension}', wildcard=wildcard,
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
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
        if (getattr(self.frame, '_closing', False)
                or self.frame.library is not scope.library or self.frame.active != scope.ids):
            return False
        # A background result can outlive its authorized read snapshot even when
        # this frame's selection is unchanged. Recheck external archival too.
        try:
            with scope.library.read(scope.workspace) as db:
                if scope.topic is not None:
                    TopicTree(db, scope.workspace)._visible(scope.topic)
            return True
        except (OSError, ValueError, sqlite3.Error):
            return False

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
                if self.frame.cancel_event.is_set() or not self._current(scope):
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
                    elif command.kind == 'outline':
                        self.outline(include_descendants=command.include_descendants,
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

    def outline(self, event=None, *, include_descendants=True, tag_filter=None):
        try:
            scope = self._scope(topic=True)
            options = self.dialogs.outline_options(self.frame,
                include_descendants=include_descendants, tag_filter=tag_filter)
            if options is None:
                self.frame.report('Outline export cancelled. No file was written.')
                return
            self._require_current(scope)
            descendants, tag, format = options
            self._export(scope, kind='outline', format=format,
                         include_descendants=descendants, tag_filter=tag)
        except Exception as exc:
            self.frame.report(f'Outline export unavailable: {exc}')

    def _export(self, scope, *, kind, format, include_descendants, tag_filter):
        self._require_current(scope)
        destination = self.dialogs.save_path(self.frame, kind=kind, format=format)
        if destination is None:
            self.frame.report('Export cancelled. No file was written.')
            return
        self._require_current(scope)
        exporter = export_outline if kind == 'outline' else export_matrix

        def save():
            self._require_current(scope)
            text = exporter(scope.library, scope.workspace, scope.topic, format=format,
                include_descendants=include_descendants, tag_filter=tag_filter,
                cancelled=self.frame.cancel_event.is_set)
            if self.frame.cancel_event.is_set():
                raise InterruptedError('Export cancelled. No file was written.')
            self._require_current(scope)
            write_export(destination, text, private_root=scope.library.root,
                         cancelled=self.frame.cancel_event.is_set)
            return destination

        def done(path):
            if self._current(scope):
                self.frame.report(f'Export saved: {path}')

        self.frame.run_job('Exporting research outline' if kind == 'outline' else 'Exporting source comparison', save, done)


def edit_catalog_edition(frame, workspace, edition_id):
    """Open the existing native metadata form for a quality-review selection."""
    actions = AcademicActions(frame)
    try:
        scope = actions._scope()
        if scope.workspace != workspace:
            raise ValueError('The selected library changed. Run the catalog check again.')

        def done(current):
            if not actions._current(scope):
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


def install_academic_actions(frame):
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    from .bibliography_import import open_bibliography_import
    from .catalog_quality import open_catalog_quality

    actions = AcademicActions(frame)
    academic_index = frame.menu_bar.FindMenu('Academic')
    menu = (frame.menu_bar.GetMenu(academic_index)
            if academic_index >= 0 else wx.Menu())
    commands = (
        ('academic.compare_sources', 'Compare sources in active topic\tCtrl+Alt+M', actions.compare_sources),
        ('academic.export_outline', 'Export research outline with subtopics', actions.outline),
        ('academic.import_ris', 'Import bibliographic metadata from RIS', lambda event: open_bibliography_import(frame)),
        ('academic.catalog_quality', 'Check catalog quality', lambda event: open_catalog_quality(frame,
            edit_edition=lambda workspace, edition: edit_catalog_edition(frame, workspace, edition))),
    )
    installed = []
    for identity, title, handler in commands:
        item = menu.Append(wx.ID_ANY, title)
        frame.Bind(wx.EVT_MENU, handler, item)
        caption, _, shortcut = title.partition('\t')
        installed.append((CommandSpec(identity, caption, 'main', shortcut), item))
    # Preserve existing menus and their handlers, including post-merge fixes.
    if academic_index < 0:
        help_index = frame.menu_bar.FindMenu('Help')
        frame.menu_bar.Insert(help_index if help_index >= 0 else frame.menu_bar.GetMenuCount(), menu, '&Academic')
    for spec, item in installed:
        register_menu_command(frame, spec, item)
    frame.academic_actions = actions
    return actions
