"""Accessible review and existing desktop-worker dispatch for audio batches."""
import json
from pathlib import Path

from ..audio.models import AudioCancelled
from ..audio.conversion_batch import AudioConversionBatch, _settings, _options
from ..durable_state import local_path, regular
from ..runtime_settings import audio_runtime
from ..state_backup import relative_name


def _choose_directory(frame):
    import wx
    chooser = wx.DirDialog(frame, 'Choose a parent folder for converted audio',
                           style=wx.DD_DIR_MUST_EXIST)
    try:
        if chooser.ShowModal() != wx.ID_OK:
            return None
        parent = Path(chooser.GetPath())
    finally:
        chooser.Destroy()
    name = wx.TextEntryDialog(frame, 'Name of the selected batch folder',
                              'Batch output folder', 'Converted audio')
    try:
        if name.ShowModal() != wx.ID_OK:
            return None
        selected = name.GetValue().strip()
        relative = relative_name(selected)
        if len(relative.parts) != 1:
            raise ValueError('Choose one batch folder name inside the selected parent.')
        return parent / selected
    finally:
        name.Destroy()


def _review_text(plan, resume):
    lines = [
        ('Resume only matching saved outputs.' if resume else 'Create a new converted-audio folder.'),
        'Original files will remain unchanged. Existing output files are never overwritten.',
        'Output folder: ' + plan['directory'],
        'Output format: ' + plan['format'].upper(),
        f"Output: {plan['settings']['sample_rate']} Hz, {plan['settings']['channels']} channel(s), "
        f"{plan['settings']['bit_depth']}-bit PCM / {plan['settings']['bitrate']} kbps for encoded formats.",
        f"Selected files: {len(plan['sources'])}. Review each source and output below.",
        '',
    ]
    for row in plan['sources']:
        lines += [f"{row['index'] + 1}. Source: " + json.dumps(row['source'], ensure_ascii=False),
                  '   Output: ' + row['output']]
        if row['inspection_error']:
            lines.append('   Cannot convert this file: ' + row['inspection_error'])
        else:
            info = row['information']
            lines.append(f"   Measured input: {info['duration']:.3f} seconds, "
                         f"{info['sample_rate']} Hz, {info['channels']} channel(s).")
    lines += ['', 'A failed input is recorded separately. Cancellation keeps completed files. '
              'Continuing a saved batch requires selecting the same originals and reviewed settings.']
    return '\n'.join(lines)


def _confirm_plan(frame, plan, resume):
    import wx
    dialog = wx.Dialog(frame, title='Review audio conversion batch', size=(900, 680),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    outer = wx.BoxSizer(wx.VERTICAL)
    text = wx.TextCtrl(dialog, value=_review_text(plan, resume),
                       style=wx.TE_MULTILINE | wx.TE_READONLY)
    text.SetName('Selected audio batch review')
    outer.Add(text, 1, wx.EXPAND | wx.ALL, 8)
    buttons = dialog.CreateButtonSizer(wx.OK | wx.CANCEL)
    outer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
    dialog.SetSizer(outer)
    confirm = dialog.FindWindowById(wx.ID_OK)
    confirm.SetLabel('Convert reviewed files')
    confirm.SetName('Convert reviewed files')
    cancel = dialog.FindWindowById(wx.ID_CANCEL)
    cancel.SetDefault()
    text.SetFocus()
    try:
        return dialog.ShowModal() == wx.ID_OK
    finally:
        dialog.Destroy()


def start_audio_batch(frame, sources, *, format='wav', resume=False, **settings):
    """Capture fields now; review/hash/encode only through the owned worker."""
    if getattr(frame, '_closing', False) or getattr(frame, 'working', False):
        return
    library = frame.library
    root = local_path(library.root, exists=True)
    opened = regular(root, directory=True)
    root_identity = (opened.st_dev, opened.st_ino)
    selected_sources = tuple(str(Path(value).absolute()) for value in sources)
    selected_settings = _settings(**settings)
    if type(resume) is not bool:
        raise ValueError('Choose an explicit matching-batch resume option.')
    destination = _choose_directory(frame)
    if destination is None:
        return
    destination = str(Path(destination).absolute())
    cancellation = frame.cancel_event

    def scope():
        if getattr(frame, '_closing', False):
            raise AudioCancelled('The audio conversion owner is closing.')
        if frame.library is not library or local_path(frame.library.root, exists=True) != root:
            raise ValueError('The selected library changed; review conversion again.')
        current = regular(root, directory=True)
        if (current.st_dev, current.st_ino) != root_identity:
            raise ValueError('The selected library directory was replaced.')

    def available():
        return not getattr(frame, '_closing', False)

    def failed(error):
        if available():
            frame.report('Audio batch: ' + str(error))

    def prepare(progress):
        scope()
        runtime = audio_runtime(Path(destination).parent, state_root=root / 'state',
                                input_roots=tuple(dict.fromkeys(Path(path).parent for path in selected_sources)))
        service = AudioConversionBatch(runtime, library_root=root, scope_guard=scope)
        plan = service.prepare(selected_sources, destination, format=format, resume=resume,
                               cancelled=cancellation.is_set, progress=progress,
                               **_options(selected_settings))
        scope()
        return {'status': 'completed', 'service': service, 'plan': plan}

    def finished(result):
        if not available():
            return
        # The receipt belongs to the captured output even if the active library
        # later changed. Do not relabel already committed bytes as a failure.
        message = (f"Audio batch {result['status']}: {result['done']} of {len(result['items'])} files saved; "
                   f"{result['failed']} failed.\nFolder: {result['plan']['directory']}")
        if result['warning']:
            message += '\n' + result['warning']
        errors = [row['output'] + ': ' + row['error'] for row in result['items'] if row['error']]
        if errors:
            message += '\n' + '\n'.join(errors)
        frame.report(message)

    def reviewed(result):
        if not available():
            return
        try:
            scope()
            if cancellation.is_set():
                raise AudioCancelled('The batch review was cancelled.')
            plan, service = result['plan'], result['service']
            if not _confirm_plan(frame, plan, resume):
                frame.report('Audio batch review cancelled. Existing outputs were unchanged; no conversion started.')
                return
            scope()
            if cancellation.is_set():
                raise AudioCancelled('The batch review was cancelled.')
            frame.run_job('Converting reviewed audio batch',
                lambda progress: service.run(plan, resume=resume, cancelled=cancellation.is_set,
                                              progress=progress),
                finished, failed, progress_total=len(plan['sources']),
                progress_unit='files', with_progress=True)
        except Exception as exc:
            failed(exc)

    frame.run_job('Reviewing selected audio batch', prepare, reviewed, failed,
                  progress_total=len(selected_sources), progress_unit='files', with_progress=True)
