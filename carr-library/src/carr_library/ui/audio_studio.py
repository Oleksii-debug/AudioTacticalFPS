"""Native local narration projects with worker-only discovery and rendering.

The editor contains a narration derivative, never mutable library source text.
Audio engines and durable project operations live in audio.voices/director.
"""
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
import shutil
import stat


@dataclass(frozen=True)
class AudioDraft:
    text: str = ''
    title: str = 'Narration'
    voice_id: str = ''
    rate: int = 0
    volume: int = 100
    ffmpeg: str = ''
    project: str = ''

    def content(self):
        return self.text, self.title, self.voice_id, self.rate, self.volume, self.project


def _voices(**kwargs):
    from ..audio.voices import list_voices
    return list_voices(**kwargs)


def _plan(*args, **kwargs):
    from ..audio.director import plan_narration
    return plan_narration(*args, **kwargs)


def _render(*args, **kwargs):
    from ..audio.director import render_narration
    return render_narration(*args, **kwargs)


def _save(*args, **kwargs):
    from ..audio.director import save_plan
    return save_plan(*args, **kwargs)


def _load(*args, **kwargs):
    from ..audio.director import load_plan
    return load_plan(*args, **kwargs)


def _post(function, *args):
    import wx
    wx.CallAfter(function, *args)


def _local_path(value, *, directory=False):
    if (not isinstance(value, str) or not value.strip() or '\0' in value
            or value.startswith(('\\\\', '//'))):
        raise ValueError('Choose a local path; network locations are unavailable in Audio Studio.')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Choose an absolute local path without parent traversal.')
    if (PureWindowsPath(path.name).is_reserved() or path.name.rstrip(' .') != path.name
            or any(ch in '<>:"|?*' or ord(ch) < 32 for ch in path.name)):
        raise ValueError('Choose a valid local project or executable name.')
    for parent in (*reversed(path.parents), path) if directory else reversed(path.parents):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            if directory and parent == path:
                break
            raise ValueError('The parent folder must already exist.') from None
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('Choose a local directory without links or junctions.')
    return path


def _project_path(value, private_root):
    path = _local_path(value, directory=True)
    target, private = path.resolve(), Path(private_root).resolve()
    if target.is_relative_to(private) or private.is_relative_to(target):
        raise ValueError('Choose an audio project outside the private library folder.')
    return path


def _ffmpeg_path(value):
    path = _local_path(value)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or path.name.casefold() not in ('ffmpeg', 'ffmpeg.exe')):
        raise ValueError('Choose the installed local FFmpeg executable.')
    return path


def _published_file(value, project):
    path = Path(value)
    if not path.is_absolute() or not path.is_relative_to(project):
        raise ValueError('The audio worker returned an output outside its project.')
    _local_path(str(path))
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400 or info.st_size == 0):
        raise ValueError('The audio worker did not publish a complete regular output file.')
    return path


def progress_text(value):
    """Expose bounded, factual tool progress without source text or code."""
    if not isinstance(value, dict):
        return 'Local audio operation is running.'
    labels = {'planning': 'Preparing narration', 'synthesizing': 'Generating speech',
              'cached': 'Reusing a verified chunk', 'synthesized': 'Speech chunk saved',
              'mixing': 'Assembling audio', 'complete': 'Verifying completed audio'}
    text = labels.get(value.get('state'), 'Local audio operation is running')
    completed, total = value.get('completed'), value.get('total')
    if type(completed) is int and type(total) is int and 0 <= completed <= total:
        text += f': {completed} of {total}'
    return text + '.'


class AudioStudioController:
    """UI-thread commands; all filesystem and engine work uses the host worker."""
    def __init__(self, frame, view, *, voice_loader=_voices, planner=_plan,
                 renderer=_render, saver=_save, loader=_load, post=_post):
        self.frame, self.view, self.library = frame, view, frame.library
        self.voice_loader, self.planner, self.renderer = voice_loader, planner, renderer
        self.saver, self.loader, self.post = saver, loader, post
        self.voices = ()
        self.alive, self.running = True, False
        self.epoch = 0
        self.saved = None

    def _current(self):
        return (self.alive and self.frame.library is self.library
                and not getattr(self.frame, '_closing', False))

    def _cancelled(self):
        return self.frame.cancel_event.is_set() or not self._current()

    def _ready(self):
        if not self._current():
            raise ValueError('The library folder changed. Reopen Audio Studio; your draft was retained.')
        if self.running or self.frame.working or getattr(self.frame, 'busy', False):
            raise ValueError('Wait for the current operation or choose Cancel operation.')

    def _check(self):
        if self._cancelled():
            raise InterruptedError('Audio operation cancelled; the editor text was retained.')

    def _job(self, title, operation, done):
        self._ready()
        self.running = True
        self.epoch += 1
        token = self.epoch
        self.view.set_busy(True)
        self.view.report(title + '…')

        def finish():
            self.running = False
            if self.alive:
                self.view.set_busy(False)
            elif hasattr(self.view, 'finish_disposal'):
                self.view.finish_disposal()

        def complete(value):
            finish()
            if self._current() and token == self.epoch:
                done(value)

        def failed(error):
            finish()
            if self.alive and token == self.epoch:
                self.view.report(f'{title} did not complete: {error} The editor text was retained.', focus=True)

        def execute():
            self._check()
            return operation(token)

        try:
            self.frame.run_job(title, execute, complete, failed)
        except Exception as exc:
            failed(exc)

    def reload_voices(self):
        try:
            self._ready()
            selected = self.view.values().voice_id

            def done(voices):
                if self._cancelled():
                    self.view.report('Voice discovery cancelled. Your existing voice selection was retained.')
                    return
                self.voices = tuple(voices)
                self.view.set_voices(self.voices, selected)
                self.view.report(f'{len(self.voices)} installed local voices found. Choose a voice and enter narration text.'
                    if self.voices else 'No enabled local SAPI 5 voices were found. Install a compatible Windows voice, then choose Reload voices. Your text remains editable.', focus=True)

            self._job('Discovering installed Windows voices',
                lambda _: self.voice_loader(cancelled=self._cancelled), done)
        except Exception as exc:
            self.view.report(str(exc), focus=True)

    def _make_plan(self, draft):
        if not draft.voice_id:
            raise ValueError('Choose a local voice before saving or generating this narration project.')
        return self.planner(draft.text, draft.voice_id, title=draft.title,
                            rate=draft.rate, volume=draft.volume)

    def save(self):
        try:
            self._ready()
            draft = self.view.values()

            def write(_):
                project = _project_path(draft.project, self.library.root)
                plan = self._make_plan(draft)
                self._check()
                result = self.saver(plan, project)
                return _published_file(result, project)

            def done(path):
                self.saved = draft.content()
                self.view.report(f'Narration project saved: {path}. Original library sources were not changed.', focus=True)

            self._job('Saving narration project', write, done)
        except Exception as exc:
            self.view.report(f'Project not saved: {exc} The editor text was retained.', focus=True)

    def generate(self):
        try:
            self._ready()
            draft = self.view.values()
            if draft.voice_id not in {voice.id for voice in self.voices}:
                raise ValueError('Choose an available local voice. Use Reload voices after installing or changing a voice.')

            def render(token):
                project = _project_path(draft.project, self.library.root)
                ffmpeg = _ffmpeg_path(draft.ffmpeg)
                plan = self._make_plan(draft)
                self._check()
                def progress(value):
                    self.post(self._progress, token, progress_text(value))
                result = self.renderer(plan, project, ffmpeg=ffmpeg,
                                       cancelled=self._cancelled, progress=progress)
                for path in (result.master, result.manifest, result.captions):
                    _published_file(path, project)
                if (Path(result.project) != project or type(result.duration_ms) is not int
                        or result.duration_ms <= 0):
                    raise ValueError('The audio worker returned an inconsistent completion result.')
                return result

            def done(result):
                self.saved = draft.content()
                self.view.show_output(str(result.master))
                self.view.report(f'Audio saved: {result.master}\n'
                    f'Duration: {result.duration_ms / 1000:.2f} seconds. '
                    f'Generated chunks: {result.generated_chunks}; reused chunks: {result.reused_chunks}.\n'
                    f'Captions: {result.captions}\nProduction record: {result.manifest}', focus=True)

            self._job('Generating local narration', render, done)
        except Exception as exc:
            self.view.report(f'Audio not generated: {exc} The editor text was retained.', focus=True)

    def _progress(self, token, text):
        if self._current() and self.running and self.epoch == token:
            self.view.report(text)

    def open_project(self, selected):
        try:
            self._ready()
            if self.dirty() and not self.view.confirm_replace():
                return
            previous = self.view.values()

            def load(_):
                project = _project_path(selected, self.library.root)
                plan = self.loader(project)
                if len(plan.roles) != 1:
                    raise ValueError('This project uses several voices. Open it through the script production workflow; this editor preserves single-voice narration projects.')
                role = plan.roles[0]
                return AudioDraft(plan.source_text, plan.title, role.voice_id, role.rate,
                                  role.volume, previous.ffmpeg, str(project))

            def done(draft):
                if self._cancelled() or self.view.values() != previous:
                    self.view.report('The draft changed or opening was cancelled. The current text was retained.', focus=True)
                    return
                self.view.set_draft(draft)
                self.saved = draft.content()
                self.view.report('Narration project opened. Generate resumes verified chunks for this project. Choose the local FFmpeg executable before rendering.', focus=True)

            self._job('Opening narration project', load, done)
        except Exception as exc:
            self.view.report(f'Project not opened: {exc}', focus=True)

    def use_source(self):
        """Load only the explicitly selected, freshly verified source excerpt."""
        try:
            self._ready()
            previous = self.view.values()
            if previous.text and not self.view.confirm_replace():
                return
            if self.frame.book is None or self.frame.active is None:
                raise ValueError('Select a verified result in the library Search tab first.')
            panel = self.frame.book.GetPage(0)
            hit = panel.selected()
            scope, epoch = self.frame.active, panel._context_epoch
            if (panel.bound_library is not self.library or hit.workspace != scope[0]
                    or (panel.bound_workspace, panel.bound_topic) != scope):
                raise ValueError('The selected source belongs to another library.')
            selection = hit.workspace, hit.ref, hit.revision
            verifier = panel.verified_copy_service

            def verify(_):
                if self.frame.active != scope or panel._context_epoch != epoch:
                    raise ValueError('The selected source changed. Select it again.')
                self._check()
                return verifier(*selection, 'quote')

            def done(text):
                current = panel.selected()
                if (self._cancelled() or self.frame.active != scope or panel._context_epoch != epoch
                        or (current.workspace, current.ref, current.revision) != selection
                        or self.view.values() != previous):
                    self.view.report('The source or draft changed. The current text was retained.', focus=True)
                    return
                self.view.set_draft(replace(previous, text=text, title=hit.title or previous.title))
                self.view.report('Verified source excerpt copied into the narration editor. Subsequent edits apply only to this narration project.', focus=True)

            self._job('Verifying the selected source excerpt', verify, done)
        except Exception as exc:
            self.view.report(f'Source text not loaded: {exc}', focus=True)

    def cancel(self):
        if self.running:
            self.frame.cancel_event.set()
            self.view.report('Cancelling the operation. Completed audio chunks remain available for resume; the editor text was retained.', focus=True)

    def dirty(self):
        draft = self.view.values()
        if self.saved is not None:
            return draft.content() != self.saved
        return bool(draft.text or draft.project or draft.title not in ('', 'Narration'))

    def request_close(self):
        if self.running:
            self.cancel()
            return False
        return not self.dirty() or self.view.confirm_discard()

    def dispose(self):
        self.alive = False
        self.epoch += 1
        if self.running:
            self.frame.cancel_event.set()


class NativeAudioStudio:
    def __init__(self, frame, draft=None):
        import wx
        self.wx, self.frame = wx, frame
        self.window = wx.Dialog(frame, title='Audio Studio — local narration', size=(880, 820),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.alive, self.deferred_destroy = True, False
        self.voice_ids, self.requested_voice = [], ''
        self.mutable = []
        layout = wx.BoxSizer(wx.VERTICAL)
        explanation = wx.StaticText(self.window, label='Create a local narration project with installed Windows voices. '
            'Ctrl+S saves the project. Generate resumes completed chunks. Original library sources remain unchanged.')
        explanation.Wrap(820)
        layout.Add(explanation, 0, wx.EXPAND | wx.ALL, 8)
        form = wx.FlexGridSizer(cols=2, hgap=8, vgap=6)
        form.AddGrowableCol(1)

        def field(label, control):
            control.SetName(label)
            form.Add(wx.StaticText(self.window, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            form.Add(control, 0, wx.EXPAND)
            self.mutable.append(control)
            return control

        self.title = field('Narration title', wx.TextCtrl(self.window))
        self.voice = field('Installed Windows voice (SAPI 5)', wx.Choice(self.window))
        self.rate = field('Voice rate (-10 to 10; 0 is default)', wx.SpinCtrl(self.window, min=-10, max=10, initial=0))
        self.volume = field('Voice volume (0 to 100)', wx.SpinCtrl(self.window, min=0, max=100, initial=100))
        self.ffmpeg = field('Local FFmpeg executable', wx.TextCtrl(self.window))
        self.project = field('Audio project folder (new or previously saved)', wx.TextCtrl(self.window))
        layout.Add(form, 0, wx.EXPAND | wx.ALL, 8)
        tools = wx.WrapSizer(wx.HORIZONTAL)
        def button(sizer, label, callback):
            control = wx.Button(self.window, label=label)
            control.Bind(wx.EVT_BUTTON, callback)
            sizer.Add(control, 0, wx.ALL, 4)
            self.mutable.append(control)
            return control

        self.reload = button(tools, '&Reload voices', lambda _: self.controller.reload_voices())
        button(tools, 'Choose &FFmpeg...', self.choose_ffmpeg)
        button(tools, '&New project folder...', self.choose_project)
        button(tools, '&Open project...', self.open_project)
        button(tools, 'Use selected source e&xcerpt', lambda _: self.controller.use_source())
        layout.Add(tools, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)
        layout.Add(wx.StaticText(self.window, label='Narration text'), 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self.text = wx.TextCtrl(self.window, style=wx.TE_MULTILINE | wx.TE_RICH2, size=(-1, 190))
        self.text.SetName('Editable narration text; original library sources are unchanged')
        self.text.SetMaxLength(0)
        self.mutable.append(self.text)
        layout.Add(self.text, 1, wx.EXPAND | wx.ALL, 8)
        actions = wx.WrapSizer(wx.HORIZONTAL)
        self.save_button = button(actions, '&Save project (Ctrl+S)', lambda _: self.controller.save())
        self.generate = button(actions, '&Generate narration', lambda _: self.controller.generate())
        self.cancel_button = wx.Button(self.window, label='&Cancel operation')
        self.cancel_button.Bind(wx.EVT_BUTTON, lambda _: self.controller.cancel())
        actions.Add(self.cancel_button, 0, wx.ALL, 4)
        layout.Add(actions, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)
        self.output = wx.TextCtrl(self.window, style=wx.TE_READONLY)
        self.output.SetName('Completed audio file path')
        layout.Add(self.output, 0, wx.EXPAND | wx.ALL, 8)
        self.status = wx.TextCtrl(self.window, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 100))
        self.status.SetName('Audio Studio operation status and output details')
        layout.Add(self.status, 0, wx.EXPAND | wx.ALL, 8)
        self.close_button = wx.Button(self.window, label='C&lose')
        self.close_button.Bind(wx.EVT_BUTTON, self.close)
        layout.Add(self.close_button, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        self.window.SetSizer(layout)
        self.window.SetMinSize((760, 720))
        self.window.SetEscapeId(self.close_button.GetId())
        self.window.Bind(wx.EVT_CLOSE, self.close)
        self.window.Bind(wx.EVT_WINDOW_DESTROY, self.destroyed)
        self.save_id = wx.NewIdRef()
        self.window.Bind(wx.EVT_MENU, lambda _: self.controller.save(), id=int(self.save_id))
        self.window.SetAcceleratorTable(wx.AcceleratorTable([(wx.ACCEL_CTRL, ord('S'), int(self.save_id))]))
        self.controller = AudioStudioController(frame, self, post=wx.CallAfter)
        self.set_draft(draft or AudioDraft(ffmpeg=shutil.which('ffmpeg') or ''))
        self.set_busy(False)
        self.report('Choose Reload voices to discover installed local speech engines. No remote service is used.')
        self.text.SetFocus()
        wx.CallAfter(self.controller.reload_voices)

    def values(self):
        index = self.voice.GetSelection()
        voice_id = self.voice_ids[index] if 0 <= index < len(self.voice_ids) else self.requested_voice
        return AudioDraft(self.text.GetValue(), self.title.GetValue(), voice_id,
            self.rate.GetValue(), self.volume.GetValue(), self.ffmpeg.GetValue(), self.project.GetValue())

    def set_draft(self, draft):
        for control, value in ((self.text, draft.text), (self.title, draft.title),
                               (self.ffmpeg, draft.ffmpeg), (self.project, draft.project)):
            control.ChangeValue(value)
        self.rate.SetValue(draft.rate)
        self.volume.SetValue(draft.volume)
        self.requested_voice = draft.voice_id
        self.voice.SetSelection(self.voice_ids.index(draft.voice_id) if draft.voice_id in self.voice_ids else -1)

    def set_voices(self, voices, selected):
        self.voice_ids = [voice.id for voice in voices]
        self.voice.SetItems([f'{number}. {voice.name}; {voice.language}; {voice.engine}'
                             for number, voice in enumerate(voices, 1)])
        index = self.voice_ids.index(selected) if selected in self.voice_ids else (0 if not selected and voices else -1)
        self.requested_voice = selected
        self.voice.SetSelection(index)

    def set_busy(self, busy):
        for control in self.mutable:
            control.Enable(not busy)
        self.cancel_button.Enable(busy)
        self.close_button.Enable(not busy)

    def report(self, text, *, focus=False):
        if self.alive:
            self.status.ChangeValue(text)
            self.status.SetInsertionPoint(0)
            if focus:
                self.status.SetFocus()

    def show_output(self, path):
        self.output.ChangeValue(path)

    def confirm_replace(self):
        return self.wx.MessageBox('Replace the current narration text? Save the current project first if you need it.',
            'Replace narration text', self.wx.YES_NO | self.wx.NO_DEFAULT | self.wx.ICON_QUESTION,
            self.window) == self.wx.YES

    def confirm_discard(self):
        return self.wx.MessageBox('This narration has unsaved changes. Close without saving?',
            'Unsaved narration', self.wx.YES_NO | self.wx.NO_DEFAULT | self.wx.ICON_QUESTION,
            self.window) == self.wx.YES

    def choose_ffmpeg(self, event=None):
        wx = self.wx
        dialog = wx.FileDialog(self.window, 'Select an installed local FFmpeg executable',
            wildcard='FFmpeg executable|ffmpeg.exe;ffmpeg|All files|*', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if dialog.ShowModal() == wx.ID_OK:
                self.ffmpeg.ChangeValue(dialog.GetPath())
        finally:
            dialog.Destroy()

    def choose_project(self, event=None):
        wx = self.wx
        dialog = wx.DirDialog(self.window, 'Choose the parent folder for a new audio project', style=wx.DD_DIR_MUST_EXIST)
        try:
            parent = dialog.GetPath() if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()
        if parent is None:
            return
        name = wx.TextEntryDialog(self.window, 'New project folder name', 'New audio project', 'Narration')
        try:
            if name.ShowModal() != wx.ID_OK:
                return
            value = name.GetValue()
            if not value or Path(value).name != value or '/' in value or '\\' in value:
                self.report('Choose one folder name; the selected parent folder is kept.', focus=True)
                return
            self.project.ChangeValue(str(Path(parent) / value))
        finally:
            name.Destroy()

    def open_project(self, event=None):
        wx = self.wx
        dialog = wx.DirDialog(self.window, 'Open an existing narration project', style=wx.DD_DIR_MUST_EXIST)
        try:
            selected = dialog.GetPath() if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()
        if selected is not None:
            self.controller.open_project(selected)

    def close(self, event=None):
        if not self.alive:
            return
        if self.controller.request_close():
            self.window.EndModal(self.wx.ID_CANCEL)
        elif event is not None and hasattr(event, 'CanVeto') and event.CanVeto():
            event.Veto()

    def destroyed(self, event):
        if event.GetEventObject() is self.window:
            self.alive = False
            self.controller.dispose()
        event.Skip()

    def finish_disposal(self):
        if self.deferred_destroy and self.alive:
            self.deferred_destroy = False
            self.window.Destroy()

    def show(self):
        try:
            self.window.ShowModal()
        finally:
            if self.alive:
                self.last_draft = self.values()
                if self.controller.running:
                    self.deferred_destroy = True
                    self.controller.dispose()
                    self.window.Hide()
                else:
                    self.controller.dispose()
                    self.window.Destroy()


class AudioStudioActions:
    def __init__(self, frame, factory=NativeAudioStudio):
        self.frame, self.factory, self.draft = frame, factory, None

    def open(self, event=None):
        if self.frame.working or getattr(self.frame, 'busy', False) or getattr(self.frame, '_closing', False):
            self.frame.report('Wait for the current operation before opening Audio Studio.')
            return
        try:
            dialog = self.factory(self.frame, self.draft)
            dialog.show()
            self.draft = getattr(dialog, 'last_draft', self.draft)
        except Exception as exc:
            self.frame.report(f'Audio Studio could not open: {exc}')


def install_audio_studio_actions(frame):
    existing = getattr(frame, 'audio_studio_actions', None)
    if existing is not None:
        return existing
    import wx
    actions = AudioStudioActions(frame)
    menu = wx.Menu()
    item = menu.Append(wx.ID_ANY, '&Narration studio...')
    frame.Bind(wx.EVT_MENU, actions.open, item)
    frame.menu_bar.Append(menu, 'A&udio')
    frame.audio_studio_actions = actions
    return actions
