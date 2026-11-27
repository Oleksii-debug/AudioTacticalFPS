"""Native explicit local runtime setup; users select previously installed assets."""
from pathlib import Path
from ..durable_state import read_json
from ..runtime_settings import load_settings, save_settings


def show_engine_settings(frame):
    import wx
    state_root = frame.library.root / 'state'
    settings = load_settings(state_root)
    dialog = wx.Dialog(frame, title='Local audio, voice and music engines', size=(800, 650),
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
    status = wx.TextCtrl(dialog, style=wx.TE_MULTILINE | wx.TE_READONLY)
    status.SetName('Local engine configuration and availability')
    layout.Add(status, 1, wx.EXPAND | wx.ALL, 6)

    def describe():
        from .dordiai_settings import connection_settings
        try:
            origin, selected = connection_settings(frame.library.root, allow_ambiguous=True)
            dordiai_status = ('Ambiguous canonical and legacy connections; explicitly Disable before reconnecting'
                if origin == 'ambiguous' else
                ('Enabled' if selected['enabled'] else 'Disabled') + ' (' + origin + ' saved declaration)')
        except (OSError, ValueError) as exc:
            dordiai_status = 'Configuration unavailable: ' + str(exc)
        status.ChangeValue('All paths below refer to local installations. No download or remote API is started.\n'
            'FFmpeg: ' + str(settings['ffmpeg'] or 'Discover installed executable') + '\n'
            'FFprobe: ' + str(settings['ffprobe'] or 'Discover installed executable') + '\n'
            f'Piper registrations: {len(settings["piper"])}\n'
            f'Explicit voice helpers: {len(settings["voice_helpers"])}\n'
            'Dordiai-voices: ' + dordiai_status + '\n'
            'Neural music configurations: ' + ', '.join(model['engine'] for model in settings['music_models']) + '\n'
            'MusicGen is optional for private noncommercial use with separately reviewed weights. '
            'Configured assets are distinct from tested inference. A missing runtime fails clearly.\n'
            'Voice helper registrations use our JSON contract. The separate dordiai-voices connection '
            'uses its reviewed public worker bridge; installed resources do not establish acoustic quality. '
            'An unknown SAPI4 installation is not presumed compatible.')

    def executable(key):
        if not active():
            return
        picker = wx.FileDialog(dialog, 'Choose installed ' + key, style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() == wx.ID_OK and active():
                candidate = {**settings, key: picker.GetPath()}
                save_settings(state_root, candidate)
                settings.update(candidate)
                describe()
        except Exception as exc:
            if active():
                status.ChangeValue(str(exc))
        finally:
            if picker:
                picker.Destroy()

    def register(kind):
        if not active():
            return
        picker = wx.FileDialog(dialog, 'Choose reviewed local engine registration JSON',
                               wildcard='JSON (*.json)|*.json', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() != wx.ID_OK or not active():
                return
            row = read_json(Path(picker.GetPath()))
            if kind == 'voice_helpers':
                if wx.MessageBox('Enable this reviewed local executable? It runs with your account permissions. '
                                 'Only register a helper you intentionally installed and trust.',
                                 'Enable local voice helper', wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES or not active():
                    return
                row['enabled'] = True
            candidate = {**settings, kind: [*settings[kind], row]}
            save_settings(state_root, candidate)
            settings.update(candidate)
            describe()
        except Exception as exc:
            if active():
                status.ChangeValue(str(exc))
        finally:
            if picker:
                picker.Destroy()

    def reset(event):
        if not active():
            return
        if wx.MessageBox('Remove local engine overrides? Installed files will remain on disk.', 'Reset configuration',
                         wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES or not active():
            return
        from .dordiai_settings import disable_runtime
        disable_runtime(frame.library.root)
        settings.update(ffmpeg=None, ffprobe=None, piper=[], voice_helpers=[], music_models=[], fluidsynth=None,
                        dordiai=None)
        save_settings(state_root, settings)
        describe()

    def dordiai(event):
        if not active():
            return
        from .dordiai_settings import show_dordiai_settings
        show_dordiai_settings(frame, parent=dialog)
        if active():
            settings.clear()
            settings.update(load_settings(state_root))
            describe()

    for label, callback in [('Choose FFmpeg executable', lambda event: executable('ffmpeg')),
        ('Choose FFprobe executable', lambda event: executable('ffprobe')),
        ('Register a local Piper voice manifest', lambda event: register('piper')),
        ('Register a MusicGen or ACE-Step installation', lambda event: register('music_models')),
        ('Register a reviewed local voice helper', lambda event: register('voice_helpers')),
        ('Connect or disable dordiai-voices', dordiai),
        ('Reset overrides', reset)]:
        button = wx.Button(dialog, label=label)
        button.Bind(wx.EVT_BUTTON, callback)
        layout.Add(button, 0, wx.EXPAND | wx.ALL, 4)
    layout.Add(dialog.CreateButtonSizer(wx.CLOSE), 0, wx.ALL, 5)
    dialog.SetEscapeId(wx.ID_CLOSE)
    dialog.Bind(wx.EVT_BUTTON, lambda event: dialog.EndModal(wx.ID_CLOSE) if active() else None, id=wx.ID_CLOSE)
    dialog.SetSizer(layout)
    describe()
    try:
        dialog.ShowModal()
    finally:
        alive[0] = False
        if dialog:
            dialog.Destroy()
