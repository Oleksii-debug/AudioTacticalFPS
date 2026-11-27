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
from ..speech.navigation import native_cursor_position, native_selection, select_text_unit
from ..speech._io import text_value
from .pronunciation_dialog import PronunciationDialog, caret_offset, native_caret_position, text_at_caret


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
    library = getattr(parent, 'library', None)
    library_root = Path(library.root) if library is not None else None
    pronunciations = ()
    playback_generation = 0
    save_path = None
    last_audio = None
    source_loaded = source_loader is None
    draft_store = None
    recovered = None
    recovery_notice = ''
    last_recovery_signature = None

    def check_current(*, check_source=True):
        if not alive or getattr(parent, '_closing', False):
            raise InterruptedError('The voice reader is closing.')
        if library is not None and (getattr(parent, 'library', None) is not library
                                   or Path(library.root) != library_root):
            raise InterruptedError('The active library changed. Reopen the voice reader from that library.')
        if check_source:
            source_guard()

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
    layout.Add(controls, 0, wx.EXPAND)
    editor = labelled('&Reading copy (editable; Ctrl+S saves)', wx.TextCtrl,
                      value=text, style=wx.TE_MULTILINE | wx.TE_DONTWRAP, proportion=1)
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
    row = wx.WrapSizer(wx.HORIZONTAL)
    read_line = wx.Button(dialog, label='Read current &line')
    read_paragraph = wx.Button(dialog, label='Read current para&graph')
    read_clipboard_button = wx.Button(dialog, label='Read &clipboard once')
    dictionary = wx.Button(dialog, label='Pronunciation &dictionary...')
    find_button = wx.Button(dialog, label='Find and replace... (Ctrl+F)')
    for control in (read_line, read_paragraph, read_clipboard_button, dictionary, find_button):
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
    cleanup = wx.Button(dialog, label='Normalize s&paces')
    commands = wx.Button(dialog, label='Find voice reader command...')
    keyboard_settings = wx.Button(dialog, label='Voice reader keyboard shortcuts...')
    close = wx.Button(dialog, wx.ID_CANCEL, 'C&lose')
    for control in (seek, cleanup, commands, keyboard_settings, close):
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
        for button in (read, cursor, read_line, read_paragraph, read_clipboard_button, export_audio):
            button.Enable(bool(current) and not busy)
        dictionary.Enable(source_loaded and not busy)
        find_button.Enable(source_loaded and not busy)
        for control in (navigation_unit, previous_unit, current_unit, next_unit):
            control.Enable(source_loaded and not busy)
        read_unit.Enable(source_loaded and bool(current) and not busy)
        progress_voice.Enable(bool(progress is not None and current and 'volume' in current.capabilities) and not busy)
        if event is not None and current:
            report(f'{current.name}; {current.language}; {current.engine}. '
                   f'Controls: {", ".join(current.capabilities) or "voice defaults only"}. {current.timing}')

    def run(work, on_done, *, on_error=None, on_finished=None):
        nonlocal busy
        if not alive:
            return False
        if busy:
            report('A speech operation is running. Stop it before starting another.', focus=True)
            return False
        try:
            check_current()
        except Exception as exc:
            (on_error or (lambda error: report(str(error), focus=True)))(exc)
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
                check_current()
                on_done(future.result())
            except Exception as exc:
                if on_error is not None:
                    on_error(exc)
                else:
                    report(f'Operation did not complete. {exc}', focus=True)
            finally:
                if on_finished is not None:
                    on_finished()
                capabilities()
        def guarded():
            check_current()
            if cancel.is_set():
                raise InterruptedError('Speech operation cancelled before it started.')
            return work()
        try:
            executor.submit(guarded).add_done_callback(lambda future: wx.CallAfter(finish, future))
        except Exception as exc:
            busy = False
            reload_button.Enable()
            (on_error or (lambda error: report(str(error), focus=True)))(exc)
            if on_finished is not None:
                on_finished()
            capabilities()
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

    def start_playback(action):
        generation = playback_generation
        def current_action():
            check_current()
            if cancel.is_set():
                return
            action()
        play_when_quiet((progress,), current_action, schedule=wx.CallLater,
            alive=lambda: alive and generation == playback_generation,
            on_error=lambda exc: report(f'Playback did not start. {exc}', focus=True))

    def reload(event=None):
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
                selected = next((i for i, voice in enumerate(voices)
                                 if recovered is not None and voice.id == recovered.voice_id), 0)
                voice_choice.SetSelection(selected)
                if recovered is not None:
                    editor.SetInsertionPoint(native_caret_position(editor, recovered.cursor))
                report(recovery_notice + f'{len(voices)} installed voices available. Select text or press F6 to read. '
                       'Voice pitch, emotional delivery and word timing depend on the selected engine.')
            else:
                report(recovery_notice + 'No local voice is available. Install a SAPI 5 voice on Windows or an optional '
                       'local engine. ' + ' '.join(registry.notices), focus=True)
        def load():
            source = source_loader(cancel.is_set) if not source_loaded else None
            return discover_configured(), source
        run(load, loaded)

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
        check_current()
        sync_document()
        selected = voice_choice.GetSelection()
        voice_id = voices[selected].id if 0 <= selected < len(voices) else ''
        position = player.position_seconds if last_audio is not None else 0
        cursor_offset = min(caret_offset(editor), len(document.text))
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

    def selected_text(from_cursor=False, *, mode='selection'):
        sync_document()
        if from_cursor:
            return editor.GetRange(editor.GetInsertionPoint(), editor.GetLastPosition())
        if mode in ('line', 'paragraph'):
            return text_at_caret(editor, paragraph=mode == 'paragraph')
        selected = editor.GetStringSelection()
        return selected or editor.GetValue()

    def navigate_unit(event=None, *, direction=0, read_aloud=False):
        try:
            if not alive or busy or not source_loaded:
                raise ValueError('Wait for the current reader operation before navigating text.')
            check_current()
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

    def find_replace(event=None):
        try:
            if busy or not source_loaded or not alive:
                raise ValueError('Wait for the current reader operation before finding or replacing text.')
            check_current()
            from ..durable_state import local_path
            from .pronunciation_dialog import ReadingSnapshot
            from .find_replace_dialog import apply_find_preview, check_find_preview, create_find_dialog
            selected_library = getattr(parent, 'library', None)
            selected_root = (local_path(selected_library.root, exists=True)
                             if selected_library is not None else None)

            def guard():
                if (not alive or getattr(parent, '_closing', False)
                        or getattr(parent, 'library', None) is not selected_library
                        or selected_library is not None
                        and local_path(selected_library.root, exists=True) != selected_root):
                    raise ValueError('The library changed or the reader closed. Reopen find and replace.')
                check_current()

            def capture():
                guard(); sync_document()
                return ReadingSnapshot.capture(document, native_selection(editor, document.text))

            def select(preview, index):
                current = capture()
                check_find_preview(document, preview, current)
                if type(index) is not int or not 0 <= index < len(preview.matches):
                    raise ValueError('Choose a match from this reviewed search.')
                item = preview.matches[index]
                start = native_cursor_position(editor, document.text, item.start)
                end = native_cursor_position(editor, document.text, item.end)
                editor.SetSelection(start, end); editor.ShowPosition(end)
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

            def apply(preview, index):
                guard(); current = capture()
                check_find_preview(document, preview, current)
                player.stop()
                if progress is not None:
                    progress.refresh_audio_state()
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

            review = create_find_dialog(dialog, capture=capture, select=select, apply=apply,
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

    def speak(event=None, *, from_cursor=False, destination=None, mode='selection', content=None):
        nonlocal last_audio, playback_generation
        try:
            if busy or not alive:
                return
            check_current()
            if registry is None or not voices:
                raise ValueError('Choose an installed local voice first.')
            content = text_value(content if content is not None else selected_text(from_cursor, mode=mode),
                                 'Text to read')
            selected = voice_choice.GetSelection()
            if not 0 <= selected < len(voices):
                raise ValueError('Choose an installed local voice first.')
            voice = voices[selected]
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
                        report(f'Playing {result.duration_seconds:.1f} seconds. F7 pauses or resumes; F8 stops. '
                               'Word highlighting is unavailable for this voice.')
                    start_playback(playing)
                else:
                    report(f'WAV saved: {result.path}. Duration {result.duration_seconds:.1f} seconds; '
                           f'{result.sample_rate} Hz; {result.channels} channels.', focus=True)
            report(('Rendering an explicit clipboard snapshot; it is not part of this reading copy. '
                    if mode == 'clipboard' else 'Rendering locally. Original text is unchanged. ')
                   + 'F8 cancels.')
            run(lambda: registry.synthesize(content, voice.id, output,
                                            settings=settings, cancelled=cancel.is_set), rendered)
        except Exception as exc:
            report(str(exc), focus=True)

    def read_clipboard(event=None):
        try:
            if busy or not alive:
                return
            check_current()
            if not wx.TheClipboard.Open():
                raise RuntimeError('The clipboard is unavailable. Try the explicit Read clipboard action again.')
            try:
                data = wx.TextDataObject()
                if not wx.TheClipboard.GetData(data):
                    raise ValueError('The clipboard does not contain readable text.')
                content = text_value(data.GetText(), 'Clipboard text')
            finally:
                wx.TheClipboard.Close()
            check_current()
            speak(content=content, mode='clipboard')
        except Exception as exc:
            report(str(exc), focus=True)

    def prepare_dictionary_apply(rules):
        check_current()
        sync_document()
        expected_document, expected_text = document, document.text
        expected_signature = document._signature()
        selected = voice_choice.GetSelection()
        voice = voices[selected] if 0 <= selected < len(voices) else None
        def stopped():
            # Per-match checks stay cheap; the worker and accepted result both
            # reauthorize the retained source at their existing boundaries.
            check_current(check_source=False)
            return cancel.is_set()
        def work():
            copy = ReadingDocument(expected_text)
            copy.apply_pronunciations(rules, language=voice.language if voice else '',
                                     voice_id=voice.id if voice else '', cancelled=stopped)
            return copy.text
        def accept(value):
            check_current()
            if cancel.is_set():
                raise InterruptedError('Pronunciation application cancelled; the reading copy is unchanged.')
            if (document is not expected_document or document._signature() != expected_signature
                    or editor.GetValue() != expected_text):
                raise ValueError('The reading copy changed. Review it and apply the dictionary again.')
            document.set_text(value, label='Pronunciation reading layer')
            editor.ChangeValue(document.text)
            sync_document()
            report('Pronunciation rules applied as one undoable reading-copy edit. The retained source is unchanged.')
        return work, accept

    def show_dictionary(event=None):
        nonlocal pronunciations
        try:
            if busy or not alive or not source_loaded:
                return
            check_current()
            def changed(rules):
                nonlocal pronunciations
                check_current()
                pronunciations = tuple(rules)
            selected = voice_choice.GetSelection()
            voice = voices[selected] if 0 <= selected < len(voices) else None
            window = PronunciationDialog(dialog, pronunciations, run=run,
                cancelled=cancel.is_set, cancel=cancel.set, guard=check_current,
                private_root=library_root or Path(audio_files.name), changed=changed,
                prepare_apply=prepare_dictionary_apply,
                active_voice=(f'Current language: {voice.language}; voice ID: {voice.id}.' if voice
                              else 'No voice selected; only unscoped rules can apply.'), wx_module=wx)
            window.show()
        except Exception as exc:
            report(str(exc), focus=True)

    def save_audio(event=None):
        with wx.FileDialog(dialog, 'Save narrated PCM WAV', wildcard='PCM WAV (*.wav)|*.wav',
                           style=wx.FD_SAVE) as chooser:
            if chooser.ShowModal() == wx.ID_OK:
                speak(destination=Path(chooser.GetPath()))

    def pause_audio(event=None):
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
        nonlocal playback_generation
        playback_generation += 1
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
                        player.seek(seconds)
                        report(f'Playback at {player.position_seconds:.1f} seconds.'
                               + (' Position is approximate on this playback backend.' if player.position_is_approximate else ''))
                    start_playback(sought)
        except Exception as exc:
            report(str(exc), focus=True)

    def save_project(event=None, *, choose=False):
        nonlocal save_path
        try:
            check_current()
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
            check_current()
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
        nonlocal document, save_path, playback_generation, draft_store, recovery_notice
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
            player.stop()
            playback_generation += 1
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
        (export_audio, save_audio), (save_button, save_project),
        (save_as, lambda event: save_project(choose=True)), (open_button, open_project),
        (undo, history), (redo, lambda event: history(forward=True)),
        (original, compare), (seek, seek_audio), (cleanup, clean), (close, closing),
        (read_line, lambda event: speak(mode='line')),
        (read_paragraph, lambda event: speak(mode='paragraph')),
        (read_clipboard_button, read_clipboard), (dictionary, show_dictionary),
        (find_button, find_replace),
        (previous_unit, lambda event: navigate_unit(direction=-1)),
        (current_unit, navigate_unit), (next_unit, lambda event: navigate_unit(direction=1)),
        (read_unit, lambda event: navigate_unit(read_aloud=True))):
        control.Bind(wx.EVT_BUTTON, handler)
    voice_choice.Bind(wx.EVT_CHOICE, capabilities)
    dialog.Bind(wx.EVT_CLOSE, closing)
    from .reading_shortcuts import ReaderKeyboardAdapter
    class VoiceCommands:
        @property
        def closed(self): return not alive
        @property
        def busy(self): return busy
    command_view = VoiceCommands()
    command_view.dialog = dialog
    command_view.command_buttons = {
        'voice_reader.read': (read, speak),
        'voice_reader.cursor': (cursor, lambda event: speak(from_cursor=True)),
        'voice_reader.line': (read_line, lambda event: speak(mode='line')),
        'voice_reader.paragraph': (read_paragraph, lambda event: speak(mode='paragraph')),
        'voice_reader.clipboard': (read_clipboard_button, read_clipboard),
        'voice_reader.dictionary': (dictionary, show_dictionary),
        'voice_reader.find': (find_button, find_replace),
        'voice_reader.previous_unit': (previous_unit, lambda event: navigate_unit(direction=-1)),
        'voice_reader.current_unit': (current_unit, navigate_unit),
        'voice_reader.next_unit': (next_unit, lambda event: navigate_unit(direction=1)),
        'voice_reader.read_unit': (read_unit, lambda event: navigate_unit(read_aloud=True)),
        'voice_reader.pause': (pause, pause_audio), 'voice_reader.stop': (stop, stop_audio),
        'voice_reader.save': (save_button, save_project),
        'voice_reader.save_as': (save_as, lambda event: save_project(choose=True)),
        'voice_reader.commands': (commands, lambda event: keyboard.commands(event)),
        'voice_reader.settings': (keyboard_settings, lambda event: keyboard.settings(event)),
    }
    from .builtin_keyboard_commands import VOICE_READER_COMMANDS
    for command in VOICE_READER_COMMANDS:
        command_view.command_buttons[command.command_id][0].SetName(command.label)
    def command_available(identity):
        pair = command_view.command_buttons.get(identity)
        return bool(pair and pair[0].IsEnabled() and (not busy or identity == 'voice_reader.stop'))
    keyboard = ReaderKeyboardAdapter(command_view, getattr(parent, 'keyboard_commands', None),
        context='voice_reader', private_root=library_root or Path(audio_files.name), available=command_available)
    commands.Bind(wx.EVT_BUTTON, keyboard.commands)
    keyboard_settings.Bind(wx.EVT_BUTTON, keyboard.settings)
    autosave_timer = wx.Timer(dialog)
    dialog.Bind(wx.EVT_TIMER, autosave, autosave_timer)
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
    unregister_save = (coordinator.register('reading.' + str(id(dialog)), 'Voice reading copy',
                    dirty=unsaved, save=save_registered, focus=editor.SetFocus)
                  if coordinator is not None else lambda: None)
    def unregister():
        try:
            unregister_save()
        finally:
            keyboard.close()
    try:
        capabilities()
        reload()
        autosave_timer.Start(5000)
        editor.SetFocus()
        dialog.ShowModal()
    finally:
        alive = False
        cancel.set()
        notices = []
        for cleanup, label in ((unregister, 'Save coordinator'),
                               (autosave_timer.Stop, 'Reading-draft timer')):
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
