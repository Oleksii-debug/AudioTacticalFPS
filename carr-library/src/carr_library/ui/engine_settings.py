"""Native local engine review; asset hashing and enumeration run off the UI thread."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import threading

from ..durable_state import read_json
from ..runtime_settings import load_settings, save_settings


class EngineSettingsReview:
    """One cancellable review worker with UI-dispatched, lifetime-checked results."""

    def __init__(self, state_root, *, dispatch):
        self.state_root = Path(state_root)
        self.dispatch = dispatch
        self.settings = load_settings(state_root)  # Structural state only; no engine access.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-engine-review')
        self._future = None
        self._cancel = threading.Event()
        self._closed = False
        self._generation = 0

    @property
    def busy(self):
        return self._future is not None

    def _submit(self, action, complete, *, updates_settings=False):
        if self._closed:
            raise ValueError('The local engine window is closed.')
        if self.busy:
            raise ValueError('A local engine review is already running.')
        self._cancel = threading.Event()
        cancelled = self._cancel
        self._generation += 1
        generation = self._generation
        future = self._executor.submit(action, cancelled.is_set)
        self._future = future

        def done(result):
            if self._closed:
                return
            def finish():
                if self._closed or generation != self._generation:
                    return
                self._future = None
                try:
                    value, error = result.result(), None
                except Exception as exc:
                    value, error = None, exc
                if updates_settings and error is None:
                    self.settings = value
                complete(value, error)
            self.dispatch(finish)
        future.add_done_callback(done)
        return future

    def review(self, candidate=None, *, complete):
        snapshot = deepcopy(self.settings if candidate is None else candidate)
        def action(cancelled):
            save_settings(self.state_root, snapshot, cancelled=cancelled)
            # Return the exact published document, including a cleared restore
            # marker, instead of echoing the pre-review in-memory candidate.
            return load_settings(self.state_root)
        return self._submit(action, complete, updates_settings=True)

    def load_registration(self, path, *, complete):
        selected = Path(path)
        def action(cancelled):
            if cancelled():
                raise InterruptedError('Local registration review was cancelled.')
            value = read_json(selected)
            if type(value) is not dict:
                raise ValueError('A local registration must be a JSON object.')
            if cancelled():
                raise InterruptedError('Local registration review was cancelled.')
            return value
        return self._submit(action, complete)

    def cancel(self):
        if not self.busy:
            return False
        self._cancel.set()
        return True

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._cancel.set()
        self._executor.shutdown(wait=False, cancel_futures=True)


def describe_engine_settings(settings):
    restored = settings.get('restore_requires_revalidation', False)
    return (
        ('RESTORED: engine activation is blocked until you review and revalidate these registrations.\n'
         if restored else 'Local registration review is current; installed assets are checked again when used.\n')
        + 'All paths refer to local installations. No download or remote API is started.\n'
        + 'FFmpeg: ' + str(settings['ffmpeg'] or 'Discover installed executable') + '\n'
        + 'FFprobe: ' + str(settings['ffprobe'] or 'Discover installed executable') + '\n'
        + f'Piper registrations: {len(settings["piper"])}\n'
        + f'Explicit voice helpers: {len(settings["voice_helpers"])}\n'
        + 'Neural music configurations: ' + ', '.join(model['engine'] for model in settings['music_models']) + '\n'
        + 'MusicGen and ACE-Step inference requires a separately isolated Linux network namespace; '
          'Windows inference is unavailable. MusicGen weights also require the private noncommercial profile.\n'
        + 'Registered assets are distinct from tested inference. Helpers must implement the reviewed local '
          'JSON contract. Studio voice registrations remain managed in Local voice engines.')


def show_engine_settings(frame):
    import wx

    state_root = frame.library.root / 'state'
    review = EngineSettingsReview(state_root, dispatch=wx.CallAfter)
    dialog = wx.Dialog(frame, title='Local audio, voice and music engines', size=(850, 750),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    status = wx.TextCtrl(dialog, style=wx.TE_MULTILINE | wx.TE_READONLY)
    status.SetName('Local engine configuration, review and availability')
    layout.Add(status, 1, wx.EXPAND | wx.ALL, 6)
    actions = []
    alive = True

    def describe(message=''):
        if alive:
            status.ChangeValue((message + '\n\n' if message else '') + describe_engine_settings(review.settings))

    def enabled():
        if alive:
            for button in actions:
                button.Enable(not review.busy)
            cancel_button.Enable(review.busy)

    def finished(value, error):
        if not alive:
            return
        if error is not None:
            describe('Local review stopped: ' + (str(error) or 'The operation was cancelled.'))
            status.SetFocus()
        else:
            describe('Local registrations were verified and saved. This is not a neural-inference acceptance test.')
        enabled()

    def start_review(candidate=None):
        if review.busy:
            return
        try:
            review.review(candidate, complete=finished)
        except Exception as exc:
            describe(str(exc))
            return
        status.ChangeValue('Reviewing local file identities and enabled engine catalogs. '
                           'The window stays available. Cancel requests a stop; completed saves remain saved.')
        enabled()

    def executable(key):
        picker = wx.FileDialog(dialog, 'Choose installed ' + key, style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() == wx.ID_OK:
                start_review({**review.settings, key: picker.GetPath()})
        finally:
            picker.Destroy()

    def register(kind):
        picker = wx.FileDialog(dialog, 'Choose reviewed local engine registration JSON',
                               wildcard='JSON (*.json)|*.json', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() != wx.ID_OK:
                return
            selected = Path(picker.GetPath())
        finally:
            picker.Destroy()

        def loaded(row, error):
            if not alive:
                return
            if error is not None:
                finished(None, error)
                return
            if kind == 'voice_helpers':
                message = ('Enable this explicitly reviewed local helper?\n\n'
                           'Engine: ' + str(row.get('engine_name', ''))[:256] + '\n'
                           'Executable: ' + str(row.get('executable', ''))[:2048] + '\n'
                           'Expected SHA256: ' + str(row.get('sha256', ''))[:128] + '\n\n'
                           'It will run with your account permissions during review and synthesis.')
                if wx.MessageBox(message, 'Enable local voice helper',
                                 wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                    describe('Registration was not enabled.')
                    enabled()
                    return
                row = {**row, 'enabled': True}
            candidate = {**review.settings, kind: [*review.settings[kind], row]}
            start_review(candidate)
        try:
            review.load_registration(selected, complete=loaded)
        except Exception as exc:
            describe(str(exc))
            return
        status.ChangeValue('Reading the selected local registration. No engine has been enabled.')
        enabled()

    def reset(event):
        if wx.MessageBox('Remove local engine overrides? Installed files will remain on disk.', 'Reset configuration',
                         wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
            return
        candidate = {**review.settings, 'ffmpeg': None, 'ffprobe': None, 'piper': [],
                     'voice_helpers': [], 'music_models': [], 'fluidsynth': None}
        start_review(candidate)

    def cancel(event):
        if review.cancel():
            status.ChangeValue('Cancellation requested. The worker is stopping its current check. '
                               'A save that already completed remains saved.')

    def close(event=None):
        nonlocal alive
        if not alive:
            return
        alive = False
        review.close()
        if dialog.IsModal():
            dialog.EndModal(wx.ID_CLOSE)

    for label, callback in [
        ('Review and revalidate local registrations', lambda event: start_review()),
        ('Choose FFmpeg executable', lambda event: executable('ffmpeg')),
        ('Choose FFprobe executable', lambda event: executable('ffprobe')),
        ('Register a local Piper voice manifest', lambda event: register('piper')),
        ('Register a MusicGen or ACE-Step installation', lambda event: register('music_models')),
        ('Register a reviewed local voice helper', lambda event: register('voice_helpers')),
        ('Reset overrides', reset),
    ]:
        button = wx.Button(dialog, label=label)
        button.Bind(wx.EVT_BUTTON, callback)
        actions.append(button)
        layout.Add(button, 0, wx.EXPAND | wx.ALL, 4)
    cancel_button = wx.Button(dialog, label='Cancel current review')
    cancel_button.SetName('Cancel current local engine review')
    cancel_button.Bind(wx.EVT_BUTTON, cancel)
    layout.Add(cancel_button, 0, wx.EXPAND | wx.ALL, 4)
    layout.Add(dialog.CreateButtonSizer(wx.CLOSE), 0, wx.ALL, 5)
    dialog.SetEscapeId(wx.ID_CLOSE)
    dialog.Bind(wx.EVT_BUTTON, close, id=wx.ID_CLOSE)
    dialog.Bind(wx.EVT_CLOSE, close)
    dialog.SetSizer(layout)
    describe()
    enabled()
    try:
        dialog.ShowModal()
    finally:
        close()
        dialog.Destroy()
