"""One explicit batch review using the host's existing worker and progress hub."""
from pathlib import Path

from ..audio.conversion_batch import AudioConversionBatch, FORMATS, MANIFEST


def show_reviewed_batch(frame, library_root, sources, settings, *, current_root, scope_current):
    import wx
    current_root()
    chooser = wx.DirDialog(frame, 'Choose an existing folder for new converted files',
                           style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
    try:
        if chooser.ShowModal() != wx.ID_OK:
            return
        current_root()
        directory = Path(chooser.GetPath())
    finally:
        chooser.Destroy()
    dialog = wx.Dialog(frame, title='Review audio batch destination', size=(680, 420),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    text = wx.TextCtrl(dialog, value=f'{len(sources)} explicitly selected files.\nOutput folder: {directory}\n'
        'Existing files are never replaced. The external manifest records the selected paths and receipts.',
        style=wx.TE_READONLY | wx.TE_MULTILINE)
    text.SetName('Audio batch destination and selected file count')
    layout.Add(text, 1, wx.EXPAND | wx.ALL, 8)
    layout.Add(wx.StaticText(dialog, label='Output format for every selected file'), 0, wx.ALL, 8)
    formats = sorted(extension[1:] for extension in FORMATS if extension != '.aif')
    format_choice = wx.Choice(dialog, choices=formats)
    format_choice.SetName('Audio batch output format')
    format_choice.SetSelection(formats.index('wav'))
    layout.Add(format_choice, 0, wx.EXPAND | wx.ALL, 8)
    resume = wx.CheckBox(dialog, label='Resume this exact saved batch after a new source and settings review')
    resume.SetName('Explicitly resume saved audio batch')
    layout.Add(resume, 0, wx.ALL, 8)
    layout.Add(dialog.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL, 8)
    dialog.SetSizer(layout)
    try:
        if dialog.ShowModal() != wx.ID_OK:
            return
        current_root()
        selected_format = formats[format_choice.GetSelection()]
        selected_resume = bool(resume.GetValue())
    finally:
        dialog.Destroy()
    service = AudioConversionBatch(library_root, scope_guard=current_root)
    frozen_sources = tuple(sources)

    def failed(error):
        if scope_current():
            frame.report('Audio batch: ' + str(error))

    def completed(result):
        if not scope_current():
            return
        message = (f'Audio batch {result["status"]}: {result["done"]}/{result["total"]} committed outputs. '
                   f'Receipts: {result["manifest"]}.')
        if result.get('warning'):
            message += ' ' + result['warning']
        if not result['receipt_persisted']:
            message += ' The latest success receipt was not durably recorded; explicit review is required.'
        frame.report(message)

    def present_review(review):
        if not scope_current():
            return
        if frame.cancel_event.is_set():
            frame.report('Audio batch review was cancelled; no audio outputs were written.')
            return
        current_root()
        plan = review.plan
        lines = [f'{row["source"]}\n  → {row["output"]}' for row in plan['sources']]
        message = (f'{len(lines)} explicit files; format {plan["format"]}; '
                   f'{plan["settings"]["sample_rate"]} Hz / {plan["settings"]["channels"]} channels.\n'
                   f'Ordered effects: {len(plan["settings"]["effects"])}.\n'
                   f'Output folder: {plan["directory"]}\n'
                   f'Review identity: {review.fingerprint}\n\n' + '\n'.join(lines) +
                   '\n\nRun only this reviewed selection? Existing unreceipted files require attention; '
                   'matching bytes do not authorize recovery. Cancel retains completed outputs.')
        confirm = wx.Dialog(frame, title='Run reviewed audio batch?', size=(820, 620),
                            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        layout = wx.BoxSizer(wx.VERTICAL)
        reviewed_text = wx.TextCtrl(confirm, value=message, style=wx.TE_READONLY | wx.TE_MULTILINE)
        reviewed_text.SetName('Reviewed audio batch files and settings')
        layout.Add(reviewed_text, 1, wx.EXPAND | wx.ALL, 8)
        layout.Add(confirm.CreateButtonSizer(wx.YES | wx.NO), 0, wx.ALL, 8)
        confirm.SetSizer(layout)
        yes = confirm.FindWindowById(wx.ID_YES)
        no = confirm.FindWindowById(wx.ID_NO)
        yes.SetName('Run exactly reviewed audio batch')
        no.SetName('Keep audio review without running')
        no.SetDefault()
        no.SetFocus()
        yes.Bind(wx.EVT_BUTTON, lambda event: confirm.EndModal(wx.ID_YES))
        no.Bind(wx.EVT_BUTTON, lambda event: confirm.EndModal(wx.ID_NO))
        try:
            approved = confirm.ShowModal() == wx.ID_YES
            current_root()
        finally:
            confirm.Destroy()
        if not approved:
            if scope_current():
                frame.report('Audio batch was not started; the reviewed originals are unchanged.')
            return
        current_root()
        frame.run_job('Converting reviewed audio batch',
            lambda update: service.run(review, cancelled=frame.cancel_event.is_set, progress=update),
            completed, failed, with_progress=True, progress_total=len(frozen_sources), progress_unit='files')

    def reviewed(review):
        try:
            present_review(review)
        except Exception as error:
            failed(error)  # A replaced/closing host receives no late report or focus.

    current_root()
    frame.run_job('Reviewing explicit audio batch',
        lambda update: service.prepare(frozen_sources, directory, format=selected_format,
            settings=settings, resume=selected_resume, cancelled=frame.cancel_event.is_set, progress=update),
        reviewed, failed, with_progress=True, progress_total=len(frozen_sources), progress_unit='files')
