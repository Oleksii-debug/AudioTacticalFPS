"""Native explicit connection controls using one canonical settings writer."""
from pathlib import Path

from ..speech import dordiai
from ..speech.dordiai_settings import default_settings, load_settings, save_settings
from .. import runtime_settings as engines


def _engine_settings(root):
    """Read inert declarations even when an unrelated selected tool is missing."""
    from ..durable_state import read_json
    path = engines.settings_path(Path(root) / 'state')
    value = read_json(path) if path.exists() else {
        'schema': 1, 'ffmpeg': None, 'ffprobe': None, 'piper': [], 'voice_helpers': [],
        'music_models': [], 'fluidsynth': None, 'dordiai': None}
    return engines._normalize_settings(value)


def connection_settings(root, *, allow_ambiguous=False):
    """Describe canonical or explicitly retained historical declarations."""
    saved = _engine_settings(root)['dordiai']
    legacy = load_settings(root)
    ambiguous = saved is not None and saved['enabled'] and legacy['enabled']
    if ambiguous and not allow_ambiguous:
        raise ValueError('Both canonical and legacy dordiai-voices connections are enabled; disable them before reconnecting.')
    if saved is not None and (saved['enabled'] or not legacy['enabled']):
        value = default_settings()
        value.update(saved)
        return ('ambiguous' if ambiguous else 'canonical'), value
    if legacy['enabled'] or legacy['runtime_directory'] is not None:
        return 'legacy', legacy
    return 'canonical', default_settings()


def connect_runtime(root, runtime_directory, *, python_executable=None, expected_worker_sha256=None):
    """Validate selected bytes; write only canonical engines.json; run no probe."""
    if load_settings(root)['enabled']:
        raise ValueError('Explicitly disable the legacy connection before saving a reviewed canonical connection. Existing jobs retain their original voice identities.')
    value = default_settings()
    value.update(enabled=True, runtime_directory=runtime_directory,
                 python_executable=python_executable or None)
    if expected_worker_sha256 is not None:
        value['expected_worker_sha256'] = expected_worker_sha256
    value = dordiai.validate_connection(value)
    settings = _engine_settings(root)
    settings['dordiai'] = {name: entry for name, entry in value.items() if name != 'version'}
    dordiai.cancel_active(root)
    try:
        engines.save_settings(Path(root) / 'state', settings)
    finally:
        dordiai.cancel_active(root)
    return value


def disable_runtime(root):
    """Stop scoped work even if persistence fails; never inspect a voice runtime."""
    dordiai.cancel_active(root)
    settings = _engine_settings(root)
    legacy = load_settings(root)
    origin = settings['dordiai']
    selected = default_settings()
    selected.update(origin if origin is not None and origin.get('enabled') else legacy)
    try:
        if legacy['enabled']:
            # This is the only compatibility write: disable the existing
            # declaration without changing its paths, interpreter or identity.
            save_settings(root, {**legacy, 'enabled': False})
        if origin is not None:
            settings['dordiai'] = {**origin, 'enabled': False}
            engines.save_settings(Path(root) / 'state', settings)
    finally:
        dordiai.cancel_active(root)
    return {**selected, 'enabled': False}


def show_dordiai_settings(frame, *, parent=None):
    import wx

    parent = parent if parent is not None else frame
    root = frame.library.root
    try:
        origin, value = connection_settings(root, allow_ambiguous=True)
    except (OSError, ValueError, RuntimeError) as exc:
        wx.MessageBox(str(exc), 'Local voice settings', wx.OK | wx.ICON_ERROR, parent=parent)
        return
    dialog = wx.Dialog(parent, title='Connect or disable dordiai-voices', size=(790, 590),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    alive = [True]
    native_id = dialog.GetId()
    def destroyed(event):
        if event.GetId() == native_id:
            alive[0] = False
        event.Skip()
    if hasattr(wx, 'EVT_WINDOW_DESTROY'):
        dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    def active():
        return alive[0] and not getattr(frame, '_closing', False) and bool(dialog)
    layout = wx.BoxSizer(wx.VERTICAL)
    introduction = wx.StaticText(dialog, label=(
        'Connect a separately installed, reviewed dordiai-voices worker for this library.\n'
        'Connection checks local paths and worker identity. Reload voices in the reader\n'
        'or reopen Audio Studio to check installed resources. No models are downloaded.'))
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
        if not active():
            return
        picker = wx.DirDialog(dialog, 'Select the installed kx47Runtime directory',
                              defaultPath=control.GetValue(), style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
        try:
            if picker.ShowModal() == wx.ID_OK and active() and picker:
                control.ChangeValue(picker.GetPath())
        finally:
            if picker:
                picker.Destroy()

    def choose_python(control):
        if not active():
            return
        picker = wx.FileDialog(dialog, 'Select its private Python executable',
                               wildcard='Executable files|*.exe;python*|All files|*',
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() == wx.ID_OK and active() and picker:
                control.ChangeValue(picker.GetPath())
        finally:
            if picker:
                picker.Destroy()

    runtime = field('Installed &runtime directory (kx47Runtime)', value['runtime_directory'], choose_directory)
    executable = field('&Private Python executable (blank uses runtime/python/python.exe)',
                       value['python_executable'], choose_python)
    identity = field('Reviewed worker &SHA256', value['expected_worker_sha256'])
    layout.Add(wx.StaticText(dialog, label=(
        'The fingerprint must match the worker whose protocol you reviewed.\n'
        'Available resources do not establish pronunciation or voice quality.\n'
        'Backups keep runtime references; the separate worker and models are not included.')),
        0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
    explanation = ('Both saved connections are enabled. Explicitly Disable before reconnecting.'
                   if origin == 'ambiguous' else
                   'Legacy connection retained with its original voice IDs. Disable it before a reviewed canonical reconnect.'
                   if origin == 'legacy' else
                   'Canonical connection enabled for this library.' if value['enabled'] else
                   'Canonical connection disabled for this library.')
    status = wx.TextCtrl(dialog, value=explanation,
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
        if not active():
            return
        try:
            connect_runtime(root, runtime.GetValue().strip(),
                            python_executable=executable.GetValue().strip() or None,
                            expected_worker_sha256=identity.GetValue().strip())
        except (OSError, ValueError, RuntimeError) as exc:
            if not active():
                return
            status.ChangeValue('Connection was not saved: ' + str(exc))
        else:
            if not active():
                return
            status.ChangeValue('Enabled for this library. Reload voices in the reader or reopen Audio Studio to check the selected runtime resources. No voice quality test has been performed.')
        status.SetFocus()

    def turn_off(event):
        if not active():
            return
        try:
            disable_runtime(root)
        except (OSError, ValueError, RuntimeError) as exc:
            if not active():
                return
            status.ChangeValue('Active work was signalled to stop, but the disabled preference could not be saved: ' + str(exc))
        else:
            if not active():
                return
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
        alive[0] = False
        if dialog:
            dialog.Destroy()
