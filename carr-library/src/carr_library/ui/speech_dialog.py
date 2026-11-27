"""Native local voice reader with keyboard controls and reversible reading copy."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from uuid import uuid4

from ..speech import ReadingDocument, SpeechSettings, VoiceRegistry
from ..speech.player import WavePlayer
from ..speech.progress_speech import activate_progress_voice, play_when_quiet
from ..speech.session import ReaderDraftStore, ReaderResume
from ..speech.navigation import native_cursor_position, native_insertion_offset, native_selection, select_text_unit


def show_speech_reader(parent, *, text='', title='Reading copy', source_loader=None,
                       source_guard=lambda: None, recovery_slot='manual'):
    import wx

    dialog = wx.Dialog(parent, title='Voice reader and reading copy', size=(900, 800),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-speech')
    cancel = threading.Event()
    audio_files = TemporaryDirectory(prefix='research-reader-')
    player = WavePlayer()
    progress = getattr(parent, 'progress_panel', None)
    if progress is not None:
        progress.register_player(player)
    document = ReadingDocument(text, title=title)
    registry = None
    owned_registries = set()
    owned_progress_voices = set()
    voices = ()
    alive = True
    busy = False
    playback_generation = 0
    repeat_scheduled = None
    playback_refresh = None
    reader_library = getattr(parent, 'library', None)
    reader_root = Path(reader_library.root) if reader_library is not None else None
    save_path = None
    last_audio = None
    source_loaded = source_loader is None
    draft_store = None
    recovered = None
    recovery_notice = ''
    last_recovery_signature = None
    try:
        if getattr(getattr(parent, 'library', None), 'root', None) is not None:
            draft_store = ReaderDraftStore(parent.library.root, slot=recovery_slot)
            recovered = draft_store.load()
            if recovered is not None and not text and source_loader is None:
                document = recovered.document
                text = document.text
                recovery_notice = 'Recovered your saved reading draft and voice settings. '
            if recovered is not None and recovered.document.format_notice:
                recovery_notice += recovered.document.format_notice
                # The earlier dialect is imported in memory; retain its saved
                # recovery bytes until the user explicitly writes a new copy.
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
        value='Edits affect this reading copy only. Saved source quotations are unchanged.',
        style=wx.TE_READONLY)
    voice_choice = labelled('&Voice (installed locally)', wx.Choice)
    controls = wx.BoxSizer(wx.HORIZONTAL)
    controls.Add(wx.StaticText(dialog, label='Synthesis &rate (-10 to 10)'), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
    rate = wx.SpinCtrl(dialog, min=-10, max=10, initial=recovered.rate if recovered else 0)
    rate.SetName('Synthesis rate, minus ten to ten')
    controls.Add(rate, 0, wx.ALL, 6)
    controls.Add(wx.StaticText(dialog, label='&Volume (0 to 100)'), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 6)
    volume = wx.SpinCtrl(dialog, min=0, max=100, initial=recovered.volume if recovered else 80)
    volume.SetName('Speech volume, zero to one hundred')
    controls.Add(volume, 0, wx.ALL, 6)
    reload_button = wx.Button(dialog, label='Reload &voices')
    controls.Add(reload_button, 0, wx.ALL, 6)
    progress_voice = wx.Button(dialog, label='Use voice for progress...')
    progress_voice.SetName('Use selected local voice for progress')
    controls.Add(progress_voice, 0, wx.ALL, 6)
    voice_preferences_button = wx.Button(dialog, label='Voice preferences...')
    voice_preferences_button.SetName('Voice preferences')
    controls.Add(voice_preferences_button, 0, wx.ALL, 6)
    layout.Add(controls, 0, wx.EXPAND)
    editor = labelled('&Reading copy (editable; Ctrl+S saves)', wx.TextCtrl,
                      value=text, style=wx.TE_MULTILINE | wx.TE_DONTWRAP, proportion=1)
    from .reading_presentation import load_reading_presentation
    presentation_library = reader_library
    presentation, presentation_store, presentation_notice = load_reading_presentation(
        editor, reader_root, wx)
    recovery_notice += presentation_notice
    dialog.reading_presentation = presentation
    navigation = wx.BoxSizer(wx.HORIZONTAL)
    navigation.Add(wx.StaticText(dialog, label='Navigation unit'), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
    navigation_unit = wx.Choice(dialog, choices=['Word', 'Sentence', 'Paragraph'])
    navigation_unit.SetName('Navigation unit')
    navigation_unit.SetSelection(0)
    navigation.Add(navigation_unit, 0, wx.ALL, 4)
    previous_unit = wx.Button(dialog, label='Previous unit')
    previous_unit.SetName('Previous text unit')
    current_unit = wx.Button(dialog, label='Select current unit')
    current_unit.SetName('Select current text unit')
    next_unit = wx.Button(dialog, label='Next unit')
    next_unit.SetName('Next text unit')
    read_unit = wx.Button(dialog, label='Read current unit')
    read_unit.SetName('Read current text unit')
    for control in (previous_unit, current_unit, next_unit, read_unit):
        navigation.Add(control, 0, wx.ALL, 4)
    layout.Add(navigation, 0, wx.EXPAND)
    status = labelled('Speech and save status', wx.TextCtrl, style=wx.TE_READONLY | wx.TE_MULTILINE,
                      value='Finding installed local voices. No speech starts automatically.', size=(-1, 90))
    row = wx.BoxSizer(wx.HORIZONTAL)
    read = wx.Button(dialog, label='Read &selection / all (F6)')
    cursor = wx.Button(dialog, label='Read from &cursor')
    pause = wx.Button(dialog, label='&Pause / resume (F7)')
    stop = wx.Button(dialog, label='S&top (F8)')
    export_audio = wx.Button(dialog, label='Save &WAV')
    for control in (read, cursor, pause, stop, export_audio):
        row.Add(control, 0, wx.ALL, 4)
    layout.Add(row, 0, wx.EXPAND)
    row = wx.BoxSizer(wx.HORIZONTAL)
    open_button = wx.Button(dialog, label='&Open text / project')
    save_button = wx.Button(dialog, label='&Save reading copy')
    save_as = wx.Button(dialog, label='Save &as')
    undo = wx.Button(dialog, label='&Undo reading edit')
    redo = wx.Button(dialog, label='R&edo reading edit')
    original = wx.Button(dialog, label='Original and &diff')
    for control in (open_button, save_button, save_as, undo, redo, original):
        row.Add(control, 0, wx.ALL, 4)
    layout.Add(row, 0, wx.EXPAND)
    row = wx.BoxSizer(wx.HORIZONTAL)
    seek = wx.Button(dialog, label='Seek to &time')
    playback_controls = wx.Button(dialog, label='Playback controls...')
    playback_controls.SetName('Reader playback controls')
    appearance_button = wx.Button(dialog, label='Text appearance...')
    appearance_button.SetName('Reading appearance: font, colors and zoom')
    for control in (seek, playback_controls, appearance_button):
        row.Add(control, 0, wx.ALL, 4)
    layout.Add(row, 0, wx.EXPAND)
    row = wx.BoxSizer(wx.HORIZONTAL)
    cleanup = wx.Button(dialog, label='Normalize s&paces')
    find_button = wx.Button(dialog, label='Find and replace... (Ctrl+F)')
    find_button.SetName('Find and replace')
    spelling_button = wx.Button(dialog, label='Spelling review...')
    spelling_button.SetName('Spelling review with Hunspell')
    pronunciation = wx.Button(dialog, label='Pronunciatio&n rules')
    pronunciation.SetName('Pronunciation rules')
    close = wx.Button(dialog, wx.ID_CANCEL, 'C&lose')
    for control in (cleanup, find_button, spelling_button, pronunciation, close):
        row.Add(control, 0, wx.ALL, 4)
    layout.Add(row, 0, wx.ALIGN_RIGHT)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(wx.ID_CANCEL)

    def report(message, *, focus=False):
        if alive:
            status.ChangeValue(message)
            if focus:
                status.SetFocus()

    def capabilities(event=None):
        selected = voice_choice.GetSelection()
        current = voices[selected] if 0 <= selected < len(voices) else None
        rate.Enable(bool(current and 'rate' in current.capabilities) and not busy)
        volume.Enable(bool(current and 'volume' in current.capabilities) and not busy)
        if current and 'rate' not in current.capabilities:
            rate.SetValue(0)
        if current and 'volume' not in current.capabilities:
            volume.SetValue(100)
        for button in (read, cursor, export_audio):
            button.Enable(bool(current) and not busy)
        progress_voice.Enable(bool(progress is not None and current and 'volume' in current.capabilities) and not busy)
        voice_preferences_button.Enable(reader_root is not None and source_loaded and bool(voices) and not busy)
        pronunciation.Enable(source_loaded and not busy)
        find_button.Enable(source_loaded and not busy)
        appearance_button.Enable(source_loaded and not busy and presentation_store is not None)
        spelling_button.Enable(source_loaded and not busy)
        playback_controls.Enable(not busy and last_audio is not None and getattr(player, 'path', None) is not None)
        for button in (navigation_unit, previous_unit, current_unit, next_unit):
            button.Enable(source_loaded and not busy)
        read_unit.Enable(source_loaded and bool(current) and not busy)
        if event is not None and current:
            report(f'{current.name}; {current.language}; {current.engine}. '
                   f'Controls: {", ".join(current.capabilities) or "voice defaults only"}. {current.timing}')

    def choose_reading_appearance(event=None):
        from .reading_presentation import show_reading_presentation
        def guard():
            if (not alive or busy or not source_loaded or getattr(parent, '_closing', False)
                    or presentation_store is None or getattr(parent, 'library', None) is not presentation_library
                    or Path(presentation_library.root).absolute() != presentation_store.root):
                raise ValueError('The reader or library changed. Reopen reading appearance.')
            presentation_store.guard()
            source_guard()
        try:
            guard()
            applied = show_reading_presentation(dialog, library_root=presentation_store.root,
                presenter=presentation, guard=guard)
            guard()
            if applied:
                report('Reading appearance saved for this library. Text, source and editing history are unchanged.')
            editor.SetFocus()
        except Exception as exc:
            report(str(exc), focus=True)

    def run(work, on_done, *, on_failed=None):
        nonlocal busy
        if busy:
            report('A speech operation is running. Stop it before starting another.', focus=True)
            return False
        busy = True
        cancel.clear()
        capabilities()
        reload_button.Disable()

        def finish(future):
            nonlocal busy
            if not alive:
                return
            busy = False
            reload_button.Enable()
            try:
                on_done(future.result())
            except Exception as exc:
                if on_failed is not None:
                    on_failed(exc)
                else:
                    report(f'Operation did not complete. {exc}', focus=True)
            capabilities()
        try:
            executor.submit(work).add_done_callback(lambda future: wx.CallAfter(finish, future))
        except Exception as exc:
            busy = False
            reload_button.Enable(); capabilities()
            if on_failed is not None:
                on_failed(exc)
            else:
                report(f'Operation did not start. {exc}', focus=True)
            return False
        return True

    def select_progress_voice(event=None):
        selected = voice_choice.GetSelection()
        if progress is None or not 0 <= selected < len(voices):
            return
        root, voice_id = Path(parent.library.root), voices[selected].id

        def activate():
            provider = activate_progress_voice(root, voice_id)
            owned_progress_voices.add(provider)
            return provider

        def activated(provider):
            try:
                progress.set_speech_provider(provider, root=root)
                owned_progress_voices.discard(provider)
            except Exception:
                provider.close()
                raise
            progress.settings()
            report(f'Progress voice selected for this session: {provider.label}. '
                   f'Sound mode: {progress.preferences.mode}.')

        run(activate, activated)

    def playback_scope(*, source=True):
        if not alive or not source_loaded:
            raise ValueError('The reading session is no longer available.')
        current = getattr(parent, 'library', None)
        if current is not reader_library or (current is not None and Path(current.root) != reader_root):
            raise ValueError('The active library changed. Open the voice reader again.')
        if source:
            source_guard()

    def playback_failed(exc):
        nonlocal playback_generation, repeat_scheduled
        playback_generation += 1
        repeat_scheduled = None
        cleanup = ''
        try:
            player.stop()
        except Exception as error:
            cleanup = f' Playback stop is unconfirmed: {error}'
        if progress is not None:
            try:
                progress.refresh_audio_state()
            except Exception as error:
                cleanup += f' Progress feedback is unavailable: {error}'
        report(f'Playback did not start. {exc}{cleanup}')

    def start_playback(action):
        generation = playback_generation
        def admitted():
            playback_scope()
            action()
        play_when_quiet((progress,), admitted, schedule=wx.CallLater,
            alive=lambda: alive and generation == playback_generation,
            on_error=playback_failed)

    def poll_audio(event=None):
        nonlocal playback_generation, repeat_scheduled
        if not alive:
            return
        try:
            if getattr(player, 'path', None) is not None:
                # Keep the timer lightweight. Full source authorization runs
                # at every actual repeat or explicit playback admission.
                playback_scope(source=False)
                outcome = player.poll()
                if outcome == 'sleep':
                    playback_generation += 1
                    repeat_scheduled = None
                    if progress is not None:
                        progress.refresh_audio_state()
                    report('Sleep timer stopped playback and cancelled repeat.')
                elif outcome == 'repeat-pending' and not busy and repeat_scheduled != playback_generation:
                    generation = playback_generation
                    repeat_scheduled = generation
                    def repeat():
                        nonlocal repeat_scheduled
                        try:
                            # Stop/quiet/pause may have disarmed this between
                            # the native end and the deferred quiet callback.
                            if generation == playback_generation:
                                player.repeat_if_ready()
                        finally:
                            repeat_scheduled = None
                    start_playback(repeat)
            if playback_refresh is not None:
                playback_refresh()
        except Exception as exc:
            playback_failed(exc)
            playback_timer.Stop()

    def playback_control(action, *, audible=False):
        nonlocal playback_generation, repeat_scheduled
        playback_scope()
        if busy:
            raise ValueError('Finish or stop the current speech operation before controlling playback.')
        playback_generation += 1
        repeat_scheduled = None
        if audible:
            start_playback(action)
        else:
            action()
            if progress is not None:
                progress.refresh_audio_state()

    def show_playback(event=None):
        nonlocal playback_refresh, playback_generation
        try:
            playback_scope()
            if last_audio is None or getattr(player, 'path', None) is None:
                raise ValueError('Read text once before controlling its current recording.')
            if busy:
                raise ValueError('Finish or stop the current speech operation first.')
            from .reader_playback import show_reader_playback
            def subscribe(callback):
                nonlocal playback_refresh
                playback_refresh = callback
            show_reader_playback(dialog, player, execute=playback_control,
                                 stop=stop_audio, pause=pause_audio, subscribe=subscribe)
        except Exception as exc:
            report(str(exc), focus=True)
        finally:
            playback_refresh = None
            # A control queued by a now-closed modal must never start later.
            playback_generation += 1
            if alive:
                editor.SetFocus()

    def reload(event=None):
        previous_index = voice_choice.GetSelection()
        previous_voice_id = voices[previous_index].id if 0 <= previous_index < len(voices) else ''
        def discover_configured():
            library_root = getattr(getattr(parent, 'library', None), 'root', None)
            if library_root is not None:
                from ..runtime_settings import voice_registry
                found = voice_registry(Path(library_root) / 'state', cancelled=cancel.is_set)
            else:
                found = VoiceRegistry.discover(cancelled=cancel.is_set)
            owned_registries.add(found)
            return found

        def loaded(value):
            nonlocal registry, voices, document, source_loaded, draft_store, recovered, recovery_notice
            previous = registry
            registry, source = value
            if previous is not None and previous is not registry:
                previous.close()
                owned_registries.discard(previous)
            voices = registry.voices()
            if source is not None:
                source_guard()
                fresh = source.reading_document()
                if (recovered is not None and recovered.document.source_text == fresh.source_text
                        and recovered.document.source_anchor == fresh.source_anchor):
                    document = recovered.document
                else:
                    if recovered is not None:
                        # Never attach an unbound or differently bound draft by text equality.
                        # Keep the prior recovery file unchanged; Save As is still available.
                        draft_store = None
                        recovered = None
                        recovery_notice += ('Previous draft has no matching source lineage and was preserved. '
                                            'Use Save As for this new copy. ')
                    document = fresh
                editor.ChangeValue(document.text)
                source_loaded = True
                source_status.ChangeValue(f'{source.title}; {len(source.positions)} source reading positions. '
                    + (' '.join(source.warnings[:3]) if source.warnings else 'Source positions authenticated.')
                    + ' This is a separate narration copy; it does not approve quotations. '
                    + recovery_notice)
            voice_choice.Set([f'{voice.name}; {voice.language}; {voice.engine}' for voice in voices])
            if voices:
                requested_voice = previous_voice_id or (recovered.voice_id if recovered is not None else '')
                selected = next((i for i, voice in enumerate(voices)
                                 if voice.id == requested_voice), -1 if requested_voice else 0)
                voice_choice.SetSelection(selected)
                if recovered is not None:
                    editor.SetInsertionPoint(native_cursor_position(editor, document.text, min(recovered.cursor, len(document.text))))
                report(recovery_notice + f'{len(voices)} installed voices available. Select text or press F6 to read. '
                       'Voice pitch, emotional delivery and word timing depend on the selected engine. '
                       + ('The saved or selected voice is unavailable; choose a voice explicitly. No substitute was selected.'
                          if selected < 0 else ''))
            else:
                report(recovery_notice + 'No local voice is available. Install a SAPI 5 voice on Windows or an optional '
                       'local engine. ' + ' '.join(registry.notices), focus=True)
        def load():
            source = source_loader(cancel.is_set) if not source_loaded else None
            return discover_configured(), source
        run(load, loaded)

    def voice_preferences(event=None):
        from ..runtime_settings import load_voice_preferences
        from ..language_qualification.catalog import canonical_language_tag
        from .voice_preferences import show_voice_preferences
        try:
            playback_scope(source=False)
            if reader_root is None or busy:
                raise ValueError('Open an idle reader in a library before editing voice preferences.')
            index = voice_choice.GetSelection()
            if not 0 <= index < len(voices):
                raise ValueError('Choose an installed voice first.')
            current = voices[index]
            try:
                declared_language = canonical_language_tag(current.language, adapter_underscores=True)
            except ValueError:
                declared_language = ''  # Favorites do not invent a language declaration.
            selected = {'voice_id': current.id,
                        'language': declared_language,
                        'rate': rate.GetValue(), 'volume': volume.GetValue()}
            def apply_selection(value):
                playback_scope(source=False)
                index = next((i for i, item in enumerate(voices) if item.id == value['voice_id']), None)
                if index is None:
                    raise ValueError('This voice is no longer available; reload installed voices.')
                voice_choice.SetSelection(index)
                rate.SetValue(value['rate'])
                volume.SetValue(value['volume'])
                capabilities()
                save_recovery(force=True)
            def opened(preferences):
                playback_scope(source=False)
                show_voice_preferences(dialog, state_root=reader_root / 'state', voices=voices,
                    preferences=preferences, selected=selected, execute=run,
                    guard=lambda: playback_scope(source=False), apply_selection=apply_selection)
            run(lambda: load_voice_preferences(reader_root / 'state'), opened)
        except Exception as exc:
            report(str(exc), focus=True)

    def sync_document():
        document.set_text(editor.GetValue())
        lineage = ('Retained-source locator recorded; quotation authority not granted. '
                   if document.source_anchor is not None else 'No retained-library source locator. ')
        source_status.ChangeValue(f'Original text preserved; reading revision {document.revision}; '
            f'{"unsaved changes" if document.dirty else "saved"}. ' + lineage
            + 'The reading copy is not quotation evidence. ' + recovery_notice)

    def save_recovery(event=None, *, force=False):
        nonlocal last_recovery_signature
        if not alive or draft_store is None or not source_loaded:
            return False
        sync_document()
        selected = voice_choice.GetSelection()
        voice_id = voices[selected].id if 0 <= selected < len(voices) else ''
        position = player.position_seconds if last_audio is not None else 0
        cursor_offset = native_insertion_offset(editor, document.text)
        signature = (document._signature(), voice_id, rate.GetValue(), volume.GetValue(), cursor_offset,
                     round(position, 1))
        if not force and signature == last_recovery_signature:
            return True
        draft_store.save(ReaderResume(document, voice_id, rate.GetValue(), volume.GetValue(),
                                     cursor_offset, position))
        last_recovery_signature = signature
        return True

    def autosave(event=None):
        try:
            save_recovery()
        except Exception as exc:
            report(f'The reading draft could not be autosaved. Keep this window open or use Save As. {exc}')

    def selected_text(from_cursor=False):
        sync_document()
        if from_cursor:
            return document.text[native_insertion_offset(editor, document.text):]
        selected = editor.GetStringSelection()
        return selected or editor.GetValue()

    def navigate_unit(event=None, *, direction=0, read_aloud=False):
        try:
            if not alive or busy or not source_loaded:
                raise ValueError('Wait for the current reader operation before navigating text.')
            source_guard()
            displayed = editor.GetValue()
            index = navigation_unit.GetSelection()
            if not 0 <= index < 3:
                raise ValueError('Choose word, sentence or paragraph navigation.')
            unit = ('word', 'sentence', 'paragraph')[index]
            span = select_text_unit(editor, displayed, unit, direction=direction)
            if span is None:
                report('No further ' + unit + ' in this direction. The selection is unchanged.')
                return
            report(f'Selected {unit}, characters {span[0] + 1} to {span[1]}. '
                   'Navigation does not start speech; F6 reads the selection.')
            if read_aloud:
                speak()
        except Exception as exc:
            report(str(exc), focus=True)

    def find_replace(event=None, *, spelling=False):
        try:
            if busy or not source_loaded or not alive:
                raise ValueError('Wait for the current reader operation before finding or replacing text.')
            from ..durable_state import local_path
            from .pronunciation_dialog import ReadingSnapshot
            from .find_replace_dialog import apply_find_preview, check_find_preview, create_find_dialog
            if spelling:
                from .spelling_dialog import apply_correction, create_spelling_dialog
            selected_library = getattr(parent, 'library', None)
            selected_root = (local_path(selected_library.root, exists=True)
                             if selected_library is not None else None)

            def guard():
                if (not alive or getattr(parent, '_closing', False)
                        or getattr(parent, 'library', None) is not selected_library
                        or selected_library is not None
                        and local_path(selected_library.root, exists=True) != selected_root):
                    raise ValueError('The library changed or the reader closed. Reopen find and replace.')
                source_guard()

            def capture():
                guard(); sync_document()
                return ReadingSnapshot.capture(document, native_selection(editor, document.text))

            def select(preview, index):
                current = capture()
                if spelling:
                    if current != preview or type(index) is not tuple or len(index) != 2:
                        raise ValueError('The reading copy or spelling issue changed. Check again.')
                    first, last = index
                    document._range(first, last)
                else:
                    check_find_preview(document, preview, current)
                    if type(index) is not int or not 0 <= index < len(preview.matches):
                        raise ValueError('Choose a match from this reviewed search.')
                    item = preview.matches[index]
                    first, last = item.start, item.end
                start = native_cursor_position(editor, document.text, first)
                end = native_cursor_position(editor, document.text, last)
                editor.SetSelection(start, end); editor.ShowPosition(end)
                if native_selection(editor, document.text) != (first, last):
                    raise ValueError('The text control could not select the exact reviewed range.')
                return capture()

            def synchronize():
                nonlocal last_audio, playback_generation
                playback_generation += 1; last_audio = None
                editor.ChangeValue(document.text); sync_document()
                try:
                    save_recovery(force=True)
                except Exception as exc:
                    return f'The reading change is in memory, but recovery save failed: {exc}. Use Save As before closing.'
                return ''

            def apply(preview, index=None):
                guard(); current = capture()
                if not spelling:
                    check_find_preview(document, preview, current)
                player.stop()
                if progress is not None:
                    progress.refresh_audio_state()
                if spelling:
                    apply_correction(document, preview, current)
                else:
                    apply_find_preview(document, preview, current, index=index)
                warning = synchronize()
                return capture(), warning

            def undo_find(expected):
                guard()
                if capture() != expected:
                    raise ValueError('The reading copy changed after replacement. Use the reader undo history explicitly.')
                player.stop()
                if progress is not None:
                    progress.refresh_audio_state()
                if not document.undo():
                    raise ValueError('The replacement is no longer available to undo.')
                return synchronize()

            factory = create_spelling_dialog if spelling else create_find_dialog
            review = factory(dialog, capture=capture, select=select, apply=apply,
                undo=undo_find, guard=guard, run=run, cancel=cancel)
            try:
                review.ShowModal()
            finally:
                if review.alive:
                    review.on_close()
                review.Destroy()
            if alive:
                capabilities(); editor.SetFocus()
        except Exception as exc:
            report(str(exc), focus=True)

    def pronunciations(event=None):
        try:
            if busy or not source_loaded:
                raise ValueError('Wait for the reader operation to finish before editing pronunciation rules.')
            from ..durable_state import local_path
            from .pronunciation_dialog import (ReadingSnapshot, apply_pronunciation_preview,
                                               create_pronunciation_dialog)
            selected_library = parent.library
            selected_root = local_path(selected_library.root, exists=True)

            def guard():
                if (not alive or getattr(parent, '_closing', False) or parent.library is not selected_library
                        or local_path(parent.library.root, exists=True) != selected_root):
                    raise ValueError('The library changed or the reader closed. Reopen pronunciation rules.')
                source_guard()

            def capture():
                guard(); sync_document()
                # wx positions are not Python character indexes on every native
                # text control. Convert the actual ranges, including Unicode/CRLF.
                start, end = native_selection(editor, document.text)
                index = voice_choice.GetSelection()
                voice = voices[index] if 0 <= index < len(voices) else None
                return ReadingSnapshot.capture(document, (start, end),
                    language=voice.language if voice else '', voice_id=voice.id if voice else '')

            def synchronize():
                nonlocal last_audio, playback_generation
                playback_generation += 1; last_audio = None
                editor.ChangeValue(document.text); sync_document()
                try:
                    save_recovery(force=True)
                except Exception as exc:
                    return f'The reading change is in memory, but recovery save failed: {exc}. Use Save As before closing.'
                return ''

            def apply(preview):
                guard(); current = capture()
                player.stop()
                if progress is not None:
                    progress.refresh_audio_state()
                apply_pronunciation_preview(document, preview, current)
                warning = synchronize()
                return capture(), warning

            def undo_pronunciation(expected):
                guard()
                if capture() != expected:
                    raise ValueError('The reading copy changed after applying. Use the reader undo history explicitly.')
                player.stop()
                if progress is not None:
                    progress.refresh_audio_state()
                if not document.undo():
                    raise ValueError('The pronunciation change is no longer available to undo.')
                return synchronize()

            review = create_pronunciation_dialog(dialog, library_root=selected_root, capture=capture,
                apply=apply, undo=undo_pronunciation, guard=guard, run=run, cancel=cancel)
            try:
                review.on_load()
                review.ShowModal()
            finally:
                if review.alive:
                    review.finish_close()
                review.Destroy()
            if alive:
                capabilities(); editor.SetFocus()
        except Exception as exc:
            report(str(exc), focus=True)

    def speak(event=None, *, from_cursor=False, destination=None):
        nonlocal last_audio, playback_generation
        try:
            if registry is None or not voices:
                raise ValueError('Choose an installed local voice first.')
            source_guard()
            content = selected_text(from_cursor)
            index = voice_choice.GetSelection()
            if not 0 <= index < len(voices):
                raise ValueError('Choose an installed local voice first.')
            voice = voices[index]
            settings = SpeechSettings(rate=rate.GetValue(), volume=volume.GetValue())
            playback_generation += 1
            player.stop()
            if progress is not None:
                progress.refresh_audio_state()
            output = destination or (Path(audio_files.name) / (uuid4().hex + '.wav'))

            def rendered(result):
                nonlocal last_audio
                last_audio = result.path
                if destination is None:
                    if cancel.is_set():
                        return
                    def playing():
                        player.play(result.path)
                        capabilities()
                        report(f'Playing {result.duration_seconds:.1f} seconds. F7 pauses or resumes; F8 stops. '
                               'Word highlighting is unavailable for this voice.')
                    start_playback(playing)
                else:
                    report(f'WAV saved: {result.path}. Duration {result.duration_seconds:.1f} seconds; '
                           f'{result.sample_rate} Hz; {result.channels} channels.', focus=True)
            report('Rendering locally. F8 cancels. Original text is unchanged.')
            run(lambda: registry.synthesize(content, voice.id, output,
                                            settings=settings, cancelled=cancel.is_set), rendered)
        except Exception as exc:
            report(str(exc), focus=True)

    def save_audio(event=None):
        with wx.FileDialog(dialog, 'Save narrated PCM WAV', wildcard='PCM WAV (*.wav)|*.wav',
                           style=wx.FD_SAVE) as chooser:
            if chooser.ShowModal() == wx.ID_OK:
                speak(destination=Path(chooser.GetPath()))

    def pause_audio(event=None):
        nonlocal playback_generation, repeat_scheduled
        playback_generation += 1
        repeat_scheduled = None
        try:
            if player.state == 'paused':
                def resumed():
                    player.resume()
                    report(f'Playback {player.state}; {player.position_seconds:.1f} seconds.')
                start_playback(resumed)
                return
            else:
                player.pause()
            if progress is not None:
                progress.refresh_audio_state()
            report(f'Playback {player.state}; {player.position_seconds:.1f} seconds.')
        except Exception as exc:
            report(str(exc), focus=True)

    def stop_audio(event=None):
        nonlocal playback_generation, repeat_scheduled
        playback_generation += 1
        repeat_scheduled = None
        cancel.set()
        try:
            player.stop()
            if progress is not None:
                progress.refresh_audio_state()
            report('Stopping synthesis or playback. Any completed saved audio remains available.')
        except Exception as exc:
            report(str(exc), focus=True)

    def seek_audio(event=None):
        try:
            if last_audio is None:
                raise ValueError('Read text once before seeking within its recording.')
            with wx.TextEntryDialog(dialog, f'Seconds from 0 to {player.duration_seconds:.1f}',
                                    'Seek playback', str(round(player.position_seconds, 1))) as entry:
                if entry.ShowModal() == wx.ID_OK:
                    seconds = float(entry.GetValue())
                    def sought():
                        player.seek(seconds, start_if_stopped=False)
                        report(f'Playback at {player.position_seconds:.1f} seconds.'
                               + (' Position is approximate on this playback backend.' if player.position_is_approximate else ''))
                    playback_control(sought, audible=True)
        except Exception as exc:
            report(str(exc), focus=True)

    def save_project(event=None, *, choose=False):
        nonlocal save_path
        try:
            source_guard()
            sync_document()
            if not choose and save_path is None and draft_store is not None:
                save_recovery(force=True)
                document._saved_signature = document._signature()
                sync_document()
                report('Reading draft, voice settings and current position saved for recovery. '
                       'Save As exports an independent reading project.')
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
            save_recovery(force=True)
            sync_document()
            report(f'Reading copy and undo history saved: {selected}. Original source retained exactly.')
            return True
        except Exception as exc:
            report(f'Reading copy was not saved. {exc}', focus=True)
            return False

    def open_project(event=None):
        nonlocal document, save_path, draft_store, recovery_notice, last_audio, playback_generation
        if busy:
            report('Stop the current render before opening another document.', focus=True)
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
                source = Path(chooser.GetPath())
            if source.suffix.lower() == '.json':
                loaded = ReadingDocument.load(source)
                selected = source
            else:
                from ..speech._io import local_file
                source = local_file(source)
                if source.stat().st_size > 8_000_000:
                    raise ValueError('Open one text chapter at a time, up to 8 MB.')
                loaded = ReadingDocument(source.read_text(encoding='utf-8-sig'), title=source.stem, source_path=source)
                selected = None
            if source_loader is not None and (not source_loaded or loaded.source_anchor is None
                    or loaded.source_anchor != document.source_anchor):
                raise ValueError('Open unrelated or unbound reading projects in the standalone voice reader.')
            playback_generation += 1
            player.stop()
            last_audio = None
            if progress is not None:
                progress.refresh_audio_state()
            document, save_path = loaded, selected
            if document.format_notice:
                draft_store = None
                save_path = None
                recovery_notice = document.format_notice
            editor.ChangeValue(document.text)
            sync_document()
            editor.SetFocus()
            capabilities()
            report(recovery_notice + 'Reading copy opened. Original text is retained separately; no quotation was approved.')
        except Exception as exc:
            report(str(exc), focus=True)

    def history(event=None, *, forward=False):
        try:
            sync_document()
            changed = document.redo() if forward else document.undo()
            editor.ChangeValue(document.text)
            sync_document()
            report(f'Reading revision {document.revision}.' if changed else 'No further reading edit.')
            editor.SetFocus()
        except Exception as exc:
            report(str(exc), focus=True)

    def clean(event=None):
        try:
            sync_document()
            document.cleanup()
            editor.ChangeValue(document.text)
            sync_document()
            report('Whitespace normalized in the reading copy. Undo restores it; the source is unchanged.')
        except Exception as exc:
            report(str(exc), focus=True)

    def compare(event=None):
        try:
            sync_document()
            preview = wx.Dialog(dialog, title='Original source and reading-copy diff', size=(800, 600),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            box = wx.BoxSizer(wx.VERTICAL)
            label = wx.StaticText(preview, label='Immutable source followed by reading-copy changes')
            box.Add(label, 0, wx.ALL, 6)
            field = wx.TextCtrl(preview, value=document.source_text + '\n\nREADING-COPY DIFF\n' + document.diff(),
                                style=wx.TE_MULTILINE | wx.TE_READONLY)
            field.SetName('Original source and reading-copy changes')
            box.Add(field, 1, wx.ALL | wx.EXPAND, 6)
            box.Add(wx.Button(preview, wx.ID_CANCEL, 'Close'), 0, wx.ALL | wx.ALIGN_RIGHT, 6)
            preview.SetSizer(box)
            field.SetFocus()
            preview.ShowModal()
            preview.Destroy()
        except Exception as exc:
            report(str(exc), focus=True)

    def closing(event):
        try:
            sync_document()
            if document.dirty and document.text:
                answer = wx.MessageBox('Save the reading copy and its undo history before closing?',
                                       'Unsaved reading copy', wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION, dialog)
                if answer == wx.CANCEL or (answer == wx.YES and not save_project()):
                    return
            dialog.EndModal(wx.ID_CANCEL)
        except Exception as exc:
            report(f'The reading copy is still open. {exc}', focus=True)

    for control, handler in ((reload_button, reload), (progress_voice, select_progress_voice), (read, speak),
        (cursor, lambda event: speak(from_cursor=True)), (pause, pause_audio), (stop, stop_audio),
        (previous_unit, lambda event: navigate_unit(direction=-1)), (current_unit, navigate_unit),
        (next_unit, lambda event: navigate_unit(direction=1)),
        (read_unit, lambda event: navigate_unit(read_aloud=True)),
        (export_audio, save_audio), (save_button, save_project),
        (save_as, lambda event: save_project(choose=True)), (open_button, open_project),
        (undo, history), (redo, lambda event: history(forward=True)),
        (original, compare), (seek, seek_audio), (playback_controls, show_playback),
        (appearance_button, choose_reading_appearance),
        (cleanup, clean), (find_button, find_replace),
        (pronunciation, pronunciations), (close, closing)):
        control.Bind(wx.EVT_BUTTON, handler)
    voice_choice.Bind(wx.EVT_CHOICE, capabilities)
    voice_preferences_button.Bind(wx.EVT_BUTTON, voice_preferences)
    spelling_button.Bind(wx.EVT_BUTTON, lambda event: find_replace(event, spelling=True))
    dialog.Bind(wx.EVT_CLOSE, closing)
    accelerators = []
    for flags, key, handler in ((wx.ACCEL_CTRL, ord('S'), save_project),
        (wx.ACCEL_CTRL, ord('F'), find_replace),
        (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('S'), lambda event: save_project(choose=True)),
        (0, wx.WXK_F6, speak), (0, wx.WXK_F7, pause_audio), (0, wx.WXK_F8, stop_audio),
        (wx.ACCEL_CTRL | wx.ACCEL_ALT, wx.WXK_LEFT, lambda event: navigate_unit(direction=-1)),
        (wx.ACCEL_CTRL | wx.ACCEL_ALT, wx.WXK_HOME, navigate_unit),
        (wx.ACCEL_CTRL | wx.ACCEL_ALT, wx.WXK_RIGHT, lambda event: navigate_unit(direction=1)),
        (wx.ACCEL_CTRL | wx.ACCEL_ALT, wx.WXK_RETURN, lambda event: navigate_unit(read_aloud=True))):
        identifier = wx.NewIdRef()
        accelerators.append((flags, key, int(identifier)))
        dialog.Bind(wx.EVT_MENU, handler, id=int(identifier))
    dialog.SetAcceleratorTable(wx.AcceleratorTable(accelerators))
    autosave_timer = wx.Timer(dialog)
    dialog.Bind(wx.EVT_TIMER, autosave, autosave_timer)
    playback_timer = wx.Timer(dialog)
    dialog.Bind(wx.EVT_TIMER, poll_audio, playback_timer)
    from .save_all import SaveResult, coordinator_for
    coordinator = coordinator_for(parent)
    def unsaved():
        sync_document()
        return bool(document.text) and document.dirty
    def save_registered(completed):
        if busy:
            completed(SaveResult(False, 'Speech is running; stop it before saving the reading copy.'))
        else:
            completed(SaveResult(bool(save_project()), 'The reading copy was not saved.'))
    unregister = (coordinator.register('reading.' + str(id(dialog)), 'Voice reading copy',
                    dirty=unsaved, save=save_registered, focus=editor.SetFocus)
                  if coordinator is not None else lambda: None)
    try:
        capabilities()
        reload()
        autosave_timer.Start(5000)
        playback_timer.Start(100)
        editor.SetFocus()
        dialog.ShowModal()
    finally:
        alive = False
        cancel.set()
        notices = []
        for cleanup, label in ((unregister, 'Save coordinator'),
                               (autosave_timer.Stop, 'Reading-draft timer'),
                               (playback_timer.Stop, 'Playback timer')):
            try:
                cleanup()
            except Exception as exc:
                notices.append(f'{label} could not be detached: {exc}')
        silence_confirmed = False
        try:
            player.close()
            silence_confirmed = True
        except Exception as exc:
            notices.append(f'Playback device reported a close error: {exc}')
        if progress is not None:
            try:
                progress.unregister_player(player, silence_confirmed=silence_confirmed)
            except Exception as exc:
                notices.append(f'Playback progress feedback could not be refreshed: {exc}')

        def retain_resources(resources):
            retained = getattr(parent, '_reader_playback_retained', None)
            if not isinstance(retained, list):
                retained = []
                parent._reader_playback_retained = retained
            retained.append((player, audio_files, executor, resources))

        # The serial worker releases registries and temporary output only after
        # active synthesis/discovery exits; closing the UI never waits for it.
        class ReaderResources:
            def close(self):
                errors = []
                for owned in (*owned_registries, *owned_progress_voices):
                    try:
                        owned.close()
                    except Exception as exc:
                        errors.append(str(exc))
                if silence_confirmed:
                    audio_files.cleanup()
                if errors:
                    raise RuntimeError('; '.join(errors))
        resources = ReaderResources()
        if not silence_confirmed:
            retain_resources(resources)
            notices.append('Temporary playback audio is retained until the device confirms it is closed.')
        try:
            from .worker_cleanup import close_after_worker
            cleanup_future = close_after_worker(executor, resources)
            def cleanup_finished(future):
                try:
                    future.result()
                except Exception as exc:
                    # The dialog is already closed. Keep failed cleanup's owned
                    # files alive and deliver its warning on the native queue.
                    retain_resources(resources)
                    message = f'Temporary reading audio cleanup needs attention: {exc}'
                    def notify():
                        try:
                            parent.report(message)
                        except Exception:
                            pass
                    try:
                        wx.CallAfter(notify)
                    except Exception:
                        pass
            cleanup_future.add_done_callback(cleanup_finished)
        except Exception as exc:
            retain_resources(resources)
            notices.append(f'The reading worker has not confirmed cleanup: {exc}')
        finally:
            try:
                if notices:
                    try:
                        parent.report(' '.join(notices))
                    except Exception:
                        pass  # A detached reporter must not trap the modal.
            finally:
                dialog.Destroy()


def install_speech_actions(frame):
    """Add one conventional menu without modifying existing keyboard bindings."""
    import wx
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command

    menu = wx.Menu()
    item = menu.Append(wx.ID_ANY, 'Voice reader and reading copy...\tCtrl+Alt+V')
    frame.Bind(wx.EVT_MENU, lambda event: show_speech_reader(frame), item)
    register_menu_command(frame, CommandSpec('voice.reader', 'Voice reader and reading copy',
                                           'main', 'Ctrl+Alt+V'), item)
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
    register_menu_command(frame, CommandSpec('voice.book', 'Narrate selected book or chapter',
                                           'main', 'Ctrl+Alt+R'), selected)
    frame.speech_reading_actions = actions
    frame.GetMenuBar().Append(menu, '&Voice')
