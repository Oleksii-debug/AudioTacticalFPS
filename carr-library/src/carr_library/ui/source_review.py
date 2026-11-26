"""Optional, accessible recording of an independent PDF text review.

The dialog never approves extraction merely because it is readable. It binds
the displayed full page to one existing search selection and delegates writes
to LibraryApp. Native controls are loaded only when the desktop installs the
actions, so the service boundary can also be checked without a GUI.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256

from ..reader.source_file import prepare_source_file, launch_source_file
from ..research.topic_tree import TopicTree


REVIEW_GUIDANCE = (
    'This optional action records an independent human comparison. You can read '
    'the extracted text without approving it. Compare the complete text below '
    'with the original PDF page before recording a review. Reading the same OCR '
    'through another program, or agreement between parsers, is not an independent '
    'comparison. For an image-only original, a reviewer able to inspect that '
    'image is needed. Leave the page unverified if that comparison is unavailable '
    'or any text differs. Open original gives the reader a separate exact viewing '
    'copy; editing that copy does not change the retained source. This action '
    'does not verify printed page numbering.'
)


def _reviewer(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 512
            or any(ord(char) < 32 for char in value)):
        raise ValueError('Enter the independent reviewer\'s name or reference.')
    return value.strip()


@dataclass(frozen=True)
class ReviewPage:
    identity: object
    locator: object
    raw_sha256: str
    raw_text: str
    preview: str
    size: int
    review: tuple | None

    @property
    def state(self):
        if self.review is None:
            return 'unverified'
        return 'reviewed' if self.review[-1] == 1 else 'revoked'

    @property
    def status(self):
        if self.state == 'reviewed':
            return ('Independent text review recorded by ' + self.review[-2]
                    + '. Use Revoke selected PDF page review to withdraw it.')
        if self.state == 'revoked':
            return ('The earlier review was revoked. This page remains unverified; '
                    'the old approval cannot be restored here.')
        return 'Unverified. No independent text review is recorded.'


class PdfReviewSession:
    """Bind human confirmation to the exact displayed source and review state."""

    def __init__(self, library, workspace, reference, revision, *, topic=None,
                 revocation_only=False):
        self.library = library
        self.workspace, self.reference, self.revision = workspace, reference, revision
        self.topic = topic
        self._revocation_only = revocation_only
        self._opened_source = None
        self.page = self._read_page(revoking=revocation_only)

    def _visible_topic(self, db):
        if self.topic is not None:
            TopicTree(db, self.workspace)._visible(self.topic)

    def _read_page(self, *, revoking=False):
        preview = None
        if not revoking:
            preview = self.library.preview(self.workspace, self.reference, self.revision,
                                           authorized_topic=self.topic)
        with self.library.read(self.workspace) as db:
            self._visible_topic(db)
            if revoking:
                # Withdrawal uses only the scoped immutable record. It cannot
                # turn a missing or damaged original into an authentic source.
                _, identity, locator, raw, metadata = self.library._stored_selection(
                    db, self.workspace, self.library._selections.get(self.reference), self.revision)
            else:
                _, identity, locator, raw, metadata = self.library._selected(
                    db, self.workspace, self.reference, self.revision)
            asset = db.execute(
                'SELECT a.mime,a.size FROM catalog_edition e JOIN catalog_asset a '
                'ON a.digest=e.asset WHERE e.workspace=? AND e.id=? AND e.asset=?',
                (self.workspace, identity.edition_id, identity.asset_sha256)).fetchone()
            if locator.kind != 'physical_page' or asset is None or asset[0] != 'application/pdf':
                raise ValueError('Select a physical PDF page in Search. TXT and DOCX '
                                 'sources cannot receive a PDF page review.')
            expected = (f'{metadata.title}\n{locator.display()}\nExtracted text; '
                        'verification status is shown in the search result.\n\n' + raw)
            if revoking:
                preview = expected
            elif preview != expected:
                raise ValueError('The source preview changed. Open its review again.')
            review = db.execute(
                'SELECT raw_sha256,render_sha256,renderer,reviewer,active '
                'FROM source_page_review WHERE workspace=? AND edition=? '
                'AND revision=? AND physical=?',
                (self.workspace, identity.edition_id, self.revision, locator.index)).fetchone()
            return ReviewPage(identity, locator, sha256(raw.encode('utf-8')).hexdigest(),
                              raw, preview, asset[1], tuple(review) if review is not None else None)

    def validate(self):
        if self._revocation_only:
            raise ValueError('This session is only for withdrawing an independent review.')
        if self._read_page() != self.page:
            raise ValueError('The selected source or its review status changed. '
                             'Open the review again; no change was saved.')

    def viewing_copy(self):
        """Reuse the shared service; external readers never receive originals."""
        self.validate()
        source = prepare_source_file(_ScopedSourceAccess(self), self.workspace,
                                     self.reference, self.revision)
        if (source.identity != self.page.identity or source.locator != self.page.locator
                or source.ref != self.reference or source.mime != 'application/pdf'):
            raise ValueError('The selected source changed. Open its review again.')
        return source

    def open_original(self, launcher):
        # The shared service independently verifies both the original and a
        # separate viewing copy immediately before calling the native opener.
        source = self.viewing_copy()
        launch_source_file(_ScopedSourceAccess(self), source, launcher)
        self._opened_source = source

    def approve(self, reviewer, *, confirmed=False, cancelled=lambda: False):
        if confirmed is not True:
            raise ValueError('Confirm that an independent comparison was completed.')
        reviewer = _reviewer(reviewer)
        if self.page.review is not None:
            raise ValueError('An existing or revoked review cannot be replaced here.')
        if cancelled():
            raise InterruptedError('Review cancelled; no change was saved.')
        self.validate()
        source = self._opened_source or self.viewing_copy()
        # Reuse the launch-boundary checks without opening another viewer.
        # An edited viewing copy must be rejected, not silently replaced after
        # the user has already confirmed comparison with that copy.
        launch_source_file(_ScopedSourceAccess(self), source, lambda _path: True)
        if cancelled():
            raise InterruptedError('Review cancelled; no change was saved.')
        self.library.review_pdf_page(self.workspace, self.reference, self.revision,
                                     reviewer, confirmed=True, cancelled=cancelled,
                                     authorized_topic=self.topic)

    def revoke(self, *, confirmed=False, cancelled=lambda: False):
        if confirmed is not True:
            raise ValueError('Confirm withdrawal of this independent text review.')
        if self._read_page(revoking=True) != self.page:
            raise ValueError('The selected source or its review status changed. '
                             'Open the review again; no change was saved.')
        if self.page.state != 'reviewed':
            raise ValueError('This page has no active review to revoke.')
        if cancelled():
            raise InterruptedError('Revocation cancelled; no change was saved.')
        # Revocation remains possible when an original is missing or damaged.
        self.library.revoke_pdf_review(self.workspace, self.page.identity.edition_id,
                                       self.revision, self.page.locator.index, cancelled=cancelled,
                                       authorized_topic=self.topic)


class _ScopedSourceAccess:
    """Apply the displayed topic boundary inside the shared file service reads."""

    def __init__(self, session):
        self.session = session

    def __getattr__(self, name):
        return getattr(self.session.library, name)

    @contextmanager
    def read(self, workspace=None):
        if workspace != self.session.workspace:
            raise ValueError('The selected source belongs to another library.')
        with self.session.library.read(workspace) as db:
            self.session._visible_topic(db)
            yield db


class ReviewDialog:
    """Native text-first dialog. Cancellation is the default action."""

    def __init__(self, wx, parent, session, guard):
        self.wx, self.session, self.guard = wx, session, guard
        self.decision = None
        self.dialog = wx.Dialog(parent, title='Independent PDF text review', size=(820, 720))
        layout = wx.BoxSizer(wx.VERTICAL)
        self.dialog.SetSizer(layout)
        guidance = wx.TextCtrl(self.dialog, value=REVIEW_GUIDANCE,
                               style=wx.TE_MULTILINE | wx.TE_READONLY)
        guidance.SetName('Independent review instructions')
        layout.Add(guidance, 1, wx.EXPAND | wx.ALL, 8)
        reader = wx.TextCtrl(self.dialog, value=session.page.preview,
                             style=wx.TE_MULTILINE | wx.TE_READONLY)
        reader.SetName('Full extracted PDF page text')
        reader.SetInsertionPoint(0)
        layout.Add(reader, 3, wx.EXPAND | wx.ALL, 8)
        original = wx.Button(self.dialog, label='&Open original PDF in the default reader')
        original.Bind(wx.EVT_BUTTON, self._open_original)
        layout.Add(original, 0, wx.ALL, 8)
        layout.Add(wx.StaticText(self.dialog, label='Independent reviewer\'s name or reference'),
                   0, wx.LEFT | wx.RIGHT, 8)
        self.reviewer = wx.TextCtrl(self.dialog, value='')
        self.reviewer.SetName('Independent reviewer\'s name or reference')
        self.reviewer.SetMaxLength(512)
        layout.Add(self.reviewer, 0, wx.EXPAND | wx.ALL, 8)
        self.confirmed = wx.CheckBox(self.dialog,
            label='Independent comparison completed; the full extracted text is exact')
        self.confirmed.SetName('Confirm completed independent comparison')
        self.confirmed.SetValue(False)
        layout.Add(self.confirmed, 0, wx.EXPAND | wx.ALL, 8)
        self.status = wx.TextCtrl(self.dialog, value=session.page.status, style=wx.TE_READONLY)
        self.status.SetName('Source review status')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.record = wx.Button(self.dialog, label='&Record independent text review')
        self.record.Bind(wx.EVT_BUTTON, self._accept)
        buttons.Add(self.record, 0, wx.ALL, 8)
        cancel = wx.Button(self.dialog, wx.ID_CANCEL, label='&Leave unchanged')
        cancel.SetDefault()
        buttons.Add(cancel, 0, wx.ALL, 8)
        layout.Add(buttons, 0, wx.ALL, 0)
        self.dialog.SetEscapeId(wx.ID_CANCEL)
        self.reviewer.Bind(wx.EVT_TEXT, self._update)
        self.confirmed.Bind(wx.EVT_CHECKBOX, self._update)
        if session.page.state != 'unverified':
            self.reviewer.Enable(False)
            self.confirmed.Enable(False)
        self._update()
        guidance.SetFocus()

    def _update(self, event=None):
        try:
            _reviewer(self.reviewer.GetValue())
            ready = self.confirmed.GetValue() is True and self.session.page.state == 'unverified'
        except ValueError:
            ready = False
        self.record.Enable(ready)

    def _error(self, error):
        self.status.SetValue(str(error))
        self.status.SetFocus()

    def _open_original(self, event=None):
        try:
            self.guard()
            self.confirmed.SetValue(False)
            self._update()
            self.session.open_original(self.wx.LaunchDefaultApplication)
            self.status.SetValue('Original opened. Compare physical PDF page '
                                 + str(self.session.page.locator.index) + ' with the full extracted text.')
        except Exception as exc:
            self._error(exc)

    def _accept(self, event=None):
        try:
            self.guard()
            self.session.validate()
            reviewer = _reviewer(self.reviewer.GetValue())
            if self.confirmed.GetValue() is not True or self.session.page.state != 'unverified':
                raise ValueError('An independent comparison must be confirmed before recording a review.')
            self.decision = reviewer
            self.dialog.EndModal(self.wx.ID_OK)
        except Exception as exc:
            self._error(exc)

    def show(self):
        try:
            return self.decision if self.dialog.ShowModal() == self.wx.ID_OK else None
        finally:
            self.dialog.Destroy()


@dataclass(frozen=True)
class _Selection:
    library: object
    panel: object
    scope: tuple
    workspace: str
    reference: str
    revision: str
    epoch: object


class SourceReviewActions:
    def __init__(self, frame, wx, *, dialog_factory=ReviewDialog):
        self.frame, self.wx, self.dialog_factory = frame, wx, dialog_factory

    def _capture(self):
        if self.frame.working or self.frame.book is None or self.frame.active is None:
            raise ValueError('Open Search and select a PDF result when the current operation is complete.')
        panel = self.frame.book.GetCurrentPage()
        if not callable(getattr(panel, 'selected', None)):
            raise ValueError('Open the Search tab and select a PDF result first.')
        hit = panel.selected()
        if hit.workspace != self.frame.active[0] or panel.model.workspace != hit.workspace:
            raise ValueError('The selected result belongs to a different library.')
        if getattr(panel, 'bound_topic', None) != self.frame.active[1]:
            raise ValueError('The selected result belongs to a different research topic.')
        return _Selection(self.frame.library, panel, self.frame.active, hit.workspace, hit.ref, hit.revision,
                          getattr(panel, '_context_epoch', None))

    def _guard(self, selected):
        if self.frame.library is not selected.library:
            raise ValueError('The library data location changed. Open the source review again.')
        if self._capture() != selected:
            raise ValueError('The selected result changed. Open its review again.')

    def _restore_focus(self, selected):
        try:
            self._guard(selected)
            selected.panel.grid.control.SetFocus()
        except Exception:
            self.frame.status.SetFocus()

    def _session(self, selected, *, revocation_only=False):
        return PdfReviewSession(selected.library, selected.workspace,
                                selected.reference, selected.revision, topic=selected.scope[1],
                                revocation_only=revocation_only)

    def _run(self, selected, operation):
        # This check reads only Python host state, not native controls, and is
        # safe on the worker. A queued action cannot write the previous root.
        if (self.frame.library is not selected.library or self.frame.active != selected.scope
                or getattr(selected.panel, '_context_epoch', None) != selected.epoch):
            raise ValueError('The library or topic changed before the operation started.')
        return operation()

    def _finished(self, selected, message):
        try:
            self._guard(selected)
            panel = selected.panel
            panel._context_epoch += 1
            panel.model.clear()
            panel.results.clear()
            panel.grid.refresh()
            panel.details.ChangeValue('')
            panel.status.ChangeValue(message)
        except (ValueError, RuntimeError):
            pass
        self.frame.report(message)

    def review(self, event=None):
        try:
            selected = self._capture()
            session = self._session(selected)
            dialog = self.dialog_factory(self.wx, self.frame, session, lambda: self._guard(selected))
            reviewer = dialog.show()
            if reviewer is None:
                self.frame.report('Review left unchanged. The extracted text remains available for reading.')
                self._restore_focus(selected)
                return
            self._guard(selected)
            self.frame.run_job('Recording independent PDF text review',
                lambda: self._run(selected, lambda: session.approve(reviewer, confirmed=True,
                                        cancelled=self.frame.cancel_event.is_set)),
                lambda _: self._finished(selected, 'Independent text review recorded. Search again to '
                    'load its current status. Printed page numbering was not approved.'))
        except Exception as exc:
            self.frame.report(f'Review not recorded: {exc}')

    def revoke(self, event=None):
        try:
            selected = self._capture()
            session = self._session(selected, revocation_only=True)
            if session.page.state != 'reviewed':
                raise ValueError('The selected page has no active review to revoke.')
            dialog = self.wx.MessageDialog(self.frame,
                f'Revoke the independent text review for physical PDF page {session.page.locator.index}?\n\n'
                'Its quotations will require review before copying or exporting. '
                'The existing approval cannot be reinstated here. Original text and research notes are kept.',
                'Revoke selected PDF page review', self.wx.YES_NO | self.wx.NO_DEFAULT | self.wx.ICON_WARNING)
            try:
                confirmed = dialog.ShowModal() == self.wx.ID_YES
            finally:
                dialog.Destroy()
            if not confirmed:
                self.frame.report('Review left unchanged.')
                self._restore_focus(selected)
                return
            self._guard(selected)
            self.frame.run_job('Revoking independent PDF text review',
                lambda: self._run(selected, lambda: session.revoke(confirmed=True,
                                        cancelled=self.frame.cancel_event.is_set)),
                lambda _: self._finished(selected, 'Independent text review revoked. Search again to '
                    'load its current status. Existing source text and notes were kept.'))
        except Exception as exc:
            self.frame.report(f'Review not revoked: {exc}')


def install_source_review_actions(frame):
    """Add keyboard-accessible Source actions to an already constructed frame."""
    existing = getattr(frame, 'source_review_actions', None)
    if existing is not None:
        return existing
    import wx
    actions = SourceReviewActions(frame, wx)
    menu = wx.Menu()
    for label, handler in [('&Review selected PDF page...', actions.review),
                           ('Re&voke selected PDF page review...', actions.revoke)]:
        item = menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, handler, item)
    frame.GetMenuBar().Append(menu, '&Source')
    frame.source_review_actions = actions
    return actions
