"""Explicit, read-only native review of silence candidates in completed audio."""
from math import isfinite

from ..audio.silence import MAX_SILENCE_INTERVALS, SilenceCriteria

DEFAULT_THRESHOLD_DBFS = -50.0
DEFAULT_MINIMUM_SECONDS = 2.0
MAX_REPORT_CHARACTERS = 1024 * 1024
REVIEW_NOTICE = (
    'These are signal-level candidates, not a quality verdict. '
    'No matching interval means only that none met these criteria. '
    'The completed audio is not changed or played.'
)


def criteria_from_text(threshold, minimum):
    """Use the same finite criteria contract as the actual audio inspection."""
    try:
        threshold_value, minimum_value = float(threshold), float(minimum)
    except (TypeError, ValueError):
        raise ValueError('Enter a numeric threshold in dBFS and minimum duration in seconds.') from None
    if not isfinite(threshold_value) or not isfinite(minimum_value):
        raise ValueError('The threshold and minimum duration must be finite numbers.')
    return SilenceCriteria(threshold_value, minimum_value)


def choose_criteria(parent, *, current):
    """Collect explicit criteria on the owner thread; opening never starts work."""
    import wx
    if not current():
        return None
    window = wx.Dialog(parent, title='Inspect long silences — criteria',
                       size=(650, 360), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    owner_id, destroyed, chosen = window.GetId(), False, None
    layout = wx.BoxSizer(wx.VERTICAL)

    def field(label, value):
        layout.Add(wx.StaticText(window, label=label), 0, wx.ALL, 6)
        control = wx.TextCtrl(window, value=value)
        control.SetName(label.replace('&', ''))
        control.SetMaxLength(128)
        layout.Add(control, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)
        return control

    threshold = field('Silence &threshold in dBFS (-192 through 0)', format(DEFAULT_THRESHOLD_DBFS, 'g'))
    minimum = field('&Minimum silence in seconds (greater than 0; at most 31536000)', format(DEFAULT_MINIMUM_SECONDS, 'g'))
    feedback = wx.TextCtrl(window, value=REVIEW_NOTICE, size=(-1, 90),
                           style=wx.TE_MULTILINE | wx.TE_READONLY)
    feedback.SetName('Silence inspection criteria and validation status')
    layout.Add(feedback, 1, wx.EXPAND | wx.ALL, 6)
    row = wx.BoxSizer(wx.HORIZONTAL)
    inspect = wx.Button(window, wx.ID_OK, label='&Inspect completed audio')
    inspect.SetName('Inspect completed audio with these criteria')
    cancel = wx.Button(window, wx.ID_CANCEL, label='&Cancel')
    for button in (inspect, cancel):
        row.Add(button, 0, wx.ALL, 6)
    layout.Add(row, 0, wx.ALIGN_RIGHT)
    window.SetSizer(layout)

    def owner_destroyed(event):
        nonlocal destroyed
        if event.GetId() == owner_id:
            destroyed = True
        event.Skip()

    def accept(event):
        nonlocal chosen
        if destroyed or not current():
            return
        try:
            value = criteria_from_text(threshold.GetValue(), minimum.GetValue())
        except (TypeError, ValueError) as exc:
            feedback.ChangeValue(str(exc))
            feedback.SetFocus()
            return
        if not current():
            return
        chosen = value
        window.EndModal(wx.ID_OK)

    inspect.Bind(wx.EVT_BUTTON, accept)
    window.Bind(wx.EVT_WINDOW_DESTROY, owner_destroyed)
    threshold.SetFocus()
    try:
        decision = window.ShowModal()
        if destroyed or not current() or decision != wx.ID_OK:
            return None
        return chosen
    finally:
        if not destroyed:
            window.Destroy()


def result_text(result):
    """Use seconds for reading; keep the original exact frame/hash result intact."""
    if (not isinstance(result, dict) or result.get('schema') != 'carr-silence-inspection/1'
            or result.get('status') != 'complete'):
        raise ValueError('The completed silence inspection did not return its expected report.')
    intervals = result['intervals']
    count, omitted = result['interval_count'], result['omitted_count']
    if (not isinstance(intervals, list) or len(intervals) > MAX_SILENCE_INTERVALS
            or type(count) is not int or type(omitted) is not int
            or count < 0 or omitted < 0 or count != len(intervals) + omitted):
        raise ValueError('The silence inspection interval counts are inconsistent.')
    audio, criteria = result['audio'], result['criteria']
    lines = [
        'Completed master.wav — long-silence inspection',
        REVIEW_NOTICE,
        f"Threshold: {criteria['threshold_dbfs']:g} dBFS; "
        f"minimum interval: {criteria['minimum_seconds']:g} seconds.",
        f"Analyzed duration: {audio['frames'] / audio['sample_rate']:.6f} seconds; "
        f"channels: {audio['channels']}. Every channel must be at or below the threshold.",
        f"Sample rate: {audio['sample_rate']} Hz; total frames: {audio['frames']}.",
        'Original master SHA-256: ' + result['source']['sha256'],
        f'Matching intervals: {count}; shown: {len(intervals)}; omitted: {omitted}.',
    ]
    if omitted:
        lines.append('Additional matching intervals are omitted from this bounded list; '
                     'the counts include them.')
    if not count:
        lines.append('No interval met the selected threshold and minimum duration.')
    for index, interval in enumerate(intervals, 1):
        lines.append(
            f"{index}. {interval['start_seconds']:.6f} to {interval['end_seconds']:.6f} seconds; "
            f"duration {interval['duration_seconds']:.6f} seconds; "
            f"frames [{interval['start_frame']}, {interval['end_frame']}).")
    lines.append(str(result['notice']))
    text = '\n'.join(lines)
    if len(text) > MAX_REPORT_CHARACTERS:
        raise ValueError('The silence inspection text exceeds its bounded display size.')
    return text


def show_result(parent, result, *, current):
    """Present only a still-current completed result; no playback or mutation."""
    import wx
    text = result_text(result)
    if not current():
        raise InterruptedError('The selected production changed before inspection results could be shown.')
    window = wx.Dialog(parent, title='Long-silence inspection — completed output',
                       size=(780, 560), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    owner_id, destroyed = window.GetId(), False
    window.inspection_result = result
    layout = wx.BoxSizer(wx.VERTICAL)
    output = wx.TextCtrl(window, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY)
    output.SetName('Long-silence inspection results in seconds')
    layout.Add(output, 1, wx.EXPAND | wx.ALL, 8)
    close = wx.Button(window, wx.ID_CLOSE, label='&Close inspection')
    close.SetName('Close long-silence inspection')
    layout.Add(close, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
    window.SetSizer(layout)

    def owner_destroyed(event):
        nonlocal destroyed
        if event.GetId() == owner_id:
            destroyed = True
        event.Skip()

    close.Bind(wx.EVT_BUTTON, lambda event: window.EndModal(wx.ID_CLOSE))
    window.Bind(wx.EVT_WINDOW_DESTROY, owner_destroyed)
    output.SetFocus()
    try:
        if not current():
            raise InterruptedError('The selected production changed before inspection results could be shown.')
        window.ShowModal()
    finally:
        if not destroyed:
            window.Destroy()
