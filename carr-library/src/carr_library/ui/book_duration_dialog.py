"""Explicit measured-audio publication on the existing desktop worker."""
import threading

from ..studio.book_audio import BookAudioService, BookPlan

HELP = (
    'Choose an already completed part of this saved Book plan. Preview reads its '
    'actual PCM master, measured timeline and source receipts; it starts no speech. '
    'Maximum length is in milliseconds. The audio split uses exact sample frames. '
    'Text references remain whole measured lines; a cut through a line is marked '
    'as approximate whole-line source scope, without word or character alignment.\n\n'
    'Publish requires a separate confirmation. The ZIP contains exact PCM WAV parts, '
    'the original part Project, measured timeline, Book source provenance and a '
    'checked manifest. Existing source, jobs and outputs are unchanged. The new '
    'publication file must be outside private data and the completed output folder.\n\n'
    'A backup restores historical publication receipts but grants no old output '
    'authority. Verify an existing publication explicitly through Studio after restore. '
    'Cancel or Close stops pending work; once atomic publication completes, the '
    'saved receipt remains the authority for recovery.'
)


def review_text(plan, index, value):
    content, pcm = value['content'], value['content']['pcm']
    rows = ['MEASURED PREVIEW — Publish is a separate explicit action.',
        'Original book: ' + plan.source['title'],
        'Source revision: ' + plan.source['source_anchor']['revision'],
        'Completed source part: %d; %s' % (index, plan.parts[index - 1].label),
        'Verified PCM: %d frames; %d Hz; %d channels; %d-bit; %.6f seconds' % (
            pcm['frames'], pcm['sample_rate'], pcm['channels'], pcm['sample_width'] * 8,
            pcm['frames'] / pcm['sample_rate']),
        'Maximum measured part: %d milliseconds' % content['maximum_ms'],
        'Publication: ' + value['output'], 'Review SHA-256: ' + value['approval_sha256'],
        content['notice']]
    for part in content['parts']:
        partial = sum(row['partial_line'] for row in part['source_lines'])
        rows.append('%d. %s\nFrames %d–%d; %.6f seconds; %d whole-line source references, %d partial intersections.' % (
            part['index'], part['name'], part['start_frame'], part['end_frame'],
            part['frames'] / pcm['sample_rate'], len(part['source_lines']), partial))
    return '\n\n'.join(rows)


def create_book_duration_dialog(frame, *, service, plan, wx_module=None,
                                owner_guard=lambda: None, owner_cancelled=lambda: False):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    if not isinstance(service, BookAudioService) or not isinstance(plan, BookPlan):
        raise ValueError('Choose the existing saved Book plan and its owning service.')
    scope, library = getattr(frame, 'active', None), frame.library
    if (scope is None or service.library is not library
            or service.workspace != scope[0]
            or (plan.source['source_anchor']['workspace'], plan.source['source_topic']) != scope):
        raise ValueError('Open measured audio in the exact original library/topic scope.')
    service._check_scope()
    owner_guard()
    if owner_cancelled():
        raise InterruptedError('The owning Book review was closed.')
    root = library.root
    dialog = wx.Dialog(frame, title='Measured duration audio parts', size=(920, 760),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    fields_layout = wx.FlexGridSizer(cols=2, vgap=4, hgap=8)
    fields_layout.AddGrowableCol(1, 1)
    layout.Add(fields_layout, 0, wx.EXPAND | wx.ALL, 8)
    alive, busy = True, False
    reviewed, reviewed_fields = None, None
    cancel = threading.Event()

    def field(label, cls=wx.TextCtrl, *, proportion=0, **kwargs):
        target = layout if proportion else fields_layout
        target.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        widget = cls(dialog, **kwargs)
        widget.SetName(label.replace('&', ''))
        target.Add(widget, proportion, wx.EXPAND | wx.ALL, 4)
        return widget

    parts = field('Completed Book &part', wx.Choice,
                  choices=['%d. %s' % (index, part.label) for index, part in enumerate(plan.parts, 1)])
    parts.SetSelection(0)
    maximum = field('Maximum measured part &milliseconds', value='600000')
    names = field('Measured part &name template: index, book, part', value='{index:04d} - {book} - {part}')
    output = field('Reviewed measured-parts &ZIP', value='')
    status = field('Measured audio preview and publication status', proportion=1,
                   style=wx.TE_MULTILINE | wx.TE_READONLY, value=HELP)
    row = wx.BoxSizer(wx.HORIZONTAL)
    buttons = {}
    for name, label in (('file', 'Choose publication &file'), ('preview', '&Preview measured parts'),
                        ('publish', 'P&ublish reviewed parts'), ('cancel', '&Cancel current operation'),
                        ('close', '&Close')):
        button = wx.Button(dialog, wx.ID_CANCEL if name == 'close' else wx.ID_ANY, label=label)
        button.SetName(label.replace('&', ''))
        button.Enable(name not in ('publish', 'cancel'))
        row.Add(button, 0, wx.ALL, 4)
        buttons[name] = button
    layout.Add(row, 0, wx.EXPAND)
    dialog.SetSizer(layout)

    def can_present():
        if (not alive or getattr(frame, '_closing', False) or owner_cancelled()
                or frame.library is not library or frame.library.root != root
                or getattr(frame, 'active', None) != scope):
            return False
        try:
            service._check_scope()
            owner_guard()
        except (OSError, ValueError, RuntimeError):
            return False
        return True

    def report(value):
        if can_present():
            status.ChangeValue(str(value))

    def guard():
        if not alive or getattr(frame, '_closing', False) or owner_cancelled():
            raise InterruptedError('The owning Book review is closed.')
        if (frame.library is not library or frame.library.root != root
                or getattr(frame, 'active', None) != scope):
            raise ValueError('The original Book library/topic is no longer active.')
        service._check_scope()
        owner_guard()

    def worker_guard():
        guard()
        if service.load(plan.id).fingerprint != plan.fingerprint:
            raise ValueError('The saved Book plan changed. Reopen its current review.')

    def stopped():
        return (cancel.is_set() or frame.cancel_event.is_set() or not alive
                or getattr(frame, '_closing', False) or owner_cancelled())

    def values():
        return parts.GetSelection(), maximum.GetValue(), names.GetValue(), output.GetValue()

    def controls():
        if not can_present():
            return
        for widget in (parts, maximum, names, output):
            widget.Enable(not busy)
        buttons['file'].Enable(not busy)
        buttons['preview'].Enable(not busy)
        buttons['publish'].Enable(not busy and reviewed is not None)
        buttons['cancel'].Enable(busy)

    def invalidate(event=None):
        nonlocal reviewed, reviewed_fields
        reviewed, reviewed_fields = None, None
        controls()
        if event is not None:
            event.Skip()

    def begin(title, operation, done):
        nonlocal busy
        try:
            guard()
            if busy or getattr(frame, 'working', False):
                raise ValueError('Finish or cancel the current desktop operation first.')
        except Exception as exc:
            report(exc)
            return
        cancel.clear()
        busy = True
        controls()
        def work():
            worker_guard()
            if stopped():
                raise InterruptedError('Measured publication cancelled before work.')
            return operation()
        def complete(value):
            nonlocal busy
            busy = False
            controls()
            if can_present():
                try:
                    guard()
                    done(value)
                except Exception as exc:
                    report(exc)
                if can_present():
                    controls()
                    status.SetFocus()
        def failed(error):
            nonlocal busy, reviewed, reviewed_fields
            from ..studio.publishing import MeasuredPublicationCommitted
            busy = False
            if isinstance(error, MeasuredPublicationCommitted):
                reviewed, reviewed_fields = None, None
            controls()
            report(error)
            if can_present():
                status.SetFocus()
        try:
            accepted = frame.run_job(title, work, done=complete, failed=failed)
        except Exception as exc:
            busy = False
            controls()
            report(exc)
            return
        if accepted is False:
            busy = False
            controls()

    def choose_file(event=None):
        try:
            guard()
            picker = wx.FileDialog(dialog, 'Choose a new measured-parts ZIP',
                wildcard='Measured PCM parts (*.zip)|*.zip', style=wx.FD_SAVE)
            selected = None
            try:
                if picker.ShowModal() == wx.ID_OK:
                    selected = picker.GetPath()
            finally:
                picker.Destroy()
            guard()
            if selected is not None:
                output.ChangeValue(selected)
                invalidate()
        except Exception as exc:
            report(exc)

    def preview(event=None):
        try:
            guard()
            frozen = values()
            index, milliseconds, template, target = frozen[0] + 1, int(frozen[1]), frozen[2], frozen[3]
            if not 1 <= index <= len(plan.parts):
                raise ValueError('Choose the exact completed source part.')
        except Exception as exc:
            report(exc)
            return
        invalidate()
        def accepted(value):
            nonlocal reviewed, reviewed_fields
            if values() != frozen:
                raise ValueError('The measured split or output changed. Preview this exact selection again.')
            reviewed, reviewed_fields = value, frozen
            report(review_text(plan, index, value))
        begin('Reviewing actual completed Book PCM frames',
            lambda: service.review_duration(plan.id, index, target, maximum_ms=milliseconds,
                name_template=template, cancelled=stopped, guard=worker_guard,
                progress=lambda message: wx.CallAfter(report, message)), accepted)

    def publish(event=None):
        try:
            guard()
            if reviewed is None or reviewed_fields != values():
                raise ValueError('Preview the exact measured ranges, names and new ZIP first.')
            approved, frozen = reviewed, reviewed_fields
            if wx.MessageBox('Publish %d exact PCM parts from this completed Book item?\n%s\n\n'
                    'This creates one new ZIP and starts no speech. Partial-line source references remain approximate.'
                    % (len(approved['content']['parts']), approved['output']),
                    'Publish reviewed measured audio', wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                return
            guard()
            if reviewed is not approved or reviewed_fields != frozen or values() != frozen:
                raise ValueError('The measured review changed during confirmation. Preview it again.')
        except Exception as exc:
            report(exc)
            return
        def complete(record):
            nonlocal reviewed, reviewed_fields
            reviewed, reviewed_fields = None, None
            report('Published and verified: ' + record['output'] +
                   '\nPublication receipt: ' + record['id'] + '\n' + record['notice'])
        begin('Publishing reviewed measured Book audio',
            lambda: service.publish_duration(plan.id, frozen[0] + 1, frozen[3],
                maximum_ms=int(frozen[1]), name_template=frozen[2], approval=approved['approval_sha256'],
                cancelled=stopped, guard=worker_guard,
                progress=lambda message: wx.CallAfter(report, message)), complete)

    def close(event=None):
        nonlocal alive
        if not alive:
            return
        cancel.set()
        alive = False
        if getattr(dialog, 'IsModal', lambda: True)():
            dialog.EndModal(wx.ID_CANCEL)
        # The parent Book dialog owns the service and its worker finalizer.

    for name, handler in (('file', choose_file), ('preview', preview), ('publish', publish),
                          ('cancel', lambda event: cancel.set()), ('close', close)):
        buttons[name].Bind(wx.EVT_BUTTON, handler)
    for widget in (parts, maximum, names, output):
        widget.Bind(wx.EVT_CHOICE if widget is parts else wx.EVT_TEXT, invalidate)
    def destroyed(event):
        nonlocal alive
        if event.GetEventObject() is dialog:
            cancel.set()
            alive = False
        event.Skip()

    dialog.Bind(wx.EVT_CLOSE, close)
    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    controls()
    parts.SetFocus()
    return dialog, close


def show_book_duration(frame, *, service, plan, owner_guard=lambda: None,
                       owner_cancelled=lambda: False):
    dialog, close = create_book_duration_dialog(frame, service=service, plan=plan,
        owner_guard=owner_guard, owner_cancelled=owner_cancelled)
    try:
        dialog.ShowModal()
    finally:
        close()
        dialog.Destroy()
