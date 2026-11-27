"""Keyboard-first academic bibliography and reading workspace.

The controller uses fresh scoped application transactions. Native wx classes
are loaded lazily so service workflows can also be checked without a desktop.
Bibliographic notes are separate from source quotations and their approvals.
"""
from pathlib import Path
import threading

from ..research.academic_library import AcademicLibrary
from ..research.export_file import capture_export_destination, write_export


PAGE_SIZE = 200
MAX_IMPORT_BYTES = 8 * 1024 * 1024
ITEM_TYPES = (('book', 'Book'), ('article', 'Journal article'), ('thesis', 'Thesis'),
              ('report', 'Report'), ('webpage', 'Web page'), ('other', 'Other'))
READING_STATUSES = (('unread', 'Unread'), ('reading', 'Reading'), ('read', 'Read'),
                    ('on_hold', 'On hold'))
PRIORITIES = (('low', 'Low'), ('normal', 'Normal'), ('high', 'High'))
SORTS = (('title', 'Title'), ('year', 'Year, newest first'),
         ('priority', 'Priority, highest first'), ('updated', 'Recently updated'))
FORMATS = (('text', 'Plain text bibliography', 'txt'), ('ris', 'RIS', 'ris'),
           ('bibtex', 'BibTeX', 'bib'), ('csl-json', 'CSL JSON', 'json'),
           ('csv', 'CSV spreadsheet', 'csv'))


class AcademicController:
    """Bind every action to the original library object and one workspace."""

    def __init__(self, library, workspace):
        self.library, self.workspace = library, workspace
        # Opening the window itself rechecks workspace access and schema.
        with library.read(workspace) as db:
            AcademicLibrary(db, workspace)

    def list(self, **filters):
        with self.library.read(self.workspace) as db:
            return AcademicLibrary(db, self.workspace).list(**filters)

    def get(self, identity):
        with self.library.read(self.workspace) as db:
            return AcademicLibrary(db, self.workspace).get(identity)

    def create(self, values):
        with self.library.write(self.workspace) as db:
            return AcademicLibrary(db, self.workspace).create(**values)

    def update(self, reference, values):
        if reference.workspace != self.workspace:
            raise ValueError('This reference belongs to another library.')
        with self.library.write(self.workspace) as db:
            return AcademicLibrary(db, self.workspace).update(reference.id, expected=reference, **values)

    def archive(self, reference, *, archived=True):
        if reference.workspace != self.workspace:
            raise ValueError('This reference belongs to another library.')
        with self.library.write(self.workspace) as db:
            return AcademicLibrary(db, self.workspace).archive(reference.id, archived=archived, expected=reference)

    def statistics(self):
        with self.library.read(self.workspace) as db:
            return AcademicLibrary(db, self.workspace).statistics()

    def duplicate_report(self):
        with self.library.read(self.workspace) as db:
            service = AcademicLibrary(db, self.workspace)
            groups = service.duplicates()
            sections = ['Suggestions require your comparison. Different editions may be intentional. '
                        'No references are merged or deleted automatically.']
            for number, group in enumerate(groups, 1):
                members = []
                for identity in group.reference_ids:
                    row = service.get(identity)
                    members.append(f'{row.title}\nAuthors: {"; ".join(row.authors) or "Unknown"}\n'
                                   f'Year: {row.year or "Unknown"}\n'
                                   f'DOI: {row.doi or "Not entered"}\nISBN: {row.isbn or "Not entered"}')
                sections.append(f'Group {number}. Reason: {group.reason}\n\n' + '\n\n'.join(members))
            if not groups:
                sections.append('No possible duplicates found in this library.')
            return '\n\n'.join(sections)

    def preview(self, data, format):
        with self.library.read(self.workspace) as db:
            return AcademicLibrary(db, self.workspace).import_preview(data, format=format)

    def import_selected(self, preview, indices, *, skip_duplicates=False):
        with self.library.write(self.workspace) as db:
            return AcademicLibrary(db, self.workspace).import_selected(
                preview, indices, skip_duplicates=skip_duplicates)

    def export(self, *, identity=None, format='text', include_notes=False):
        with self.library.read(self.workspace) as db:
            return AcademicLibrary(db, self.workspace).export(
                None if identity is None else (identity,), format=format, include_notes=include_notes)

    def export_to(self, destination, *, identity=None, format='text', include_notes=False,
                  cancelled=lambda: False, approval=None):
        if not callable(cancelled):
            raise ValueError('An export cancellation callback is required.')
        if cancelled():
            raise TimeoutError('Export cancelled; the destination was not changed.')
        content = self.export(identity=identity, format=format, include_notes=include_notes)
        if cancelled():
            raise TimeoutError('Export cancelled; the destination was not changed.')
        # Reauthorize after rendering, immediately before the guarded writer.
        with self.library.read(self.workspace):
            pass
        write_export(destination, content, private_root=self.library.root,
                     cancelled=cancelled, approval=approval)
        return content

    def books(self):
        return self.library.browse(self.workspace)

    def from_book(self, edition_id):
        row = self.library.edition_metadata(self.workspace, edition_id)
        return {'title': row.title or '', 'authors': row.authors, 'item_type': 'book',
                'edition_id': row.edition_id}

    def prepare_document(self, reference, *, cancelled=lambda: False):
        if reference.workspace != self.workspace or self.get(reference.id) != reference:
            raise ValueError('The selected reference changed. Refresh it before opening its document.')
        if reference.edition_id is None:
            raise ValueError('This reference has no linked local book. Edit its Bibliography tab to link one.')
        return self.library.prepare_edition_file(self.workspace, reference.edition_id, cancelled=cancelled)


def selected_row(rows, index):
    if type(index) is not int or not 0 <= index < len(rows):
        raise ValueError('Select a reference in the list first.')
    return rows[index]


def reference_labels(rows, *, offset=0):
    """Numbers distinguish equal titles without exposing internal identifiers."""
    status_names = dict(READING_STATUSES)
    result = []
    for number, row in enumerate(rows, offset + 1):
        authors = '; '.join(row.authors) or 'Unknown author'
        year = row.year or 'Unknown year'
        state = 'Archived' if row.archived else status_names[row.reading_status]
        result.append(f'{number}. {row.title} — {authors}; {year}; {state}')
    return result


def book_link_labels(rows):
    """Expose edition metadata when choosing between similarly named books."""
    return [f'{number}. {row.title or "Untitled"} — {"; ".join(row.authors) or "Unknown author"}; '
            f'Edition: {row.edition or "unknown"}; Volume: {row.volume or "unknown"}; '
            f'Language: {row.language or "unknown"}'
            for number, row in enumerate(rows, 1)]


def reference_details(row):
    values = (
        ('Title', row.title), ('Authors', '\n'.join(row.authors) or 'Unknown'),
        ('Type', dict(ITEM_TYPES)[row.item_type]),
        ('Year', row.year or 'Unknown'),
        ('Journal or container', row.container_title), ('Publisher', row.publisher),
        ('Volume', row.volume), ('Issue', row.issue), ('Pages', row.pages),
        ('DOI', row.doi), ('ISBN', row.isbn), ('URL', row.url), ('Abstract', row.abstract),
        ('Reading status', dict(READING_STATUSES)[row.reading_status]),
        ('Priority', dict(PRIORITIES)[row.priority]),
        ('Rating', f'{row.rating} of 5' if row.rating else 'Not rated'),
        ('Reading progress', f'{row.progress}%'), ('Tags', '\n'.join(row.tags)),
        ('Personal research notes', row.notes),
        ('Local book', 'Linked to an imported book' if row.edition_id else 'No linked local book'),
        ('Archive state', 'Archived' if row.archived else 'Active'),
    )
    return '\n\n'.join(f'{label}:\n{value or "Not entered"}' for label, value in values)


def statistics_text(values):
    names = (('active', 'Active references'), ('archived', 'Archived references'),
             ('unread', 'Unread'), ('reading', 'Currently reading'), ('read', 'Read'),
             ('on_hold', 'On hold'), ('high_priority', 'High priority'),
             ('linked', 'Linked to local books'), ('unlinked', 'Without a linked local book'),
             ('missing_identifier', 'Without DOI or ISBN'), ('missing_author', 'Without an author'),
             ('missing_year', 'Without a publication year'))
    return '\n'.join(f'{label}: {values[key]}' for key, label in names)


def initial_fields(reference=None, initial=None):
    values = dict(title='', authors=(), item_type='book', year='', container_title='',
                  publisher='', volume='', issue='', pages='', doi='', isbn='', url='',
                  abstract='', notes='', tags=(), reading_status='unread', priority='normal',
                  rating=0, progress=0, edition_id=None)
    if reference is not None:
        values.update((key, getattr(reference, key)) for key in values)
    if initial:
        values.update(initial)
    return values


def form_values(raw):
    """Parse user input without coercing a fraction, missing choice or bad range."""
    values = dict(raw)
    for field, label, minimum, maximum in (('progress', 'Reading progress', 0, 100),
                                          ('rating', 'Rating', 0, 5)):
        text = str(values.get(field, '')).strip()
        if not text.isdecimal() or not minimum <= int(text) <= maximum:
            raise ValueError(f'{label} must be a whole number from {minimum} to {maximum}.')
        else:
            values[field] = int(text)
    if not isinstance(values['year'], str):
        raise ValueError('Enter the publication year as printed, or leave it empty if unknown.')
    for field in ('authors', 'tags'):
        text = values[field]
        if not isinstance(text, str):
            raise ValueError(f'Enter {field}, one per line.')
        names = tuple(line.strip() for line in text.splitlines() if line.strip())
        values[field] = tuple(dict.fromkeys(names)) if field == 'tags' else names
    if not isinstance(values['title'], str) or not values['title'].strip():
        raise ValueError('Enter a title. Your other edits remain in this form.')
    for field, options in (('item_type', ITEM_TYPES), ('reading_status', READING_STATUSES),
                           ('priority', PRIORITIES)):
        if values[field] not in dict(options):
            raise ValueError(f'Choose a valid {field.replace("_", " ")}.')
    return values


def _native_types():
    """Return real native classes; importing the controller does not import wx."""
    import wx

    def report(control, text, *, focus=True):
        control.SetValue(text)
        control.SetInsertionPoint(0)
        if focus:
            control.SetFocus()

    def label_control(parent, layout, label, control, proportion=0):
        layout.Add(wx.StaticText(parent, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        control.SetName(label)
        layout.Add(control, proportion, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        return control

    def readable(parent, title, text):
        dialog = wx.Dialog(parent, title=title, size=(760, 640),
                           style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        try:
            layout = wx.BoxSizer(wx.VERTICAL)
            reader = wx.TextCtrl(dialog, value=text, style=wx.TE_READONLY | wx.TE_MULTILINE)
            reader.SetName(title)
            layout.Add(reader, 1, wx.EXPAND | wx.ALL, 8)
            layout.Add(wx.Button(dialog, wx.ID_CANCEL, '&Close'), 0, wx.ALL | wx.ALIGN_RIGHT, 8)
            dialog.SetSizer(layout)
            dialog.SetEscapeId(wx.ID_CANCEL)
            reader.SetInsertionPoint(0)
            reader.SetFocus()
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    class ReferenceEditor(wx.Dialog):
        def __init__(self, parent, *, save, books=(), reference=None, initial=None,
                     executor=None, confirm_discard=None):
            super().__init__(parent, title='Edit academic reference' if reference else 'New academic reference',
                             size=(780, 740), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self._window_alive = True
            self.save, self.executor = save, executor
            self.confirm_discard = confirm_discard
            self.pending, self.saved = False, None
            self.controls, self.choice_values = {}, {}
            values = initial_fields(reference, initial)
            layout = wx.BoxSizer(wx.VERTICAL)
            explanation = wx.StaticText(self, label='Bibliography and personal reading notes are separate from source quotations. '
                                        'Use Ctrl+Tab to move between Bibliography and Reading and Notes.')
            explanation.Wrap(720)
            layout.Add(explanation, 0, wx.EXPAND | wx.ALL, 8)
            self.book = wx.Notebook(self)
            self.book.SetName('Reference editing sections')
            bibliography = wx.ScrolledWindow(self.book, style=wx.VSCROLL)
            reading = wx.ScrolledWindow(self.book, style=wx.VSCROLL)
            for page, title in ((bibliography, 'Bibliography'), (reading, 'Reading and Notes')):
                page.SetScrollRate(0, 16)
                page.SetSizer(wx.BoxSizer(wx.VERTICAL))
                self.book.AddPage(page, title)

            def text(page, key, caption, *, multiline=False):
                value = values[key]
                if key in ('authors', 'tags'):
                    value = '\n'.join(value)
                control = wx.TextCtrl(page, value='' if value is None else str(value),
                                      style=wx.TE_MULTILINE if multiline else 0)
                if multiline:
                    control.SetMinSize((-1, 100 if key != 'notes' else 220))
                self.controls[key] = label_control(page, page.GetSizer(), caption, control)

            def choice(page, key, caption, options):
                self.choice_values[key] = [code for code, _ in options]
                control = wx.Choice(page, choices=[title for _, title in options])
                current = values[key]
                control.SetSelection(self.choice_values[key].index(current))
                self.controls[key] = label_control(page, page.GetSizer(), caption, control)

            text(bibliography, 'title', 'Title')
            text(bibliography, 'authors', 'Authors, one per line, in publication order', multiline=True)
            choice(bibliography, 'item_type', 'Publication type', ITEM_TYPES)
            for key, caption in (('year', 'Publication year as printed; leave empty if unknown'),
                                 ('container_title', 'Journal, proceedings or container title'),
                                 ('publisher', 'Publisher'), ('volume', 'Publication volume'),
                                 ('issue', 'Publication issue'), ('pages', 'Publication page range'),
                                 ('doi', 'DOI'), ('isbn', 'ISBN'), ('url', 'Web address')):
                text(bibliography, key, caption)
            linked = [(None, 'No linked local book')]
            for row, label in zip(books, book_link_labels(books)):
                linked.append((row.edition_id, label))
            if values['edition_id'] is not None and values['edition_id'] not in {key for key, _ in linked}:
                linked.append((values['edition_id'], 'Keep existing local book link'))
            choice(bibliography, 'edition_id', 'Linked imported book; optional', linked)
            text(bibliography, 'abstract', 'Publication abstract', multiline=True)
            choice(reading, 'reading_status', 'Reading status', READING_STATUSES)
            choice(reading, 'priority', 'Reading priority', PRIORITIES)
            choice(reading, 'rating', 'Personal rating', tuple((i, 'Not rated' if i == 0 else f'{i} of 5') for i in range(6)))
            text(reading, 'progress', 'Reading progress, from 0 to 100 percent')
            text(reading, 'tags', 'Reference tags, one per line', multiline=True)
            text(reading, 'notes', 'Personal research notes; excluded from exports unless selected', multiline=True)
            layout.Add(self.book, 1, wx.EXPAND | wx.ALL, 8)
            self.status = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE)
            self.status.SetName('Reference save status')
            self.status.SetMinSize((-1, 56))
            layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.save_button = wx.Button(self, wx.ID_SAVE, '&Save reference')
            self.cancel_button = wx.Button(self, wx.ID_CANCEL, '&Cancel')
            self.save_button.SetDefault()
            self.save_button.Bind(wx.EVT_BUTTON, self.on_save)
            self.cancel_button.Bind(wx.EVT_BUTTON, self.on_cancel)
            buttons.Add(self.save_button, 0, wx.ALL, 8)
            buttons.Add(self.cancel_button, 0, wx.ALL, 8)
            layout.Add(buttons, 0, wx.ALIGN_RIGHT)
            self.SetSizer(layout)
            self.SetMinSize((620, 540))
            self.SetEscapeId(wx.ID_CANCEL)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self._initial = self.raw_values()
            self.controls['title'].SetFocus()

        def on_destroy(self, event):
            if event.GetEventObject() is self:
                self._window_alive = False
            event.Skip()

        def raw_values(self):
            values = {}
            for key, control in self.controls.items():
                if key in self.choice_values:
                    index = control.GetSelection()
                    if not 0 <= index < len(self.choice_values[key]):
                        raise ValueError('Choose a value for ' + key.replace('_', ' ') + '.')
                    values[key] = self.choice_values[key][index]
                else:
                    values[key] = control.GetValue()
            return values

        def _enabled(self, enabled):
            for control in (*self.controls.values(), self.save_button, self.cancel_button):
                control.Enable(enabled)

        def _finish(self, result=None, error=None):
            self.pending = False
            # A save can commit before a forced destruction. Retain its result,
            # but never re-enter native controls after their destroy event.
            if error is None:
                self.saved = result
            if not self._window_alive:
                return
            self._enabled(True)
            if error is not None:
                report(self.status, f'Reference not saved: {error}\nYour edits remain in this form.')
                return
            if self.IsModal():
                self.EndModal(wx.ID_OK)
            else:
                self.SetReturnCode(wx.ID_OK)
                self.Hide()

        def on_save(self, event=None):
            if not self._window_alive or self.pending:
                return
            try:
                values = form_values(self.raw_values())
            except Exception as exc:
                report(self.status, str(exc))
                return
            self.pending = True
            self._enabled(False)
            report(self.status, 'Saving reference. Please wait.')
            if self.executor is None:
                try:
                    result = self.save(values)
                except Exception as exc:
                    self._finish(error=exc)
                else:
                    self._finish(result=result)
                return
            try:
                future = self.executor.submit(self.save, values)
            except Exception as exc:
                self._finish(error=exc)
                return

            def done(completed):
                try:
                    result = completed.result()
                except Exception as exc:
                    wx.CallAfter(self._finish, error=exc)
                else:
                    wx.CallAfter(self._finish, result=result)
            future.add_done_callback(done)

        def _can_discard(self):
            if self.pending:
                report(self.status, 'The reference is being saved. Wait for the result before closing.')
                return False
            try:
                dirty = self.raw_values() != self._initial
            except ValueError:
                dirty = True
            if not dirty:
                return True
            answer = (self.confirm_discard() if self.confirm_discard is not None else
                      wx.MessageBox('Discard the unsaved changes in this reference?', 'Unsaved reference',
                                    wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) == wx.YES)
            if not answer:
                report(self.status, 'Changes retained. Continue editing or save this reference.')
            return bool(answer)

        def on_cancel(self, event=None):
            if not self._window_alive:
                return
            if self._can_discard():
                if self.IsModal():
                    self.EndModal(wx.ID_CANCEL)
                else:
                    self.SetReturnCode(wx.ID_CANCEL)
                    self.Hide()

        def on_close(self, event):
            if not self._window_alive:
                return
            if not self._can_discard():
                if event.CanVeto():
                    event.Veto()
                return
            if self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            else:
                event.Skip()

    class ImportPreviewDialog(wx.Dialog):
        def __init__(self, parent, controller, preview):
            super().__init__(parent, title='Review bibliographic import', size=(820, 700),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.controller, self.preview = controller, preview
            self.result = None
            layout = wx.BoxSizer(wx.VERTICAL)
            text = wx.StaticText(self, label='Select the references to save. Space toggles a selected entry. '
                                 'Nothing is imported until you choose Import selected. Original documents are not downloaded.')
            text.Wrap(760)
            layout.Add(text, 0, wx.EXPAND | wx.ALL, 8)
            labels = []
            for number, entry in enumerate(preview.entries, 1):
                duplicate = ' — possible duplicate' if entry.duplicate_ids or entry.duplicate_indices else ''
                warning = ' — review warning' if entry.warnings else ''
                labels.append(f'{number}. {entry.title} — {"; ".join(entry.authors) or "Unknown author"}{duplicate}{warning}')
            self.entries = wx.CheckListBox(self, choices=labels)
            self.entries.SetName('References to import; Space selects or clears an entry')
            self.entries.Bind(wx.EVT_LISTBOX, self.on_selected)
            layout.Add(self.entries, 1, wx.EXPAND | wx.ALL, 8)
            selection_buttons = wx.BoxSizer(wx.HORIZONTAL)
            for caption, checked in (('Select &all', True), ('&Clear all', False)):
                button = wx.Button(self, label=caption)
                button.Bind(wx.EVT_BUTTON, lambda event, value=checked: self.select_all(value))
                selection_buttons.Add(button, 0, wx.ALL, 4)
            layout.Add(selection_buttons, 0, wx.ALL, 4)
            self.details = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE)
            self.details.SetName('Import preview details and warnings')
            self.details.SetMinSize((-1, 180))
            layout.Add(self.details, 1, wx.EXPAND | wx.ALL, 8)
            self.skip_duplicates = wx.CheckBox(self, label='Skip possible duplicates, including matching titles, authors and year')
            self.skip_duplicates.SetValue(False)
            layout.Add(self.skip_duplicates, 0, wx.ALL, 8)
            self.status = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE)
            self.status.SetName('Bibliography import status')
            self.status.SetMinSize((-1, 56))
            layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.import_button = wx.Button(self, label='&Import selected')
            self.import_button.Bind(wx.EVT_BUTTON, self.on_import)
            buttons.Add(self.import_button, 0, wx.ALL, 8)
            buttons.Add(wx.Button(self, wx.ID_CANCEL, '&Cancel'), 0, wx.ALL, 8)
            layout.Add(buttons, 0, wx.ALIGN_RIGHT)
            self.SetSizer(layout)
            self.SetMinSize((640, 520))
            self.SetEscapeId(wx.ID_CANCEL)
            report(self.status, '\n'.join(preview.warnings) or f'{len(preview.entries)} references available. Select entries to import.', focus=False)
            if preview.entries:
                self.entries.SetSelection(0)
                self.on_selected()
            self.entries.SetFocus()

        def select_all(self, checked):
            for index in range(len(self.preview.entries)):
                self.entries.Check(index, checked)
            self.entries.SetFocus()

        def on_selected(self, event=None):
            try:
                entry = selected_row(self.preview.entries, self.entries.GetSelection())
            except ValueError:
                self.details.SetValue('Choose an entry to read its metadata and warnings.')
                return
            lines = []
            for key, value in entry.fields.items():
                if value is not None and value != '' and value != ():
                    display = '\n'.join(value) if isinstance(value, (tuple, list)) else str(value)
                    lines.append(key.replace('_', ' ').capitalize() + ':\n' + display)
            if entry.duplicate_ids:
                lines.append(f'Possible duplicates already in this library: {len(entry.duplicate_ids)}.')
            if entry.duplicate_indices:
                lines.append('Possible duplicates in this import: ' + ', '.join(str(i + 1) for i in entry.duplicate_indices) + '.')
            if entry.warnings:
                lines.append('Warnings:\n' + '\n'.join(entry.warnings))
            report(self.details, '\n\n'.join(lines), focus=False)

        def on_import(self, event=None):
            indices = tuple(index for index in range(len(self.preview.entries)) if self.entries.IsChecked(index))
            if not indices:
                report(self.status, 'Select at least one reference. Space selects the current entry.')
                return
            try:
                self.result = self.controller.import_selected(self.preview, indices,
                    skip_duplicates=self.skip_duplicates.GetValue())
            except Exception as exc:
                report(self.status, f'Import not completed: {exc}\nYour selection is retained. No partial import is saved.')
                return
            if self.IsModal():
                self.EndModal(wx.ID_OK)
            else:
                self.SetReturnCode(wx.ID_OK)
                self.Hide()

    return wx, ReferenceEditor, ImportPreviewDialog, readable, report, label_control


def create_academic_dialog(parent, controller, *, executor=None):
    wx, ReferenceEditor, ImportPreviewDialog, readable, report, label_control = _native_types()

    class AcademicDialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Academic library', size=(960, 820),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self._window_alive = True
            self.controller, self.executor = controller, executor
            self._export_host = parent
            self.rows, self.offset, self.has_more = (), 0, False
            self.pending = False
            self.cancel_event = threading.Event()
            layout = wx.BoxSizer(wx.VERTICAL)
            filter_grid = wx.FlexGridSizer(rows=0, cols=2, vgap=4, hgap=8)
            filter_grid.AddGrowableCol(1)
            self.query = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
            self.reading_status = wx.Choice(self, choices=['All reading statuses'] + [label for _, label in READING_STATUSES])
            self.reading_status.SetSelection(0)
            self.tag = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
            self.sort = wx.Choice(self, choices=[label for _, label in SORTS])
            self.sort.SetSelection(0)
            for caption, control in (('Search references', self.query), ('Reading status filter', self.reading_status),
                                     ('Reference tag filter; exact tag', self.tag), ('Sort references', self.sort)):
                control.SetName(caption)
                filter_grid.Add(wx.StaticText(self, label=caption), 0, wx.ALIGN_CENTER_VERTICAL)
                filter_grid.Add(control, 1, wx.EXPAND)
            layout.Add(filter_grid, 0, wx.EXPAND | wx.ALL, 8)
            filter_buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.include_archived = wx.CheckBox(self, label='Include archived references')
            self.include_archived.SetValue(False)
            filter_buttons.Add(self.include_archived, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
            apply_button = wx.Button(self, label='&Apply filters')
            apply_button.Bind(wx.EVT_BUTTON, self.on_filter)
            self.query.Bind(wx.EVT_TEXT_ENTER, self.on_filter)
            self.tag.Bind(wx.EVT_TEXT_ENTER, self.on_filter)
            filter_buttons.Add(apply_button, 0, wx.ALL, 4)
            clear_button = wx.Button(self, label='Clear &filters')
            clear_button.Bind(wx.EVT_BUTTON, self.on_clear_filters)
            filter_buttons.Add(clear_button, 0, wx.ALL, 4)
            layout.Add(filter_buttons, 0, wx.LEFT | wx.RIGHT, 4)
            self.references = wx.ListBox(self)
            self.references.SetName('Academic references')
            self.references.SetMinSize((-1, 160))
            self.references.Bind(wx.EVT_LISTBOX, self.on_selected)
            self.references.Bind(wx.EVT_LISTBOX_DCLICK, self.on_edit)
            layout.Add(self.references, 1, wx.EXPAND | wx.ALL, 8)
            page_buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.previous = wx.Button(self, label='&Previous page')
            self.next = wx.Button(self, label='&Next page')
            self.previous.Bind(wx.EVT_BUTTON, self.on_previous)
            self.next.Bind(wx.EVT_BUTTON, self.on_next)
            page_buttons.Add(self.previous, 0, wx.ALL, 4)
            page_buttons.Add(self.next, 0, wx.ALL, 4)
            layout.Add(page_buttons, 0, wx.LEFT | wx.RIGHT, 4)
            self.details = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE)
            self.details.SetName('Selected reference details')
            self.details.SetMinSize((-1, 150))
            layout.Add(self.details, 1, wx.EXPAND | wx.ALL, 8)
            buttons = wx.GridSizer(rows=0, cols=3, vgap=4, hgap=4)
            self.actions = {}
            for name, caption, handler in (
                ('add', 'New &reference', self.on_add), ('edit', '&Edit reference', self.on_edit),
                ('archive', 'Archi&ve reference', self.on_archive),
                ('import', '&Import bibliography', self.on_import),
                ('export', 'E&xport bibliography', self.on_export),
                ('copy', '&Copy bibliography', self.on_copy),
                ('duplicates', 'Find &duplicates', self.on_duplicates),
                ('statistics', 'Reading &overview', self.on_statistics),
                ('document', 'Open linked &document', self.on_document),
                ('styles', 'Citation &styles', self.on_styles),
            ):
                button = wx.Button(self, label=caption)
                button.Bind(wx.EVT_BUTTON, handler)
                self.actions[name] = button
                buttons.Add(button, 0, wx.EXPAND)
            layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
            self.status = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE)
            self.status.SetName('Academic library status')
            self.status.SetMinSize((-1, 56))
            layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
            self.close_button = wx.Button(self, wx.ID_CANCEL, '&Close')
            self.close_button.Bind(wx.EVT_BUTTON, self.on_cancel)
            layout.Add(self.close_button, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
            self.job_controls = (self.query, self.reading_status, self.tag, self.sort,
                                 self.include_archived, apply_button, clear_button,
                                 self.references, self.previous, self.next, *self.actions.values())
            self.SetSizer(layout)
            self.SetMinSize((740, 680))
            self.SetEscapeId(wx.ID_CANCEL)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            self.refresh()
            self.query.SetFocus()

        def on_destroy(self, event):
            if event.GetEventObject() is self:
                self._window_alive = False
                self.cancel_event.set()
            event.Skip()

        def filters(self):
            status_index = self.reading_status.GetSelection()
            if not 0 <= status_index <= len(READING_STATUSES):
                raise ValueError('Choose a reading status filter.')
            sort_index = self.sort.GetSelection()
            sort = selected_row(SORTS, sort_index)[0]
            return dict(query=self.query.GetValue(),
                        status=None if status_index == 0 else READING_STATUSES[status_index - 1][0],
                        tag=self.tag.GetValue().strip() or None, sort=sort,
                        include_archived=self.include_archived.GetValue())

        def selected(self):
            return selected_row(self.rows, self.references.GetSelection())

        def _buttons(self):
            try:
                row = self.selected()
            except ValueError:
                row = None
            for name in ('edit', 'archive', 'copy', 'document'):
                self.actions[name].Enable(row is not None)
            if row is not None:
                self.actions['edit'].Enable(not row.archived)
            self.actions['archive'].SetLabel('&Restore reference' if row and row.archived else 'Archi&ve reference')
            self.previous.Enable(self.offset > 0)
            self.next.Enable(self.has_more)

        def _run(self, title, operation, done, *, publication=False):
            if not self._window_alive:
                return
            if self.pending:
                report(self.status, 'An academic library operation is running. Use Cancel operation to stop it.')
                return
            self.pending = True
            self.cancel_event.clear()
            for control in self.job_controls:
                control.Disable()
            self.close_button.SetLabel('&Cancel operation')
            report(self.status, title + '. Please wait.')

            def complete(result=None, error=None):
                self.pending = False
                if not self._window_alive:
                    return
                for control in self.job_controls:
                    control.Enable()
                self.close_button.SetLabel('&Close')
                self._buttons()
                if error is not None:
                    report(self.status, f'{title} not completed: {error}')
                    return
                try:
                    # A successful writer result means the atomic publication
                    # completed; a later cancel cannot undo that saved file.
                    if self.cancel_event.is_set() and not publication:
                        raise TimeoutError('Operation cancelled.')
                    done(result)
                except Exception as exc:
                    if self._window_alive:
                        report(self.status, f'{title} not completed: {exc}')

            if self.executor is None:
                try:
                    result = operation()
                except Exception as exc:
                    complete(error=exc)
                else:
                    complete(result=result)
                return
            try:
                future = self.executor.submit(operation)
            except Exception as exc:
                complete(error=exc)
                return

            def finished(completed):
                try:
                    result = completed.result()
                except Exception as exc:
                    wx.CallAfter(complete, error=exc)
                else:
                    wx.CallAfter(complete, result=result)
            future.add_done_callback(finished)

        def on_cancel(self, event=None):
            if not self._window_alive:
                return
            self.cancel_event.set()
            if self.pending:
                report(self.status, 'Cancellation requested. Wait for the current operation to stop.')
            elif self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            else:
                self.Hide()

        def on_close(self, event):
            if not self._window_alive:
                return
            self.cancel_event.set()
            if self.pending:
                self.on_cancel()
                if event.CanVeto():
                    event.Veto()
            elif self.IsModal():
                self.EndModal(wx.ID_CANCEL)
            else:
                event.Skip()

        def refresh(self, *, selected_id=None):
            if selected_id is None:
                try:
                    selected_id = self.selected().id
                except ValueError:
                    pass
            try:
                candidates = self.controller.list(**self.filters(), limit=PAGE_SIZE + 1, offset=self.offset)
                self.has_more = len(candidates) > PAGE_SIZE
                self.rows = tuple(candidates[:PAGE_SIZE])
                self.references.Set(reference_labels(self.rows, offset=self.offset))
                if self.rows:
                    ids = [row.id for row in self.rows]
                    self.references.SetSelection(ids.index(selected_id) if selected_id in ids else 0)
                self.on_selected()
                count = len(self.rows)
                text = (f'Showing references {self.offset + 1} to {self.offset + count}.' if count else
                        'No references match these filters. Add a reference, import a bibliography, or change the filters.')
                if self.has_more:
                    text += ' More results are available with Next page.'
                report(self.status, text, focus=False)
            except Exception as exc:
                self.rows, self.has_more = (), False
                self.references.Clear()
                self.details.Clear()
                self._buttons()
                report(self.status, f'Academic library unavailable: {exc}')

        def on_filter(self, event=None):
            self.offset = 0
            self.refresh()
            if self.rows:
                self.references.SetFocus()
            else:
                self.status.SetFocus()

        def on_clear_filters(self, event=None):
            self.query.SetValue('')
            self.tag.SetValue('')
            self.reading_status.SetSelection(0)
            self.include_archived.SetValue(False)
            self.on_filter()

        def on_previous(self, event=None):
            if self.offset:
                self.offset = max(0, self.offset - PAGE_SIZE)
                self.refresh()
                self.references.SetFocus()

        def on_next(self, event=None):
            if self.has_more:
                self.offset += PAGE_SIZE
                self.refresh()
                self.references.SetFocus()

        def on_selected(self, event=None):
            try:
                text = reference_details(self.selected())
            except ValueError:
                text = 'Select a reference to read its bibliography and personal reading notes.'
            report(self.details, text, focus=False)
            self._buttons()

        def _edit(self, reference=None):
            save = self.controller.create if reference is None else lambda values: self.controller.update(reference, values)
            dialog = ReferenceEditor(self, save=save, books=self.controller.books(),
                                     reference=reference, executor=self.executor)
            try:
                if dialog.ShowModal() == wx.ID_OK:
                    self.refresh(selected_id=dialog.saved.id)
                    report(self.status, 'Reference saved. The current filters are retained.')
                else:
                    self.references.SetFocus()
            finally:
                dialog.Destroy()

        def on_add(self, event=None):
            try:
                self._edit()
            except Exception as exc:
                report(self.status, f'New reference could not be opened: {exc}')

        def on_edit(self, event=None):
            try:
                row = self.selected()
                if row.archived:
                    raise ValueError('Restore this archived reference before editing it.')
                self._edit(row)
            except Exception as exc:
                report(self.status, f'Reference could not be edited: {exc}')

        def on_archive(self, event=None):
            try:
                row = self.selected()
                action = 'Restore' if row.archived else 'Archive'
                explanation = ('It will appear among active references.' if row.archived else
                               'It will be preserved and can be restored by selecting Include archived references.')
                if wx.MessageBox(f'{action} "{row.title}"?\n{explanation}', action + ' reference',
                                 wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
                    self.references.SetFocus()
                    return
                saved = self.controller.archive(row, archived=not row.archived)
                self.refresh(selected_id=saved.id)
                report(self.status, 'Reference restored.' if row.archived else
                       'Reference archived. Select Include archived references and Apply filters to restore it.')
            except Exception as exc:
                report(self.status, f'Reference state not changed: {exc}')

        def on_statistics(self, event=None):
            try:
                readable(self, 'Academic reading overview', statistics_text(self.controller.statistics()))
                self.references.SetFocus()
            except Exception as exc:
                report(self.status, f'Reading overview unavailable: {exc}')

        def on_duplicates(self, event=None):
            def done(text):
                readable(self, 'Possible duplicate references', text)
                self.references.SetFocus()
            self._run('Finding duplicate references', self.controller.duplicate_report, done)

        def on_document(self, event=None):
            try:
                reference = self.selected()
                if reference.edition_id is None:
                    raise ValueError('This reference has no linked local book. Edit it to choose a local book link.')
                from ..reader.source_file import launch_source_file

                def ready(source):
                    if self.controller.get(reference.id) != reference or self.selected().id != reference.id:
                        raise ValueError('The selected reference changed. Refresh it and try again.')
                    launch_source_file(self.controller.library, source, wx.LaunchDefaultApplication,
                                       cancelled=self.cancel_event.is_set)
                    report(self.status, 'Opened a viewing copy of the linked document. The retained original is unchanged.')

                self._run('Opening linked document',
                    lambda: self.controller.prepare_document(reference, cancelled=self.cancel_event.is_set), ready)
            except Exception as exc:
                report(self.status, f'Linked document not opened: {exc}')

        def on_copy(self, event=None):
            try:
                row = self.selected()
                content = self.controller.export(identity=row.id, format='text', include_notes=False)
                if not wx.TheClipboard.Open():
                    raise ValueError('The clipboard is busy. Try again.')
                try:
                    if not wx.TheClipboard.SetData(wx.TextDataObject(content)):
                        raise ValueError('The bibliography could not be copied.')
                finally:
                    wx.TheClipboard.Close()
                report(self.status, 'Bibliography copied. Personal notes were not included.')
            except Exception as exc:
                report(self.status, f'Bibliography not copied: {exc}')

        def on_import(self, event=None):
            chooser = wx.FileDialog(self, 'Choose a RIS, BibTeX or CSL JSON bibliography',
                                   wildcard='Bibliography (*.ris;*.bib;*.json)|*.ris;*.bib;*.json|RIS (*.ris)|*.ris|BibTeX (*.bib)|*.bib|CSL JSON (*.json)|*.json',
                                   style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            try:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                path = Path(chooser.GetPath())
            finally:
                chooser.Destroy()
            try:
                format = {'.ris': 'ris', '.bib': 'bibtex', '.json': 'csl-json'}.get(path.suffix.lower())
                if format is None:
                    raise ValueError('Choose a .ris, .bib or .json bibliography file.')
                with path.open('rb') as stream:
                    data = stream.read(MAX_IMPORT_BYTES + 1)
                if len(data) > MAX_IMPORT_BYTES:
                    raise ValueError('Import files must be at most 8 MiB. Split this bibliography into smaller files.')
                preview = self.controller.preview(data, format)
                dialog = ImportPreviewDialog(self, self.controller, preview)
                try:
                    if dialog.ShowModal() == wx.ID_OK:
                        result = dialog.result
                        self.refresh(selected_id=next(iter(result.imported_ids), None))
                        report(self.status, f'Imported {len(result.imported_ids)} references; '
                               f'skipped {len(result.skipped_indices)}. Current filters are retained.')
                    else:
                        self.references.SetFocus()
                finally:
                    dialog.Destroy()
            except Exception as exc:
                report(self.status, f'Bibliography import not completed: {exc}')

        def on_styles(self, event=None):
            dialog = None
            try:
                from .citation_styles_dialog import create_citation_styles_dialog
                try:
                    identity = self.selected().id
                except ValueError:
                    identity = None
                dialog = create_citation_styles_dialog(self, self.controller,
                    selected_identity=identity, executor=self.executor)
                dialog.ShowModal()
                self.references.SetFocus()
            except Exception as exc:
                report(self.status, f'Citation styles could not be opened: {exc}')
            finally:
                if dialog is not None:
                    dialog.Destroy()

        def _export_options(self):
            dialog = wx.Dialog(self, title='Export bibliography', size=(560, 320))
            try:
                layout = wx.BoxSizer(wx.VERTICAL)
                format_choice = wx.Choice(dialog, choices=[label for _, label, _ in FORMATS])
                format_choice.SetSelection(0)
                label_control(dialog, layout, 'Export format', format_choice)
                scope = wx.Choice(dialog, choices=['All active references in this library', 'Selected reference only'])
                scope.SetSelection(0)
                label_control(dialog, layout, 'Export scope; independent of the current list filters', scope)
                notes = wx.CheckBox(dialog, label='Include personal research notes')
                notes.SetValue(False)
                layout.Add(notes, 0, wx.ALL, 8)
                layout.Add(dialog.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALIGN_RIGHT | wx.ALL, 8)
                dialog.SetSizer(layout)
                dialog.SetEscapeId(wx.ID_CANCEL)
                format_choice.SetFocus()
                if dialog.ShowModal() != wx.ID_OK:
                    return None
                format = selected_row(FORMATS, format_choice.GetSelection())
                index = scope.GetSelection()
                if index not in (0, 1):
                    raise ValueError('Choose the export scope.')
                identity = self.selected().id if index == 1 else None
                return format, identity, notes.GetValue()
            finally:
                dialog.Destroy()

        def export_to(self, path, *, format='text', identity=None, include_notes=False):
            content = self.controller.export_to(path, identity=identity, format=format, include_notes=include_notes)
            report(self.status, f'Bibliography exported to {path}. '
                   + ('Personal notes were included.' if include_notes else 'Personal notes were not included.'))
            return content

        def on_export(self, event=None):
            if not self._window_alive or self.pending:
                return
            try:
                if self.executor is None:
                    raise ValueError('The background export worker is unavailable.')
                controller = self.controller
                library, workspace = controller.library, controller.workspace
                host = getattr(self, '_export_host', None)
                host_library = getattr(host, 'library', None)
                host_scope = getattr(host, 'active', None)
                token = self._export_token = object()
                self.cancel_event.clear()

                def cancelled():
                    return (not self._window_alive or self._export_token is not token
                        or self.controller is not controller or self.cancel_event.is_set()
                        or controller.library is not library or controller.workspace != workspace
                        or getattr(host, '_closing', False)
                        or getattr(host, 'library', None) is not host_library
                        or getattr(host, 'active', None) != host_scope)

                def current():
                    if cancelled():
                        raise TimeoutError('Export cancelled or its library changed; the destination was not changed.')

                options = self._export_options()
                current()
                if options is None:
                    return
                (format, label, extension), identity, include_notes = options
                dialog = wx.FileDialog(self, 'Save academic bibliography',
                                       defaultFile='academic-bibliography.' + extension,
                                       wildcard=f'{label} (*.{extension})|*.{extension}',
                                       style=wx.FD_SAVE)
                try:
                    if dialog.ShowModal() != wx.ID_OK:
                        return
                    path = Path(dialog.GetPath())
                finally:
                    dialog.Destroy()
                current()

                def capture():
                    current()
                    with library.read(workspace):
                        pass
                    return capture_export_destination(path, private_root=library.root, cancelled=cancelled)

                def captured(approval):
                    current()
                    if not approval.require_absent:
                        decision = wx.MessageBox(
                            f'Replace the existing export {path.name}?', 'Replace bibliography',
                            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self)
                        current()  # A modal confirmation can close or change its owner.
                        if decision != wx.YES:
                            report(self.status, 'Bibliography export cancelled.')
                            return
                    current()

                    def publish():
                        current()
                        return controller.export_to(path, format=format, identity=identity,
                            include_notes=include_notes, cancelled=cancelled, approval=approval)

                    def saved(content):
                        report(self.status, f'Bibliography exported to {path}. '
                            + ('Personal notes were included.' if include_notes else 'Personal notes were not included.'))

                    self._run('Exporting bibliography', publish, saved, publication=True)

                self._run('Checking export destination', capture, captured)
            except Exception as exc:
                if self._window_alive:
                    report(self.status, f'Bibliography not exported: {exc}')

    return AcademicDialog()


def _frame_scope(frame):
    if frame.working or getattr(frame, 'busy', False) or getattr(frame, '_closing', False):
        raise ValueError('Wait for the current operation before opening the academic library.')
    workspace = frame._workspace()
    return frame.library, workspace


def open_academic_library(frame):
    dialog = None
    try:
        library, workspace = _frame_scope(frame)
        import wx
        previous = wx.Window.FindFocus()
        controller = AcademicController(library, workspace)
        dialog = create_academic_dialog(frame, controller, executor=frame.executor)
        dialog.ShowModal()
        if frame.library is library and frame.active and frame.active[0] == workspace:
            try:
                if previous is not None:
                    previous.SetFocus()
            except RuntimeError:
                frame.choice.SetFocus()
    except Exception as exc:
        frame.report(f'Academic library could not be opened: {exc}')
    finally:
        if dialog is not None:
            dialog.Destroy()


def add_selected_book_reference(frame):
    dialog = None
    try:
        library, workspace = _frame_scope(frame)
        if frame.book is None:
            raise ValueError('Open Books and Collections and select an imported book first.')
        panel = frame.book.GetPage(1)
        if panel.workspace != workspace or not panel.ready:
            raise ValueError('Refresh Books and Collections before adding a reference.')
        selection = panel._selection()
        edition_id = selection[2]
        if edition_id is None:
            raise ValueError('Select an imported book in Books and Collections first.')
        controller = AcademicController(library, workspace)
        values = controller.from_book(edition_id)
        wx, ReferenceEditor, _, _, _, _ = _native_types()
        dialog = ReferenceEditor(frame, save=controller.create, books=controller.books(), initial=values,
                                 executor=frame.executor)
        if dialog.ShowModal() == wx.ID_OK:
            frame.report('Academic reference saved and linked to the imported book. '
                         'Open Academic library with Ctrl+Shift+B to manage it.')
        elif frame.library is library and frame.active and frame.active[0] == workspace:
            panel.books.SetFocus()
    except Exception as exc:
        frame.report(f'Academic reference not added: {exc}')
    finally:
        if dialog is not None:
            dialog.Destroy()
