"""Canonical editable reading copy with one shared bounded live voice controller."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

from ..speech import ReadingDocument
from ..speech.progress_speech import activate_progress_voice
from ..speech.session import ReaderDraftStore, ReaderResume, open_source_draft
from ..reader.live_reading import DocumentSequence
from .live_reading import (LiveReadingControls, bind_live_shortcuts, native_cursor_position,
                          reading_range)


def capture_document_text(document, native_value, *, displayed_value, displayed_text):
    """Native newline presentation is not an authored reading-copy edit."""
    value = displayed_text if native_value == displayed_value else native_value
    return document.set_text(value)


def show_speech_reader(parent, *, text='', title='Reading copy', source_loader=None,
                       source_guard=lambda: None, recovery_slot='manual',
                       on_ready=None, live_factory=None):
    import wx

    library_root = getattr(getattr(parent, 'library', None), 'root', None)
    if library_root is None:
        raise ValueError('Open a local library folder before using the voice reader.')
    root = Path(library_root)
    dialog = wx.Dialog(parent, title='Voice reader and reading copy', size=(1020, 900),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    native_id = dialog.GetId()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='reading-copy-source')
    cancel = threading.Event()
    document = ReadingDocument(text, title=title)
    progress = getattr(parent, 'operation_progress', None)
    source_locations = ()
    alive, busy, resources_closed = True, False, False
    source_loaded = source_loader is None
    save_path, last_recovery_signature = None, None
    recovered, draft_store = None, None
    recovery_notice = ''
    try:
        draft_store = ReaderDraftStore(root, slot=recovery_slot)
        recovered = draft_store.load()
        if recovered is not None and not text and source_loader is None:
            document = recovered.document
            source_locations = recovered.source_locations
            recovery_notice = 'Recovered your saved reading draft. Choose a voice explicitly; playback remains stopped. '
        if recovered is not None and recovered.document.format_notice and source_loader is None:
            if document is recovered.document:
                recovery_notice += document.format_notice
            else:
                recovery_notice += ('A pre-release reading draft was preserved separately. '
                                    'Use Save As for this new reading copy. ')
            # Resolve a source-bound draft first. Explicitly supplied text must
            # never autosave over an earlier-format draft that was not selected.
            draft_store = None
    except (OSError, ValueError, RuntimeError) as exc:
        recovery_notice = f'Reading-draft recovery is unavailable: {exc}. Save As remains available. '
        draft_store = None

    layout = wx.BoxSizer(wx.VERTICAL)

    def labelled(label, factory, *, proportion=0, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.LEFT | wx.TOP | wx.RIGHT, 6)
        control = factory(dialog, **kwargs)
        control.SetName(label.replace('&', ''))
        layout.Add(control, proportion, wx.EXPAND | wx.ALL, 6)
        return control

    source_status = labelled('Original and reading-copy status', wx.TextCtrl,
        value='Edits affect this reading copy only. Source quotations remain unchanged.',
        style=wx.TE_READONLY)
    editor = labelled('&Reading copy (editable; Ctrl+S saves)', wx.TextCtrl,
                      value=document.text, style=wx.TE_MULTILINE | wx.TE_DONTWRAP, proportion=1)
    displayed = {'value': editor.GetValue(), 'text': document.text}
    sequence = DocumentSequence(root, document, guard=lambda: source_guard(), source_locations=source_locations)

    def sync_document():
        capture_document_text(document, editor.GetValue(), displayed_value=displayed['value'],
                              displayed_text=displayed['text'])
        lineage = ('Library source identity retained; reopening does not reapprove it. '
                   if document.source_bound else 'Unbound reading copy; no library source identity was recorded. ')
        source_status.ChangeValue(f'Original text preserved; reading revision {document.revision}; '
            f'{"unsaved changes" if document.dirty else "saved"}. ' + lineage
            + 'The reading copy is not quotation evidence. ' + recovery_notice)

    def display_document():
        editor.ChangeValue(document.text)
        displayed.update(value=editor.GetValue(), text=document.text)
        sync_document()

    def current():
        if not source_loaded or busy:
            raise ValueError('Wait for the current reading-copy operation.')
        sync_document()
        sequence.prepare(document, source_locations=source_locations)
        return 1, document.text

    def show_section(section):
        source_guard()
        sync_document()
        if document.text != section.text:
            raise ValueError('The reading copy changed. Start reading the edited text explicitly.')

    options = {'core_factory': live_factory} if live_factory is not None else {}
    live = LiveReadingControls(dialog, layout, editor, sequence, current=current,
        show_section=show_section, ready=lambda: alive and source_loaded and not busy,
        host=parent, on_stop=cancel.set, **options)
    if recovered is not None:
        live.rate.SetValue(recovered.rate)
        live.volume.SetValue(recovered.volume)
    status = labelled('Reading-copy save and export status', wx.TextCtrl,
        style=wx.TE_READONLY | wx.TE_MULTILINE,
        value=recovery_notice + 'Changes belong to the reading copy. Live positions stay within the current session.',
        size=(-1, 70))
    row = wx.WrapSizer(wx.HORIZONTAL)
    buttons = {}

    def button(label, action, name):
        control = wx.Button(dialog, label=label)
        control.SetName(label.replace('&', ''))
        control.Bind(wx.EVT_BUTTON, action)
        row.Add(control, 0, wx.ALL, 4)
        buttons[name] = control
        return control

    def report(message, *, focus=False):
        if alive:
            status.ChangeValue(message)
            if focus:
                status.SetFocus()

    def with_recovery_warning(message):
        warning = getattr(draft_store, 'discovery_warning', '')
        return message + (f' {warning}' if warning else '')


    def run(work, on_done, *, discard=None, committed=False, allow_playback=False):
        nonlocal busy
        if busy or (live.core.busy and not allow_playback):
            report('Stop the current operation and wait for it to finish.', focus=False)
            return
        busy = True
        cancel.clear()

        def discard_result(future):
            if discard is not None:
                try:
                    discard(future.result())
                except Exception:
                    pass

        def finish(future):
            def apply():
                nonlocal busy
                if not alive:
                    discard_result(future)
                    return
                busy = False
                delivered = False
                try:
                    result = future.result()
                    # A successful atomic export is already durable. A later
                    # Stop or scope change cannot turn that receipt into a
                    # false failure. Prepared source/provider results still
                    # require admission when the UI accepts them.
                    if not committed:
                        if cancel.is_set():
                            raise InterruptedError('The operation was cancelled.')
                        source_guard()
                    delivered = True
                    on_done(result)
                except Exception as exc:
                    if not delivered:
                        discard_result(future)
                    report(f'Operation did not complete. {exc}')
            if alive:
                wx.CallAfter(apply)
            else:
                discard_result(future)
        try:
            executor.submit(work).add_done_callback(finish)
        except Exception as exc:
            busy = False
            report(str(exc))

    def select_progress_voice(event=None):
        selected = live.voice.GetSelection()
        if progress is None or not 0 <= selected < len(live._voices):
            return
        voice = live._voices[selected]
        if 'volume' not in voice.capabilities:
            report('This local voice does not expose volume control for progress notices.')
            return
        library_root = Path(parent.library.root)
        voice_id = voice.id

        def activated(provider):
            try:
                progress.set_speech_provider(provider, root=library_root)
            except Exception:
                provider.close()
                raise
            progress.settings_dialog()
            report(f'Progress voice selected for this session: {provider.label}. '
                   f'Sound mode: {progress.preferences.mode}.')

        run(lambda: activate_progress_voice(library_root, voice_id), activated,
            discard=lambda provider: provider.close())

    def save_recovery(event=None, *, force=False):
        nonlocal last_recovery_signature
        if not alive or draft_store is None or not source_loaded:
            return False
        sync_document()
        selected = live.voice.GetSelection()
        voice_id = (live._voices[selected].id if 0 <= selected < len(live._voices)
                    else recovered.voice_id if recovered is not None else '')
        cursor, _end = reading_range(editor, document.text, 'cursor')
        signature = (document._signature(), voice_id, live.rate.GetValue(),
                     live.volume.GetValue(), cursor, source_locations)
        if not force and signature == last_recovery_signature:
            return True
        source_guard()
        # Canonical audio_seconds remains a legacy interchange field. A time
        # within regenerated chunks is not a durable position in a whole book.
        draft_store.save(ReaderResume(document, voice_id, live.rate.GetValue(),
            live.volume.GetValue(), cursor, 0, source_locations))
        last_recovery_signature = signature
        if warning := getattr(draft_store, 'discovery_warning', ''):
            report(warning)
        return True

    def autosave(event=None):
        if not alive:
            return
        try:
            save_recovery()
        except Exception as exc:
            report(f'The reading draft could not be autosaved. Save As remains available. {exc}')

    def save_project(event=None, *, choose=False):
        nonlocal save_path
        try:
            source_guard()
            sync_document()
            if not choose and save_path is None and draft_store is not None:
                save_recovery(force=True)
                document._saved_signature = document._signature()
                sync_document()
                report(with_recovery_warning('Reading draft, voice settings and current position saved for recovery. '
                       'Save As exports an independent reading project.'))
                return True
            if choose or save_path is None:
                with wx.FileDialog(dialog, 'Save reading-copy project',
                    wildcard='Reading project (*.reading.json)|*.reading.json', style=wx.FD_SAVE) as chooser:
                    if chooser.ShowModal() != wx.ID_OK:
                        return False
                    selected = Path(chooser.GetPath())
            else:
                selected = save_path
            # A modal chooser can outlive the source's current authorization.
            source_guard()
            document.save(selected)
            save_path = selected
            completion = f'Reading copy and undo history saved: {selected}. Original source retained exactly.'
            try:
                save_recovery(force=True)
            except Exception as exc:
                completion += f' The recovery draft could not be updated. {exc}'
            else:
                completion = with_recovery_warning(completion)
            sync_document()
            report(completion)
            return True
        except Exception as exc:
            report(f'Reading copy was not saved. {exc}', focus=True)
            return False

    def open_project(event=None):
        nonlocal document, save_path, source_locations, source_loaded, draft_store, recovery_notice
        if busy:
            report('Stop the current operation before opening another document.')
            return
        try:
            sync_document()
            if document.dirty and document.text and not save_project():
                return
            with wx.FileDialog(dialog, 'Open a local text file or reading project',
                wildcard='Reading files (*.txt;*.md;*.json)|*.txt;*.md;*.json',
                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as chooser:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                selected = Path(chooser.GetPath())
            if selected.suffix.lower() == '.json':
                loaded = ReadingDocument.load(selected)
                target = selected
            else:
                from ..speech._io import local_file
                selected = local_file(selected)
                if selected.stat().st_size > 8_000_000:
                    raise ValueError('Open one text chapter at a time, up to 8 MB.')
                # read_bytes avoids silently normalizing original CRLF bytes.
                loaded = ReadingDocument(selected.read_bytes().decode('utf-8-sig'),
                                          title=selected.stem, source_path=selected)
                target = None
            source_guard()
            if source_loader is not None and (not source_loaded or loaded.source_anchor is None
                    or loaded.source_anchor != document.source_anchor):
                raise ValueError('Open unrelated or unbound reading projects in the standalone voice reader.')
            # A cancelled or rejected choice leaves the previous live operation
            # intact. Stop it only after the chosen source identity is admitted.
            live.stop()
            document, save_path, source_locations, source_loaded = loaded, target, (), True
            recovery_notice = document.format_notice
            if recovery_notice:
                draft_store = None
                save_path = None
            display_document()
            editor.SetFocus()
            report(recovery_notice + ('Reading copy opened. A retained-source locator still requires fresh library authorization.'
                   if document.source_anchor is not None else
                   'Independent reading copy opened. No library-source authority was imported.'))
        except Exception as exc:
            report(str(exc))

    def recover_saved_draft(event=None):
        """Discover old unidentified drafts through the existing worker/UI."""
        if busy:
            report('Stop the current operation before opening a saved draft.', focus=True)
            return
        try:
            sync_document()
            if document.dirty and document.text and document.revision > 0 and not save_project():
                return
            root = getattr(getattr(parent, 'library', None), 'root', None)
            if root is None:
                raise ValueError('Open a library before browsing its saved reading drafts.')
            recovery_store = ReaderDraftStore(root)
        except Exception as exc:
            report(f'Saved drafts were not opened. {exc}', focus=True)
            return

        def opened(result):
            nonlocal document, draft_store, recovered, save_path, source_locations, recovery_notice
            nonlocal source_guard, source_loader, source_loaded, last_recovery_signature
            resumed, separate_store = result
            live.stop()
            document, draft_store, recovered = resumed.document, separate_store, resumed
            # Explicitly opening one's saved text does not attach it to the
            # selected library source or inherit that source's authorization.
            source_guard, source_loader, source_loaded = lambda: None, None, True
            save_path, last_recovery_signature = None, None
            source_locations = resumed.source_locations
            recovery_notice = document.format_notice
            live.rate.SetValue(resumed.rate)
            live.volume.SetValue(resumed.volume)
            live.voice.SetSelection(wx.NOT_FOUND)
            display_document()
            restore_cursor()
            editor.SetFocus()
            report(with_recovery_warning('Saved draft recovered as a separate editable copy. The previous draft '
                   'is unchanged. No source identity was inferred or approved. Use Save As for a portable reading project.'))

        def choose_saved(listing):
            if not listing.copies:
                report(f'No readable saved drafts were found. {listing.skipped} entries could not be listed; '
                       'their files were kept unchanged.', focus=True)
                return
            from .research_actions import NativeDialogs
            notice = ('Saved draft files are listed by most recent modification. Every selection opens a separate copy. '
                      'Unbound older drafts have no recorded library identity.')
            if listing.truncated or listing.skipped:
                notice += f' This list is bounded; {listing.skipped} entries are not shown. All saved files are retained.'
            selected = NativeDialogs().choose(dialog, 'Recover saved reading draft', notice,
                                              [item.label for item in listing.copies])
            if selected is None:
                report('Recovery cancelled. The current copy and all saved drafts are unchanged.')
                return
            if type(selected) is not int or not 0 <= selected < len(listing.copies):
                raise ValueError('Choose a saved draft from the list.')
            summary = listing.copies[selected]
            run(lambda: recovery_store.open_saved(summary, cancelled=cancel.is_set), opened, allow_playback=True)

        run(lambda: recovery_store.list_saved(cancelled=cancel.is_set), choose_saved, allow_playback=True)


    def history(event=None, *, forward=False):
        if busy or live.core.busy:
            report('Stop voice reading before changing its reading revision.')
            return
        try:
            sync_document()
            changed = document.redo() if forward else document.undo()
            display_document()
            report(f'Reading revision {document.revision}.' if changed else 'No further reading edit.')
            editor.SetFocus()
        except Exception as exc:
            report(str(exc))

    def clean(event=None):
        if busy or live.core.busy:
            report('Stop voice reading before changing its reading revision.')
            return
        try:
            sync_document()
            document.cleanup()
            display_document()
            report('Whitespace normalized in the reading copy. Undo restores it; the source is unchanged.')
        except Exception as exc:
            report(str(exc))

    def compare(event=None):
        try:
            sync_document()
            preview = wx.Dialog(dialog, title='Original source and reading-copy diff', size=(800, 600),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            box = wx.BoxSizer(wx.VERTICAL)
            box.Add(wx.StaticText(preview, label='Immutable source followed by reading-copy changes'),
                    0, wx.ALL, 6)
            field = wx.TextCtrl(preview, value=document.source_text + '\n\nREADING-COPY DIFF\n' + document.diff(),
                                style=wx.TE_MULTILINE | wx.TE_READONLY)
            field.SetName('Original source and reading-copy changes')
            box.Add(field, 1, wx.ALL | wx.EXPAND, 6)
            box.Add(wx.Button(preview, wx.ID_CANCEL, 'Close'), 0, wx.ALL | wx.ALIGN_RIGHT)
            preview.SetSizer(box)
            field.SetFocus()
            try:
                preview.ShowModal()
            finally:
                preview.Destroy()
        except Exception as exc:
            report(str(exc))

    def save_audio(event=None):
        if busy or live.core.busy:
            report('Stop the current operation before exporting WAV.')
            return
        try:
            ordinal, raw = current()
            first, last = editor.GetSelection()
            mode = 'selection' if first != last else 'page'
            start, end = reading_range(editor, raw, mode)
            voice, ratio, volume = live._voice_settings()
            if not raw[start:end].strip():
                raise ValueError('Choose some reading text to export.')
            with wx.FileDialog(dialog, 'Save narrated PCM WAV', wildcard='PCM WAV (*.wav)|*.wav',
                               style=wx.FD_SAVE) as chooser:
                if chooser.ShowModal() != wx.ID_OK:
                    return
                destination = Path(chooser.GetPath())
            # Export uses the same canonical registry adapter as live speech.
            # It has its own immutable document snapshot and no audio playback.
            captured = DocumentSequence(root, document, guard=source_guard,
                                        source_locations=source_locations)
            def work():
                from ..reader.live_export import export_document_speech
                return export_document_speech(captured, destination, start=start, end=end,
                    voice_id=voice, rate=ratio, volume=volume, cancelled=cancel.is_set)
            run(work, lambda result: report(f'WAV saved: {result.path}. '
                f'Duration {result.duration_seconds:.1f} seconds; {result.sample_rate} Hz; '
                f'{result.channels} channels.'), committed=True)
        except Exception as exc:
            report(str(exc))

    def close_resources():
        nonlocal alive, resources_closed
        if resources_closed:
            return
        resources_closed, alive = True, False
        cancel.set()
        notices = []
        for label, operation in (
                ('Reading-draft timer', autosave_timer.Stop),
                ('Live playback cleanup', live.close),
                ('Reading-copy worker shutdown', lambda: executor.shutdown(wait=False, cancel_futures=True))):
            try:
                operation()
            except Exception as exc:
                notices.append(f'{label}: {exc}')
        if notices:
            try:
                parent.report(' '.join(notices))
            except Exception:
                pass

    def closing(event=None):
        if not alive:
            return
        try:
            sync_document()
            if source_loaded and document.dirty and document.text:
                answer = wx.MessageBox('Save the reading copy and its undo history before closing?',
                    'Unsaved reading copy', wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION, dialog)
                if answer == wx.CANCEL or (answer == wx.YES and not save_project()):
                    if event is not None and hasattr(event, 'Veto'):
                        event.Veto()
                    return
            close_resources()
            if dialog.IsModal():
                dialog.EndModal(wx.ID_CANCEL)
        except Exception as exc:
            report(f'The reading copy is still open. {exc}')

    def edited(event):
        if alive and live.core.busy:
            live.stop()
        event.Skip()

    button('&Open text / project', open_project, 'open')
    saved_drafts = button('Saved dra&fts', recover_saved_draft, 'recover')
    saved_drafts.SetName('Recover saved reading draft')
    button('&Save reading copy', save_project, 'save')
    button('Save &as', lambda event: save_project(choose=True), 'save_as')
    button('&Undo reading edit', history, 'undo')
    button('R&edo reading edit', lambda event: history(forward=True), 'redo')
    button('Original and &diff', compare, 'diff')
    button('Normalize s&paces', clean, 'cleanup')
    button('Save &WAV', save_audio, 'export_audio')
    progress_voice = button('Use voice for progress...', select_progress_voice, 'progress_voice')
    progress_voice.SetName('Use selected local voice for progress')
    progress_voice.Enable(progress is not None)
    button('C&lose', closing, 'close')
    layout.Add(row, 0, wx.EXPAND)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(buttons['close'].GetId())
    dialog.Bind(wx.EVT_CLOSE, closing)
    editor.Bind(wx.EVT_TEXT, edited)
    accelerators = []
    shortcut_ids = []
    for flags, key, handler in ((wx.ACCEL_CTRL, ord('S'), save_project),
        (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('S'), lambda event: save_project(choose=True))):
        identifier = wx.NewIdRef()
        shortcut_ids.append(identifier)
        accelerators.append((flags, key, int(identifier)))
        dialog.Bind(wx.EVT_MENU, handler, id=int(identifier))
    bind_live_shortcuts(dialog, live, accelerators)
    autosave_timer = wx.Timer(dialog)
    dialog.Bind(wx.EVT_TIMER, autosave, autosave_timer)

    def destroyed(event):
        # SIP may recreate the event object's wrapper during native deletion.
        # The captured window ID survives; no method on the old dialog is used.
        if event.GetId() == native_id:
            close_resources()
        event.Skip()

    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)

    def loaded(source):
        nonlocal document, source_locations, source_loaded, draft_store, recovered, recovery_notice
        nonlocal source_guard, source_loader
        source_guard()
        detached = False
        if draft_store is not None:
            from .research_actions import NativeDialogs

            def choose_source_draft():
                nonlocal recovery_notice
                recovery_notice += ('Previous draft has no matching source lineage and was preserved. '
                                    'Use Save As for this new copy. ')
                return NativeDialogs().choose(dialog,
                    'Recover saved reading draft',
                    'The saved draft has a different or unrecorded source identity. '
                    'Choose how to continue. The saved draft stays unchanged; '
                    'recovering it never attaches it to the current source.',
                    ('Open saved draft as a separate reading copy',
                     'Read the current source and keep the saved draft'))

            recovered, draft_store, detached = open_source_draft(source, draft_store,
                guard=source_guard, choose=choose_source_draft)
            document = recovered.document
        else:
            document = source.reading_document()
        source_locations = recovered.source_locations if detached else source.locations
        if detached:
            source_guard, source_loader = lambda: None, None
            recovery_notice = ('Recovered the saved draft separately. Its original recovery file '
                               'is unchanged. Use Save As to keep a portable reading project. ')
        if document.format_notice:
            recovery_notice += document.format_notice
            # Keep earlier recovery bytes after resolving their exact lineage.
            draft_store = None
        source_loaded = True
        if recovered is not None:
            live.rate.SetValue(recovered.rate)
            live.volume.SetValue(recovered.volume)
        display_document()
        if not detached:
            source_status.ChangeValue(f'{source.title}; {len(source.positions)} source reading positions. '
                + (' '.join(source.warnings[:3]) if source.warnings else 'Original source retained exactly.')
                + ' This is a separate reading copy, not quotation evidence. ' + recovery_notice)
        if recovery_notice or getattr(draft_store, 'discovery_warning', ''):
            report(with_recovery_warning(recovery_notice))
        restore_cursor()

    def restore_cursor():
        if recovered is not None:
            try:
                editor.SetInsertionPoint(native_cursor_position(editor, document.text, recovered.cursor))
            except ValueError:
                report('The draft is recovered, but its previous cursor cannot be represented in this native text view.')

    try:
        display_document()
        if source_loader is not None:
            run(lambda: source_loader(cancel.is_set), loaded)
        else:
            restore_cursor()
        live.discover()
        autosave_timer.Start(5000)
        editor.SetFocus()
        if on_ready is not None:
            def notify_ready():
                if alive:
                    on_ready(dialog, dict(editor=editor, live=live, status=status,
                                          buttons=buttons, document=lambda: document, close=closing,
                                          save_command_id=int(shortcut_ids[0])))
            wx.CallAfter(notify_ready)
        dialog.ShowModal()
    finally:
        try:
            close_resources()
        finally:
            if dialog:
                dialog.Destroy()


def install_speech_actions(frame):
    """Add one conventional menu without modifying existing keyboard bindings."""
    import wx

    menu = wx.Menu()
    item = menu.Append(wx.ID_ANY, 'Voice reader and reading copy...\tCtrl+Alt+V')
    frame.Bind(wx.EVT_MENU, lambda event: show_speech_reader(frame), item)
    from .reading_desk import ReadingActions
    from ..speech.source import read_source_range

    class BookSpeechReader:
        def __init__(self, parent, desk, state, guard):
            self.parent, self.desk, self.state, self.guard = parent, desk, state, guard

        def show(self):
            self.guard()
            total = len(self.desk.pages)
            with wx.TextEntryDialog(self.parent,
                f'Choose source reading positions from 1 to {total}. Enter a single number or first-last. '
                'These are source positions, not inferred printed page labels.',
                'Narrate a book or chapter', f'1-{total}') as choose:
                if choose.ShowModal() != wx.ID_OK:
                    return
                raw = choose.GetValue().strip()
            import re
            matched = re.fullmatch(r'(\d+)(?:\s*-\s*(\d+))?', raw)
            if not matched:
                raise ValueError('Enter a source position or a range such as 1-3.')
            first, last = int(matched[1]), int(matched[2] or matched[1])
            if not 1 <= first <= last <= total:
                raise ValueError('The selected source range is outside this book.')
            show_speech_reader(self.parent, title=self.desk.title, source_guard=self.guard,
                recovery_slot=f'{self.desk.workspace}/{self.desk.edition}/{self.desk.revision}/{first}-{last}',
                source_loader=lambda cancelled: read_source_range(self.desk, first, last,
                    guard=self.guard, cancelled=cancelled))

    actions = ReadingActions(frame, reader_factory=BookSpeechReader)
    selected = menu.Append(wx.ID_ANY, 'Narrate selected book or chapter...\tCtrl+Alt+R')
    frame.Bind(wx.EVT_MENU, actions.open_book, selected)
    frame.speech_reading_actions = actions
    frame.GetMenuBar().Append(menu, '&Voice')
