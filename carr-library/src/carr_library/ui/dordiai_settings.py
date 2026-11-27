"""Native, explicit connection controls for the optional local voice bridge."""
from ..speech import dordiai
from ..speech.dordiai_settings import default_settings, load_settings, save_settings


def connect_runtime(root, runtime_directory, *, python_executable=None, expected_worker_sha256=None):
    """Validate the selected worker bytes and save; never run a metadata probe."""
    value = default_settings()
    value.update(enabled=True, runtime_directory=runtime_directory,
                 python_executable=python_executable or None)
    if expected_worker_sha256 is not None:
        value['expected_worker_sha256'] = expected_worker_sha256
    value = dordiai.validate_connection(value)
    saved = save_settings(root, value)
    dordiai.cancel_active(root)
    return saved


def disable_runtime(root):
    """Disable even when the selected runtime is missing, without touching it."""
    # Stop active work even when persisting this preference is blocked.
    dordiai.cancel_active(root)
    value = load_settings(root)
    value['enabled'] = False
    saved = save_settings(root, value)
    dordiai.cancel_active(root)
    return saved


def show_dordiai_settings(frame):
    import wx

    root = frame.library.root
    try:
        value = load_settings(root)
    except (OSError, ValueError, RuntimeError) as exc:
        wx.MessageBox(str(exc), 'Local voice settings', wx.OK | wx.ICON_ERROR, parent=frame)
        return
    dialog = wx.Dialog(frame, title='Connect or disable dordiai-voices', size=(790, 590),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    introduction = wx.StaticText(dialog, label=(
        'Connect a separately installed, reviewed dordiai-voices worker for this library.\n'
        'Connection checks local paths and worker identity. Voice > Installed local voices\n'
        'then checks installed resources in the selected interpreter. No models are downloaded.'))
    layout.Add(introduction, 0, wx.ALL | wx.EXPAND, 10)

    def field(label, text, browse=None):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        row = wx.BoxSizer(wx.HORIZONTAL)
        control = wx.TextCtrl(dialog, value=text or '')
        control.SetName(label.replace('&', ''))
        row.Add(control, 1, wx.RIGHT, 6)
        if browse is not None:
            button = wx.Button(dialog, label='Browse')
            button.SetName('Browse ' + label.replace('&', ''))
            button.Bind(wx.EVT_BUTTON, lambda event: browse(control))
            row.Add(button, 0)
        layout.Add(row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
        return control

    def choose_directory(control):
        picker = wx.DirDialog(dialog, 'Select the installed kx47Runtime directory',
                              defaultPath=control.GetValue(), style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
        try:
            if picker.ShowModal() == wx.ID_OK:
                control.ChangeValue(picker.GetPath())
        finally:
            picker.Destroy()

    def choose_python(control):
        picker = wx.FileDialog(dialog, 'Select its private Python executable',
                               wildcard='Executable files|*.exe;python*|All files|*',
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() == wx.ID_OK:
                control.ChangeValue(picker.GetPath())
        finally:
            picker.Destroy()

    runtime = field('Installed &runtime directory (kx47Runtime)', value['runtime_directory'], choose_directory)
    executable = field('&Private Python executable (blank uses runtime/python/python.exe)',
                       value['python_executable'], choose_python)
    identity = field('Reviewed worker &SHA256', value['expected_worker_sha256'])
    layout.Add(wx.StaticText(dialog, label=(
        'The fingerprint must match the worker whose protocol you reviewed.\n'
        'Available resources do not establish pronunciation or voice quality.\n'
        'A restored backup keeps this connection disabled until you connect it again.')),
        0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
    status = wx.TextCtrl(dialog, value=('Enabled for this library.' if value['enabled']
                                       else 'Disabled for this library.'),
                         style=wx.TE_MULTILINE | wx.TE_READONLY)
    status.SetName('Local voice connection status')
    layout.Add(status, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)
    buttons = wx.BoxSizer(wx.HORIZONTAL)
    connect = wx.Button(dialog, label='&Connect selected runtime')
    connect.SetName('Connect selected dordiai-voices runtime')
    disable = wx.Button(dialog, label='&Disable')
    disable.SetName('Disable dordiai-voices for this library')
    close = wx.Button(dialog, id=wx.ID_CANCEL, label='Close')
    for button in (connect, disable, close):
        buttons.Add(button, 0, wx.RIGHT, 8)
    layout.Add(buttons, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

    def apply(event):
        try:
            connect_runtime(root, runtime.GetValue().strip(),
                            python_executable=executable.GetValue().strip() or None,
                            expected_worker_sha256=identity.GetValue().strip())
        except (OSError, ValueError, RuntimeError) as exc:
            status.ChangeValue('Connection was not saved: ' + str(exc))
        else:
            status.ChangeValue('Enabled for this library. Open Voice > Installed local voices to check the selected runtime resources. No voice quality test has been performed.')
        status.SetFocus()

    def turn_off(event):
        try:
            disable_runtime(root)
        except (OSError, ValueError, RuntimeError) as exc:
            status.ChangeValue('Active work was signalled to stop, but the disabled preference could not be saved: ' + str(exc))
        else:
            status.ChangeValue('Disabled for this library. Active dordiai-voices work has been signalled to stop; completed audio remains available.')
        status.SetFocus()

    connect.Bind(wx.EVT_BUTTON, apply)
    disable.Bind(wx.EVT_BUTTON, turn_off)
    dialog.SetSizer(layout)
    dialog.SetMinSize((650, 530))
    runtime.SetFocus()
    try:
        dialog.ShowModal()
    finally:
        dialog.Destroy()
