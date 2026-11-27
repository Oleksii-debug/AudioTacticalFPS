"""Explicit native setup for an existing Google Desktop refresh grant."""


def show_google_refresh(parent, frame, store, vault, profile, *, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    from ..assistant.google_oauth import refresh_credential, save_google_refresh
    from ..assistant.models import local_path
    import stat

    library, root = frame.library, frame.library.root

    def root_snapshot():
        selected = local_path(root)
        metadata = selected.stat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError('The selected library directory is unavailable.')
        return selected, (metadata.st_dev, metadata.st_ino)

    root_identity = root_snapshot()

    def current():
        if (getattr(frame, '_closing', False) or frame.library is not library
                or library.root != root or root_snapshot() != root_identity
                or store.directory != (root / 'state' / 'providers').resolve()):
            raise ValueError('The selected library or account changed. Reopen its connection settings.')
        selected_profile = store.get(profile.id)
        if selected_profile != profile or root_snapshot() != root_identity:
            raise ValueError('The selected library or account changed. Reopen its connection settings.')
        if profile.adapter != 'google-translation-v3':
            raise ValueError('Select an existing Google Advanced v3 account before configuring renewal.')

    current()
    form = wx.Dialog(parent, title='Automatic Google token renewal', size=(740, 570),
                     style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    box = wx.BoxSizer(wx.VERTICAL)
    notice = wx.StaticText(form, label=(
        'Use the client ID and refresh token from your already authorized Google Desktop app grant.\n'
        'No sign-in, browser, credential file or cloud service starts from this window.\n'
        'A later approved job may contact oauth2.googleapis.com to renew its access token.\n'
        'That exchange contains credentials only; document text goes only to the selected translation API.\n'
        'Replacing this credential preserves this account and its existing approved job references.'))
    notice.Wrap(690)
    box.Add(notice, 0, wx.ALL | wx.EXPAND, 8)

    def field(label, *, password=False):
        box.Add(wx.StaticText(form, label=label), 0, wx.ALL, 4)
        value = wx.TextCtrl(form, style=wx.TE_PASSWORD if password else 0)
        value.SetName(label)
        box.Add(value, 0, wx.ALL | wx.EXPAND, 4)
        return value

    client_id = field('Google Desktop app client ID')
    client_secret = field('Google Desktop app client secret; blank only if your client does not use one', password=True)
    refresh_token = field('Authorized Google refresh token', password=True)
    box.Add(form.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL, 8)
    form.SetSizer(box)
    try:
        if form.ShowModal() != wx.ID_OK:
            return None
        current()
        values = (client_id.GetValue(), client_secret.GetValue(), refresh_token.GetValue())
        refresh_credential(*values, profile.endpoint)
        if wx.MessageBox(
                'Store this explicit refresh credential in Windows Credential Manager for account '
                + profile.name + '?\n\nA later authorized job may renew a token at '
                'https://oauth2.googleapis.com/token. Existing approvals and budgets remain required. '
                'No request is made now.',
                'Replace Google credential for this account', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                form) != wx.YES:
            return None
        current()
        return save_google_refresh(store, vault, profile, *values, current=profile, guard=current)
    finally:
        client_secret.ChangeValue('')
        refresh_token.ChangeValue('')
        form.Destroy()
