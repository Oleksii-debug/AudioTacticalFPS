"""Native, explicit RIS review for an existing imported edition; no new schema."""
import wx

from ..catalog.ris_apply import (FIELDS, RisReview, apply_ris_metadata,
                                 read_ris_file, selected_metadata)
from ..catalog.membership import Catalog
from ..catalog.read_views import get_edition


NOT_IMPORTED = (
    'Identifiers and reference type are unverified hints and are not imported. '
    'Publication year, journal, publisher, abstract, notes, keywords, page ranges, '
    'URLs, attachments and all other unsupported fields are not imported. '
    'No file is attached or downloaded. Bibliographic metadata does not verify a quotation.'
)
_LABELS = dict(title='Title', authors='Authors, in RIS order', language='Language',
               edition='Edition', volume='Volume')
_CANDIDATES = dict(title='titles', authors='authors', language='languages',
                   edition='editions', volume='volumes')


def _edition_label(row):
    return f'{row.title or "Untitled"} — {"; ".join(row.authors) or "Unknown author"} — {row.edition or "Edition unknown"} [{row.edition_id}]'


class BibliographyReviewDialog(wx.Dialog):
    """Every import checkbox starts off and resets when either selection changes."""
    def __init__(self, parent, app, workspace, records, editions, *, edition_id=None):
        super().__init__(parent, title='Review RIS bibliography metadata', size=(780, 750))
        self.app, self.workspace = app, workspace
        self.records, self.editions = tuple(records), tuple(editions)
        self.result = None
        self.checks, self.fields = {}, {}
        layout = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(self, label=(
            'Choose an imported edition and one RIS record, then check only the fields to replace. '
            'Unchecked and missing fields keep their current values. Title and authors change '
            'all editions of the same work; saved quotations keep their historical citations.'))
        label.Wrap(730)
        layout.Add(label, 0, wx.EXPAND | wx.ALL, 8)
        layout.Add(wx.StaticText(self, label='Target imported edition in this library'), 0, wx.LEFT, 8)
        self.target = wx.Choice(self, choices=[_edition_label(row) for row in self.editions])
        self.target.SetName('Target imported edition in this library')
        selected = next((i for i, row in enumerate(self.editions) if row.edition_id == edition_id), 0)
        self.target.SetSelection(selected)
        layout.Add(self.target, 0, wx.EXPAND | wx.ALL, 8)
        layout.Add(wx.StaticText(self, label='Unverified RIS record'), 0, wx.LEFT, 8)
        self.record = wx.Choice(self, choices=[
            f'{row.record_number}. {(row.titles[0] if row.titles else "Title missing")[:220]}'
            for row in self.records])
        self.record.SetName('Unverified RIS record')
        self.record.SetSelection(0)
        layout.Add(self.record, 0, wx.EXPAND | wx.ALL, 8)
        self.details = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.details.SetName('Current edition metadata and unverified RIS identifiers')
        self.details.SetMinSize((-1, 160))
        layout.Add(self.details, 1, wx.EXPAND | wx.ALL, 8)
        for key in FIELDS:
            row = wx.BoxSizer(wx.HORIZONTAL)
            check = wx.CheckBox(self, label=f'Use RIS {_LABELS[key].lower()}')
            check.SetName(f'Apply RIS {_LABELS[key].lower()}')
            control = (wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
                       if key == 'authors' else wx.Choice(self))
            control.SetName(f'Unverified RIS {_LABELS[key].lower()} to apply')
            if key == 'authors':
                control.SetMinSize((-1, 60))
            self.checks[key], self.fields[key] = check, control
            row.Add(check, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
            row.Add(control, 1, wx.EXPAND | wx.ALL, 5)
            layout.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 3)
        self.status = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.status.SetName('RIS review status')
        self.status.SetMinSize((-1, 50))
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        apply_button = wx.Button(self, wx.ID_OK, '&Apply selected fields')
        apply_button.Bind(wx.EVT_BUTTON, self.on_apply)
        buttons.Add(apply_button, 0, wx.ALL, 8)
        buttons.Add(wx.Button(self, wx.ID_CANCEL, '&Cancel'), 0, wx.ALL, 8)
        layout.Add(buttons, 0, wx.ALIGN_RIGHT)
        self.SetSizer(layout)
        self.SetEscapeId(wx.ID_CANCEL)
        self.target.Bind(wx.EVT_CHOICE, self.on_selection)
        self.record.Bind(wx.EVT_CHOICE, self.on_selection)
        self.on_selection()
        self.target.SetFocus()

    def _review(self):
        target, record = self.target.GetSelection(), self.record.GetSelection()
        if not 0 <= target < len(self.editions) or not 0 <= record < len(self.records):
            raise ValueError('Choose an imported edition and a RIS record.')
        return RisReview(self.app.root, self.workspace, self.editions[target], self.records[record])

    def on_selection(self, event=None):
        review = self._review()
        current, hint = review.metadata, review.record
        lines = ['Current edition metadata:']
        for key in FIELDS:
            value = getattr(current, key)
            lines.append(f'{_LABELS[key]}: {"; ".join(value) if key == "authors" else value or "(unknown)"}')
            candidates = getattr(hint, _CANDIDATES[key])
            self.checks[key].SetValue(False)
            self.checks[key].Enable(bool(candidates))
            control = self.fields[key]
            if key == 'authors':
                control.SetValue('\n'.join(candidates))
            else:
                control.SetItems(list(candidates))
                control.SetSelection(0 if candidates else wx.NOT_FOUND)
        lines.extend(('', f'Unverified RIS type: {hint.reference_type}',
                      'Unverified RIS identifiers (not imported): ' + ('; '.join(hint.identifiers) or '(none)'),
                      '', NOT_IMPORTED))
        self.details.SetValue('\n'.join(lines))
        self.status.SetValue('No fields selected. Choose the values to replace and check their boxes.')

    def on_apply(self, event=None):
        try:
            review = self._review()
            selected = {}
            for key in FIELDS:
                if self.checks[key].GetValue():
                    selected[key] = (review.record.authors if key == 'authors'
                                     else self.fields[key].GetStringSelection())
            selected_metadata(review, selected)
            self.result = review, selected
            self.EndModal(wx.ID_OK)
        except (ValueError, TypeError) as exc:
            self.status.SetValue(str(exc))
            self.status.SetFocus()


def _same_scope(frame, app, scope):
    if (getattr(frame, '_closing', False) or frame.library is not app or frame.active != scope):
        raise ValueError('The selected library or topic changed. Open bibliography import again.')


def _editions(app, workspace, edition_id, cancelled):
    if cancelled():
        raise TimeoutError('Bibliography import cancelled.')
    with app.read(workspace) as db:
        active = tuple(r[0] for r in db.execute(
            'SELECT edition FROM active WHERE workspace=? ORDER BY edition LIMIT 501', (workspace,)))
        if len(active) > 500:
            if edition_id is None:
                raise ValueError('Select a book in Books and Collections before importing RIS into this large library.')
            active = tuple(r[0] for r in db.execute(
                'SELECT edition FROM active WHERE workspace=? AND edition=?', (workspace, edition_id)))
        if edition_id is not None and edition_id not in active:
            raise ValueError('Select an imported edition in this library before applying RIS metadata.')
        catalog = Catalog(db, workspace)
        rows = []
        for identity in active:
            if cancelled():
                raise TimeoutError('Bibliography import cancelled.')
            rows.append(get_edition(catalog, identity))
    if not rows:
        raise ValueError('Import a book into this library before applying bibliography metadata.')
    return tuple(rows)


def open_bibliography_import(frame, *, edition_id=None):
    """Menu entry point. Parsing and atomic publication use the frame worker."""
    try:
        if frame.working or getattr(frame, 'busy', False):
            raise ValueError('Wait for the current operation before importing bibliography metadata.')
        scope, app = frame.active, frame.library
        if scope is None:
            raise ValueError('Open a library first.')
        _same_scope(frame, app, scope)
        if edition_id is None and frame.book is not None:
            panel = frame.book.GetPage(1)
            if panel.workspace == scope[0] and panel.ready:
                edition_id = panel._selection()[2]
        dialog = wx.FileDialog(frame, 'Choose a local RIS bibliography',
                               wildcard='RIS bibliography (*.ris)|*.ris|All files (*.*)|*.*',
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                frame.report('Bibliography import cancelled. No metadata was applied.')
                return
            path = dialog.GetPath()
        finally:
            dialog.Destroy()
        _same_scope(frame, app, scope)

        def load():
            _same_scope(frame, app, scope)
            records = read_ris_file(path, cancelled=frame.cancel_event.is_set)
            rows = _editions(app, scope[0], edition_id, frame.cancel_event.is_set)
            if frame.cancel_event.is_set():
                raise TimeoutError('Bibliography import cancelled.')
            return records, rows

        def loaded(result):
            _same_scope(frame, app, scope)
            if frame.cancel_event.is_set():
                raise TimeoutError('Bibliography import cancelled.')
            review_dialog = BibliographyReviewDialog(frame, app, scope[0], *result, edition_id=edition_id)
            try:
                if review_dialog.ShowModal() != wx.ID_OK:
                    frame.report('Bibliography review cancelled. No metadata was applied.')
                    return
                review, selected = review_dialog.result
            finally:
                review_dialog.Destroy()
            _same_scope(frame, app, scope)

            def apply():
                _same_scope(frame, app, scope)
                return apply_ris_metadata(app, scope[0], review, selected,
                                          cancelled=frame.cancel_event.is_set)

            def saved(revision):
                if frame.library is not app or frame.active != scope:
                    frame.report('Reviewed RIS metadata was saved in the original library. '
                                 'The current library view was not changed.')
                    return
                message = ('Selected RIS fields saved. Other fields, original files and saved quotations '
                           'are preserved. Search again to use the updated citation.')
                try:
                    if frame.book is not None:
                        catalog = frame.book.GetPage(1)
                        if catalog.workspace == scope[0]:
                            selection = catalog._selection()
                            if selection[2] != review.metadata.edition_id:
                                selection = (None, None, review.metadata.edition_id)
                            if not catalog.refresh(selection=selection):
                                message += ' Refresh Books and Collections to see the result.'
                            frame.book.GetPage(0).refresh_collections()
                except Exception as exc:
                    message += f' The view could not refresh: {exc}'
                frame.report(message)

            frame.run_job('Applying reviewed RIS metadata', apply, saved)

        frame.run_job('Reading RIS bibliography', load, loaded)
    except Exception as exc:
        frame.report(f'Bibliography import not completed: {exc}')
