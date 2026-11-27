"""Explicit legacy-queue review on the current Studio worker and native controls."""
from pathlib import Path

from ..audio import legacy_playlist as service


def review_dialog(wx, parent, description):
    """Named native controls keep a long review readable and keyboard reachable."""
    dialog = wx.Dialog(parent, title='Review previous playback queue',
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    text = wx.TextCtrl(dialog, value=description,
                       style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
    text.SetName('Previous playback queue review')
    layout.Add(text, 1, wx.EXPAND | wx.ALL, 12)
    buttons = wx.StdDialogButtonSizer()
    accept = wx.Button(dialog, wx.ID_OK, 'Import as new project')
    accept.SetName('Import as new project')
    cancel = wx.Button(dialog, wx.ID_CANCEL, 'Cancel queue import')
    cancel.SetName('Cancel queue import')
    buttons.AddButton(accept)
    buttons.AddButton(cancel)
    buttons.Realize()
    layout.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 12)
    dialog.SetSizer(layout)
    dialog.SetSize(dialog.FromDIP((640, 380)))
    dialog.SetMinSize(dialog.FromDIP((400, 260)))
    dialog.CentreOnParent()
    cancel.SetDefault()
    text.SetFocus()
    return dialog


class LegacyQueueAction:
    def __init__(self, owner):
        self.owner = owner
        self.last_result = None

    def _token(self):
        owner = self.owner
        return (owner.controller, owner.controller.project_id, owner.controller.project_path,
                getattr(getattr(owner, 'parent', None), 'library', None),
                Path(owner.controller.private_root))

    def _current(self, token):
        return not self.owner.closed and self._token() == token

    def _cancelled(self, token):
        if not self._current(token):
            raise InterruptedError('The current library or project changed during queue review.')
        return self.owner.cancelled.is_set()

    def start(self, event=None):
        owner = self.owner
        if owner.closed or owner.busy:
            return False
        token = self._token()
        return owner._run('Listing preserved playback queues',
            lambda: service.sources(token[4], cancelled=lambda: self._cancelled(token)),
            lambda choices: self._choose(token, choices), stop_playback=False)

    def _choose(self, token, choices):
        if not self._current(token):
            return
        wx, owner = self.owner.wx, self.owner
        labels = [row.label for row in choices] + ['Choose a canonical library folder…']
        chooser = wx.SingleChoiceDialog(owner.dialog,
            'Choose preserved queue data. This does not select an output device or start playback.',
            'Import previous playback queue', labels)
        try:
            if chooser.ShowModal() != wx.ID_OK:
                return
            selected = chooser.GetSelection()
        finally:
            chooser.Destroy()
        if not self._current(token):
            return
        if type(selected) is not int or not 0 <= selected <= len(choices):
            raise ValueError('Choose one listed queue or a canonical library folder.')
        options = dict(voice=owner.controller.voice, language=owner.controller.language,
                       cancelled=lambda: self._cancelled(token))
        if selected == len(choices):
            dialog = wx.DirDialog(owner.dialog, 'Choose the canonical library containing the saved queue',
                                  style=wx.DD_DIR_MUST_EXIST)
            try:
                if dialog.ShowModal() != wx.ID_OK:
                    return
                root = Path(dialog.GetPath())
            finally:
                dialog.Destroy()
            if not self._current(token):
                return
            action = lambda: service.review_root(token[4], root, **options)
        else:
            choice = choices[selected]
            if choice.kind == 'archive':
                action = lambda: service.review_archive(token[4], choice.identity, **options)
            elif choice.kind == 'current-root':
                action = lambda: service.review_root(token[4], token[4], **options)
            else:
                raise ValueError('Unsupported historical queue source.')
        owner._run('Verifying original queue recordings', action,
                   lambda review: self._confirm(token, review), stop_playback=False)

    def _confirm(self, token, review):
        if not self._current(token):
            return
        wx, owner = self.owner.wx, self.owner
        dialog = review_dialog(wx, owner.dialog, review.description())
        try:
            accepted = dialog.ShowModal() == wx.ID_OK
        finally:
            dialog.Destroy()
        if not accepted or not self._current(token):
            return
        def importing():
            try:
                result = service.import_reviewed(review, cancelled=lambda: self._cancelled(token))
            except service.QueueImportCommittedError as error:
                self.last_result = error.result
                raise
            # Keep the committed ID even when the owning window suppresses its
            # stale result callback after a close or library change.
            self.last_result = result
            return result
        owner._run('Importing the reviewed queue as a new project', importing,
            lambda result: self._complete(token, result),
            failed=lambda error: self._failed(token, error))

    def _failed(self, token, error):
        if isinstance(error, service.QueueImportCommittedError):
            self._complete(token, error.result, open_project=False)

    def _complete(self, token, result, *, open_project=True):
        # A committed identity survives a stale UI/library or a later reopen
        # failure. It is never converted into an instruction to import again.
        self.last_result = result
        message = (f'Playback queue saved as library project {result.project_id}. '
                   'Its original metadata remains preserved; playback is stopped. ')
        if not result.durability_confirmed:
            message += 'Final durability was not acknowledged. Reopen and verify this existing project before continuing. '
        if result.warning:
            message += result.warning + ' '
        if not self._current(token):
            report = getattr(getattr(self.owner, 'parent', None), 'report', None)
            if callable(report):
                report(message + 'The current library changed, so the project was not opened here.')
            return
        owner = self.owner
        if open_project and result.durability_confirmed and owner._unsaved():
            if not self._current(token):
                return
            if owner._stop_for_project_change():
                owner.controller.open(result.project_path)
                owner._refresh_text()
                owner.plan_text.ChangeValue('')
        owner._message(message + 'Choose Restore saved queue, a current output, and explicit Play or Resume.',
                       focus=True)


def install_legacy_queue_controls(owner, page, layout):
    action = LegacyQueueAction(owner)
    row = owner.wx.WrapSizer(owner.wx.HORIZONTAL)
    owner._button(page, row, 'Import previous playback queue', action.start)
    layout.Add(row, 0, owner.wx.ALL, 5)
    owner.legacy_queue_action = action
    return action
