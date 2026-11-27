"""Native publication actions over the existing Audio Studio output and host."""
from pathlib import Path

from ..audio.jobs import RenderCancelled
from ..audio.publishing import PublicationService, validate_publication_receipt
from ..progress import ProgressTracker
from ..research.export_file import capture_export_destination


def publication_outcome(value):
    validate_publication_receipt(value)
    if value['status'] != 'complete':
        raise ValueError('Publication did not confirm a complete verified result.')
    return 'completed'


class PublicationActions:
    def __init__(self, frame, *, service_factory=PublicationService):
        self.frame, self.service_factory = frame, service_factory

    def _idle(self):
        frame = self.frame
        if getattr(frame, '_closing', False):
            return False
        if getattr(frame, 'working', False) or getattr(frame, 'busy', False):
            frame.report('Wait for the current operation or cancel it before publishing audio.')
            return False
        return True

    def _service(self):
        import wx
        frame = self.frame
        controller = getattr(frame, 'active_studio_controller', None)
        default = str(getattr(controller, 'output_dir', None) or '')
        dialog = wx.DirDialog(frame, 'Choose an Audio Studio output folder containing completed renders',
                              defaultPath=default, style=wx.DD_DIR_MUST_EXIST)
        dialog.SetName('Completed Audio Studio output folder')
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return None
            root = Path(dialog.GetPath())
        finally:
            dialog.Destroy()
        return self.service_factory(root, private_root=frame.library.root,
                                    ffmpeg=getattr(controller, 'ffmpeg', None))

    def _choice(self, title, prompt, choices):
        import wx
        dialog = wx.SingleChoiceDialog(self.frame, prompt, title, choices)
        dialog.SetName(title)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return None
            index = dialog.GetSelection()
            if not 0 <= index < len(choices):
                raise ValueError('Select an item explicitly before continuing.')
            return index
        finally:
            dialog.Destroy()

    def _text(self, title, prompt, value=''):
        import wx
        dialog = wx.TextEntryDialog(self.frame, prompt, title, value)
        dialog.SetName(title)
        try:
            return dialog.GetValue() if dialog.ShowModal() == wx.ID_OK else None
        finally:
            dialog.Destroy()

    def publish(self, event=None):
        if not self._idle():
            return
        frame = self.frame
        owner = frame.library
        try:
            service = self._service()
            if service is None:
                return
            def loaded(rows):
                if frame.library is not owner or frame.cancel_event.is_set() or getattr(frame, '_closing', False):
                    return
                if not rows:
                    frame.report('This output folder has no completed Audio Studio productions.')
                    return
                self._choose_export(service, tuple(rows), owner)
            frame.run_job('Reading completed audio productions',
                          lambda: service.jobs(cancelled=frame.cancel_event.is_set), loaded)
        except Exception as exc:
            frame.report('Publication was not started: ' + str(exc))

    def _choose_export(self, service, rows, owner):
        import wx
        frame = self.frame
        index = self._choice('Completed production', 'Choose the exact saved production to publish.',
                             [row['title'] + ' — ' + row['id'] for row in rows])
        if index is None:
            return
        selected = rows[index]['id']
        choice = self._choice('Publication format',
                              'Choose an audio-synchronized book format.',
                              ['EPUB 3 with measured audio overlays', 'DAISY 2.02 ZIP with NCC, text, SMIL and MP3'])
        if choice is None:
            return
        language = self._text('Publication language', 'Language tag, such as en, uk or und (undetermined)', 'und')
        if language is None:
            return
        creator = self._text('Publication creator', 'Creator metadata (optional); no rights holder is inferred')
        if creator is None:
            return
        publisher = ''
        if choice == 1:
            publisher = self._text('DAISY publisher', 'Publisher metadata supplied by you (required)')
            if publisher is None:
                return
        suffix = '.epub' if choice == 0 else '.zip'
        dialog = wx.FileDialog(frame, 'Choose a new publication filename outside the library and audio project',
                               defaultFile='publication' + suffix,
                               wildcard=('EPUB files (*.epub)|*.epub' if choice == 0 else 'DAISY ZIP files (*.zip)|*.zip'),
                               style=wx.FD_SAVE)
        dialog.SetName('New synchronized publication filename')
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            output = Path(dialog.GetPath())
        finally:
            dialog.Destroy()
        # Capture before queued work; a later file cannot acquire overwrite consent.
        approval = capture_export_destination(output, private_root=owner.root)
        service._destination(output, approval)
        options = {'language': language, 'creator': creator}
        if choice == 1:
            options['publisher'] = publisher
        # Finish the listing host callback before submitting another operation.
        wx.CallAfter(self._start_export, service, selected, output, choice, options, approval, owner)

    def _start_export(self, service, selected, output, choice, options, approval, owner):
        frame = self.frame
        if not self._idle():
            return
        if frame.library is not owner or Path(frame.library.root).resolve() != service.private_root:
            frame.report('The active library changed. Select the completed production and destination again.')
            return
        tracker = ProgressTracker('Publish synchronized audio')
        def operation():
            try:
                method = service.export_epub if choice == 0 else service.export_daisy
                return method(selected, output, approval=approval,
                              cancelled=frame.cancel_event.is_set,
                              progress=lambda message: tracker.update(phase=message), **options)
            except RenderCancelled as exc:
                raise InterruptedError(str(exc)) from exc
        def displayed(record):
            frame.report('Publication saved: ' + record['output'] + '\nReceipt: ' + record['id']
                + '\nSynchronization uses measured line intervals. The exported reading copy remains a derivative.')
        frame.run_job('Publishing EPUB or DAISY', operation, displayed,
                      progress_tracker=tracker, progress_outcome=publication_outcome, done_is_observer=True)

    def verify(self, event=None):
        if not self._idle():
            return
        frame = self.frame
        owner = frame.library
        try:
            service = self._service()
            if service is None:
                return
            def loaded(rows):
                if frame.library is not owner or frame.cancel_event.is_set() or getattr(frame, '_closing', False):
                    return
                if not rows:
                    frame.report('This audio project has no saved publication receipts.')
                    return
                self._choose_verification(service, tuple(rows), owner)
            frame.run_job('Reading saved publication receipts',
                          lambda: service.publications(cancelled=frame.cancel_event.is_set), loaded)
        except Exception as exc:
            frame.report('Publication verification was not started: ' + str(exc))

    def _choose_verification(self, service, rows, owner):
        import wx
        index = self._choice('Saved publication receipt',
                             'Choose the receipt whose existing exported file you want to verify.',
                             [row['format'] + ' — ' + row['id'] + ' — ' + row['status'] for row in rows])
        if index is None:
            return
        record = rows[index]
        dialog = wx.FileDialog(self.frame, 'Select the existing publication file explicitly',
                               wildcard='Synchronized publications (*.epub;*.zip)|*.epub;*.zip',
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        dialog.SetName('Existing publication file for saved receipt')
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            output = Path(dialog.GetPath())
        finally:
            dialog.Destroy()
        wx.CallAfter(self._start_verification, service, record['id'], output, owner)

    def _start_verification(self, service, publication_id, output, owner):
        frame = self.frame
        if not self._idle():
            return
        if frame.library is not owner or Path(frame.library.root).resolve() != service.private_root:
            frame.report('The library changed. Choose the receipt and exported file again.')
            return
        def operation():
            try:
                return service.verify(publication_id, output=output, cancelled=frame.cancel_event.is_set)
            except RenderCancelled as exc:
                raise InterruptedError(str(exc)) from exc
        frame.run_job('Verifying saved publication', operation,
                      lambda record: frame.report('Publication bytes and source provenance verified: ' + record['output']),
                      progress_outcome=publication_outcome, done_is_observer=True)


def install_publication_actions(frame, audio_menu):
    import wx
    actions = PublicationActions(frame)
    for label, handler in (('Publish completed audio as EPUB or DAISY...', actions.publish),
                           ('Verify a saved audio publication...', actions.verify)):
        item = audio_menu.Append(wx.ID_ANY, label)
        frame.Bind(wx.EVT_MENU, handler, item)
    frame.publication_actions = actions
    return actions
