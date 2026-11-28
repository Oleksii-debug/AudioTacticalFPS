"""Explicit native setup for one reviewed local audio runtime.

The shared frame worker performs download, extraction, hashing and version
checks. This module creates no executor and no activation during preparation.
"""
from pathlib import Path
import threading

from ..audio.local_runtime import (install_or_select, prepare_local_audio_runtime,
                                   source_summary)
from ..runtime_settings import load_settings
from ..audio.render import RenderCancelled


class AudioSetupController:
    def __init__(self, frame):
        self.frame = frame
        self.library = frame.library
        self.root = Path(self.library.root)
        self.settings = load_settings(self.root / 'state')
        self.alive, self.busy = True, False
        self._cancel = threading.Event()
        self._generation = 0

    def _current(self):
        return (self.alive and not getattr(self.frame, '_closing', False)
                and self.frame.library is self.library
                and Path(self.frame.library.root) == self.root)

    def cancel(self):
        if not self.busy:
            return False
        self._cancel.set()
        self.frame.cancel_event.set()
        return True

    def close(self):
        if self.alive:
            self.cancel()
            self.alive = False
            self._generation += 1

    def start(self, *, complete, verify=False, archive=None, existing=None, download=False):
        if not self._current():
            raise ValueError('The local audio setup belongs to a closed or changed library.')
        if self.busy or getattr(self.frame, 'working', False):
            raise ValueError('Wait for the current operation or cancel it before local audio setup.')
        self.busy = True
        self._generation += 1
        generation = self._generation
        own_cancel = self._cancel = threading.Event()
        shared_cancel = self.frame.cancel_event

        def cancelled():
            return (own_cancel.is_set() or shared_cancel.is_set() or not self._current())

        def operation(report):
            def progress(phase, completed, total):
                report(completed, total, phase)
            if cancelled():
                raise InterruptedError('Local audio setup was cancelled before it started.')
            try:
                if verify:
                    binding = prepare_local_audio_runtime(self.root, cancelled=cancelled, progress=progress)
                    return {'status': 'VERIFIED', 'binding': binding}
                return install_or_select(self.root, archive=archive, existing=existing,
                    download=download, expected_settings=self.settings,
                    cancelled=cancelled, progress=progress)
            except RenderCancelled as error:
                # The existing frame classifies InterruptedError as cancellation.
                raise InterruptedError(str(error)) from None

        def done(result):
            self.busy = False
            binding = result['binding']
            if generation != self._generation or not self._current() or cancelled():
                binding.close()
                was_registered = result['status'] == 'REGISTERED'
                result['status'] = 'CANCELLED'
                if self._current():
                    complete({'status': 'REGISTERED_NOT_ACTIVATED' if was_registered else 'CANCELLED',
                        'message': ('Local registration was saved. Activation was cancelled; use Verify current '
                                    'before producing audio.' if was_registered else
                                    'Verification was cancelled. The active runtime was not changed.')}, None)
                return
            self.settings = binding.settings or load_settings(self.root / 'state')
            binding.activate(previous=getattr(self.frame, 'audio_runtime_binding', None), strict=False)
            self.frame.audio_runtime_binding = binding
            if binding.notice:
                result['status'] = 'FAILED'
                raise RuntimeError(binding.notice)
            result['status'] = 'COMPLETED'
            complete({'status': 'ACTIVE' if binding.directory is not None else 'NO_SELECTION',
                      'message': ('The exact local runtime is verified and active for this library.'
                                  if binding.directory is not None else
                                  'No complete local runtime is selected. Explicit legacy registrations remain available.'),
                      'runtime_directory': str(binding.directory) if binding.directory is not None else None,
                      'manifest_sha256': (binding.report or {}).get('manifest_sha256')}, None)

        def failed(error):
            self.busy = False
            if generation == self._generation and self._current():
                complete(None, error)

        try:
            self.frame.run_job('Verify local audio runtime' if verify else 'Set up local audio runtime',
                               operation, done, failed, with_progress=True,
                               discard=lambda result: result['binding'].close())
        except BaseException:
            self.busy = False
            raise


def download_message():
    information = source_summary()
    archive = information['archive']
    return ('Download and install the exact reviewed FFmpeg ' + information['release'] + ' full Windows pack?\n\n'
            + 'Source: ' + archive['url'] + '\n'
            + 'Archive bytes: ' + str(archive['size_bytes']) + '\n'
            + 'SHA256: ' + archive['sha256'] + '\n'
            + 'License: ' + information['license'] + '\n\n'
            + 'This is an explicit local installation. It stays outside the application and library backup. '
              'This action does not grant permission to transfer or redistribute the runtime.\n\n'
              'The shared operation progress and Cancel remain available. A saved registration remains '
              'saved if cancellation arrives after its atomic publication.')


def confirm_download(parent):
    import wx
    dialog = wx.MessageDialog(parent, download_message(), 'Download reviewed local audio runtime',
                              wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
    try:
        return dialog.ShowModal() == wx.ID_YES
    finally:
        dialog.Destroy()


def _bind_modal_progress_query(frame, dialog, handler):
    """Reuse the live menu command and its configured native accelerator."""
    import wx
    keyboard = getattr(frame, 'keyboard_actions', None)
    binding = next((binding for binding in getattr(keyboard, 'bindings', ())
                    if binding.command.identity == 'Help/Current operation progress'), None)
    if binding is None:
        return ''
    item = binding.item
    entry = item.GetAccel()
    if entry is None:
        return ''
    dialog.Bind(wx.EVT_MENU, handler, id=item.GetId())
    dialog.SetAcceleratorTable(wx.AcceleratorTable([
        (entry.GetFlags(), entry.GetKeyCode(), item.GetId())]))
    label = item.GetItemLabel()
    return label.split('\t', 1)[1] if '\t' in label else ''


def show_audio_runtime_setup(frame):
    import wx

    controller = AudioSetupController(frame)
    dialog = wx.Dialog(frame, title='Local audio runtime setup', size=(800, 650),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName('Local audio runtime setup')
    layout = wx.BoxSizer(wx.VERTICAL)
    description = wx.StaticText(dialog, label=(
        'Choose an existing complete reviewed runtime, use the exact local ZIP, or explicitly download it.\n'
        'A transferable application does not include this optional runtime. No neural models are acquired.'))
    layout.Add(description, 0, wx.EXPAND | wx.ALL, 8)
    route = wx.Choice(dialog, choices=['Existing complete runtime folder', 'Exact reviewed full-build ZIP'])
    route.SetName('Local audio setup source type')
    route.SetSelection(0)
    layout.Add(route, 0, wx.EXPAND | wx.ALL, 6)
    path = wx.TextCtrl(dialog)
    path.SetName('Local audio setup source path')
    layout.Add(path, 0, wx.EXPAND | wx.ALL, 6)
    status = wx.TextCtrl(dialog, style=wx.TE_MULTILINE | wx.TE_READONLY)
    status.SetName('Local audio setup status')
    layout.Add(status, 1, wx.EXPAND | wx.ALL, 6)
    actions = []

    def refresh(message=''):
        if not controller.alive:
            return
        settings = controller.settings
        text = ('Restored registrations require explicit review in Scene voice and music engines.\n'
                if settings.get('restore_requires_revalidation') else '')
        text += ('Selected manifest SHA256: ' + str(settings.get('audio_runtime_manifest_sha256', 'none')) + '\n'
                 'FFmpeg: ' + str(settings['ffmpeg'] or 'no override') + '\n'
                 'FFprobe: ' + str(settings['ffprobe'] or 'no override'))
        status.ChangeValue((message + '\n\n' if message else '') + text)
        for control in (*actions, route, path):
            control.Enable(not controller.busy)
        cancel_button.Enable(controller.busy)

    def query_progress(event=None):
        progress = getattr(frame, 'operation_progress', None)
        if progress is None or not controller._current():
            return
        progress.query(event)
        # The modal window owns keyboard focus; mirror the existing query result
        # here instead of relying on focus in its disabled parent frame.
        refresh(frame.status.GetValue())
        status.SetFocus()

    def complete(result, error):
        if error is not None:
            refresh('Local audio setup stopped: ' + (str(error) or 'Cancelled.'))
        else:
            refresh(result['message'])
        status.SetFocus()

    def start(**options):
        try:
            controller.start(complete=complete, **options)
        except Exception as error:
            refresh(str(error))
            return
        refresh('Working in the background. Use Current operation progress'
                + (' or ' + progress_shortcut if progress_shortcut else '') + '. '
                'Cancel requests a stop without waiting on the window thread.')

    def browse(event):
        if route.GetSelection() == 0:
            picker = wx.DirDialog(dialog, 'Choose the complete reviewed local runtime',
                                  style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST)
        else:
            picker = wx.FileDialog(dialog, 'Choose the exact reviewed full-build ZIP',
                wildcard='ZIP archives (*.zip)|*.zip', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() == wx.ID_OK:
                path.ChangeValue(picker.GetPath())
        finally:
            picker.Destroy()

    def select(event):
        if not path.GetValue().strip():
            refresh('Choose a local source path first.')
            return
        value = Path(path.GetValue())
        start(**({'existing': value} if route.GetSelection() == 0 else {'archive': value}))

    def download(event):
        if confirm_download(dialog):
            # The modal can outlive a scope change/cancellation. Controller.start
            # checks current ownership again before submitting any network work.
            start(download=True)
        else:
            refresh('Download was not requested. No network operation was started.')

    def cancel(event):
        if controller.cancel():
            refresh('Cancellation requested. The current read or child process is stopping. '
                    'A registration already saved remains saved.')

    def close(event=None):
        controller.close()
        if dialog.IsModal():
            dialog.EndModal(wx.ID_CLOSE)

    for label, callback in (
        ('Browse local source...', browse),
        ('Verify and select local source', select),
        ('Download and install reviewed runtime...', download),
        ('Verify current registration', lambda event: start(verify=True)),
    ):
        button = wx.Button(dialog, label=label)
        button.SetName(label.rstrip('.'))
        button.Bind(wx.EVT_BUTTON, callback)
        actions.append(button)
        layout.Add(button, 0, wx.EXPAND | wx.ALL, 4)
    cancel_button = wx.Button(dialog, label='Cancel current setup')
    cancel_button.SetName('Cancel current local audio setup')
    cancel_button.Bind(wx.EVT_BUTTON, cancel)
    layout.Add(cancel_button, 0, wx.EXPAND | wx.ALL, 4)
    query_button = wx.Button(dialog, label='Current operation progress')
    query_button.SetName('Current operation progress')
    query_button.Bind(wx.EVT_BUTTON, query_progress)
    query_button.Enable(getattr(frame, 'operation_progress', None) is not None)
    # Deliberately outside actions: query stays enabled while the worker runs.
    layout.Add(query_button, 0, wx.EXPAND | wx.ALL, 4)
    progress_shortcut = _bind_modal_progress_query(frame, dialog, query_progress)
    layout.Add(dialog.CreateButtonSizer(wx.CLOSE), 0, wx.ALL, 5)
    dialog.Bind(wx.EVT_BUTTON, close, id=wx.ID_CLOSE)
    dialog.Bind(wx.EVT_CLOSE, close)
    dialog.SetEscapeId(wx.ID_CLOSE)
    dialog.SetSizer(layout)
    refresh()
    try:
        dialog.ShowModal()
    finally:
        close()
        dialog.Destroy()


def install_audio_runtime_setup(frame):
    """Add one entry to the existing Studio menu before command discovery."""
    import wx
    for index in range(frame.menu_bar.GetMenuCount()):
        if frame.menu_bar.GetMenuLabel(index).replace('&', '').casefold() == 'audio studio':
            menu = frame.menu_bar.GetMenu(index)
            action = menu.Append(wx.ID_ANY, 'Local audio runtime setup...')
            frame.Bind(wx.EVT_MENU, lambda event: show_audio_runtime_setup(frame), action)
            return
    raise RuntimeError('Install the existing Audio Studio menu before local audio setup.')
