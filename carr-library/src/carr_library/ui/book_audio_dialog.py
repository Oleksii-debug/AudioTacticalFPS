"""Accessible book split review on the desktop's existing cancellable worker."""
import json
import threading

from ..assistant.translation_source import source_options
from ..studio.book_audio import BookAudioService, SplitOptions, capture_plan, FORMATS, MODES
from ..studio.project import Role

HELP = (
    'Select an existing book in Books and Collections, then load it here. '
    'Discover and explicitly choose an installed local voice. No provider account or network request is used.\n\n'
    'Blank range fields select the whole source. Otherwise enter both 1-based reading positions. '
    'A physical-page split requires PDF pages; a chapter split requires the actual EPUB chapter inventory. '
    'Paragraphs use text line breaks; blank-lines group lines; sentences use punctuation boundaries, '
    'including text without spaces. These are deterministic text conventions, not linguistic analysis. '
    'Character splits count Unicode characters, not bytes or estimated audio duration. '
    'Regular expressions run in the existing isolated one-second worker; matched separators remain in the preceding part.\n\n'
    'Preview shows exact source revision, voice, part ranges and sanitized output folder names. '
    'Prepare saves immutable review metadata plus ordinary Studio jobs and starts no speech. '
    'Run is a separate explicit action. Resume verifies every completed artifact and reuses existing accepted nodes. '
    'The source and active scope are rechecked before work and publication. '
    'After restoring a backup, choose a new output folder, Prepare, and review the new names before Run.\n\n'
    'Limits: two million source characters and 256 parts. Duration-based splitting and linguistic sentence detection '
    'are unavailable. Unverified extraction remains unverified; narration creates no quotation approval. '
    'Pause or closing this window requests cancellation and keeps saved Studio work. '
    'Tab/Shift+Tab move through named controls; Escape closes the dialog.'
)


def review_text(plan, review):
    source = plan.source
    rows = [
        'PREPARED — Run is a separate action.' if review.prepared else 'PREVIEW ONLY — no speech or jobs started.',
        'Source: ' + source['title'],
        'Metadata: ' + json.dumps(source['source_metadata'], ensure_ascii=False, sort_keys=True),
        'Library: ' + source['source_anchor']['workspace'],
        'Edition: ' + source['source_anchor']['edition'],
        'Revision: ' + source['source_anchor']['revision'],
        'Retained original SHA-256: ' + source['source_anchor']['asset_sha256'],
        'Voice: ' + plan.role.voice_id + '; ' + plan.role.language,
        'Split: ' + plan.options.mode + '; format: ' + plan.format,
        'Output parent: ' + review.output_parent,
        'Review SHA-256: ' + review.fingerprint,
        'Extraction approval is unchanged. Source offsets below are zero-based Unicode characters.',
    ]
    rows.extend(source['warnings'])
    for index, (part, output) in enumerate(zip(plan.parts, review.destinations), 1):
        rows.append(f'{index}. {part.label}; characters {part.start}–{part.end}; {part.end - part.start} characters\n{output}')
    if not review.prepared:
        rows.append('Restored items receive fresh names during Prepare; review those actual names before Run.')
    return '\n\n'.join(rows)


def create_book_audio_dialog(frame, *, service=None, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    scope = getattr(frame, 'active', None)
    if scope is None:
        raise ValueError('Open the existing library or topic before Book audio.')
    root, library = frame.library.root, frame.library
    owned = service is None
    service = service or BookAudioService(library, scope[0])
    if service.library is not library or service.workspace != scope[0]:
        raise ValueError('Use the book-audio service for the active library.')
    dialog = wx.Dialog(frame, title='Book audio — review split and render', size=(950, 800),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName('Book audio workspace')
    layout = wx.BoxSizer(wx.VERTICAL)
    form = wx.FlexGridSizer(cols=2, hgap=8, vgap=4)
    form.AddGrowableCol(1)
    layout.Add(form, 0, wx.ALL | wx.EXPAND, 6)
    alive, busy, selected_source, plan, review, reviewed_fields = True, False, None, None, None, None
    locked_plan, voices = False, ()
    cleanup_started = False
    cancel = threading.Event()

    def field(label, cls=wx.TextCtrl, proportion=0, **kwargs):
        target = layout if proportion else form
        target.Add(wx.StaticText(dialog, label=label), 0, wx.ALL, 4)
        widget = cls(dialog, **kwargs)
        widget.SetName(label.replace('&', ''))
        target.Add(widget, proportion, wx.ALL | wx.EXPAND, 4)
        return widget

    source_status = field('Selected library source', style=wx.TE_READONLY,
                         value='Select a book in Books and Collections, then choose Load selected book.')
    first = field('&First reading position; blank for whole source')
    last = field('&Last reading position; blank for whole source')
    modes = field('Split &mode', wx.Choice, choices=[
        'Whole selected source', 'EPUB chapters', 'Physical PDF pages',
        'Paragraphs — text line breaks', 'Sentences — punctuation',
        'Unicode character count', 'Blank-line groups', 'Bounded regular expression'])
    modes.SetSelection(0)
    characters = field('Characters per part', value='10000')
    pattern = field('Split expression; matched separators stay in preceding part')
    ignore_case = field('Regex case', wx.Choice, choices=['Case sensitive', 'Ignore case'])
    ignore_case.SetSelection(0)
    names = field('Part &name template: index, book, part', value='{index:04d} - {book} - {part}')
    voice = field('Installed local &voice', wx.Choice, choices=[])
    formats = field('Audio &format', wx.Choice, choices=list(FORMATS))
    formats.SetSelection(0)
    output = field('Reviewed &output folder')
    status = field('Book split preview and saved job status', proportion=1,
                   style=wx.TE_MULTILINE | wx.TE_READONLY, value=HELP)
    buttons = {}
    for row_items in [
        [('source', '&Load selected book'), ('voices', 'Discover local &voices'),
         ('folder', 'Choose &output folder'), ('preview', '&Preview split and names')],
        [('prepare', '&Prepare saved items'), ('run', '&Run or resume saved items'),
         ('pause', 'Pa&use'), ('saved', 'Saved &book plans'), ('help', '&Help'), ('close', '&Close')],
    ]:
        row = wx.BoxSizer(wx.HORIZONTAL)
        for key, label in row_items:
            button = wx.Button(dialog, wx.ID_CANCEL if key == 'close' else wx.ID_ANY, label=label)
            button.SetName(label.replace('&', ''))
            buttons[key] = button
            row.Add(button, 0, wx.ALL, 4)
        layout.Add(row, 0, wx.EXPAND)
    dialog.SetSizer(layout)
    plan_controls = (first, last, modes, characters, pattern, ignore_case, names, voice, formats)

    def report(value):
        if alive:
            status.ChangeValue(str(value))

    def guard():
        if frame.library is not library or frame.library.root != root or getattr(frame, 'active', None) != scope:
            raise ValueError('The active library or topic changed. Reopen Book audio in the original scope.')

    def stopped():
        return cancel.is_set() or frame.cancel_event.is_set() or not alive

    def fields():
        return (first.GetValue(), last.GetValue(), modes.GetSelection(), characters.GetValue(),
                pattern.GetValue(), ignore_case.GetSelection(), names.GetValue(),
                voice.GetSelection(), formats.GetSelection(), output.GetValue(), selected_source)

    def controls():
        if not alive:
            return
        for widget in plan_controls:
            widget.Enable(not busy and not locked_plan)
        output.Enable(not busy)
        for name, button in buttons.items():
            button.Enable(not busy or name in ('pause', 'close'))
        buttons['pause'].Enable(busy)
        buttons['prepare'].Enable(not busy and review is not None)
        buttons['run'].Enable(not busy and review is not None and review.prepared)
        buttons['voices'].Enable(not busy and not locked_plan)

    def invalidate(event=None):
        nonlocal plan, review, reviewed_fields
        if not locked_plan:
            plan = None
        review, reviewed_fields = None, None
        controls()
        if event is not None:
            event.Skip()

    def queue_cleanup():
        nonlocal cleanup_started
        if not owned or cleanup_started:
            return True
        if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
            return False
        cleanup_started = True
        # Reuse the desktop's one worker and completion ownership. Closing a
        # native voice may block; never run it on wx or alongside synthesis.
        frame.run_job('Closing book-audio voices', service.close,
                      failed=lambda error: frame.report('Book audio cleanup needs attention: ' + str(error)))
        return True

    def begin(title, operation, done):
        nonlocal busy
        if busy:
            return
        try:
            guard()
            if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
                raise ValueError('Finish or cancel the current desktop operation first.')
        except Exception as exc:
            report(exc)
            return
        cancel.clear()
        busy = True
        controls()
        def checked():
            guard()
            if stopped():
                raise InterruptedError('Book audio cancelled before starting.')
            return operation()
        def finished(value):
            nonlocal busy
            busy = False
            controls()
            if not alive:
                queue_cleanup()
                return
            if alive:
                try:
                    guard()
                    done(value)
                except Exception as exc:
                    report(exc)
                status.SetFocus()
        def failed(error):
            nonlocal busy
            busy = False
            controls()
            if not alive:
                queue_cleanup()
                return
            report(error)
            if alive:
                status.SetFocus()
        if frame.run_job(title, checked, done=finished, failed=failed) is False:
            busy = False
            controls()

    def load_source(event=None):
        nonlocal plan, review, locked_plan, selected_source
        try:
            guard()
            panel = frame.book.GetPage(1)
            if not panel.ready or panel.workspace != scope[0]:
                raise ValueError('Wait for Books and Collections to finish loading.')
            edition = panel._selection()[2]
            if edition not in panel.rows:
                raise ValueError('Select an existing book in Books and Collections.')
        except Exception as exc:
            report(exc)
            return
        def loaded(value):
            nonlocal selected_source, locked_plan, plan, review, reviewed_fields, voices
            selected_source = (edition, value['revision'])
            locked_plan, plan, review, reviewed_fields, voices = False, None, None, None, ()
            voice.SetItems([])
            first.ChangeValue('')
            last.ChangeValue('')
            source_status.ChangeValue(value['title'] + '; ' + str(len(value['positions'])) +
                                      ' reading positions; revision ' + value['revision'])
            report('\n'.join(value['positions']) + '\nChoose a local voice, split and explicit output folder.')
            controls()
        begin('Reading the selected book inventory',
              lambda: source_options(library, scope[0], edition, topic=scope[1], cancelled=stopped), loaded)

    def discover(event=None):
        def loaded(registry):
            nonlocal voices
            voices = tuple(registry.voices())
            voice.SetItems([item.name + '; ' + item.language for item in voices])
            if voices:
                voice.SetSelection(0)
            invalidate()
            report('Choose and preview an installed local voice. No speech has started.' if voices else
                   'No installed voice is available. Configure Local voice and music engines first.')
        begin('Discovering configured local voices',
              lambda: service.production.discover_voices(cancelled=stopped), loaded)

    def choose_output(event=None):
        picker = wx.DirDialog(dialog, 'Choose the existing local output parent')
        try:
            if picker.ShowModal() == wx.ID_OK:
                output.ChangeValue(picker.GetPath())
                invalidate()
        finally:
            picker.Destroy()

    def preview(event=None):
        nonlocal plan, review, reviewed_fields
        try:
            snapshot = fields()
            destination = snapshot[9]
            selected_plan = plan if locked_plan or reviewed_fields == snapshot else None
            if selected_plan is None:
                if selected_source is None:
                    raise ValueError('Load the selected source book first.')
                mode_index, format_index, voice_index = snapshot[2], snapshot[8], snapshot[7]
                if (not 0 <= mode_index < len(MODES) or not 0 <= format_index < len(FORMATS)
                        or not 0 <= voice_index < len(voices)):
                    raise ValueError('Choose an installed local voice, split and output format.')
                options = SplitOptions(MODES[mode_index], int(snapshot[3]),
                                       snapshot[4], snapshot[5] == 1, snapshot[6])
                role = Role('narrator', 'Narrator', voices[voice_index].id, voices[voice_index].language)
                start = int(snapshot[0]) if snapshot[0].strip() else None
                end = int(snapshot[1]) if snapshot[1].strip() else None
                edition, revision = selected_source
                format = FORMATS[format_index]
        except Exception as exc:
            report(exc)
            return
        review, reviewed_fields = None, None
        controls()
        def work():
            candidate = selected_plan or capture_plan(library, scope[0], edition, topic=scope[1],
                revision=revision, first=start, last=end, options=options, role=role, format=format,
                cancelled=stopped, guard=guard)
            return candidate, service.review(candidate, destination, cancelled=stopped, guard=guard)
        def ready(value):
            nonlocal plan, review, reviewed_fields
            if fields() != snapshot:
                raise ValueError('The source or options changed. Preview this exact selection again.')
            plan, review = value
            reviewed_fields = snapshot
            report(review_text(plan, review))
            controls()
        begin('Reviewing exact book source and split names', work, ready)

    def checked_review():
        guard()
        if plan is None or review is None or reviewed_fields != fields():
            raise ValueError('Preview this exact source, split, voice and output folder first.')
        return plan, review

    def prepare(event=None):
        try:
            candidate, approved = checked_review()
            if wx.MessageBox('Save %d ordinary Studio jobs for local voice %s in:\n%s\n\n'
                    'No speech starts now. Review the displayed names before Run.' %
                    (len(candidate.parts), candidate.role.voice_id, approved.output_parent),
                    'Prepare reviewed book items', wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                return
        except Exception as exc:
            report(exc)
            return
        def ready(value):
            nonlocal review
            review = value
            report(review_text(candidate, value))
            controls()
        begin('Saving reviewed book items',
              lambda: service.prepare(candidate, approved.output_parent, approval=approved.fingerprint,
                                      cancelled=stopped, guard=guard), ready)

    def run(event=None):
        try:
            candidate, approved = checked_review()
            if not approved.prepared:
                raise ValueError('Prepare the saved items and review their final names first.')
            if wx.MessageBox('Run the displayed %d saved items with local voice %s?\nOutput: %s\n\n'
                    'Completed files are checksum-verified before reuse. This starts local speech rendering.' %
                    (len(candidate.parts), candidate.role.voice_id, approved.output_parent),
                    'Run reviewed book audio', wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                return
        except Exception as exc:
            report(exc)
            return
        def progress(message):
            wx.CallAfter(report, message)
        def complete(rows):
            report('Verified saved items:\n' + '\n'.join('%d. %s — %s' %
                (row['index'], row['status'], row['destination']) for row in rows))
        begin('Rendering reviewed book items',
              lambda: service.run(candidate.id, approved.output_parent, approval=approved.fingerprint,
                                  cancelled=stopped, guard=guard, progress=progress), complete)

    def saved(event=None):
        def choose(values):
            nonlocal plan, review, reviewed_fields, locked_plan, selected_source
            values = tuple(value for value in values if value.source['source_topic'] == scope[1])
            if not values:
                report('No saved book plans in this library/topic scope.')
                return
            picker = wx.SingleChoiceDialog(dialog, 'Choose an immutable source review.',
                'Saved book plans', [value.source['title'] + '; ' + str(len(value.parts)) +
                                    ' parts; ' + value.id for value in values])
            try:
                if picker.ShowModal() != wx.ID_OK:
                    return
                index = picker.GetSelection()
            finally:
                picker.Destroy()
            if not 0 <= index < len(values):
                raise ValueError('Choose a saved book plan.')
            plan, locked_plan = values[index], True
            review, reviewed_fields = None, None
            source = plan.source
            selected_source = (source['source_anchor']['edition'], source['source_anchor']['revision'])
            first.ChangeValue(str(source['source_anchor']['positions'][0]['ordinal']))
            last.ChangeValue(str(source['source_anchor']['positions'][-1]['ordinal']))
            modes.SetSelection(MODES.index(plan.options.mode))
            characters.ChangeValue(str(plan.options.characters))
            pattern.ChangeValue(plan.options.pattern)
            ignore_case.SetSelection(int(plan.options.ignore_case))
            names.ChangeValue(plan.options.name_template)
            formats.SetSelection(FORMATS.index(plan.format))
            voice.SetItems(['Saved local voice: ' + plan.role.voice_id])
            voice.SetSelection(0)
            source_status.ChangeValue(source['title'] + '; saved immutable plan ' + plan.id)
            report('Saved voice: ' + plan.role.voice_id + '; split: ' + plan.options.mode +
                   '. Choose its original output folder, or a new folder after restore, then Preview.')
            controls()
        begin('Opening saved book reviews', service.saved, choose)

    def close(event=None):
        nonlocal alive
        if not alive:
            return
        cancel.set()
        if not busy and not queue_cleanup():
            report('Wait for the owning desktop operation to finish before closing Book audio.')
            return
        # A busy operation's completion schedules cleanup after native child
        # cleanup, while DesktopFrame still owns the current job lifetime.
        alive = False
        if getattr(dialog, 'IsModal', lambda: True)():
            dialog.EndModal(wx.ID_CANCEL)

    for key, callback in [('source', load_source), ('voices', discover), ('folder', choose_output),
                          ('preview', preview), ('prepare', prepare), ('run', run), ('saved', saved),
                          ('pause', lambda event: cancel.set()),
                          ('help', lambda event: (report(HELP), status.SetFocus())), ('close', close)]:
        buttons[key].Bind(wx.EVT_BUTTON, callback)
    for widget in plan_controls + (output,):
        widget.Bind(wx.EVT_CHOICE if widget in (modes, ignore_case, voice, formats) else wx.EVT_TEXT, invalidate)
    dialog.Bind(wx.EVT_CLOSE, close)
    controls()
    source_status.SetFocus()
    return dialog, close


def show_book_audio(frame):
    try:
        dialog, close = create_book_audio_dialog(frame)
    except Exception as exc:
        frame.report(str(exc))
        return
    try:
        dialog.ShowModal()
    finally:
        close()
        dialog.Destroy()


def install_book_audio_actions(frame):
    import wx
    bar = frame.GetMenuBar()
    menu = next((bar.GetMenu(index) for index in range(bar.GetMenuCount())
                 if bar.GetMenuLabelText(index) == 'Audio Studio'), None)
    if menu is None:
        raise ValueError('Install the existing Audio Studio menu before Book audio.')
    item = menu.Append(wx.ID_ANY, 'Book audio: split and render...')
    frame.Bind(wx.EVT_MENU, lambda event: show_book_audio(frame), item)
