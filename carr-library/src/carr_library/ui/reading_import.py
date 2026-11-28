"""Explicit native import of retained canonical reading histories."""
from dataclasses import dataclass
from pathlib import Path

from ..reader.legacy_reading import (
    LegacyReadingStore, authorize_snapshot, decode_snapshot, history_description,
    project_from_snapshot, read_snapshot, snapshot_import_files,
)
from ..reader.narration import NarrationStore
from ..ingest.writer_guard import exclusive_writer
from ..research.topic_tree import TopicTree
from .narration_editor import open_narration_editor
from .research_actions import NativeDialogs


class ReadingImportDialogs(NativeDialogs):
    def path(self, parent):
        import wx
        picker = wx.FileDialog(parent, 'Import an earlier reading history or reader session',
            wildcard='Reading history or reader session JSON (*.json)|*.json|All files (*.*)|*.*',
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            return Path(picker.GetPath()) if picker.ShowModal() == wx.ID_OK else None
        finally:
            picker.Destroy()

    def preview(self, parent, snapshot):
        import wx
        dialog = wx.Dialog(parent, title='Review historical reading revision', size=(900, 720),
                           style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            label = wx.StaticText(dialog, label=(
                'The complete original history will be retained unchanged. '
                'Create a new editable copy only after reviewing this revision. '
                'Saved session settings are shown as history; playback and current voice selection stay explicit.'))
            label.Wrap(850)
            layout.Add(label, 0, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.StaticText(dialog, label='New reading-copy title (up to 200 characters)'),
                       0, wx.LEFT | wx.RIGHT, 8)
            title = wx.TextCtrl(dialog, value=snapshot.title[:200])
            title.SetName('New reading-copy title')
            layout.Add(title, 0, wx.EXPAND | wx.ALL, 8)
            history = wx.TextCtrl(dialog, value=history_description(snapshot),
                                  style=wx.TE_READONLY | wx.TE_MULTILINE)
            history.SetName('Retained source positions and historical revision')
            history.SetMinSize((-1, 150))
            layout.Add(history, 1, wx.EXPAND | wx.ALL, 8)
            text = wx.TextCtrl(dialog, value=snapshot.text,
                               style=wx.TE_READONLY | wx.TE_MULTILINE)
            text.SetName('Historical reading text preview')
            layout.Add(text, 2, wx.EXPAND | wx.ALL, 8)
            buttons = wx.StdDialogButtonSizer()
            create = wx.Button(dialog, wx.ID_OK, 'Create new editable reading copy')
            create.SetName('Create new editable reading copy')
            cancel = wx.Button(dialog, wx.ID_CANCEL, 'Cancel')
            buttons.AddButton(create)
            buttons.AddButton(cancel)
            buttons.Realize()
            layout.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
            dialog.SetSizer(layout)
            dialog.SetEscapeId(wx.ID_CANCEL)
            text.SetInsertionPoint(0)
            text.SetFocus()
            return title.GetValue() if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()


@dataclass(frozen=True)
class _Scope:
    library: object
    workspace: str
    topic: str | None

    @property
    def ids(self):
        return self.workspace, self.topic


class ReadingImportActions:
    def __init__(self, frame, *, dialogs=None, editor=open_narration_editor):
        self.frame = frame
        self.dialogs = dialogs or ReadingImportDialogs()
        self.editor = editor

    def _scope(self):
        if (self.frame.working or getattr(self.frame, 'busy', False)
                or getattr(self.frame, '_closing', False)):
            raise ValueError('Wait for the current operation or cancel it first.')
        if self.frame.active is None:
            raise ValueError('Open a library before importing a reading history.')
        scope = _Scope(self.frame.library, *self.frame.active)
        self._guard(scope, cancelled=False)
        return scope

    def _guard(self, scope, *, cancelled=True):
        if (self.frame.library is not scope.library or self.frame.active != scope.ids
                or getattr(self.frame, '_closing', False)):
            raise ValueError('The library or topic changed. Reopen the reading import.')
        if cancelled and self.frame.cancel_event.is_set():
            raise InterruptedError('Reading-copy import cancelled.')
        with scope.library.read(scope.workspace) as db:
            if scope.topic is not None:
                TopicTree(db, scope.workspace)._visible(scope.topic)

    def _job(self, scope, label, work, done):
        self._guard(scope, cancelled=False)
        def operation():
            self._guard(scope)
            result = work()
            self._guard(scope)
            return result
        def completed(result):
            try:
                self._guard(scope)
                done(result)
            except Exception as exc:
                self.frame.report(str(exc))
        self.frame.run_job(label, operation, completed)

    def import_file(self, event=None):
        try:
            scope = self._scope()
            path = self.dialogs.path(self.frame)
            if path is None:
                return
            self._guard(scope, cancelled=False)
            self._job(scope, 'Reading earlier reading-copy history',
                lambda: read_snapshot(path, cancelled=self.frame.cancel_event.is_set),
                lambda snapshot: self._choose_revision(scope, snapshot, str(Path(path).absolute())))
        except Exception as exc:
            self.frame.report(str(exc))

    def open_retained(self, event=None):
        try:
            scope = self._scope()
            def load():
                with exclusive_writer(scope.library.root):
                    files = snapshot_import_files(scope.library.root,
                                                   cancelled=self.frame.cancel_event.is_set)
                rows = []
                for raw in files.values():
                    self._guard(scope)
                    snapshot = decode_snapshot(raw, cancelled=self.frame.cancel_event.is_set)
                    if snapshot.source_anchor is None or snapshot.source_anchor.workspace == scope.workspace:
                        rows.append(snapshot)
                return tuple(sorted(rows, key=lambda row: (row.title.casefold(), row.archive_sha256)))
            def choose(rows):
                if not rows:
                    self.frame.report('There are no retained reading histories in this library.')
                    return
                labels = [row.title + ' — ' + row.archive_sha256[:12] for row in rows]
                selected = self.dialogs.choose(self.frame, 'Open retained reading history',
                                               'Choose the original compact history.', labels)
                if selected is None:
                    return
                if type(selected) is not int or not 0 <= selected < len(rows):
                    raise ValueError('Choose one retained reading history.')
                self._guard(scope)
                self._choose_revision(scope, rows[selected], None)
            self._job(scope, 'Loading retained reading histories', load, choose)
        except Exception as exc:
            self.frame.report(str(exc))

    def _choose_revision(self, scope, snapshot, original_path):
        self._guard(scope)
        if snapshot.source_anchor is not None and snapshot.source_anchor.workspace != scope.workspace:
            raise ValueError('The imported reading copy belongs to another library.')
        selected = self.dialogs.choose(self.frame, 'Choose historical reading revision',
            f'Saved history revision: {snapshot.saved_revision}. All {len(snapshot.edits)} edits, '
            'including redo history, remain available.', snapshot.revision_labels)
        if selected is None:
            return
        if type(selected) is not int or not 0 <= selected <= len(snapshot.edits):
            raise ValueError('Choose an existing historical reading revision.')
        self._guard(scope)
        def prepare():
            chosen = snapshot.at(selected, cancelled=self.frame.cancel_event.is_set)
            authorize_snapshot(scope.library, scope.workspace, chosen,
                               cancelled=self.frame.cancel_event.is_set)
            return chosen
        self._job(scope, 'Checking historical source positions', prepare,
                  lambda chosen: self._preview(scope, chosen, original_path))

    def _preview(self, scope, snapshot, original_path):
        self._guard(scope)
        title = self.dialogs.preview(self.frame, snapshot)
        if title is None:
            return
        self._guard(scope)
        # Validate the proposed title/project before any retained state is written.
        project = project_from_snapshot(snapshot, title=title, original_path=original_path)
        def save():
            authorize_snapshot(scope.library, scope.workspace, snapshot,
                               cancelled=self.frame.cancel_event.is_set)
            self._guard(scope)
            LegacyReadingStore(scope.library.root).retain(
                snapshot.raw, cancelled=self.frame.cancel_event.is_set)
            self._guard(scope)
            store = NarrationStore(scope.library.root)
            digest = store.save(project)
            return store, project, digest
        def opened(result):
            store, saved, digest = result
            self._guard(scope)
            self.frame.report('Historical reading copy saved; complete original history retained.')
            self.editor(self.frame, store, saved, expected_digest=digest,
                        guard=lambda: self._guard(scope, cancelled=False), dialogs=self.dialogs)
        self._job(scope, 'Saving imported reading copy', save, opened)


def install_reading_import_actions(frame):
    """Stable semantic commands, with no new default shortcut collision."""
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    existing = getattr(frame, 'reading_import_actions', None)
    if existing is not None:
        return existing
    menu = next((frame.menu_bar.GetMenu(index) for index in range(frame.menu_bar.GetMenuCount())
                 if frame.menu_bar.GetMenuLabel(index).replace('&', '').casefold() == 'tools'), None)
    if menu is None:
        raise ValueError('Install the shared Tools menu before reading-history actions.')
    actions = ReadingImportActions(frame)
    for command, label, handler in (
        ('reading.import_legacy_copy', 'Import earlier reading history or reader session...', actions.import_file),
        ('reading.open_legacy_history', 'Open retained reading-copy history...', actions.open_retained),
    ):
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, handler, item)
        register_menu_command(frame, CommandSpec(command, label.rstrip('.'), 'main'), item)
    frame.reading_import_actions = actions
    return actions
