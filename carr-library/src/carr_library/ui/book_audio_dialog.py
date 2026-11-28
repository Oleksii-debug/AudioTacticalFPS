"""Accessible book split review on the desktop's existing cancellable worker."""
import json
import threading

from ..studio.book_source import source_options
from ..studio.book_voice import select_voice
from ..studio.book_audio import BookAudioService, SplitOptions, capture_plan, FORMATS, MODES
from ..studio.project import Role

HELP = (
    'Select an existing book in Books and Collections, then load it here. '
    'Discover and explicitly choose an installed local voice. No provider account or network request is used.\n\n'
    'Blank range fields select the whole source. Otherwise enter both 1-based reading positions. '
    'A physical-page split requires PDF pages; a chapter split requires the actual EPUB chapter inventory. '
    'EPUB headings use only saved source heading offsets. Bookmarks use explicitly chosen reading bookmarks '
    'at page or segment starts; a bookmark is not a remembered text cursor. Renamed, removed or stale selected '
    'bookmarks require a new review.\n\n'
    'Paragraphs use text line breaks; blank-lines group lines; sentences use punctuation boundaries, '
    'including text without spaces. These are deterministic text conventions, not linguistic analysis. '
    'Character splits count Unicode characters, not bytes or estimated audio duration. '
    'Regular expressions run in the existing isolated one-second worker; matched separators remain in the preceding part.\n\n'
    'Preview shows exact source revision, voice, part ranges, full part titles and output folder names. '
    'Folder names may abbreviate titles to stay within path limits; each exact destination is shown. '
    'Prepare saves immutable review metadata plus ordinary Studio jobs and starts no speech. '
    'Run is a separate explicit action. Resume verifies every completed artifact and reuses existing accepted nodes. '
    'To add tags or local artwork to a completed MP3/M4A/M4B, use Audio Studio > Review audio metadata and cover. '
    'Select the completed file there and save a separate reviewed copy; the Book job and its chapter receipts stay unchanged. '
    'The source and active scope are rechecked before work and publication. '
    'After restoring a backup, choose New output review to preserve historical jobs, then choose a new output folder, Preview, Prepare, and Run.\n\n'
    'Limits: two million source characters and 256 parts. Duration estimates from text and linguistic sentence '
    'detection are unavailable. After rendering, Measured duration parts splits the actual completed PCM. '
    'Unverified extraction remains unverified; narration creates no quotation approval. '
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
        'Voice: ' + plan.role.voice_id + '; ' + plan.role.language + '; rate ' + str(plan.role.rate)
            + '; volume ' + str(plan.role.volume),
        'Split: ' + plan.options.mode + '; format: ' + plan.format,
        'Output parent: ' + review.output_parent,
        'Review SHA-256: ' + review.fingerprint,
        'Extraction approval is unchanged. Source offsets below are zero-based Unicode characters.',
    ]
    rows.extend(source['warnings'])
    if plan.landmarks_json:
        from ..studio.book_landmarks import entries
        rows.append('Captured %s boundaries:\n%s' % (plan.options.mode, '\n'.join(
            '%s; character %d; %s' % (row['label'], row['start'], row['id'])
            for row in entries(source, plan.landmarks))))
    for index, (part, output) in enumerate(zip(plan.parts, review.destinations), 1):
        title = plan.options.title(index, source['title'], part.label)
        rows.append(f'{index}. {part.label}; characters {part.start}–{part.end}; {part.end - part.start} characters'
                    f'\nPart title: {title}\nOutput folder: {output}')
    if not review.prepared:
        rows.append('Restored items require New output review; historical job paths are never retargeted.')
    return '\n\n'.join(rows)


def create_book_audio_dialog(frame, *, service=None, wx_module=None, edition_id=None,
                             on_reviewed=None, initial_plan=None, initial_output=''):
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
    if initial_plan is not None and (
            initial_plan.source['source_anchor']['workspace'], initial_plan.source['source_topic']) != scope:
        raise ValueError('This saved plan belongs to another library/topic scope.')
    dialog = wx.Dialog(frame, title='Book audio — review split and render', size=(950, 800),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog.SetName('Book audio workspace')
    layout = wx.BoxSizer(wx.VERTICAL)
    form = wx.FlexGridSizer(cols=2, hgap=8, vgap=4)
    form.AddGrowableCol(1)
    layout.Add(form, 0, wx.ALL | wx.EXPAND, 6)
    alive, busy, selected_source, plan, review, reviewed_fields = True, False, None, None, None, None
    locked_plan, voices = False, ()
    inspection_active = False
    dialog_destroyed, owner_id = False, dialog.GetId()
    dialog._book_destroyed = False
    selected_bookmarks = ()
    cleanup_complete = False
    cleanup_lock = threading.Lock()
    unregister_cleanup = lambda: None
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
        'Unicode character count', 'Blank-line groups', 'Bounded regular expression',
        'Retained EPUB headings', 'Selected reading bookmarks'])
    modes.SetSelection(0)
    bookmark_status = field('Selected bookmark boundaries', style=wx.TE_READONLY,
                            value='No bookmarks selected. Use Choose reading bookmarks.')
    characters = field('Characters per part', value='10000')
    pattern = field('Split expression; matched separators stay in preceding part')
    ignore_case = field('Regex case', wx.Choice, choices=['Case sensitive', 'Ignore case'])
    ignore_case.SetSelection(0)
    names = field('Part &name template: index, book, part', value='{index:04d} - {book} - {part}')
    voice = field('Installed local &voice', wx.Choice, choices=[])
    rate = field('Voice rate, -10 to 10', value='0')
    volume = field('Voice volume, 0 to 100', value='100')
    formats = field('Audio &format', wx.Choice, choices=list(FORMATS))
    formats.SetSelection(0)
    output = field('Reviewed &output folder', value=str(initial_output))
    status = field('Book split preview and saved job status', proportion=1,
                   style=wx.TE_MULTILINE | wx.TE_READONLY, value=HELP)
    buttons = {}
    for row_items in [
        [('source', '&Load selected book'), ('voices', 'Discover local &voices'),
         ('folder', 'Choose &output folder'), ('preview', '&Preview split and names')],
        [('bookmarks', 'Choose reading &bookmarks...')],
        [('prepare', '&Prepare saved items'), ('run', '&Run or resume saved items'),
         ('pause', 'Pa&use'), ('saved', 'Saved &book plans'), ('fresh', 'New output review'), ('help', '&Help'), ('close', '&Close')],
        *([[('batch', '&Multiple books...'), ('duration', 'Measured &duration parts...'),
            ('silence', 'Inspect long silences...')]]
          if on_reviewed is None else []),
    ]:
        row = wx.BoxSizer(wx.HORIZONTAL)
        for key, label in row_items:
            if key == 'prepare' and on_reviewed is not None:
                label = '&Use reviewed book'
            button = wx.Button(dialog, wx.ID_CANCEL if key == 'close' else wx.ID_ANY, label=label)
            button.SetName(label.replace('&', ''))
            buttons[key] = button
            row.Add(button, 0, wx.ALL, 4)
        layout.Add(row, 0, wx.EXPAND)
    dialog.SetSizer(layout)
    plan_controls = (first, last, modes, characters, pattern, ignore_case, names, voice, formats,
                     rate, volume)

    def report(value):
        if alive and not getattr(frame, '_closing', False):
            status.ChangeValue(str(value))

    def guard():
        if not alive or getattr(frame, '_closing', False):
            raise ValueError('The Book audio window is closed.')
        service._check_root()
        if frame.library is not library or frame.library.root != root or getattr(frame, 'active', None) != scope:
            raise ValueError('The active library or topic changed. Reopen Book audio in the original scope.')

    def stopped():
        return (cancel.is_set() or frame.cancel_event.is_set() or not alive
                or getattr(frame, '_closing', False))

    def fields():
        return (first.GetValue(), last.GetValue(), modes.GetSelection(), characters.GetValue(),
                pattern.GetValue(), ignore_case.GetSelection(), names.GetValue(),
                voice.GetSelection(), formats.GetSelection(), output.GetValue(), selected_source,
                rate.GetValue(), volume.GetValue(), selected_bookmarks)

    def controls():
        if not alive:
            return
        for widget in plan_controls:
            widget.Enable(not busy and not locked_plan)
        output.Enable(not busy)
        for name, button in buttons.items():
            button.Enable(not busy or name in ('pause', 'close'))
        buttons['pause'].Enable(busy)
        pause_label = 'Cancel inspection' if busy and inspection_active else 'Pa&use'
        buttons['pause'].SetLabel(pause_label)
        buttons['pause'].SetName(pause_label.replace('&', ''))
        buttons['prepare'].Enable(not busy and review is not None)
        buttons['run'].Enable(not busy and review is not None and review.prepared)
        buttons['voices'].Enable(not busy and not locked_plan)
        buttons['fresh'].Enable(not busy and plan is not None and on_reviewed is None)
        buttons['bookmarks'].Enable(not busy and not locked_plan and selected_source is not None)
        if 'duration' in buttons:
            buttons['duration'].Enable(not busy and plan is not None)
        if 'silence' in buttons:
            buttons['silence'].Enable(not busy and plan is not None and
                                      (locked_plan or (review is not None and review.prepared)))
        if on_reviewed is not None:
            buttons['run'].Enable(False)
            buttons['saved'].Enable(False)
            buttons['source'].Enable(not busy and not locked_plan)

    def invalidate(event=None):
        nonlocal plan, review, reviewed_fields
        if not locked_plan:
            plan = None
        review, reviewed_fields = None, None
        controls()
        if event is not None:
            event.Skip()

    def cleanup_owned():
        nonlocal cleanup_complete
        if not owned:
            return
        # Retain retry ownership if close fails, and serialize a concurrent
        # normal close with the finalizer already captured by desktop shutdown.
        with cleanup_lock:
            if cleanup_complete:
                return
            service.close()
            cleanup_complete = True
        unregister_cleanup()

    if owned:
        unregister_cleanup = frame.register_worker_cleanup(cleanup_owned)

    def queue_cleanup():
        if not owned or cleanup_complete:
            return True
        if getattr(frame, 'working', False) or getattr(frame, '_closing', False):
            return False
        # Reuse the desktop's one worker and completion ownership. Closing a
        # native voice may block; never run it on wx or alongside synthesis.
        frame.run_job('Closing book-audio voices', cleanup_owned,
                      failed=lambda error: frame.report('Book audio cleanup needs attention: ' + str(error)))
        return True

    def begin(title, operation, done, *, inspection=False):
        nonlocal busy, inspection_active
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
        inspection_active = inspection
        controls()
        def checked():
            try:
                if stopped():
                    raise InterruptedError('Book audio cancelled before starting.')
                guard()
                return operation()
            finally:
                # DesktopFrame intentionally drops done/failed callbacks after
                # forced owner closure. Finish owned teardown on this active
                # worker, after the operation has released native work.
                if not alive or getattr(frame, '_closing', False):
                    cleanup_owned()
        def finished(value):
            nonlocal busy, inspection_active
            busy = False
            inspection_active = False
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
                if alive:
                    status.SetFocus()
        def failed(error):
            nonlocal busy, inspection_active
            busy = False
            inspection_active = False
            controls()
            if not alive:
                queue_cleanup()
                return
            report(error)
            if alive:
                status.SetFocus()
        try:
            accepted = frame.run_job(title, checked, done=finished, failed=failed)
        except Exception:
            busy = False
            inspection_active = False
            controls()
            raise
        if accepted is False:
            busy = False
            inspection_active = False
            controls()

    def load_source(event=None):
        nonlocal plan, review, locked_plan, selected_source
        try:
            guard()
            if edition_id is not None:
                edition = edition_id
            else:
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
            nonlocal selected_source, locked_plan, plan, review, reviewed_fields, voices, selected_bookmarks
            selected_source = (edition, value['revision'])
            locked_plan, plan, review, reviewed_fields, voices = False, None, None, None, ()
            voice.SetItems([])
            rate.ChangeValue('0')
            volume.ChangeValue('100')
            first.ChangeValue('')
            last.ChangeValue('')
            selected_bookmarks = ()
            bookmark_status.ChangeValue('No bookmarks selected. Use Choose reading bookmarks.')
            source_status.ChangeValue(value['title'] + '; ' + str(len(value['positions'])) +
                                      ' reading positions; revision ' + value['revision'])
            report('\n'.join(value['positions']) + '\nChoose a local voice, split and explicit output folder.')
            controls()
        begin('Reading the selected book inventory',
              lambda: source_options(library, scope[0], edition, topic=scope[1], cancelled=stopped), loaded)

    def choose_bookmarks(event=None):
        try:
            if selected_source is None:
                raise ValueError('Load the selected source book first.')
            selection = selected_source
        except Exception as exc:
            report(exc)
            return
        def choose(values):
            nonlocal selected_bookmarks
            if selected_source != selection:
                raise ValueError('The selected source changed. Load its bookmarks again.')
            if not values:
                raise ValueError('This book has no saved reading bookmarks. Add them in Reading Desk first.')
            picker = wx.MultiChoiceDialog(dialog,
                'Choose exact existing page or segment starts. Stale bookmarks cannot be used.',
                'Reading bookmark boundaries', [('%s; %s %s; %s%s' % (
                    row['bookmark'].label, row['bookmark'].location.kind, row['bookmark'].location.index,
                    row['bookmark'].id, '' if row['current_revision'] else '; STALE REVISION')) for row in values])
            try:
                if picker.ShowModal() != wx.ID_OK or not alive:
                    return
                indices = tuple(picker.GetSelections())
            finally:
                if not dialog_destroyed:
                    picker.Destroy()
            if (not 1 <= len(indices) <= 256 or len(set(indices)) != len(indices)
                    or any(type(index) is not int or not 0 <= index < len(values) for index in indices)):
                raise ValueError('Choose between one and 256 exact bookmark boundaries.')
            if any(not values[index]['current_revision'] for index in indices):
                raise ValueError('A selected bookmark belongs to a stale source revision. Review it in Reading Desk.')
            selected_bookmarks = tuple(values[index]['bookmark'].id for index in indices)
            bookmark_status.ChangeValue('; '.join(values[index]['bookmark'].label for index in indices))
            modes.SetSelection(MODES.index('bookmarks'))
            invalidate()
            bookmark_status.SetFocus()
        def load():
            from ..studio.book_landmarks import available_bookmarks
            return available_bookmarks(library, scope[0], selection[0], topic=scope[1],
                                       revision=selection[1], cancelled=stopped)
        begin('Reading existing bookmark boundaries', load, choose)

    def discover(event=None):
        def loaded(value):
            nonlocal voices
            voices = value
            voice.SetItems([item.name + '; ' + item.language for item in voices])
            if voices:
                voice.SetSelection(0)
            invalidate()
            report('Choose and preview an installed local voice. No speech has started.' if voices else
                   'No installed voice is available. Configure Local voice and music engines first.')
        begin('Discovering configured local voices',
              lambda: tuple(service.discover_voices(cancelled=stopped).voices()), loaded)

    def choose_output(event=None):
        picker = wx.DirDialog(dialog, 'Choose the existing local output parent')
        try:
            if picker.ShowModal() == wx.ID_OK and alive:
                output.ChangeValue(picker.GetPath())
                invalidate()
        finally:
            if not dialog_destroyed:
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
                chosen = voices[voice_index]
                selected = select_voice(voices, dict(voice_id=chosen.id, language=chosen.language,
                    rate=int(snapshot[11]), volume=int(snapshot[12])))
                role = Role('narrator', 'Narrator', **selected)
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
                bookmark_ids=snapshot[13] if options.mode == 'bookmarks' else None,
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
            if on_reviewed is not None:
                if wx.MessageBox('Use these exact %d parts, voice %s, format %s and output parent in the batch?\n%s\n\n'
                        'No jobs or speech start now.' % (len(candidate.parts), candidate.role.voice_id,
                                                         candidate.format, approved.output_parent),
                        'Use reviewed book in batch', wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                    return
                def confirm():
                    current = service.review(candidate, approved.output_parent, cancelled=stopped, guard=guard)
                    if current.fingerprint != approved.fingerprint:
                        raise ValueError('This book review changed; preview it again.')
                    return current
                def retained(current):
                    on_reviewed(candidate, current)
                    close()
                begin('Checking the exact book review for the batch', confirm, retained)
                return
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

    def fresh_review(event=None):
        try:
            guard()
            if plan is None or on_reviewed is not None:
                raise ValueError('Choose an existing Book plan before creating a fresh output review.')
            captured = plan
            if wx.MessageBox('Create new job identities for this same source, split and voice?\n\n'
                    'Historical plans and output receipts remain unchanged. No speech starts. '
                    'Preview the new output names and Prepare before Run.',
                    'New Book output review', wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
                return
        except Exception as exc:
            report(exc)
            return
        begin('Reviewing fresh Book output identities',
              lambda: service.fresh(captured, cancelled=stopped, guard=guard), display_saved)

    def display_saved(selected):
        nonlocal plan, review, reviewed_fields, locked_plan, selected_source, selected_bookmarks
        plan, locked_plan = selected, True
        review, reviewed_fields = None, None
        source = plan.source
        selected_source = (source['source_anchor']['edition'], source['source_anchor']['revision'])
        first.ChangeValue(str(source['source_anchor']['positions'][0]['ordinal']))
        last.ChangeValue(str(source['source_anchor']['positions'][-1]['ordinal']))
        modes.SetSelection(MODES.index(plan.options.mode))
        selected_bookmarks = (tuple(row['id'] for row in plan.landmarks['records'])
                              if plan.options.mode == 'bookmarks' else ())
        bookmark_status.ChangeValue('; '.join(row['label'] for row in plan.landmarks['records'])
                                    if selected_bookmarks else 'No bookmark boundaries in this saved plan.')
        characters.ChangeValue(str(plan.options.characters))
        pattern.ChangeValue(plan.options.pattern)
        ignore_case.SetSelection(int(plan.options.ignore_case))
        names.ChangeValue(plan.options.name_template)
        formats.SetSelection(FORMATS.index(plan.format))
        voice.SetItems(['Saved local voice: ' + plan.role.voice_id])
        voice.SetSelection(0)
        rate.ChangeValue(str(plan.role.rate))
        volume.ChangeValue(str(plan.role.volume))
        source_status.ChangeValue(source['title'] + '; saved immutable plan ' + plan.id)
        report('Saved voice: ' + plan.role.voice_id + '; split: ' + plan.options.mode +
               '. Choose its original output folder, or use New output review after restore, then Preview.')
        controls()

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
                if picker.ShowModal() != wx.ID_OK or not alive:
                    return
                index = picker.GetSelection()
            finally:
                if not dialog_destroyed:
                    picker.Destroy()
            if not 0 <= index < len(values):
                raise ValueError('Choose a saved book plan.')
            display_saved(values[index])
        begin('Opening saved book reviews', service.saved, choose)

    def multiple(event=None):
        try:
            guard()
            if busy or getattr(frame, 'working', False) or getattr(frame, '_closing', False):
                raise ValueError('Finish the current operation before choosing multiple books.')
            from .book_batch_dialog import show_book_batch
            show_book_batch(frame, service=service)
        except Exception as exc:
            report(exc)

    def inspect_silence(event=None):
        try:
            guard()
            if busy or getattr(frame, 'working', False) or getattr(frame, '_closing', False):
                raise ValueError('Finish the current operation before inspecting completed Book audio.')
            if plan is None or not (locked_plan or (review is not None and review.prepared)):
                raise ValueError('Choose an existing saved Book plan first.')
            captured, signature = plan, fields()

            def current():
                try:
                    guard()
                    return plan is captured
                except (ValueError, OSError, RuntimeError):
                    return False

            def current_ui():
                return current() and fields() == signature

            chooser = wx.SingleChoiceDialog(dialog,
                'Choose one completed part from this saved Book plan. The audio will not be changed.',
                'Book part for silence inspection',
                [f'{index}. {part.label}' for index, part in enumerate(captured.parts, 1)])
            try:
                decision = chooser.ShowModal()
                if not current_ui() or decision != wx.ID_OK:
                    return
                index = chooser.GetSelection() + 1
            finally:
                if not dialog_destroyed:
                    chooser.Destroy()
            if not 1 <= index <= len(captured.parts):
                raise ValueError('Choose one part in this saved Book plan.')
            from .studio_silence_inspection import choose_criteria, show_result
            criteria = choose_criteria(dialog, current=current_ui)
            if criteria is None:
                return
            if not current_ui():
                raise ValueError('The Book plan changed while choosing inspection criteria.')
            expected_job = captured.job_id(index)

            def operation():
                if stopped() or not current():
                    raise InterruptedError('Book silence inspection was cancelled before starting.')
                return service.inspect_silence(captured.id, index,
                    threshold_dbfs=criteria.threshold_dbfs, minimum_seconds=criteria.minimum_seconds,
                    cancelled=stopped, guard=guard)

            def completed(value):
                if stopped() or not current_ui():
                    raise InterruptedError('The Book plan changed; the late inspection was discarded.')
                if value.get('job_id') != expected_job:
                    raise ValueError('The inspection belongs to a different Book part.')
                show_result(dialog, value, current=lambda: not stopped() and current_ui())

            begin('Inspecting completed Book audio for long silences',
                  operation, completed, inspection=True)
        except Exception as exc:
            report(exc)

    def measured_duration(event=None):
        try:
            guard()
            if busy or getattr(frame, 'working', False) or getattr(frame, '_closing', False):
                raise ValueError('Finish the current operation before reviewing measured audio.')
            if plan is None:
                raise ValueError('Choose the existing saved Book plan first.')
            from .book_duration_dialog import show_book_duration
            show_book_duration(frame, service=service, plan=plan)
        except Exception as exc:
            report(exc)

    def close(event=None):
        nonlocal alive
        if not alive:
            return
        cancel.set()
        if not busy and not queue_cleanup():
            report('Wait for the owning desktop operation to finish before closing Book audio.')
            return
        # A busy operation releases owned voices in its worker's finally, even
        # if the closing desktop suppresses its normal completion callbacks.
        alive = False
        if getattr(dialog, 'IsModal', lambda: True)():
            dialog.EndModal(wx.ID_CANCEL)

    for key, callback in [('source', load_source), ('voices', discover), ('folder', choose_output),
                          ('bookmarks', choose_bookmarks),
                          ('preview', preview), ('prepare', prepare), ('run', run), ('saved', saved), ('fresh', fresh_review),
                          ('pause', lambda event: cancel.set()),
                          ('help', lambda event: (report(HELP), status.SetFocus())), ('close', close)]:
        buttons[key].Bind(wx.EVT_BUTTON, callback)
    if 'batch' in buttons:
        buttons['batch'].Bind(wx.EVT_BUTTON, multiple)
    if 'duration' in buttons:
        buttons['duration'].Bind(wx.EVT_BUTTON, measured_duration)
    if 'silence' in buttons:
        buttons['silence'].Bind(wx.EVT_BUTTON, inspect_silence)
    for widget in plan_controls + (output,):
        widget.Bind(wx.EVT_CHOICE if widget in (modes, ignore_case, voice, formats) else wx.EVT_TEXT, invalidate)
    def destroyed(event):
        nonlocal alive, dialog_destroyed
        if event.GetId() == owner_id:
            alive, dialog_destroyed = False, True
            dialog._book_destroyed = True
            cancel.set()
            # The current worker performs teardown in its finally. If another
            # desktop operation owns the queue, retain cleanup behind it.
            if not busy and owned and not cleanup_complete:
                executor = getattr(frame, 'executor', None)
                if executor is not None and not getattr(frame, '_closing', False):
                    try:
                        executor.submit(cleanup_owned)
                    except RuntimeError:
                        pass  # Registered finalizer retains the exact owner.
                else:
                    queue_cleanup()
        event.Skip()

    dialog.Bind(wx.EVT_CLOSE, close)
    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    controls()
    source_status.SetFocus()
    if initial_plan is not None:
        display_saved(initial_plan)
    elif edition_id is not None:
        wx.CallAfter(load_source)
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
        if not dialog._book_destroyed:
            dialog.Destroy()


def install_book_audio_actions(frame):
    """Append before the existing KeyboardActions menu capture."""
    import wx
    bar = frame.GetMenuBar()
    matches = [index for index in range(bar.GetMenuCount())
               if bar.GetMenuLabel(index).replace('&', '').strip().casefold() == 'audio studio']
    if len(matches) != 1:
        raise ValueError('Install the existing Audio Studio menu before Book audio.')
    menu = bar.GetMenu(matches[0])
    item = menu.Append(wx.ID_ANY, 'Book audio: split and render...')
    frame.Bind(wx.EVT_MENU, lambda event: show_book_audio(frame), item)
    return item
