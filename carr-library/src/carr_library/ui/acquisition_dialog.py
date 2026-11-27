"""Explicit DOI/ISBN lookup, metadata review and lawful source acquisition.

The application owns network policy, immutable preview tokens, authorization,
download validation and the existing import quarantine. This native dialog
only captures an explicit request and presents its complete result. Opening it
never makes a network request or sends private source text.
"""
from pathlib import Path
import sqlite3
from uuid import uuid4

import wx

from ..research.topic_tree import TopicTree
from .catalog_tools_dialog import _collection_choices, _workspace


MAX_PREVIEW_CHARACTERS = 2_000_000
PROVIDERS = (('crossref', 'Crossref'), ('europe_pmc', 'Europe PMC'),
             ('unpaywall', 'Unpaywall'), ('openalex', 'OpenAlex'))


def _value(value):
    return str(value) if value not in (None, '') else 'Not supplied'


def _downloadable(source):
    return source is not None and source.format == 'pdf' and source.reported_open_access is True


def _preview_identity(preview):
    kind = getattr(preview, 'identifier_kind', 'doi')
    if kind not in ('doi', 'isbn'):
        raise ValueError('The metadata preview has an unsupported identifier type.')
    return kind, getattr(preview, 'identifier', '') or preview.doi


def metadata_text(preview):
    """Render provider claims separately from local source verification."""
    metadata = preview.metadata
    kind, identifier = _preview_identity(preview)
    lines = [
        f'{kind.upper()} metadata proposal',
        f'{kind.upper()}: {identifier}',
        f'Retrieved at UTC: {_value(preview.fetched_utc)}',
        'Result: ' + ('cached response' if preview.cached else 'fresh lookup'),
        'Provider metadata does not verify quotations or grant download rights.',
        '',
    ]
    labels = (('title', 'Title'), ('year', 'Year'), ('item_type', 'Item type'),
              ('container_title', 'Journal or collection'), ('publisher', 'Publisher'),
              ('volume', 'Volume'), ('issue', 'Issue'), ('pages', 'Publication page extent'),
              ('language', 'Language'), ('source_url', 'Metadata source URL'))
    for name, label in labels:
        lines.append(f'{label}: {_value(getattr(metadata, name))}')
    lines.append('Authors in provider order:')
    lines.extend(f'{index}. {name}' for index, name in enumerate(metadata.authors, 1))
    if not metadata.authors:
        lines.append('Not supplied')
    lines.extend(('ISBN: ' + (', '.join(metadata.isbn) or 'Not supplied'),
                  'Abstract: ' + _value(metadata.abstract), '', 'Provider provenance:'))
    for record in preview.provider_records:
        record_identifier = (f'DOI: {record.doi}' if record.doi else
                             'ISBN: ' + (', '.join(record.isbn) or 'Not supplied'))
        lines.extend((f'Provider: {record.provider}', record_identifier,
                      f'Record title: {_value(record.title)}',
                      f'Source URL: {_value(record.source_url)}',
                      f'Response SHA-256: {_value(record.raw_sha256)}'))
        lines.extend(f'Provider warning: {warning}' for warning in record.warnings)
        lines.append('')
    lines.append(f'Source locations reported: {len(preview.sources)}. Review the Download source tab.')
    lines.extend(f'Warning: {warning}' for warning in preview.warnings)
    result = '\n'.join(lines)
    if len(result) > MAX_PREVIEW_CHARACTERS:
        raise ValueError('The complete metadata preview is too large to display. '
                         'No metadata action was enabled; for DOI lookup, select fewer providers.')
    return result


def source_text(source):
    claim = {True: 'Yes, according to the provider', False: 'No',
             None: 'Unknown'}[source.reported_open_access]
    result = '\n'.join((
        f'Provider: {source.provider}', f'Download URL: {_value(source.url)}',
        f'Landing page: {_value(source.landing_url)}', f'Format: {source.format}',
        f'Version or edition: {_value(source.version)}',
        f'License reported by the provider: {_value(source.license)}',
        f'Reported open access: {claim}', f'Host type: {_value(source.host_type)}',
        '', 'A URL or an open-access claim does not by itself establish permission.',
        'Check the source, version and license before confirming your right to download.',
        'Only provider-reported open-access PDFs can be downloaded here. The file and provenance receipt',
        'are saved in your chosen new folder, then processed by the normal import workflow.',
        'Imported text remains subject to the existing extraction and quotation review rules.',
    ))
    if len(result) > MAX_PREVIEW_CHARACTERS:
        raise ValueError('The complete source information is too large to display.')
    return result


def _set_complete(control, text):
    control.SetValue(text)
    if control.GetValue().replace('\r\n', '\n') != text.replace('\r\n', '\n'):
        control.SetValue('')
        raise ValueError('The complete preview could not be displayed; no action was enabled.')
    control.SetInsertionPoint(0)


class AcquisitionDialog(wx.Dialog):
    def __init__(self, frame, workspace, collections=(), *, collection=None,
                 selected_metadata=None, selected_panel=None, selected_selection=None):
        super().__init__(frame, title='Find a source by DOI or ISBN', size=(820, 720))
        self.frame, self.library, self.workspace = frame, frame.library, workspace
        self.host_scope = getattr(frame, 'active', None)
        self.selected_metadata, self.selected_panel = selected_metadata, selected_panel
        self.selected_selection = selected_selection
        self.preview = None
        self.pending = self._closed = self._close_when_idle = False
        self.changed = False
        self._request = self._generation = 0
        self._online_allowed = False
        self._network_job = False
        self._applied_token = None
        self._identifier_kind = 'doi'
        rows = _collection_choices(collections)
        self.collection_ids = [None, *(row[0] for row in rows)]
        if collection not in self.collection_ids:
            raise ValueError('The selected collection changed. Open identifier lookup again.')

        layout = wx.BoxSizer(wx.VERTICAL)
        explanation = wx.StaticText(self, label=(
            'Look up a DOI or ISBN, review metadata, then choose whether to save a reference,\n'
            'update the selected book, or download a source you have permission to use.'))
        layout.Add(explanation, 0, wx.EXPAND | wx.ALL, 8)
        kind_row = wx.BoxSizer(wx.HORIZONTAL)
        kind_row.Add(wx.StaticText(self, label='Identifier type'), 0, wx.ALL, 4)
        self.identifier_kind = wx.Choice(self, choices=['DOI', 'ISBN'])
        self.identifier_kind.SetName('Identifier type: DOI or ISBN')
        self.identifier_kind.SetSelection(0)
        self.identifier_kind.Bind(wx.EVT_CHOICE, self.on_identifier_kind_changed)
        kind_row.Add(self.identifier_kind, 0, wx.ALL, 4)
        layout.Add(kind_row, 0, wx.LEFT | wx.RIGHT, 4)
        self.identifier_label = wx.StaticText(self, label='DOI or https://doi.org/ link')
        layout.Add(self.identifier_label, 0, wx.LEFT | wx.RIGHT, 8)
        self.doi = wx.TextCtrl(self)
        self.doi.SetName('DOI or DOI resolver link')
        self.doi.SetMaxLength(2100)
        self.doi.Bind(wx.EVT_TEXT, self.on_request_changed)
        layout.Add(self.doi, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.online = wx.CheckBox(self, label='Allow online DOI lookup and source download for this dialog')
        self.online.SetName('Allow online DOI lookup and source download for this dialog')
        self.online.SetValue(False)
        self.online.Bind(wx.EVT_CHECKBOX, self.on_online_changed)
        layout.Add(self.online, 0, wx.EXPAND | wx.ALL, 8)
        self.network_notice = wx.StaticText(self, label=(
            'Only the entered DOI and, for Unpaywall, your email are sent to the selected providers.\n'
            'Private documents, notes and saved quotations are never uploaded.'))
        layout.Add(self.network_notice, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        provider_row = wx.BoxSizer(wx.HORIZONTAL)
        self.providers = {}
        for identity, label in PROVIDERS:
            control = wx.CheckBox(self, label=label)
            control.SetName('Query ' + label)
            control.SetValue(identity == 'crossref')
            control.Bind(wx.EVT_CHECKBOX, self.on_request_changed)
            self.providers[identity] = control
            provider_row.Add(control, 0, wx.ALL, 4)
        layout.Add(provider_row, 0, wx.LEFT | wx.RIGHT, 4)
        layout.Add(wx.StaticText(self, label='Your email for Unpaywall; used only when selected'), 0,
                   wx.LEFT | wx.RIGHT, 8)
        self.email = wx.TextCtrl(self)
        self.email.SetName('Your email for Unpaywall')
        self.email.SetMaxLength(320)
        self.email.Bind(wx.EVT_TEXT, self.on_request_changed)
        layout.Add(self.email, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        self.lookup_button = wx.Button(self, label='&Look up DOI')
        self.lookup_button.Bind(wx.EVT_BUTTON, self.lookup)
        layout.Add(self.lookup_button, 0, wx.EXPAND | wx.ALL, 8)

        self.tabs = wx.Notebook(self)
        metadata_page, source_page = wx.Panel(self.tabs), wx.Panel(self.tabs)
        self.tabs.AddPage(metadata_page, 'Metadata preview')
        self.tabs.AddPage(source_page, 'Download source')
        metadata_layout = wx.BoxSizer(wx.VERTICAL)
        self.metadata_preview = wx.TextCtrl(metadata_page, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.metadata_preview.SetName('Complete DOI metadata and provider provenance preview')
        self.metadata_preview.SetMaxLength(MAX_PREVIEW_CHARACTERS)
        metadata_layout.Add(self.metadata_preview, 1, wx.EXPAND | wx.ALL, 6)
        self.save_button = wx.Button(metadata_page, label='&Save bibliographic reference')
        self.save_button.Bind(wx.EVT_BUTTON, self.save_reference)
        metadata_layout.Add(self.save_button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        selected_label = (selected_metadata.title or 'Untitled book') if selected_metadata else 'No book selected'
        selected = wx.TextCtrl(metadata_page, value='Metadata update target: ' + selected_label,
                               style=wx.TE_READONLY)
        selected.SetName('Metadata update target')
        metadata_layout.Add(selected, 0, wx.EXPAND | wx.ALL, 6)
        self.apply_button = wx.Button(metadata_page, label='&Apply metadata to selected book…')
        self.apply_button.Bind(wx.EVT_BUTTON, self.apply_metadata)
        metadata_layout.Add(self.apply_button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        metadata_page.SetSizer(metadata_layout)

        source_layout = wx.BoxSizer(wx.VERTICAL)
        source_layout.Add(wx.StaticText(source_page, label='Provider-reported source location'), 0,
                          wx.LEFT | wx.RIGHT | wx.TOP, 6)
        self.sources = wx.Choice(source_page, choices=[])
        self.sources.SetName('Provider-reported source location')
        self.sources.Bind(wx.EVT_CHOICE, self.on_source_changed)
        source_layout.Add(self.sources, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        self.source_preview = wx.TextCtrl(source_page, style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.source_preview.SetName('Source URL, version, license and access claims')
        self.source_preview.SetMaxLength(MAX_PREVIEW_CHARACTERS)
        source_layout.Add(self.source_preview, 1, wx.EXPAND | wx.ALL, 6)
        source_layout.Add(wx.StaticText(source_page, label='Collection for the imported source'), 0,
                          wx.LEFT | wx.RIGHT, 6)
        self.collection = wx.Choice(source_page, choices=[
            'Current library; no collection', *(name for _, name in rows)])
        self.collection.SetName('Collection for the imported source')
        self.collection.SetSelection(self.collection_ids.index(collection))
        self.collection.Bind(wx.EVT_CHOICE, self.on_destination_scope_changed)
        source_layout.Add(self.collection, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        self.rights = wx.CheckBox(source_page, label='I have permission to download this selected source')
        self.rights.SetName('I have permission to download this selected source')
        self.rights.SetValue(False)
        self.rights.Bind(wx.EVT_CHECKBOX, self.on_rights_changed)
        source_layout.Add(self.rights, 0, wx.EXPAND | wx.ALL, 6)
        self.download_button = wx.Button(source_page, label='&Download selected PDF and import…')
        self.download_button.Bind(wx.EVT_BUTTON, self.download_source)
        source_layout.Add(self.download_button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        source_page.SetSizer(source_layout)
        layout.Add(self.tabs, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self.status = wx.TextCtrl(self, value=(
            'Offline. Enter a DOI and explicitly enable online access to look it up.'),
            style=wx.TE_READONLY | wx.TE_MULTILINE)
        self.status.SetName('DOI acquisition status')
        self.status.SetMinSize((-1, 64))
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        footer = wx.BoxSizer(wx.HORIZONTAL)
        self.cancel_button = wx.Button(self, label='Cancel &operation')
        self.cancel_button.Bind(wx.EVT_BUTTON, self.cancel_operation)
        self.close_button = wx.Button(self, wx.ID_CANCEL, '&Close')
        self.close_button.Bind(wx.EVT_BUTTON, self.close)
        footer.Add(self.cancel_button, 0, wx.ALL, 6)
        footer.Add(self.close_button, 0, wx.ALL, 6)
        layout.Add(footer, 0, wx.ALIGN_RIGHT)
        self.SetSizer(layout)
        self.SetMinSize((680, 600))
        self.SetEscapeId(wx.ID_CANCEL)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self._enabled()
        self.doi.SetFocus()

    def _same_host(self):
        try:
            return (self.frame.library is self.library and _workspace(self.frame) == self.workspace
                    and getattr(self.frame, 'active', None) == self.host_scope
                    and not getattr(self.frame, '_closing', False))
        except (AttributeError, ValueError, RuntimeError):
            return False

    def _same_selected_book(self):
        if self.selected_metadata is None:
            return False
        if self.selected_panel is None:
            return True
        try:
            return (self.selected_panel.workspace == self.workspace and self.selected_panel.ready
                    and self.frame.book.GetPage(1) is self.selected_panel
                    and self.selected_panel._selection() == self.selected_selection)
        except (AttributeError, IndexError, ValueError, RuntimeError):
            return False

    def _current_scope(self):
        """Reauthorize UI publication without querying SQLite in cancel polling."""
        if not self._same_host():
            return False
        try:
            with self.library.read(self.workspace) as db:
                if self.host_scope is not None and self.host_scope[1] is not None:
                    TopicTree(db, self.workspace)._visible(self.host_scope[1])
            return True
        except (OSError, ValueError, sqlite3.Error):
            return False

    def _enabled(self):
        idle = not self.pending and not self._closed
        for control in (self.identifier_kind, self.doi, self.collection, self.sources):
            control.Enable(idle)
        for control in self.providers.values():
            control.Enable(idle and self._identifier_kind == 'doi')
        self.email.Enable(idle and self._identifier_kind == 'doi' and self.providers['unpaywall'].GetValue())
        # Consent can be revoked even during a network request.
        self.online.Enable(not self._closed)
        self.lookup_button.Enable(idle)
        self.save_button.Enable(idle and self.preview is not None)
        self.apply_button.Enable(idle and self.preview is not None and self.selected_metadata is not None
                                 and self._applied_token is None)
        source = self._selected_source(required=False)
        self.rights.Enable(idle and _downloadable(source))
        self.download_button.Enable(idle and _downloadable(source)
                                    and bool(self.online.GetValue()) and bool(self.rights.GetValue()))
        self.cancel_button.Enable(self.pending)
        self.close_button.Enable(not self._closed)

    def _message(self, message):
        self.status.SetValue(message)
        self.status.SetFocus()

    def _clear_preview(self):
        self.preview = None
        self.metadata_preview.SetValue('')
        self.sources.Set([])
        self.source_preview.SetValue('')
        self.rights.SetValue(False)


    def _selected_kind(self):
        index = self.identifier_kind.GetSelection()
        if index not in (0, 1):
            raise ValueError('Choose DOI or ISBN as the identifier type.')
        return ('doi', 'isbn')[index]

    def on_identifier_kind_changed(self, event=None):
        try:
            kind = self._selected_kind()
        except ValueError as exc:
            self.on_request_changed()
            self._message(str(exc))
            return
        if kind == self._identifier_kind:
            return
        self._identifier_kind = kind
        self._online_allowed = False
        self.online.SetValue(False)
        if kind == 'isbn':
            self.identifier_label.SetLabel('ISBN-10 or ISBN-13; hyphens and spaces are allowed')
            self.doi.SetName('ISBN-10 or ISBN-13')
            consent = 'Allow online ISBN metadata lookup from Open Library for this dialog'
            self.network_notice.SetLabel(
                'Only the entered ISBN is sent to Open Library. DOI providers and email are not used.\n'
                'This returns book metadata; it does not establish download rights or upload private text.')
        else:
            self.identifier_label.SetLabel('DOI or https://doi.org/ link')
            self.doi.SetName('DOI or DOI resolver link')
            consent = 'Allow online DOI lookup and source download for this dialog'
            self.network_notice.SetLabel(
                'Only the entered DOI and, for Unpaywall, your email are sent to the selected providers.\n'
                'Private documents, notes and saved quotations are never uploaded.')
        self.online.SetLabel(consent)
        self.online.SetName(consent)
        self.lookup_button.SetLabel('&Look up ' + kind.upper())
        self.metadata_preview.SetName(f'Complete {kind.upper()} metadata and provider provenance preview')
        self.status.SetName(kind.upper() + ' acquisition status')
        self.doi.SetValue('')
        self.on_request_changed()
        self.status.SetValue(f'Offline. Enter the {kind.upper()} and explicitly enable its online lookup.')
        self.Layout()

    def on_request_changed(self, event=None):
        self._generation += 1
        self._clear_preview()
        if self.pending:
            self.frame.cancel_event.set()
        self._enabled()

    def on_online_changed(self, event=None):
        self._online_allowed = bool(self.online.GetValue())
        if not self._online_allowed and self.pending and self._network_job:
            self.cancel_operation()
        elif not self._online_allowed:
            self.status.SetValue('Offline. Existing metadata may be saved locally; online lookup and downloads are disabled.')
        self._enabled()

    def on_destination_scope_changed(self, event=None):
        if self.pending:
            self.frame.cancel_event.set()

    def on_rights_changed(self, event=None):
        if self.pending and not self.rights.GetValue():
            self.frame.cancel_event.set()
        self._enabled()

    def _selected_source(self, *, required=True):
        index = self.sources.GetSelection()
        if self.preview is not None and 0 <= index < len(self.preview.sources):
            return self.preview.sources[index]
        if required:
            raise ValueError('Choose a source location from the completed preview.')
        return None

    def on_source_changed(self, event=None):
        self.rights.SetValue(False)
        if self.pending:
            self.frame.cancel_event.set()
        try:
            source = self._selected_source()
            _set_complete(self.source_preview, source_text(source))
        except Exception as exc:
            self.sources.SetSelection(-1)
            self.source_preview.SetValue('')
            self._message(str(exc))
        self._enabled()

    def _available(self):
        if self._closed:
            return False
        if self.pending or self.frame.working or getattr(self.frame, 'busy', False):
            self._message('An operation is running. Wait or choose Cancel operation.')
            return False
        if not self._current_scope():
            self._clear_preview()
            self._enabled()
            self._message('The selected library or research topic changed. Close identifier lookup and open it again.')
            return False
        return True

    def _require_preview(self):
        if (self.preview is None or self.preview.workspace != self.workspace or
                _preview_identity(self.preview)[0] != self._selected_kind()):
            raise ValueError('Look up the selected identifier again before using its metadata or source locations.')
        return self.preview

    def _start(self, title, operation, complete, *, network=False, mutation=False):
        if not self._available():
            return False
        self._request += 1
        request, generation = self._request, self._generation
        self.pending, self._network_job = True, network
        self._enabled()
        self._message(title + '…')

        def cancelled():
            return (self._closed or self.frame.cancel_event.is_set() or not self._same_host()
                    or request != self._request or generation != self._generation
                    or (network and not self._online_allowed))

        def work():
            if cancelled():
                raise InterruptedError('Operation cancelled before completion.')
            value = operation(cancelled)
            if not mutation and cancelled():
                raise InterruptedError('Operation cancelled; the result was not displayed.')
            return value

        def finish(result=None, error=None):
            if self._closed or request != self._request:
                return
            self.pending = self._network_job = False
            if error is None and not mutation and cancelled():
                error = InterruptedError('Operation cancelled; the result was not displayed.')
            if mutation and error is None:
                self.changed = True
            if not self._current_scope() or generation != self._generation:
                self._clear_preview()
                self._message('The library, topic or identifier request changed. Open lookup again before another action.')
            elif error is not None:
                self._message(f'{title} was not completed: {error}')
            else:
                try:
                    complete(result)
                except Exception as exc:
                    self._message(f'{title} could not be displayed: {exc}')
            self._enabled()
            if self._close_when_idle:
                self._closed = True
                self.EndModal(wx.ID_CANCEL)

        try:
            self.frame.run_job(title, work, lambda value: finish(result=value),
                               lambda error: finish(error=error))
            if self.pending and not self.frame.working:
                finish(error=RuntimeError('The operation could not be started. Try again.'))
        except Exception as exc:
            finish(error=exc)
            return False
        return True

    def lookup(self, event=None):
        if not self._available():
            return False
        self._online_allowed = bool(self.online.GetValue())
        try:
            kind = self._selected_kind()
            if not self._online_allowed:
                raise ValueError(f'Enable online access explicitly before looking up the {kind.upper()}.')
            identifier = self.doi.GetValue().strip()
            if not identifier or len(identifier) > 2100:
                raise ValueError('Enter an ISBN-10 or ISBN-13.' if kind == 'isbn' else 'Enter a DOI or DOI resolver link.')
            providers, email = (), ''
            if kind == 'doi':
                providers = tuple(identity for identity, _ in PROVIDERS if self.providers[identity].GetValue())
                if not providers:
                    raise ValueError('Choose at least one metadata provider.')
                email = self.email.GetValue().strip() if 'unpaywall' in providers else ''
                if 'unpaywall' in providers and not email:
                    raise ValueError('Enter your email to query Unpaywall, or deselect Unpaywall.')
        except ValueError as exc:
            self._message(str(exc))
            return False
        self._clear_preview()

        def show(preview):
            if preview.workspace != self.workspace:
                raise ValueError('The lookup returned a preview for another library.')
            if _preview_identity(preview)[0] != kind:
                raise ValueError('The lookup returned a different identifier type. Review the request again.')
            rendered = metadata_text(preview)
            _set_complete(self.metadata_preview, rendered)
            self.preview = preview
            self.sources.Set([f'{index}. {source.provider} — {source.format.upper()} — {source.host}'
                              for index, source in enumerate(preview.sources, 1)])
            if preview.sources:
                self.sources.SetSelection(0)
                self.on_source_changed()
            else:
                self.source_preview.SetValue('No source locations were reported by the selected providers.')
            self.tabs.SetSelection(0)
            self.status.SetValue(f'Complete {kind.upper()} proposal loaded. Review it before saving metadata or downloading a source.')
            self.metadata_preview.SetFocus()

        def request(cancelled):
            if kind == 'isbn':
                return self.library.lookup_isbn(self.workspace, identifier, online_allowed=True, cancelled=cancelled)
            return self.library.lookup_doi(self.workspace, identifier, online_allowed=True,
                providers=providers, unpaywall_email=email, cancelled=cancelled)

        return self._start('Looking up ' + kind.upper(), request, show, network=True)

    def save_reference(self, event=None):
        if not self._available():
            return False
        try:
            preview = self._require_preview()
        except ValueError as exc:
            self._message(str(exc))
            return False

        def saved(reference):
            kind, identifier = _preview_identity(preview)
            self._message(f'Bibliographic reference saved or reused for {kind.upper()} {identifier}.\n'
                          f'Reference ID: {reference.id}\nNo document was downloaded.')

        return self._start('Saving bibliographic reference', lambda cancelled:
            self.library.save_doi_reference(self.workspace, preview, cancelled=cancelled),
            saved, mutation=True)

    def apply_metadata(self, event=None):
        if not self._available():
            return False
        try:
            preview = self._require_preview()
            if not self._same_selected_book():
                raise ValueError('The selected book changed. Close identifier lookup, select a book and open it again.')
            if self._applied_token is not None:
                raise ValueError('Metadata was already applied in this dialog. Close it and reopen lookup to review another update.')
            current = self.selected_metadata
            proposed_title = preview.metadata.title or current.title
            kind, identifier = _preview_identity(preview)
            message = (f'Apply {kind.upper()} {identifier} metadata to this exact selected book?\n\n'
                       f'Current title: {_value(current.title)}\n'
                       f'Proposed title: {_value(proposed_title)}\n'
                       + ('The provider supplied no title; the current title is preserved.\n' if not preview.metadata.title else '') +
                       'Proposed authors in order: ' + (', '.join(preview.metadata.authors) or 'Not supplied; current authors are preserved') + '\n'
                       f'Language: {preview.metadata.language or "Current language is preserved"}\n'
                       f'Volume: {preview.metadata.volume or "Current volume is preserved"}\n'
                       'The current edition label is preserved.\n\n'
                       'Title and authors are shared by editions of the same work.\n'
                       'Original source bytes and previously saved quotations are preserved.')
            dialog = wx.MessageDialog(self, message, f'Apply {kind.upper()} metadata',
                                      style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
            try:
                approved = dialog.ShowModal() == wx.ID_YES
            finally:
                dialog.Destroy()
            if not approved:
                self.status.SetValue('Metadata update cancelled. The book was not changed.')
                self.apply_button.SetFocus()
                return False
            if not self._same_host() or not self._same_selected_book() or self.preview is not preview:
                raise ValueError('The selected book or metadata proposal changed. Review it again.')
        except Exception as exc:
            self._message(str(exc))
            return False

        def applied(result):
            self._applied_token = preview.token
            self._message(f'{kind.upper()} metadata applied to the selected book. Original source bytes and saved quotations were preserved. '
                          'Close this dialog to refresh the catalog.')

        return self._start(f'Applying {kind.upper()} metadata', lambda cancelled:
            self.library.apply_doi_metadata(self.workspace, current.edition_id, preview,
                                           expected_metadata=current, cancelled=cancelled),
            applied, mutation=True)

    def _choose_destination(self):
        folder = wx.DirDialog(self, 'Choose the parent folder for a new source download folder',
                              style=wx.DD_DIR_MUST_EXIST)
        try:
            if folder.ShowModal() != wx.ID_OK:
                return None
            parent = Path(folder.GetPath())
        finally:
            folder.Destroy()
        name_dialog = wx.TextEntryDialog(self,
            'Name of a new folder inside the selected parent. Existing folders are never overwritten.',
            'New source download folder', value='doi-source-' + uuid4().hex[:8])
        try:
            if name_dialog.ShowModal() != wx.ID_OK:
                return None
            name = name_dialog.GetValue()
        finally:
            name_dialog.Destroy()
        if (not name.strip() or name in ('.', '..') or len(name) > 120 or
                any(character in name for character in '<>:"/\\|?*\x00') or
                any(ord(character) < 32 for character in name) or name.endswith(('.', ' '))):
            raise ValueError('Use a new folder name of 1 to 120 characters without path separators or trailing dots.')
        return parent / name

    def download_source(self, event=None):
        if not self._available():
            return False
        self._online_allowed = bool(self.online.GetValue())
        try:
            preview = self._require_preview()
            source = self._selected_source()
            index = self.sources.GetSelection()
            if not self._online_allowed:
                raise ValueError('Enable online access explicitly before downloading a source.')
            if not _downloadable(source):
                raise ValueError('Automatic download requires a provider-reported open-access PDF. '
                                 'This location is available for metadata review only.')
            if not self.rights.GetValue():
                raise ValueError('Review the license and confirm your permission to download this selected source.')
            selection = self.collection.GetSelection()
            if not 0 <= selection < len(self.collection_ids):
                raise ValueError('Choose the current library or an active collection for the import.')
            collection = self.collection_ids[selection]
            destination = self._choose_destination()
            if destination is None:
                self.status.SetValue('Download cancelled. No source was requested or imported.')
                self.download_button.SetFocus()
                return False
            if (not self._same_host() or self.preview is not preview or
                    self.sources.GetSelection() != index or self.collection.GetSelection() != selection or
                    not self.online.GetValue() or not self.rights.GetValue()):
                raise ValueError('The source, destination scope or consent changed. Review the download again.')
        except Exception as exc:
            self._message(str(exc))
            return False

        def acquired(result):
            self._message('\n'.join((f'Import state: {result.state}', _value(result.message),
                f'Downloaded source: {result.path}', f'Provenance receipt: {result.receipt_path}',
                f'File SHA-256: {result.sha256}', f'Reference ID: {_value(result.reference_id)}',
                f'Edition ID: {_value(result.edition_id)}',
                'Use Import Center to inspect pending or quarantined imports.')))

        return self._start('Downloading and importing selected PDF', lambda cancelled:
            self.library.acquire_doi_source(self.workspace, preview, index, destination,
                online_allowed=True, rights_confirmed=True, collection=collection, cancelled=cancelled),
            acquired, network=True, mutation=True)

    def cancel_operation(self, event=None):
        if self.pending:
            self.frame.cancel_event.set()
            self._message('Cancellation requested. Waiting for the current operation to stop.')

    def close(self, event=None):
        if self.pending:
            self._close_when_idle = True
            self.cancel_operation()
            return
        self._closed = True
        self.EndModal(wx.ID_CANCEL)

    def on_close(self, event):
        if self.pending and event.CanVeto():
            event.Veto()
            self.close()
            return
        self.dispose()
        event.Skip()

    def dispose(self):
        self._closed = True
        if self.pending:
            self.frame.cancel_event.set()


def show_acquisition_dialog(frame):
    """Open identifier lookup without networking and retain the Books scope."""
    dialog = panel = library = workspace = selection = None
    try:
        if frame.working or getattr(frame, 'busy', False) or getattr(frame, '_closing', False):
            raise ValueError('Wait for the current operation before opening identifier lookup.')
        workspace, library = _workspace(frame), frame.library
        collections = _collection_choices(library.collections(workspace))
        collection = current = None
        if frame.book is not None:
            candidate = frame.book.GetPage(1)
            if candidate.workspace == workspace and candidate.ready:
                selection = candidate._selection()
                if selection[0] in {identity for identity, _ in collections}:
                    collection = selection[0]
                if selection[2] is not None:
                    current = library.edition_metadata(workspace, selection[2])
                    panel = candidate
        if frame.library is not library or _workspace(frame) != workspace:
            raise ValueError('The selected library changed. Open identifier lookup again.')
        dialog = AcquisitionDialog(frame, workspace, collections, collection=collection,
                                   selected_metadata=current, selected_panel=panel,
                                   selected_selection=selection)
        dialog.ShowModal()
        if dialog.changed and dialog._current_scope() and not frame.working and frame.book is not None:
            catalog = frame.book.GetPage(1)
            if catalog.workspace == workspace:
                catalog.refresh(selection=selection)
    except Exception as exc:
        frame.report(f'Identifier lookup could not be completed: {exc}')
    finally:
        if dialog is not None:
            dialog.dispose()
            dialog.Destroy()
        if library is not None and frame.library is library and not frame.working and not getattr(frame, '_closing', False):
            try:
                if _workspace(frame) == workspace:
                    frame.status.SetFocus()
            except (AttributeError, ValueError, RuntimeError):
                pass

