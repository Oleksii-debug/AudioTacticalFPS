"""Accessible local bibliography exchange and verified research-outline actions.

Reuse the catalog's optimistic metadata editor and atomic export publisher.
External reference data is a reviewed suggestion, never a source identity.
"""
from pathlib import Path
import os
import stat

import wx

from ..catalog.reference_exchange import preview_references
from ..research.export_file import write_export
from .export_destination import confirm_export_destination
from .catalog_metadata_dialog import MetadataDialog, metadata_values
from .export_destination import confirm_export_destination


MAX_REFERENCE_BYTES = 4 * 1024 * 1024


def read_reference_file(path, *, format, cancelled=lambda: False):
    """Read only the selected bounded regular file; never follow attachment URLs."""
    if cancelled():
        raise InterruptedError('Reference preview cancelled.')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('Choose a regular RIS or CSL-JSON file.')
        if info.st_size > MAX_REFERENCE_BYTES:
            raise ValueError('Reference files must be no larger than 4 MiB.')
        payload = stream.read(MAX_REFERENCE_BYTES + 1)
    if len(payload) > MAX_REFERENCE_BYTES:
        raise ValueError('Reference files must be no larger than 4 MiB.')
    if cancelled():
        raise InterruptedError('Reference preview cancelled.')
    return preview_references(payload, format=format)


def reference_candidates(hint):
    """One candidate per field, except RIS fields that explicitly have alternatives."""
    return {
        'title': hint.titles,
        'authors': ('\n'.join(hint.authors),) if hint.authors else (),
        'language': hint.languages,
        'edition': hint.editions,
        'volume': hint.volumes,
    }


def reference_summary(current, hint):
    lines = [hint.origin, 'Suggestions from an external reference file; not verified against the book.',
             'Use a reference button for each field you want to replace, or edit the fields below.',
             'Save metadata applies the displayed fields. Cancel leaves the book unchanged.', '',
             'Title and authors apply to every edition of this work. Saved quotations keep their original metadata.', '',
             'Current catalog metadata:', f'Title: {current.title}',
             'Authors: ' + ('; '.join(current.authors) or 'Unknown'),
             f'Language: {current.language or "Unknown"}',
             f'Edition: {current.edition or "Unknown"}',
             f'Volume: {current.volume or "Unknown"}', '', 'Reference suggestions:']
    for key, values in reference_candidates(hint).items():
        lines.append(key.capitalize() + ': ' + (' | '.join(values) if values else 'Not supplied'))
    if hint.unsupported_fields:
        lines.extend(('', 'Fields not stored in this catalog: ' + ', '.join(hint.unsupported_fields)))
    if hint.warnings:
        lines.extend(('', 'Review notes:', *hint.warnings))
    return '\n'.join(lines)


class ReferenceMetadataDialog(MetadataDialog):
    """Keep current values until a named reference button explicitly applies a field."""
    def __init__(self, parent, current, hint, *, save, executor):
        self.reference_controls = []
        self.current = current
        wx.Dialog.__init__(self, parent, title='Review reference metadata', size=(760, 700))
        self.save, self.executor, self.pending, self.fields = save, executor, False, {}
        outer = wx.BoxSizer(wx.VERTICAL)
        form = wx.ScrolledWindow(self, style=wx.VSCROLL | wx.TAB_TRAVERSAL)
        form.SetScrollRate(0, 12)
        layout = wx.BoxSizer(wx.VERTICAL)
        preview = wx.TextCtrl(form, value=reference_summary(current, hint),
                              style=wx.TE_READONLY | wx.TE_MULTILINE)
        preview.SetName('Reference review: current values, suggestions and unsupported fields')
        preview.SetMinSize((-1, 150))
        layout.Add(preview, 0, wx.EXPAND | wx.ALL, 8)
        self.reference_controls.append(preview)
        candidates = reference_candidates(hint)
        for key, label, value, multiline in (
            ('title', 'Title', current.title or '', False),
            ('authors', 'Authors, one per line, in source order', '\n'.join(current.authors), True),
            ('language', 'Language; leave blank if unknown', current.language or '', False),
            ('edition', 'Edition; leave blank if unknown', current.edition or '', False),
            ('volume', 'Volume; leave blank if unknown', current.volume or '', False),
        ):
            layout.Add(wx.StaticText(form, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
            target = wx.TextCtrl(form, value=value, style=wx.TE_MULTILINE if multiline else 0)
            target.SetName(label)
            if multiline:
                target.SetMinSize((-1, 85))
            self.fields[key] = target
            values = candidates[key]
            if values:
                row = wx.BoxSizer(wx.HORIZONTAL)
                choice = wx.Choice(form, choices=[
                    f'{i + 1}: ' + ' '.join(item.split())[:160] for i, item in enumerate(values)])
                choice.SetName(f'Reference {key} candidates; full values are in Reference review')
                choice.SetSelection(0)
                button = wx.Button(form, label=f'Use reference {key}')
                button.SetName(f'Use reference {key}')

                def apply(event, *, candidate=choice, candidates=values, field=target):
                    index = candidate.GetSelection()
                    if self.pending or not 0 <= index < len(candidates):
                        return
                    field.SetValue(candidates[index])
                    field.SetFocus()

                button.Bind(wx.EVT_BUTTON, apply)
                row.Add(choice, 1, wx.RIGHT, 8)
                row.Add(button, 0)
                layout.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
                self.reference_controls.extend((choice, button))
            layout.Add(target, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        form.SetSizer(layout)
        form.FitInside()
        outer.Add(form, 1, wx.EXPAND)
        self.status = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.status.SetName('Metadata save status')
        self.status.SetMinSize((-1, 60))
        outer.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.save_button = wx.Button(self, wx.ID_SAVE, '&Save metadata')
        self.save_button.SetDefault()
        self.cancel_button = wx.Button(self, wx.ID_CANCEL, '&Cancel')
        self.save_button.Bind(wx.EVT_BUTTON, self.on_save)
        buttons.Add(self.save_button, 0, wx.ALL, 8)
        buttons.Add(self.cancel_button, 0, wx.ALL, 8)
        outer.Add(buttons, 0, wx.ALIGN_RIGHT)
        self.SetSizer(outer)
        self.SetMinSize((560, 450))
        self.SetEscapeId(wx.ID_CANCEL)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        preview.SetInsertionPoint(0)
        preview.SetFocus()

    def _enabled(self, enabled):
        super()._enabled(enabled)
        for control in self.reference_controls:
            control.Enable(enabled)

    def on_save(self, event=None):
        if self.pending:
            return
        before = dict(title=self.current.title, authors=self.current.authors,
                      language=self.current.language, edition=self.current.edition,
                      volume=self.current.volume)
        try:
            after = metadata_values(**{key: control.GetValue() for key, control in self.fields.items()})
        except (ValueError, UnicodeError):
            return super().on_save(event)
        if after == before:
            self.status.SetValue('No metadata changes selected. Use a reference button, edit a field, or Cancel.')
            self.status.SetFocus()
            return
        super().on_save(event)


def _idle(frame):
    if getattr(frame, '_closing', False):
        raise ValueError('The library is closing.')
    if frame.working or frame.busy:
        raise ValueError('Wait for the current operation or cancel it first.')


def _current_scope(frame, library, workspace):
    return (not getattr(frame, '_closing', False) and frame.library is library
            and frame.active is not None and frame.active[0] == workspace)


def _same_book(frame, library, workspace, panel, selection):
    return (_current_scope(frame, library, workspace) and frame.book is not None
            and frame.book.GetPage(1) is panel and panel.ready
            and panel._selection() == selection)


def import_reference_metadata(frame):
    """Preview a reference file, choose one record, then use the existing guarded save."""
    try:
        _idle(frame)
        workspace = frame._workspace()
        library = frame.library
        if frame.book is None:
            raise ValueError('Select a book in Books and Collections first.')
        panel = frame.book.GetPage(1)
        if panel.workspace != workspace or not panel.ready:
            raise ValueError('Refresh Books and Collections before importing metadata.')
        selection = panel._selection()
        identity = selection[2]
        if identity is None:
            raise ValueError('Select a book in Books and Collections first.')
        current = library.edition_metadata(workspace, identity)
        dialog = wx.FileDialog(frame, 'Choose reference metadata for the selected book',
            wildcard='RIS (*.ris)|*.ris|CSL-JSON (*.json)|*.json', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                panel.books.SetFocus()
                return
            path = Path(dialog.GetPath())
            index = dialog.GetFilterIndex()
            if not 0 <= index < 2:
                raise ValueError('Choose RIS or CSL-JSON.')
            format = {'.ris': 'ris', '.json': 'csl-json'}.get(path.suffix.lower(), ('ris', 'csl-json')[index])
        finally:
            dialog.Destroy()
        if not _same_book(frame, library, workspace, panel, selection):
            raise ValueError('Book selection changed. Open the reference command again.')

        def done(hints):
            if (not _same_book(frame, library, workspace, panel, selection)
                    or frame.cancel_event.is_set()):
                frame.report('Reference preview discarded because the book selection changed or the operation was cancelled.')
                return
            if not hints:
                frame.report('No reference records found. The book was not changed.')
                return
            chooser = wx.SingleChoiceDialog(frame,
                'Choose a reference to review for the selected book. Nothing is saved yet.',
                'Reference records', [f'Record {h.record_number}: ' +
                    (' '.join(h.titles[0].split())[:140] if h.titles else 'Title not supplied') for h in hints])
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    panel.books.SetFocus()
                    return
                selected = chooser.GetSelection()
                if not 0 <= selected < len(hints):
                    raise ValueError('Choose a reference record.')
                hint = hints[selected]
            finally:
                chooser.Destroy()
            if not _same_book(frame, library, workspace, panel, selection):
                raise ValueError('Book selection changed. Open the reference command again.')

            def save(**values):
                return library.update_metadata(workspace, identity, expected_metadata=current, **values)

            review = ReferenceMetadataDialog(frame, current, hint, save=save, executor=frame.executor)
            try:
                if review.ShowModal() == wx.ID_OK:
                    if panel.refresh(selection=selection):
                        panel.status.SetValue('Reviewed reference metadata saved. Search again to use the updated metadata.')
                    else:
                        panel.status.SetValue('Reviewed reference metadata saved. Refresh Books and Collections to view it.')
                    panel.status.SetFocus()
                else:
                    panel.books.SetFocus()
            finally:
                review.Destroy()

        frame.run_job('Reading reference metadata',
            lambda: read_reference_file(path, format=format, cancelled=frame.cancel_event.is_set), done)
    except Exception as exc:
        frame.report(f'Reference metadata was not imported: {exc}')


def export_bibliography(frame):
    """Export a selected catalog scope without requiring quotations or topic membership."""
    try:
        _idle(frame)
        workspace = frame._workspace()
        library = frame.library
        collections = library.collections(workspace)
        choices = ['All books in this library'] + [
            f'Collection {i + 1}: {name}' for i, (_, name) in enumerate(collections)]
        chooser = wx.SingleChoiceDialog(frame, 'Choose which books to include.', 'Export bibliography', choices)
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            index = chooser.GetSelection()
            if not 0 <= index < len(choices):
                raise ValueError('Choose the library or a collection.')
            collection = None if index == 0 else collections[index - 1][0]
        finally:
            chooser.Destroy()
        dialog = wx.FileDialog(frame, 'Save bibliography', defaultFile='bibliography.json',
            wildcard='CSL-JSON (*.json)|*.json|RIS (*.ris)|*.ris|BibTeX (*.bib)|*.bib',
            style=wx.FD_SAVE)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = Path(dialog.GetPath())
            index = dialog.GetFilterIndex()
            if not 0 <= index < 3:
                raise ValueError('Choose a bibliography format.')
            format = ('csl-json', 'ris', 'bibtex')[index]
        finally:
            dialog.Destroy()
        approval = confirm_export_destination(frame, path, private_root=library.root,
                                               title='Replace bibliography', wx_module=wx)
        if approval is None:
            return
        if not _current_scope(frame, library, workspace):
            raise ValueError('Library selection changed. Open the bibliography command again.')

        def operation():
            text = library.export_bibliography(workspace, collection=collection, format=format,
                                               cancelled=frame.cancel_event.is_set)
            if frame.cancel_event.is_set():
                raise InterruptedError('Export cancelled; the destination was not changed.')
            write_export(path, text, private_root=library.root, cancelled=frame.cancel_event.is_set,
                         approval=approval)

        def done(result):
            if _current_scope(frame, library, workspace):
                frame.report('Bibliography exported with known catalog metadata and separate editions.')

        frame.run_job('Exporting bibliography', operation, done)
    except Exception as exc:
        frame.report(f'Bibliography export: {exc}')


def export_research_outline(frame):
    try:
        _idle(frame)
        workspace, topic = frame._topic()
        library = frame.library
        dialog = wx.FileDialog(frame, 'Export active topic and its nonarchived subtopics',
            defaultFile='research-outline.md', wildcard='Markdown (*.md)|*.md',
            style=wx.FD_SAVE)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = Path(dialog.GetPath())
        finally:
            dialog.Destroy()
        approval = confirm_export_destination(frame, path, private_root=library.root,
                                               title='Replace research outline', wx_module=wx)
        if approval is None:
            return
        if not _current_scope(frame, library, workspace) or frame.active != (workspace, topic):
            raise ValueError('Topic selection changed. Open the outline command again.')

        def operation():
            text = library.export_outline(workspace, topic, cancelled=frame.cancel_event.is_set)
            if frame.cancel_event.is_set():
                raise InterruptedError('Export cancelled; the destination was not changed.')
            write_export(path, text, private_root=library.root, cancelled=frame.cancel_event.is_set,
                         approval=approval)

        def done(result):
            if frame.library is library and frame.active == (workspace, topic):
                frame.report('Research outline exported with active subtopics, verified quotations and separate research notes.')

        frame.run_job('Exporting verified research outline', operation, done)
    except Exception as exc:
        frame.report(f'Research outline export: {exc}')


def install_academic_actions(frame):
    menu = wx.Menu()
    for title, action in (
        ('Review reference metadata for selected book', import_reference_metadata),
        ('Export library or collection bibliography', export_bibliography),
    ):
        item = menu.Append(wx.ID_ANY, title)
        frame.Bind(wx.EVT_MENU, lambda event, run=action: run(frame), item)
    frame.menu_bar.Insert(2, menu, '&Bibliography')
    research = frame.menu_bar.GetMenu(1)
    item = research.Append(wx.ID_ANY, 'Export topic and subtopics\tCtrl+Shift+E')
    frame.Bind(wx.EVT_MENU, lambda event: export_research_outline(frame), item)
