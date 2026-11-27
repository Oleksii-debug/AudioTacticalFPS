"""Native explicit local runtime setup; users select previously installed assets."""
from pathlib import Path
from ..durable_state import local_path, read_json, regular
from ..runtime_settings import load_settings, save_settings


def show_engine_settings(frame):
    import wx
    library = frame.library
    root = local_path(library.root, exists=True)
    info = regular(root, directory=True)
    root_identity = info.st_dev, info.st_ino
    state_root = root / 'state'
    settings = load_settings(state_root)
    closed = False
    dialog = wx.Dialog(frame, title='Local audio, voice and music engines', size=(800, 650),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    status = wx.TextCtrl(dialog, style=wx.TE_MULTILINE | wx.TE_READONLY)
    status.SetName('Local engine configuration and availability')
    layout.Add(status, 1, wx.EXPAND | wx.ALL, 6)

    def current():
        if closed or getattr(frame, '_closing', False) or frame.library is not library:
            return False
        try:
            selected = local_path(library.root, exists=True)
            info = regular(selected, directory=True)
            return selected == root and (info.st_dev, info.st_ino) == root_identity
        except (OSError, ValueError, RuntimeError):
            return False

    def require_current():
        if not current():
            raise InterruptedError('Reopen local engine settings in the current library.')

    def report(value):
        if current():
            status.ChangeValue(value)

    def describe():
        report('All paths below refer to local installations. No download or remote API is started.\n'
            'FFmpeg: ' + str(settings['ffmpeg'] or 'Discover installed executable') + '\n'
            'FFprobe: ' + str(settings['ffprobe'] or 'Discover installed executable') + '\n'
            f'Piper registrations: {len(settings["piper"])}\n'
            f'Explicit voice helpers: {len(settings["voice_helpers"])}\n'
            'dordiai-voices: ' + ('Enabled local installation; voice availability is checked in the reader or Studio'
                if settings['dordiai'] and settings['dordiai']['enabled'] else 'Disabled') + '\n'
            'Neural music configurations: ' + ', '.join(model['engine'] for model in settings['music_models']) + '\n'
            'MusicGen is optional for private noncommercial use with separately reviewed weights. '
            'Configured assets are distinct from tested inference. A missing runtime fails clearly.\n'
            'Generic voice helpers and the reviewed dordiai-voices adapter use separate contracts. '
            'Resource availability is not a pronunciation or voice-quality assessment.')

    def publish(candidate):
        require_current()
        save_settings(state_root, candidate)
        require_current()
        settings.clear()
        settings.update(candidate)
        describe()

    def executable(key):
        if not current():
            return
        picker = wx.FileDialog(dialog, 'Choose installed ' + key, style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() == wx.ID_OK:
                require_current()
                candidate = {**settings, key: picker.GetPath()}
                publish(candidate)
        except Exception as exc:
            report(str(exc))
        finally:
            picker.Destroy()

    def register(kind):
        if not current():
            return
        picker = wx.FileDialog(dialog, 'Choose reviewed local engine registration JSON',
                               wildcard='JSON (*.json)|*.json', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() != wx.ID_OK:
                return
            require_current()
            row = read_json(Path(picker.GetPath()))
            require_current()
            if kind == 'voice_helpers':
                if wx.MessageBox('Enable this reviewed local executable? It runs with your account permissions. '
                                 'Only register a helper you intentionally installed and trust.',
                                 'Enable local voice helper', wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                    return
                require_current()
                row['enabled'] = True
            candidate = {**settings, kind: [*settings[kind], row]}
            publish(candidate)
        except Exception as exc:
            report(str(exc))
        finally:
            picker.Destroy()

    def reset(event):
        if not current():
            return
        try:
            if wx.MessageBox('Remove local engine overrides? Installed files will remain on disk.', 'Reset configuration',
                             wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                return
            require_current()
            candidate = {**settings, 'ffmpeg': None, 'ffprobe': None, 'piper': [], 'voice_helpers': [],
                         'music_models': [], 'fluidsynth': None, 'dordiai': None}
            publish(candidate)
        except Exception as exc:
            report(str(exc))

    def connect_dordiai(event):
        if not current():
            return
        picker = wx.DirDialog(dialog, 'Choose the installed dordiai-voices kx47Runtime directory',
                              style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
        try:
            if picker.ShowModal() != wx.ID_OK:
                return
            require_current()
            selected = picker.GetPath()
            if wx.MessageBox('Enable this installed local voice engine? Its reviewed worker runs with '
                             'your account permissions when voices are loaded or used. No model is downloaded.',
                             'Enable dordiai-voices', wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                return
            require_current()
            # Constructor checks local paths and the reviewed worker identity.
            # Enumeration and synthesis remain in the reader/Studio background job.
            from ..integrations.dordiai_voices import DordiaiVoicesAdapter
            adapter = DordiaiVoicesAdapter(selected)
            adapter.close()
            require_current()
            candidate = {**settings, 'dordiai': {'enabled': True, 'runtime_directory': selected}}
            publish(candidate)
        except Exception as exc:
            report(str(exc))
        finally:
            picker.Destroy()

    def disconnect_dordiai(event):
        if not current():
            return
        try:
            candidate = {**settings, 'dordiai': None}
            publish(candidate)
        except Exception as exc:
            report(str(exc))

    def close(event):
        nonlocal closed
        closed = True
        dialog.EndModal(wx.ID_CLOSE)

    def destroyed(event):
        nonlocal closed
        if event.GetEventObject() is dialog:
            closed = True
        event.Skip()

    for label, callback in [('Choose FFmpeg executable', lambda event: executable('ffmpeg')),
        ('Choose FFprobe executable', lambda event: executable('ffprobe')),
        ('Register a local Piper voice manifest', lambda event: register('piper')),
        ('Connect installed &dordiai-voices', connect_dordiai),
        ('Disable dordiai-voices', disconnect_dordiai),
        ('Register a MusicGen or ACE-Step installation', lambda event: register('music_models')),
        ('Register a reviewed local voice helper', lambda event: register('voice_helpers')),
        ('Reset overrides', reset)]:
        button = wx.Button(dialog, label=label)
        button.Bind(wx.EVT_BUTTON, callback)
        layout.Add(button, 0, wx.EXPAND | wx.ALL, 4)
    layout.Add(dialog.CreateButtonSizer(wx.CLOSE), 0, wx.ALL, 5)
    dialog.SetEscapeId(wx.ID_CLOSE)
    dialog.Bind(wx.EVT_BUTTON, close, id=wx.ID_CLOSE)
    dialog.Bind(wx.EVT_CLOSE, close)
    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    dialog.SetSizer(layout)
    describe()
    try:
        if current():
            dialog.ShowModal()
    finally:
        closed = True
        dialog.Destroy()
