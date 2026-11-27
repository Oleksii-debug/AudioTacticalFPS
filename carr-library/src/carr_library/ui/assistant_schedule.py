"""Native presentation of explicit local schedule choices; no executor or timer."""
from datetime import datetime, timedelta, timezone
import json

from ..assistant.schedule_model import MAX_LATENESS_SECONDS, utc

MAX_REVIEW_CHARACTERS = 256 * 1024


def plan_text(plan):
    value = json.dumps(plan, ensure_ascii=False, indent=2, allow_nan=False)
    if len(value) > MAX_REVIEW_CHARACTERS:
        raise ValueError('This plan exceeds the native review allowance. Review it with the existing assistant CLI.')
    return value


def choose_time(parent, plan, plan_sha256):
    """Return only a chosen UTC window after the exact plan is displayed."""
    import wx
    displayed = plan_text(plan)
    dialog = wx.Dialog(parent, title='Schedule this exact reviewed plan',
                       size=(860, 720), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName('Assistant schedule time')
    layout = wx.BoxSizer(wx.VERTICAL)
    def field(label, value, style=0, proportion=0):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        control = wx.TextCtrl(dialog, value=value, style=style)
        control.SetName(label)
        layout.Add(control, proportion, wx.ALL | wx.EXPAND, 4)
        return control
    field('Exact reviewed plan and scope', displayed, wx.TE_MULTILINE | wx.TE_READONLY, 1)
    field('Approved plan SHA256', plan_sha256, wx.TE_READONLY)
    due = field('Start time in UTC with Z or +00:00',
                (datetime.now(timezone.utc) + timedelta(minutes=5)).replace(microsecond=0).isoformat())
    late = field('Start-window allowance in seconds; 1 to 86400', '3600')
    layout.Add(wx.StaticText(dialog, label=
        'Saving a time does not run or arm the job. Open Local schedules and explicitly arm it for this window. '
        'Closing the assistant disarms it; missed or uncertain attempts need review.'), 0, wx.ALL | wx.EXPAND, 5)
    row = wx.BoxSizer(wx.HORIZONTAL)
    result = [None]
    def accept(event):
        try:
            selected = utc(due.GetValue().strip())
            allowance = int(late.GetValue())
            if not 1 <= allowance <= MAX_LATENESS_SECONDS:
                raise ValueError('Choose a start-window allowance from one second to one day.')
            result[0] = selected, allowance
            dialog.EndModal(wx.ID_OK)
        except (ValueError, OverflowError) as error:
            wx.MessageBox(str(error), 'Schedule time needs review', wx.OK | wx.ICON_ERROR, dialog)
    for label, identity, callback in (
            ('Save time only', wx.ID_OK, accept),
            ('Cancel', wx.ID_CANCEL, lambda event: dialog.EndModal(wx.ID_CANCEL))):
        button = wx.Button(dialog, id=identity, label=label)
        button.SetName(label)
        button.Bind(wx.EVT_BUTTON, callback)
        row.Add(button, 0, wx.ALL, 4)
    layout.Add(row)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(wx.ID_CANCEL)
    try:
        return result[0] if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()


def choose_schedule(parent, rows, armed=()):
    """Return an exact id/hash action; all state access stays in the caller worker."""
    import wx
    rows = tuple(rows)
    if not rows:
        raise ValueError('No local schedules have been saved. Preview a plan, then choose Schedule reviewed plan.')
    dialog = wx.Dialog(parent, title='Local schedules — foreground arming',
                       size=(900, 760), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName('Assistant local schedules')
    layout = wx.BoxSizer(wx.VERTICAL)
    choices = wx.ListBox(dialog, choices=[
        f'{view.id}; {view.state}; due {view.due}; '
        + ('armed in this window' if view.id in armed else 'not armed')
        for view, plan in rows], style=wx.LB_SINGLE)
    choices.SetName('Saved concrete schedules')
    layout.Add(choices, 1, wx.EXPAND | wx.ALL, 5)
    detail = wx.TextCtrl(dialog, style=wx.TE_MULTILINE | wx.TE_READONLY)
    detail.SetName('Exact scheduled plan and permission scope')
    layout.Add(detail, 2, wx.EXPAND | wx.ALL, 5)
    layout.Add(wx.StaticText(dialog, label=
        'Arm permits one attempt in the saved time window while this assistant stays open. '
        'There is no startup service or automatic replay. Restored, expired or uncertain jobs require explicit review.'),
        0, wx.EXPAND | wx.ALL, 5)
    result, buttons, valid = [None], {}, [False]
    def selected():
        index = choices.GetSelection()
        if not 0 <= index < len(rows):
            raise ValueError('Choose a saved concrete schedule.')
        return rows[index]
    def refresh(event=None):
        valid[0] = False
        try:
            view, plan = selected()
            shown = plan_text(plan)
            detail.ChangeValue(
                f'Job: {view.id}\nState: {view.state}; journal: {view.job_status}\n'
                f'Due UTC: {view.due}\nExpires UTC: {view.expires}\n'
                f'Plan SHA256: {view.plan_sha256}\nSchedule SHA256: {view.schedule_sha256}\n'
                f'{view.message}\n\n{shown}')
            valid[0] = True
            buttons['arm'].Enable(view.state == 'pending' and view.local_binding_valid)
            buttons['disable'].Enable(True)
        except (ValueError, TypeError) as error:
            detail.ChangeValue(str(error))
            for button in buttons.values():
                button.Disable()
    def act(action):
        if not valid[0]:
            return
        view, _ = selected()
        if action == 'arm' and (view.state != 'pending' or not view.local_binding_valid):
            return
        result[0] = action, view.id, view.schedule_sha256
        dialog.EndModal(wx.ID_OK)
    row = wx.BoxSizer(wx.HORIZONTAL)
    for key, label in (('arm', 'Arm selected for this window'), ('disable', 'Disable saved time')):
        button = wx.Button(dialog, label=label)
        button.SetName(label)
        button.Bind(wx.EVT_BUTTON, lambda event, key=key: act(key))
        row.Add(button, 0, wx.ALL, 4)
        buttons[key] = button
    close = wx.Button(dialog, id=wx.ID_CANCEL, label='Close review')
    close.SetName('Close review')
    close.Bind(wx.EVT_BUTTON, lambda event: dialog.EndModal(wx.ID_CANCEL))
    row.Add(close, 0, wx.ALL, 4)
    layout.Add(row)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(wx.ID_CANCEL)
    choices.Bind(wx.EVT_LISTBOX, refresh)
    choices.SetSelection(0)
    refresh()
    try:
        return result[0] if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()
