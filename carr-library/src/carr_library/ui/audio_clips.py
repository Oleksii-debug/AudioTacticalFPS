"""Accessible clip parameters for the existing Audio Studio worker and draft."""
from dataclasses import asdict

from ..audio.models import Clip, CLIP_TRACKS, number, MAX_CLIP_SECONDS


FIELDS = (
    ('start', 'Start in chapter, seconds', '0'),
    ('trim_start', 'Start in original audio, seconds', '0'),
    ('duration', 'Selected duration, seconds; blank uses remaining audio', ''),
    ('gain_db', 'Gain, decibels: minus 60 to plus 12', '0'),
    ('pan', 'Stereo balance: minus 1 left, 0 center, plus 1 right', '0'),
    ('fade_in', 'Fade in, seconds', '0'),
    ('fade_out', 'Fade out, seconds', '0'),
)


def clip_values(values, *, track, title):
    """Pure form boundary shared by native controls and portable regressions."""
    if type(values) is not dict or set(values) != {row[0] for row in FIELDS}:
        raise ValueError('Every clip parameter needs an explicit field.')
    parsed = {}
    for key, _, _ in FIELDS:
        raw = values[key]
        if not isinstance(raw, str):
            raise ValueError('Enter clip parameters as finite numbers.')
        raw = raw.strip()
        parsed[key] = None if key == 'duration' and not raw else float(raw)
    candidate = Clip('preview', 'retained.wav', track=track, title=title, **parsed)
    data = asdict(candidate)
    return {key: value for key, value in data.items() if key not in ('id', 'asset')}


def show_clip_editor(parent, plan, *, selected_id=None, on_ready=None):
    """Return one explicit request; the Studio owns I/O, cancellation and save."""
    import wx
    dialog = wx.Dialog(parent, title='Chapter audio clips', size=(850, 800),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName('Chapter audio clips')
    request = None
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(dialog, label=(
        'Clips keep an independent PCM WAV/RF64 copy. Their start does not move spoken lines.\n'
        'Music and ambience duck under speech, microphone and sound-effects tracks.\n'
        'Save clip settings before Preview. Preview plays only that clip without chapter delay.')),
        0, wx.EXPAND | wx.ALL, 8)
    grid = wx.FlexGridSizer(cols=2, hgap=8, vgap=5)
    grid.AddGrowableCol(1)

    def field(label, control):
        grid.Add(wx.StaticText(dialog, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
        control.SetName(label)
        grid.Add(control, 1, wx.EXPAND)
        return control

    scene_choice = field('Chapter', wx.Choice(dialog, choices=[s.title for s in plan.scenes]))
    scene_choice.SetSelection(0)
    clip_choice = field('Clip', wx.Choice(dialog))
    title = field('Clip name', wx.TextCtrl(dialog, value=''))
    controls = {key: field(label, wx.TextCtrl(dialog, value=default)) for key, label, default in FIELDS}
    track = field('Track role', wx.Choice(dialog, choices=list(CLIP_TRACKS)))
    track.SetSelection(0)
    destination = field('Move to chapter', wx.Choice(dialog, choices=[s.title for s in plan.scenes]))
    destination.SetSelection(0)
    minimum = field('Minimum chapter duration, seconds', wx.TextCtrl(dialog, value='1'))
    layout.Add(grid, 1, wx.EXPAND | wx.ALL, 8)
    status = wx.TextCtrl(dialog, value='No audio starts until Preview is chosen.',
                        style=wx.TE_READONLY | wx.TE_MULTILINE, size=(-1, 65))
    status.SetName('Clip editing status')
    layout.Add(status, 0, wx.EXPAND | wx.ALL, 8)
    buttons = {}

    def current_scene():
        index = scene_choice.GetSelection()
        if not 0 <= index < len(plan.scenes):
            raise ValueError('Choose a chapter.')
        return plan.scenes[index]

    def current_clip():
        scene = current_scene()
        index = clip_choice.GetSelection()
        if not 0 <= index < len(scene.clips):
            raise ValueError('Choose an existing clip.')
        return scene.clips[index]

    def selected(event=None):
        try:
            clip = current_clip()
        except ValueError:
            clip = None
        for key, _, default in FIELDS:
            value = getattr(clip, key) if clip is not None else default
            controls[key].ChangeValue('' if value is None else str(value))
        title.ChangeValue(clip.title if clip is not None else '')
        track.SetSelection(CLIP_TRACKS.index(clip.track) if clip is not None else 0)
        for key in ('edit', 'remove', 'move', 'preview'):
            if key in buttons:
                buttons[key].Enable(clip is not None)

    def scene_changed(event=None, *, wanted=None):
        scene = current_scene()
        clip_choice.SetItems([f'{c.title or c.asset} — {c.track}, start {c.start:g}s' for c in scene.clips])
        choice = next((i for i, c in enumerate(scene.clips) if c.id == wanted), -1)
        clip_choice.SetSelection(choice)
        minimum.ChangeValue(str(scene.minimum_duration or 1))
        selected()

    def accept(action):
        nonlocal request
        try:
            scene = current_scene()
            result = {'action': action, 'scene_id': scene.id}
            if action in ('edit', 'remove', 'move', 'preview'):
                result['clip_id'] = current_clip().id
            if action in ('add', 'edit'):
                index = track.GetSelection()
                if not 0 <= index < len(CLIP_TRACKS):
                    raise ValueError('Choose a track role.')
                result['values'] = clip_values({key: item.GetValue() for key, item in controls.items()},
                                              track=CLIP_TRACKS[index], title=title.GetValue())
            if action == 'add':
                picker = wx.FileDialog(dialog, 'Retain a local PCM WAV or RF64 clip',
                    wildcard='PCM audio (*.wav;*.rf64)|*.wav;*.rf64|All files (*.*)|*.*',
                    style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
                try:
                    if picker.ShowModal() != wx.ID_OK:
                        return
                    result['source'] = picker.GetPath()
                finally:
                    picker.Destroy()
            elif action == 'duration':
                result['minimum_duration'] = number(float(minimum.GetValue()), .000001,
                                                   MAX_CLIP_SECONDS, 'Chapter minimum duration')
            elif action == 'move':
                index = destination.GetSelection()
                if not 0 <= index < len(plan.scenes) or plan.scenes[index].id == scene.id:
                    raise ValueError('Choose another destination chapter.')
                result['scene_id'] = plan.scenes[index].id
            request = result
            dialog.EndModal(wx.ID_OK)
        except (ValueError, TypeError, OverflowError) as exc:
            status.ChangeValue(str(exc))
            status.SetFocus()

    for specs in (
        (('add', '&Add local audio'), ('edit', '&Save clip settings'), ('remove', '&Remove clip')),
        (('move', '&Move to chapter'), ('duration', 'Set chapter &duration'), ('preview', '&Preview saved clip'))):
        row = wx.BoxSizer(wx.HORIZONTAL)
        for action, label in specs:
            button = wx.Button(dialog, label=label)
            button.SetName(label.replace('&', ''))
            button.Bind(wx.EVT_BUTTON, lambda event, choice=action: accept(choice))
            buttons[action] = button
            row.Add(button, 0, wx.ALL, 4)
        layout.Add(row, 0, wx.EXPAND | wx.LEFT, 4)
    close = wx.Button(dialog, wx.ID_CANCEL, 'Close clip editor')
    close.SetName('Close clip editor')
    layout.Add(close, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
    dialog.SetSizer(layout)
    scene_choice.Bind(wx.EVT_CHOICE, scene_changed)
    clip_choice.Bind(wx.EVT_CHOICE, selected)
    if selected_id:
        for index, scene in enumerate(plan.scenes):
            if any(clip.id == selected_id for clip in scene.clips):
                scene_choice.SetSelection(index)
                break
    scene_changed(wanted=selected_id)
    scene_choice.SetFocus()
    if on_ready is not None:
        wx.CallAfter(on_ready, dialog, {'scene': scene_choice, 'clip': clip_choice, 'title': title,
            'track': track, 'destination': destination, 'minimum': minimum, **controls}, buttons, status)
    try:
        return request if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()
