"""Read completed transcripts through their existing output receipts.

The preview is a bounded, verified snapshot, not an editor or a second reader
store. Hashing and UTF-8 validation stream on the desktop's existing executor.
"""
from __future__ import annotations

import codecs
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re

from ..audio.jobs import checked_directory, regular_file
from ..audio.model_manifest import local_path
from ..audio.transcription import MAX_RESULT_BYTES, receipt_notes


MAX_PREVIEW_BYTES = 64 * 1024
MAX_TRANSCRIPT_BYTES = MAX_RESULT_BYTES + 1
_HASH = re.compile(r'[0-9a-f]{64}')


@dataclass(frozen=True)
class TranscriptPreview:
    text: str
    output_folder: Path
    total_bytes: int
    displayed_bytes: int
    truncated: bool

    @property
    def summary(self):
        if self.truncated:
            return (f'Verified TXT snapshot. Preview truncated: showing the first '
                    f'{self.displayed_bytes} of {self.total_bytes} UTF-8 bytes. '
                    'The complete transcript remains in the output folder.')
        return ('Verified TXT snapshot. Empty transcript.' if not self.text else
                f'Verified TXT snapshot. Complete text: {self.total_bytes} UTF-8 bytes.')


def _file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _cancel(cancelled):
    if cancelled():
        raise InterruptedError('Transcript preview cancelled.')


def read_transcript_preview(receipt, *, max_preview_bytes=MAX_PREVIEW_BYTES,
                            cancelled=lambda: False):
    """Verify all TXT bytes but retain only a bounded prefix for native display.

    Expected bytes and SHA-256 come from the completed service receipt. Edited,
    replaced, linked, oversized or malformed results are never silently shown as
    verified. Verification does not certify acoustic accuracy or speaker identity.
    """
    _cancel(cancelled)
    if type(max_preview_bytes) is not int or not 1 <= max_preview_bytes <= MAX_PREVIEW_BYTES:
        raise ValueError('Choose a bounded transcript preview size.')
    if not isinstance(receipt, dict) or receipt.get('status') != 'complete':
        raise ValueError('Only completed transcription results can be previewed.')
    identity = receipt.get('identity')
    if not isinstance(identity, str) or not _HASH.fullmatch(identity):
        raise ValueError('The transcript receipt has no valid result identity.')
    files, outputs = receipt.get('files'), receipt.get('outputs')
    if not isinstance(files, dict) or not isinstance(outputs, dict) or 'txt' not in files:
        raise ValueError('This completed result has no TXT output to preview.')
    record = outputs.get('transcript.txt')
    if (not isinstance(record, dict) or set(record) != {'sha256', 'bytes'}
            or not isinstance(record['sha256'], str) or not _HASH.fullmatch(record['sha256'])
            or type(record['bytes']) is not int or not 0 <= record['bytes'] <= MAX_TRANSCRIPT_BYTES):
        raise ValueError('The TXT output receipt is invalid or exceeds the verification budget.')
    expected_bytes, expected_hash = record['bytes'], record['sha256']
    raw_path = files['txt']
    if not isinstance(raw_path, (str, Path)) or not Path(raw_path).is_absolute():
        raise ValueError('The completed TXT output requires an absolute local path.')
    path = local_path(raw_path)
    if path.name != 'transcript.txt' or path.parent.name != 'transcript-' + identity:
        raise ValueError('The TXT output path does not match its completed result identity.')
    before = regular_file(path)
    if before.st_size != expected_bytes:
        raise ValueError('The transcript was changed after completion. Its current contents were preserved.')
    digest, prefix, total = sha256(), bytearray(), 0
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    try:
        with path.open('rb') as stream:
            if _file_identity(os.fstat(stream.fileno())) != _file_identity(before):
                raise ValueError('The transcript changed while it was opened for verification.')
            while True:
                _cancel(cancelled)
                block = stream.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > expected_bytes or total > MAX_TRANSCRIPT_BYTES:
                    raise ValueError('The transcript changed or exceeded its verification budget.')
                if b'\x00' in block:
                    raise ValueError('The completed TXT output contains NUL; it was preserved.')
                digest.update(block)
                decoder.decode(block, final=False)
                remaining = max_preview_bytes - len(prefix)
                if remaining > 0:
                    prefix.extend(block[:remaining])
            decoder.decode(b'', final=True)
            after_open = os.fstat(stream.fileno())
        _cancel(cancelled)
        after_path = regular_file(path)
        if (total != expected_bytes or digest.hexdigest() != expected_hash
                or _file_identity(after_open) != _file_identity(before)
                or _file_identity(after_path) != _file_identity(before)):
            raise ValueError('The transcript was changed after completion. Its current contents were preserved.')
        truncated = total > len(prefix)
        # A byte limit may cut through a Unicode character. Retain complete
        # characters only; the whole file's UTF-8 was validated above.
        preview_decoder = codecs.getincrementaldecoder('utf-8')('strict')
        text = preview_decoder.decode(bytes(prefix), final=not truncated)
    except UnicodeError:
        raise ValueError('The completed TXT output is not valid UTF-8; it was preserved.') from None
    if '\x00' in text:
        raise ValueError('The completed TXT output contains NUL; it was preserved.')
    return TranscriptPreview(text, path.parent, total, len(text.encode('utf-8')), truncated)


def show_transcription_results(parent, entries, *, job_host=None):
    """Show explicitly selected completed inputs; reuse the existing job host."""
    import wx
    host = job_host if job_host is not None else parent.GetParent()
    if getattr(host, '_closing', False):
        return
    if getattr(host, 'working', False) or getattr(host, 'busy', False):
        host.report('Wait for the current operation before viewing completed transcripts.')
        return
    entries = tuple(entries)
    if not 1 <= len(entries) <= 1000:
        raise ValueError('Choose a batch with between 1 and 1000 completed results.')
    for row in entries:
        if (not isinstance(row, (tuple, list)) or len(row) != 2
                or not isinstance(row[0], str) or not row[0] or len(row[0]) > 1024
                or '\x00' in row[0] or not isinstance(row[1], dict)):
            raise ValueError('A source display name and completion receipt are required.')

    class Dialog(wx.Dialog):
        def __init__(self):
            super().__init__(parent, title='Completed transcriptions', size=(850, 700),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.pending, self.closed, self.current = False, False, None
            layout = wx.BoxSizer(wx.VERTICAL)
            layout.Add(wx.StaticText(self, label='Completed &inputs'), 0, wx.ALL, 6)
            self.inputs = wx.ListBox(self, choices=[row[0] for row in entries])
            self.inputs.SetName('Completed transcription inputs')
            self.inputs.Bind(wx.EVT_LISTBOX, self.load_selected)
            layout.Add(self.inputs, 1, wx.EXPAND | wx.ALL, 6)
            self.preview = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.preview.SetName('Verified transcript preview')
            layout.Add(self.preview, 3, wx.EXPAND | wx.ALL, 6)
            self.details = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            self.details.SetName('Transcription result details')
            layout.Add(self.details, 1, wx.EXPAND | wx.ALL, 6)
            buttons = wx.BoxSizer(wx.HORIZONTAL)
            self.folder = wx.Button(self, label='Open output &folder')
            self.folder.Bind(wx.EVT_BUTTON, self.open_folder)
            self.folder.Disable()
            self.cancel = wx.Button(self, label='&Cancel preview')
            self.cancel.Bind(wx.EVT_BUTTON, self.cancel_preview)
            self.cancel.Disable()
            close = wx.Button(self, wx.ID_CLOSE, '&Close')
            close.Bind(wx.EVT_BUTTON, self.close)
            for button in (self.folder, self.cancel, close):
                buttons.Add(button, 0, wx.ALL, 6)
            layout.Add(buttons, 0, wx.ALL, 4)
            self.SetSizer(layout)
            self.SetMinSize((600, 480))
            self.SetEscapeId(wx.ID_CLOSE)
            self.Bind(wx.EVT_CLOSE, self.close)
            self.inputs.SetSelection(0)
            self.inputs.SetFocus()
            wx.CallAfter(self.load_selected)

        def set_pending(self, value):
            self.pending = value
            self.inputs.Enable(not value)
            self.cancel.Enable(value)
            self.folder.Enable(not value and self.current is not None)

        def load_selected(self, event=None):
            if self.closed or self.pending:
                return
            index = self.inputs.GetSelection()
            if not 0 <= index < len(entries):
                return
            if getattr(host, '_closing', False) or host.working:
                self.details.SetValue('Wait for the current operation before loading this preview.')
                return
            name, receipt = entries[index]
            self.current = None
            self.preview.ChangeValue('')
            self.preview.SetName('Verified transcript preview')
            self.set_pending(True)
            self.details.SetValue('Verifying the completed TXT output before showing its preview.')

            def completed(result):
                if self.closed:
                    return
                self.current = result
                self.set_pending(False)
                self.preview.ChangeValue(result.text)
                if result.truncated:
                    self.preview.SetName('Verified transcript preview (truncated)')
                notes = receipt_notes(receipt)
                self.details.SetValue(name + '\n' + result.summary + '\nOutput folder: '
                    + str(result.output_folder) + ('\n' + '\n'.join(notes) if notes else ''))
                self.preview.SetFocus()

            def failed(error):
                if not self.closed:
                    self.set_pending(False)
                    self.details.SetValue(str(error))
                    self.details.SetFocus()

            host.run_job('Verifying transcript preview',
                lambda: read_transcript_preview(receipt, cancelled=host.cancel_event.is_set), completed, failed)

        def open_folder(self, event=None):
            try:
                if self.current is None or self.pending:
                    return
                directory = checked_directory(self.current.output_folder)
                if not wx.LaunchDefaultApplication(str(directory)):
                    raise OSError('The output folder could not be opened.')
            except Exception as exc:
                self.details.SetValue(str(exc))
                self.details.SetFocus()

        def cancel_preview(self, event=None):
            if self.pending:
                host.cancel_event.set()
                self.details.SetValue('Cancelling preview verification; all transcript files remain unchanged.')

        def close(self, event=None):
            if self.pending:
                self.cancel_preview()
                if event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
                    event.Veto()
                return
            self.closed = True
            self.EndModal(wx.ID_CLOSE)

    dialog = Dialog()
    try:
        dialog.ShowModal()
    finally:
        dialog.closed = True
        dialog.Destroy()
