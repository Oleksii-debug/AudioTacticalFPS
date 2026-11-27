"""Reviewed local audio tags and cover, using the Desktop's existing worker."""
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from uuid import uuid4

from ..audio.metadata import (TAG_FIELDS, MetadataChanges, MetadataResult,
                              inspect_metadata, review_metadata, publish_metadata, metadata_disk_bytes)
from ..audio.models import AudioCancelled
from ..audio.cleanup import _pending_owners, retained_context
from ..durable_state import local_path, regular
from ..research.export_file import ExportReceipt, _destination, export_completion
from ..audio import metadata_review


LABELS = {
    'title': 'Title', 'artist': 'Artist or author', 'album': 'Album or book',
    'album_artist': 'Album artist or book author', 'composer': 'Composer',
    'genre': 'Genre', 'date': 'Date', 'track': 'Track number, optionally /total',
    'disc': 'Disc number, optionally /total', 'comment': 'Comment',
    'copyright': 'Copyright notice',
}
AUDIO_WILDCARD = 'Supported audio (*.mp3;*.flac;*.m4a;*.m4b)|*.mp3;*.flac;*.m4a;*.m4b'
COVER_MODES = ('Keep existing cover', 'Replace with selected PNG or JPEG', 'Remove existing cover')


@dataclass(frozen=True)
class MetadataForm:
    source: str
    output: str
    changes: MetadataChanges


def review_text(plan):
    tags = dict(plan.snapshot.tags)
    for key, value in plan.changes.tags:
        if value:
            tags[key] = value
        else:
            tags.pop(key, None)
    cover = (plan.snapshot.cover if plan.changes.cover_mode == 'keep'
             else plan.replacement_cover if plan.changes.cover_mode == 'replace' else None)
    return '\n'.join([
        f'Source: {plan.snapshot.source.path}',
        f'Source SHA-256: {plan.snapshot.source.sha256}',
        f'New output: {plan.output}',
        'The encoded audio is copied without re-encoding. Existing measured chapters are verified.',
        f'Existing chapters: {len(plan.snapshot.chapters)}',
        'Cover: ' + (f'{cover.codec}, {cover.width} × {cover.height}; SHA-256 {cover.sha256}' if cover else 'none'),
        'Cover choice: ' + plan.changes.cover_mode,
        'Resulting portable tags:',
        *(f'{LABELS[key]}: {tags.get(key, "(absent)")}' for key in TAG_FIELDS),
        'Other existing metadata must survive verification. A missing or changed field prevents saving.',
        'A new adjacent .metadata.json receipt records source, output, image and audio stream hashes.',
    ])


def saved_text(result):
    text = f'Saved {result.audio.path}. Encoded audio and existing chapters were verified.'
    if result.receipt_path is not None:
        text += f' Receipt: {result.receipt_path}.'
    if result.warning:
        text += ' ' + result.warning
    return text


class MetadataController:
    """Owns only admission/cancellation; the existing Desktop owns the worker."""
    def __init__(self, frame, notify, *, runtime_factory=None):
        from ..runtime_settings import audio_runtime
        self.frame, self.notify = frame, notify
        self.library = frame.library
        self.root = local_path(self.library.root, exists=True)
        info = regular(self.root, directory=True)
        self.root_identity = info.st_dev, info.st_ino
        self.runtime_factory = runtime_factory or audio_runtime
        self.cancel_event = Event()
        self.closed = self.busy = False
        self.generation = 0
        self.snapshot = self.plan = self.runtime = self.form = self.result = None
        self.saved_choices = None

    def guard(self):
        if (self.closed or getattr(self.frame, '_closing', False)
                or self.frame.library is not self.library
                or local_path(self.library.root, exists=True) != self.root):
            raise ValueError('The library changed or closed. Reopen audio metadata.')
        info = regular(self.root, directory=True)
        if (info.st_dev, info.st_ino) != self.root_identity:
            raise ValueError('The library directory changed. Reopen audio metadata.')

    def cancelled(self):
        if self.closed or self.cancel_event.is_set() or self.frame.cancel_event.is_set():
            return True
        self.guard()
        return False

    def _available(self):
        self.guard()
        if self.busy or getattr(self.frame, 'working', False):
            raise ValueError('Finish the current operation before starting audio metadata work.')

    def invalidate(self, *, source=False):
        self.generation += 1
        self.plan = self.runtime = self.form = None
        if source:
            self.snapshot = None
        if self.busy:
            self.cancel_event.set()

    def cancel(self):
        self.cancel_event.set()

    def close(self):
        self.closed = True
        self.invalidate()

    def _admitted(self, runtime, operation, *, disk_bytes=0):
        """Keep the current per-library resource and native cleanup owners."""
        from ..operations import ResourceBroker, ResourceCancelled, ResourceRequest
        self.guard()
        request = ResourceRequest(cpu_slots=runtime.budget.threads,
            memory_bytes=max(512 * 1024**2, 2 * runtime.budget.max_alloc_bytes),
            disk_path=runtime.root if disk_bytes else None, disk_bytes=disk_bytes)
        result, lease = None, None
        try:
            broker = ResourceBroker.for_app(self.root)
            with retained_context(broker.reserve(request,
                    operation_id='audio-metadata:' + uuid4().hex, cancelled=self.cancelled,
                    timeout=min(60, runtime.budget.timeout),
                    run_timeout=min(86400, runtime.budget.timeout))) as lease:
                result = operation(lease.cancelled)
        except ResourceCancelled as exc:
            if not isinstance(result, MetadataResult):
                raise AudioCancelled('Audio metadata work cancelled before shared resource admission.') from exc
            result = replace(result, warning=(result.warning + ' Audio was saved and verified, '
                'but resource release reported cancellation: ' + str(exc)[:1000]).strip())
        except (Exception, KeyboardInterrupt) as exc:
            if not isinstance(result, MetadataResult) or _pending_owners(exc):
                raise
            result = replace(result, warning=(result.warning + ' Audio was saved and verified. '
                'Shared resource release was not confirmed: ' + type(exc).__name__ + ': ' + str(exc)[:1000]).strip())
        if lease is not None and lease.cleanup_pending and isinstance(result, MetadataResult):
            result = replace(result, warning=(result.warning + ' Shared resource ownership was '
                'released; stale coordination metadata awaits normal cleanup.').strip())
        return result

    def _run(self, title, operation, complete):
        self._available()
        self.cancel_event.clear()
        generation = self.generation
        self.busy = True
        self.notify('busy', title)

        def failed(exc):
            self.busy = False
            self.plan = self.runtime = self.form = None
            if not self.closed:
                self.notify('failed', str(exc))

        def done(value):
            self.busy = False
            # A successful publication is durable even if Close/Cancel arrives
            # between its commit and this UI callback. Never replace it by a
            # stale-library error, or access a destroyed dialog.
            if isinstance(value, MetadataResult):
                self.result = value
                self.plan = self.runtime = self.form = None
                if self.closed:
                    if not getattr(self.frame, '_closing', False):
                        self.frame.report(saved_text(value))
                else:
                    self.notify('saved', value)
                return
            if isinstance(value, ExportReceipt):
                self.plan = self.runtime = self.form = None
                message = export_completion('Saved retained artwork to ' + str(value.path)
                    + '. Select that file explicitly and review the audio again before saving.', value)
                if self.closed:
                    if not getattr(self.frame, '_closing', False):
                        self.frame.report(message)
                else:
                    self.notify('artwork-saved', message)
                return
            if self.closed:
                return
            try:
                self.guard()
                if generation != self.generation:
                    raise ValueError('The form changed during review. Inspect or review the current choices again.')
                complete(value)
            except Exception as exc:
                failed(exc)

        try:
            self.frame.run_job(title, operation, done, failed, with_progress=True)
        except Exception as exc:
            failed(exc)

    def inspect(self, source):
        self._available()
        self.invalidate(source=True)
        selected = str(source)
        def operation(report):
            self.guard()
            path = local_path(selected, exists=True)
            regular(path)
            runtime = self.runtime_factory(path.parent, state_root=self.root / 'state', input_roots=(path.parent,))
            return self._admitted(runtime, lambda stopping:
                                  inspect_metadata(runtime, path, cancelled=stopping))
        def complete(snapshot):
            self.snapshot = snapshot
            self.notify('loaded', snapshot)
        self._run('Inspecting local audio metadata', operation, complete)

    def review(self, form):
        self._available()
        if not isinstance(form, MetadataForm) or self.snapshot is None:
            raise ValueError('Inspect the selected audio before reviewing changes.')
        loaded = self.snapshot
        if Path(form.source).absolute() != loaded.source.path:
            raise ValueError('Inspect the newly selected audio before reviewing changes.')
        self.invalidate()
        def operation(report):
            self.guard()
            target, _, previous = _destination(Path(form.output).absolute(), self.root, require_absent=True)
            if previous is not None:
                raise FileExistsError('Choose a new output filename; existing audio is not replaced.')
            roots = [loaded.source.path.parent]
            if form.changes.cover_path is not None:
                roots.append(local_path(form.changes.cover_path, exists=True).parent)
            runtime = self.runtime_factory(target.parent, state_root=self.root / 'state', input_roots=tuple(roots))
            def reviewed(stopping):
                plan = review_metadata(runtime, loaded.source.path, target, form.changes, cancelled=stopping)
                if plan.snapshot.source != loaded.source:
                    raise ValueError('The source changed after inspection. Inspect it again before reviewing tags.')
                metadata_review.save_choices(self.root, runtime, plan, cancelled=stopping)
                return plan
            return runtime, self._admitted(runtime, reviewed)
        def complete(value):
            self.runtime, self.plan = value
            self.form = form
            self.notify('reviewed', self.plan)
        self._run('Reviewing audio metadata and cover', operation, complete)

    def load_saved_choices(self):
        self._available()
        self.invalidate()
        def operation(report):
            self.guard()
            if self.cancelled():
                raise AudioCancelled('Saved metadata loading cancelled.')
            value = metadata_review.load_choices(self.root)
            if value is None:
                raise ValueError('No reviewed metadata choices have been saved in this library.')
            if self.cancelled():
                raise AudioCancelled('Saved metadata loading cancelled.')
            return value
        def complete(value):
            self.saved_choices = value
            self.notify('choices', value)
        self._run('Loading passive audio metadata choices', operation, complete)

    def export_cover(self, destination):
        self._available()
        self.invalidate()
        selected = str(destination)
        def operation(report):
            self.guard()
            return metadata_review.export_saved_cover(self.root, selected, cancelled=self.cancelled)
        self._run('Exporting explicitly selected retained artwork', operation, lambda value: None)

    def save(self, form):
        self._available()
        if self.plan is None or form != self.form:
            raise ValueError('Review the current metadata, cover and new filename before saving.')
        plan, runtime = self.plan, self.runtime
        def operation(report):
            self.guard()
            target, identity, previous = _destination(plan.output, self.root)
            if target != plan.output or identity != plan.parent_identity or previous is not None:
                raise ValueError('The approved output changed. Review a new destination.')
            return self._admitted(runtime, lambda stopping:
                publish_metadata(runtime, plan, cancelled=stopping,
                    progress=lambda seconds: report(round(seconds * 1000), None, 'Copying reviewed audio and metadata')),
                disk_bytes=metadata_disk_bytes(plan) + runtime.budget.reserve_disk_bytes)
        self._run('Saving reviewed audio metadata', operation, lambda value: None)


def show_audio_metadata(frame):
    """Explicit source → inspect → edit → review → save; never starts playback."""
    import wx
    if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
        frame.report('Finish the current operation before opening audio metadata.')
        return None
    class MetadataDialog(wx.Dialog):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._native_id = self.GetId()
            self._destroy_requested = False
            self.metadata_controller = None
            self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)

        def dispose(self):
            if self.metadata_controller is not None:
                self.metadata_controller.close()

        def on_destroy(self, event):
            if event.GetId() == self._native_id:
                self._destroy_requested = True
                self.dispose()
            event.Skip()

        def Destroy(self):
            if self._destroy_requested or not self:
                return False
            self._destroy_requested = True
            self.dispose()
            return super().Destroy()

    dialog = MetadataDialog(frame, title='Audio metadata and cover', size=(900, 830),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    panel = wx.ScrolledWindow(dialog, style=wx.VSCROLL)
    panel.SetScrollRate(0, 12)
    layout = wx.BoxSizer(wx.VERTICAL)
    notice = wx.StaticText(panel, label='Choose MP3, FLAC or M4A/M4B audio, inspect its tags, then review your edits. '
        'Save creates a new file and verifies the encoded audio, chapters and cover. '
        'Select only a local PNG/JPEG you may use. PNG/JPEG covers: at most 8 MiB and 16 million pixels.')
    notice.Wrap(820)
    layout.Add(notice, 0, wx.ALL | wx.EXPAND, 6)
    editable, buttons = [], {}

    def field(label, value='', *, readonly=False, multiline=False):
        layout.Add(wx.StaticText(panel, label=label), 0, wx.LEFT | wx.TOP, 6)
        style = (wx.TE_READONLY if readonly else 0) | (wx.TE_MULTILINE | wx.TE_DONTWRAP if multiline else 0)
        control = wx.TextCtrl(panel, value=value, style=style)
        control.SetName(label)
        if not readonly:
            control.SetMaxLength(4096)
            editable.append(control)
        layout.Add(control, 0, wx.ALL | wx.EXPAND, 6)
        return control

    def button(key, label):
        control = wx.Button(panel, label=label)
        control.SetName(label)
        buttons[key] = control
        layout.Add(control, 0, wx.ALL, 5)
        return control

    source = field('Selected source audio', readonly=True)
    button('source', 'Choose source audio')
    button('inspect', 'Inspect source tags and chapters')
    button('load', 'Load saved tag choices for a new review')
    button('saved-cover', 'Export saved artwork to a new file')
    fields = {key: field(LABELS[key], multiline=key == 'comment') for key in TAG_FIELDS}
    layout.Add(wx.StaticText(panel, label='An empty field removes that portable tag after review.'), 0, wx.ALL, 6)
    mode = wx.Choice(panel, choices=COVER_MODES); mode.SetName('Cover action'); mode.SetSelection(0)
    editable.append(mode); layout.Add(mode, 0, wx.ALL | wx.EXPAND, 6)
    cover = field('Selected local PNG or JPEG cover', readonly=True)
    button('cover', 'Choose replacement cover')
    output = field('New output audio filename', readonly=True)
    button('output', 'Choose new output filename')
    details = field('Reviewed metadata and cover', readonly=True, multiline=True)
    details.SetMinSize((-1, 180))
    button('review', 'Review metadata and cover changes')
    button('save', 'Save reviewed audio copy')
    button('cancel', 'Cancel current metadata operation')
    button('progress', 'Current operation progress')
    button('close', 'Close')
    status = field('Audio metadata status', 'Choose a source and inspect its current tags.', readonly=True)
    panel.SetSizer(layout)
    outer = wx.BoxSizer(wx.VERTICAL); outer.Add(panel, 1, wx.EXPAND)
    dialog.SetSizer(outer); panel.FitInside()
    dialog.SetEscapeId(buttons['close'].GetId())

    def alive():
        return not controller.closed and not dialog._destroy_requested and bool(dialog)

    def form():
        if not alive():
            raise ValueError('The metadata dialog was closed.')
        selected_mode = ('keep', 'replace', 'remove')[mode.GetSelection()]
        return MetadataForm(source.GetValue(), output.GetValue(), MetadataChanges(
            {key: control.GetValue() for key, control in fields.items()}, selected_mode,
            cover.GetValue() or None if selected_mode == 'replace' else None))

    def controls():
        if not alive():
            return
        busy = controller.busy
        for control in editable:
            control.Enable(not busy)
        for key in ('source', 'output', 'load'):
            buttons[key].Enable(not busy)
        buttons['saved-cover'].Enable(not busy and controller.saved_choices is not None
            and controller.saved_choices['cover'] is not None)
        buttons['inspect'].Enable(not busy and bool(source.GetValue()))
        buttons['cover'].Enable(not busy and mode.GetSelection() == 1)
        buttons['review'].Enable(not busy and controller.snapshot is not None and bool(output.GetValue()))
        buttons['save'].Enable(not busy and controller.plan is not None)
        buttons['cancel'].Enable(busy)

    def notify(kind, value):
        if not alive():
            return
        if kind == 'loaded':
            for key, control in fields.items():
                control.ChangeValue(dict(value.tags).get(key, ''))
            mode.SetSelection(0); cover.ChangeValue('')
            details.ChangeValue(f'Source SHA-256: {value.source.sha256}\n'
                f'{len(value.chapters)} existing chapter(s). '
                + (f'Existing cover: {value.cover.codec}, {value.cover.width} × {value.cover.height}.'
                   if value.cover else 'No existing cover.'))
            status.ChangeValue('Tags loaded. Edit fields, choose a new filename, then review.')
        elif kind == 'choices':
            for key, content in value['changes']['tags'].items():
                fields[key].ChangeValue(content)
            selected_mode = controller.saved_choices['changes']['cover_mode']
            mode.SetSelection(('keep', 'replace', 'remove').index(selected_mode))
            cover.ChangeValue(''); details.ChangeValue('')
            status.ChangeValue('Saved choices loaded without source or output approval. '
                'Inspect an explicitly selected source. For a retained cover, export it to a new file, '
                'select that file, then review all choices again.')
        elif kind == 'reviewed':
            details.ChangeValue(review_text(value))
            status.ChangeValue('Review ready; passive choices and artwork are saved for backup. '
                'Choose Save reviewed audio copy to publish these exact choices.')
            details.SetFocus()
        elif kind == 'saved':
            status.ChangeValue(saved_text(value)); status.SetFocus()
        elif kind == 'artwork-saved':
            status.ChangeValue(value); status.SetFocus()
        else:
            status.ChangeValue(str(value))
        controls()

    controller = MetadataController(frame, notify)
    dialog.metadata_controller = controller

    def changed(event=None):
        if not alive():
            return
        controller.invalidate()
        details.ChangeValue('')
        status.ChangeValue('Changes need a new review before saving.')
        controls()

    def action(callback):
        def invoke(event=None):
            if not alive():
                return
            try:
                callback()
            except Exception as exc:
                if alive():
                    status.ChangeValue(str(exc)); controls()
        return invoke

    def pick(kind):
        if not alive() or controller.busy:
            return
        is_source, is_cover = kind == 'source', kind == 'cover'
        picker = wx.FileDialog(dialog,
            'Choose source audio' if is_source else 'Choose a replacement cover' if is_cover else 'Choose a new audio filename',
            wildcard='PNG or JPEG (*.png;*.jpg;*.jpeg)|*.png;*.jpg;*.jpeg' if is_cover else AUDIO_WILDCARD,
            defaultFile=(Path(source.GetValue()).stem + '-tagged' + Path(source.GetValue()).suffix) if kind == 'output' else '',
            style=wx.FD_SAVE if kind == 'output' else wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            result = picker.ShowModal()
            if not alive() or not picker or result != wx.ID_OK:
                return
            selected = picker.GetPath()
        finally:
            if picker:
                picker.Destroy()
        if not alive():
            return
        controller.invalidate(source=is_source)
        {'source': source, 'cover': cover, 'output': output}[kind].ChangeValue(selected)
        if is_source:
            for control in fields.values():
                control.ChangeValue('')
            mode.SetSelection(0); cover.ChangeValue(''); output.ChangeValue('')
        details.ChangeValue('')
        status.ChangeValue('Inspect the source first.' if is_source else 'Review the current choices before saving.')
        controls()

    def export_cover():
        value = controller.saved_choices
        if value is None or value['cover'] is None:
            raise ValueError('Load a saved review with replacement artwork first.')
        suffix = '.png' if value['cover']['codec'] == 'png' else '.jpg'
        picker = wx.FileDialog(dialog, 'Export saved artwork to a new file',
            defaultFile='reviewed-cover' + suffix,
            wildcard='PNG or JPEG (*.png;*.jpg;*.jpeg)|*.png;*.jpg;*.jpeg', style=wx.FD_SAVE)
        try:
            if picker.ShowModal() != wx.ID_OK or not alive() or not picker:
                return
            selected = picker.GetPath()
        finally:
            if picker:
                picker.Destroy()
        if alive():
            controller.export_cover(selected)

    def query_progress(event=None):
        progress = getattr(frame, 'operation_progress', None)
        if not alive() or progress is None:
            return
        controller.guard()
        progress.query(event)
        status.ChangeValue(frame.status.GetValue())
        status.SetFocus()

    def close(event=None):
        controller.close()
        if not dialog._destroy_requested and dialog and dialog.IsModal():
            dialog.EndModal(wx.ID_CANCEL)

    for control in fields.values():
        control.Bind(wx.EVT_TEXT, changed)
    mode.Bind(wx.EVT_CHOICE, changed)
    for key in ('source', 'cover', 'output'):
        buttons[key].Bind(wx.EVT_BUTTON, action(lambda selected=key: pick(selected)))
    buttons['inspect'].Bind(wx.EVT_BUTTON, action(lambda: controller.inspect(source.GetValue())))
    buttons['load'].Bind(wx.EVT_BUTTON, action(controller.load_saved_choices))
    buttons['saved-cover'].Bind(wx.EVT_BUTTON, action(export_cover))
    buttons['review'].Bind(wx.EVT_BUTTON, action(lambda: controller.review(form())))
    buttons['save'].Bind(wx.EVT_BUTTON, action(lambda: controller.save(form())))
    buttons['cancel'].Bind(wx.EVT_BUTTON, action(controller.cancel))
    buttons['progress'].Bind(wx.EVT_BUTTON, action(query_progress))
    buttons['progress'].Enable(getattr(frame, 'operation_progress', None) is not None)
    from .audio_runtime_setup import _bind_modal_progress_query
    _bind_modal_progress_query(frame, dialog, query_progress)
    buttons['close'].Bind(wx.EVT_BUTTON, close)
    dialog.Bind(wx.EVT_CLOSE, close)
    controls()
    source.SetFocus()
    try:
        dialog.ShowModal()
    finally:
        controller.close()
        if dialog and not dialog._destroy_requested:
            dialog.Destroy()
    return controller.result

