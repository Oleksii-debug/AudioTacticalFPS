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
from .reader_playback import ReaderPlaybackControls, show_reader_playback
from .reader_transport import ReaderPlaybackSession
from .reader_navigation import ReaderNavigation
from .reader_preparation import show_reader_preparation
from .reader_synthesis import ReaderSynthesisControls
from ..speech.navigation import UNITS as READING_UNITS
from .reader_output import ReaderOutputControls
from .reader_presentation import ReaderPresentationControls
from .reading_tools import (ReaderEditHost, ReadingSnapshot, reading_offset,
                            reading_selection, native_position, select_reading_range,
                            show_find_replace, show_pronunciations)
from .keyboard_settings import VoiceReaderKeyboardAdapter


def show_speech_reader(parent, *, text='', title='Reading copy', source_loader=None,
                       source_guard=lambda: None, recovery_slot='manual'):
    import wx

    dialog = wx.Dialog(parent, title='Voice reader and reading copy', size=(900, 800),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-speech')
    cancel = threading.Event()
    audio_files = TemporaryDirectory(prefix='research-reader-')
    player = WavePlayer()
    reader_library = getattr(parent, 'library', None)
    reader_root = getattr(reader_library, 'root', None)
    progress = getattr(parent, 'operation_progress', None)
    if progress is not None:
        progress.register_player(player)
    document = ReadingDocument(text, title=title)
    tabs_requested = source_loader is None and recovery_slot == 'manual' and reader_root is not None
    reader_tabs = None
    explicit_reader_text = bool(text)
    if tabs_requested and not text:
        # A newly opened untouched blank is clean; deleting existing text is
        # still dirty and is handled independently of its resulting length.
        document._saved_signature = document._signature()
    close_requested = False
    registry = None
    voices = ()
    alive = True
    dialog_destroyed, dialog_id = False, dialog.GetId()
    busy = False
    playback_generation = 0
    save_path = None
    last_audio = None
    last_reading_audio = None
    source_loaded = source_loader is None and not tabs_requested
    draft_store = None
    recovered = None
    recovery_notice = ''
    last_recovery_signature = None
    reading_rules = ()
    try:
        if reader_root is not None and not tabs_requested:
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
    remembered_voice_id = recovered.voice_id if recovered is not None else ''
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
    # The placeholder remains disabled while the existing reader worker loads
    # bounded recovery. Native notebook pages are attached only on completion.
    editor.Enable(source_loaded)
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
    unit_row = wx.WrapSizer(wx.HORIZONTAL)
    unit_row.Add(wx.StaticText(dialog, label='Text navigation unit'), 0, wx.ALL, 4)
    navigation_unit = wx.Choice(dialog, choices=['Word', 'Logical line', 'Sentence', 'Paragraph'])
    navigation_unit.SetName('Text unit for current, previous and next selection')
    navigation_unit.SetSelection(0)
    unit_row.Add(navigation_unit, 0, wx.ALL, 4)
    previous_unit = wx.Button(dialog, label='Previous unit')
    current_unit = wx.Button(dialog, label='Select current unit')
    next_unit = wx.Button(dialog, label='Next unit')
    for button, label in ((previous_unit, 'Previous unit'),
                          (current_unit, 'Select current unit'), (next_unit, 'Next unit')):
        button.SetName(label)
        unit_row.Add(button, 0, wx.ALL, 4)
    layout.Add(unit_row, 0, wx.EXPAND)
    read_units = {}
    unit_speech_row = wx.WrapSizer(wx.HORIZONTAL)
    for unit in READING_UNITS:
        label = 'Read current ' + ('logical line' if unit == 'line' else unit)
        button = wx.Button(dialog, label=label)
        button.SetName(label)
        read_units[unit] = button
        unit_speech_row.Add(button, 0, wx.ALL, 4)
    layout.Add(unit_speech_row, 0, wx.EXPAND)
    tools_row = wx.WrapSizer(wx.HORIZONTAL)
    find_button = wx.Button(dialog, label='&Find in reading copy...')
    find_button.SetName('Find in reading copy')
    replace_button = wx.Button(dialog, label='Find and &replace...')
    replace_button.SetName('Find and replace in reading copy')
    pronunciation_button = wx.Button(dialog, label='Pronunciation &dictionary...')
    pronunciation_button.SetName('Reading pronunciation dictionary')
    preparation_button = wx.Button(dialog, label='Prepare for speech...')
    preparation_button.SetName('Prepare reading copy for speech')
    assistant_button = wx.Button(dialog, label='Current-reader assistant...')
    assistant_button.SetName('Assistant for this reader and current playback')
    dialog.reader_assistant_button = assistant_button
    dialog.reader_preparation_button = preparation_button
    spelling_button = wx.Button(dialog, label='Check &spelling...')
    spelling_button.SetName('Check spelling in reading copy')
    appearance_button = wx.Button(dialog, label='Reading appearance...')
    appearance_button.SetName('Reading appearance')
    focus_button = wx.Button(dialog, label='Reading focus...')
    focus_button.SetName('Reading focus and approximate audio follow')
    keyboard_button = wx.Button(dialog, label='Reader &keyboard shortcuts...')
    keyboard_button.SetName('Voice reader keyboard shortcuts')
    voice_preferences_button = wx.Button(dialog, label='Voice preferences...')
    voice_preferences_button.SetName('Voice preferences')
    dialog.voice_preferences_button = voice_preferences_button
    synthesis_controls = ReaderSynthesisControls(dialog,
        pitch=recovered.pitch if recovered else None,
        punctuation=recovered.punctuation if recovered else False,
        alive=lambda: alive, busy=lambda: busy, guard=lambda: playback_guard(),
        before_apply=lambda: before_native_options_apply(),
        changed=lambda: native_options_changed(),
        report=lambda message, focus=False: report(message, focus=focus), wx=wx)
    dialog.reader_synthesis = synthesis_controls
    for control in (find_button, replace_button, pronunciation_button, preparation_button,
                    assistant_button, spelling_button, appearance_button, focus_button, keyboard_button,
                    voice_preferences_button, synthesis_controls.button):
        tools_row.Add(control, 0, wx.ALL, 4)
    layout.Add(tools_row, 0, wx.EXPAND)
    row = wx.BoxSizer(wx.HORIZONTAL)
    open_button = wx.Button(dialog, label='&Open text / project')
    save_button = wx.Button(dialog, label='&Save reading copy')
    save_as = wx.Button(dialog, label='Save &as')
    undo = wx.Button(dialog, label='&Undo reading edit')
    redo = wx.Button(dialog, label='R&edo reading edit')
    save_all_button = wx.Button(dialog, label='Save all open drafts')
    save_all_button.SetName('Save all open drafts through the shared Save All coordinator')
    cancel_all_button = wx.Button(dialog, label='Cancel Save All')
    cancel_all_button.SetName('Cancel Save All and retain unsaved drafts')
    original = wx.Button(dialog, label='Original and &diff')
    for control in (open_button, save_button, save_as, save_all_button, cancel_all_button, undo, redo, original):
        row.Add(control, 0, wx.ALL, 4)
    layout.Add(row, 0, wx.EXPAND)
    row = wx.BoxSizer(wx.HORIZONTAL)
    playback_button = wx.Button(dialog, label='Playback controls...')
    playback_button.SetName('Playback volume, skips, speed and pitch')
    seek = wx.Button(dialog, label='Seek to &time')
    cleanup = wx.Button(dialog, label='Normalize s&paces')
    close = wx.Button(dialog, wx.ID_CANCEL, 'C&lose')
    for control in (playback_button, seek, cleanup, close):
        row.Add(control, 0, wx.ALL, 4)
    layout.Add(row, 0, wx.ALIGN_RIGHT)
    transport = ReaderPlaybackControls(dialog, layout, player,
        start=lambda action, on_error: start_playback(action, on_error=on_error),
        guard=lambda: playback_guard(), cancelled=cancel.is_set,
        alive=lambda: alive, report=lambda message, focus=False: report(message, focus=focus), wx=wx)
    def invalidate_output():
        nonlocal playback_generation
        playback_generation += 1

    output_controls = ReaderOutputControls(dialog, layout, player,
        submit=lambda work, done, **kwargs: run(work, done, **kwargs),
        guard=lambda: playback_guard(), alive=lambda: alive, busy=lambda: busy,
        cancelled=cancel.is_set, report=lambda message, focus=False: report(message, focus=focus),
        invalidate=invalidate_output, wx=wx)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(wx.ID_CANCEL)

    def report(message, *, focus=False):
        if alive:
            status.ChangeValue(message)
            if focus:
                status.SetFocus()

    def playback_guard():
        if (not alive or getattr(parent, '_closing', False)
                or getattr(parent, 'library', None) is not reader_library
                or getattr(reader_library, 'root', None) != reader_root):
            raise ValueError('The library folder changed. Open the reader again.')
        source_guard()

    def capabilities(event=None):
        nonlocal remembered_voice_id
        selected = voice_choice.GetSelection()
        current = voices[selected] if 0 <= selected < len(voices) else None
        rate.Enable(bool(current and 'rate' in current.capabilities) and not busy)
        volume.Enable(bool(current and 'volume' in current.capabilities) and not busy)
        if current and 'rate' not in current.capabilities:
            rate.SetValue(0)
        if current and 'volume' not in current.capabilities:
            volume.SetValue(100)
        native_request_supported = synthesis_controls.sync(current,
            enabled=source_loaded and alive and not busy)
        for button in (read, cursor, export_audio):
            button.Enable(native_request_supported and source_loaded and not busy and alive)
        for button in read_units.values():
            button.Enable(native_request_supported and source_loaded and not busy and alive)
        for control in (navigation_unit, previous_unit, current_unit, next_unit):
            control.Enable(source_loaded and not busy and alive)
        for button in (find_button, replace_button, pronunciation_button, preparation_button, spelling_button, appearance_button, focus_button):
            button.Enable(source_loaded and not busy)
        assistant_button.Enable(source_loaded and not busy
                                and getattr(parent, 'desktop_integration', None) is not None)
        output_controls.sync()
        if reader_tabs is not None:
            reader_tabs.set_busy(busy)
        progress_voice.Enable(bool(progress is not None and current and 'volume' in current.capabilities) and not busy)
        voice_preferences_button.Enable(reader_root is not None and alive and source_loaded and not busy)
        if event is not None and current:
            remembered_voice_id = current.id
            report(f'{current.name}; {current.language}; {current.engine}. '
                   f'Controls: {", ".join(current.capabilities) or "voice defaults only"}. {current.timing} '
                   + synthesis_controls.explanation(current))
        elif current and not native_request_supported and alive and not busy:
            report(synthesis_controls.explanation(current))

    def before_native_options_apply():
        nonlocal playback_generation, last_audio, last_reading_audio
        if busy or not source_loaded:
            raise ValueError('Wait for the current reader operation before applying native voice options.')
        playback_guard()
        playback_generation += 1
        player.stop()  # Failed native cleanup vetoes changing the active request.
        last_audio, last_reading_audio = None, None
        if progress is not None:
            progress.refresh_audio_state()

    def native_options_changed():
        capabilities()
        save_recovery(force=True)
        report('Native voice options applied. Read or Save WAV to synthesize with these settings. '
               'Existing saved audio is unchanged.')

    def run(work, on_done, *, on_error=None):
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
                if on_error is None:
                    report(f'Operation did not complete. {exc}', focus=True)
                else:
                    try:
                        on_error(exc)
                    except Exception as callback_error:
                        report(f'Operation did not complete. {callback_error}', focus=True)
            capabilities()
            if close_requested:
                closing(None)
        executor.submit(work).add_done_callback(lambda future: wx.CallAfter(finish, future))
        return True

    def select_progress_voice(event=None):
        selected = voice_choice.GetSelection()
        if progress is None or not 0 <= selected < len(voices):
            return
        library_root = Path(parent.library.root)
        voice_id = voices[selected].id

        def activated(provider):
            try:
                progress.set_speech_provider(provider, root=library_root)
            except Exception:
                provider.close()
                raise
            progress.settings_dialog()
            report(f'Progress voice selected for this session: {provider.label}. '
                   f'Sound mode: {progress.preferences.mode}.')

        run(lambda: activate_progress_voice(library_root, voice_id), activated)

    def start_playback(action, *, on_error=None):
        generation = playback_generation
        def admitted():
            playback_guard()
            if not cancel.is_set():
                action()
        play_when_quiet((progress,), admitted, schedule=wx.CallLater,
            alive=lambda: alive and not cancel.is_set() and generation == playback_generation,
            on_error=on_error or (lambda exc: report(f'Playback did not start. {exc}', focus=True)))

    def reload(event=None):
        def discover_configured():
            library_root = getattr(getattr(parent, 'library', None), 'root', None)
            if library_root is not None:
                from ..runtime_settings import voice_registry
                return voice_registry(Path(library_root) / 'state')
            return VoiceRegistry.discover()

        def loaded(value):
            nonlocal registry, voices, document, source_loaded, draft_store, recovered, recovery_notice, remembered_voice_id
            registry, source = value
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
                editor.Enable(True)
                source_status.ChangeValue(f'{source.title}; {len(source.positions)} source reading positions. '
                    + (' '.join(source.warnings[:3]) if source.warnings else 'Source positions authenticated.')
                    + ' This is a separate narration copy; it does not approve quotations. '
                    + recovery_notice)
            voice_choice.Set([f'{voice.name}; {voice.language}; {voice.engine}' for voice in voices])
            if voices:
                if recovered is not None:
                    remembered_voice_id = recovered.voice_id
                selected = next((i for i, voice in enumerate(voices)
                                 if voice.id == remembered_voice_id), -1 if remembered_voice_id else 0)
                voice_choice.SetSelection(selected)
                if recovered is not None:
                    editor.SetInsertionPoint(native_position(editor, min(recovered.cursor, len(editor.GetValue()))))
                report(recovery_notice + f'{len(voices)} installed voices available. Select text or press F6 to read. '
                       'Voice pitch, emotional delivery and word timing depend on the selected engine.')
            else:
                report(recovery_notice + 'No local voice is available. Install a SAPI 5 voice on Windows or an optional '
                       'local engine. ' + ' '.join(registry.notices), focus=True)
        run(lambda: (discover_configured(),
                     source_loader(cancel.is_set) if not source_loaded else None), loaded)

    def choose_voice_preferences(event=None):
        from ..runtime_settings import load_voice_preferences, review_voice_preference
        from ..language_qualification.catalog import canonical_language_tag
        from .voice_preferences import show_voice_preferences
        try:
            playback_guard()
            if reader_root is None or busy or not source_loaded:
                raise ValueError('Open an idle reader in this library before editing voice preferences.')
            current_registry, current_voices = registry, tuple(voices)
            index = voice_choice.GetSelection()
            current = voices[index] if 0 <= index < len(voices) else None
            try:
                language = canonical_language_tag(current.language, adapter_underscores=True) if current else ''
            except ValueError:
                language = ''
            selected = {'voice_id': current.id if current else remembered_voice_id,
                        'language': language, 'rate': rate.GetValue(), 'volume': volume.GetValue()}

            def guard():
                playback_guard()
                if registry is not current_registry or tuple(voices) != current_voices:
                    raise ValueError('The reader voice inventory changed. Reopen preferences.')

            def apply_selection(value):
                nonlocal remembered_voice_id
                guard()
                if cancel.is_set():
                    raise InterruptedError('Voice selection was cancelled.')
                selected_index = next((i for i, item in enumerate(voices) if item.id == value['voice_id']), None)
                if selected_index is None:
                    raise ValueError('The selected voice is no longer available. Reload reader voices.')
                voice_choice.SetSelection(selected_index)
                rate.SetValue(value['rate'])
                volume.SetValue(value['volume'])
                remembered_voice_id = value['voice_id']
                capabilities()
                save_recovery(force=True)

            def review(value, **kwargs):
                return review_voice_preference(Path(reader_root) / 'state', current_registry,
                                               value, **kwargs)

            def opened(preferences):
                guard()
                if cancel.is_set():
                    raise InterruptedError('Voice preference loading was cancelled.')
                show_voice_preferences(dialog, state_root=Path(reader_root) / 'state',
                    voices=current_voices, preferences=preferences, selected=selected,
                    execute=run, guard=guard, apply_selection=apply_selection,
                    review_selection=review, cancelled=cancel.is_set)

            run(lambda: load_voice_preferences(Path(reader_root) / 'state'), opened)
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
        voice_id = voices[selected].id if 0 <= selected < len(voices) else remembered_voice_id
        pitch, punctuation = synthesis_controls.snapshot()
        synthesis = (voice_id, rate.GetValue(), volume.GetValue(), pitch, punctuation)
        position = (player.position_seconds if last_audio is not None else
                    reader_tabs.saved_audio_position(document, synthesis=synthesis) if reader_tabs is not None else 0)
        cursor_offset = reading_offset(editor, editor.GetInsertionPoint())
        signature = (document._signature(), voice_id, rate.GetValue(), volume.GetValue(), cursor_offset,
                     round(position, 1), pitch, punctuation)
        if not force and signature == last_recovery_signature:
            return True
        resume = ReaderResume(document, voice_id, rate.GetValue(), volume.GetValue(),
                              cursor_offset, position, pitch=pitch, punctuation=punctuation)
        if reader_tabs is not None:
            reader_tabs.save_resume(resume, save_path=save_path)
        else:
            draft_store.save(resume)
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
            return editor.GetRange(editor.GetInsertionPoint(), editor.GetLastPosition())
        selected = editor.GetStringSelection()
        return selected or editor.GetValue()

    def speak(event=None, *, from_cursor=False, destination=None):
        nonlocal last_audio, last_reading_audio, playback_generation
        try:
            selected = voice_choice.GetSelection()
            if registry is None or not 0 <= selected < len(voices):
                raise ValueError('Choose an installed local voice first.')
            playback_guard()
            content = selected_text(from_cursor)
            render_start, render_end = reading_selection(editor)
            if from_cursor:
                render_start, render_end = reading_offset(editor, editor.GetInsertionPoint()), len(document.text)
            elif render_start == render_end:
                render_start, render_end = 0, len(document.text)
            if document.text[render_start:render_end] != content:
                raise ValueError('The native reading selection changed. Select it again.')
            render_document, render_signature = document, document._signature()
            voice = voices[selected]
            settings = synthesis_controls.settings(rate.GetValue(), volume.GetValue(), voice)
            playback_generation += 1
            player.stop()
            if progress is not None:
                progress.refresh_audio_state()
            output = destination or (Path(audio_files.name) / (uuid4().hex + '.wav'))

            def rendered(result):
                nonlocal last_audio, last_reading_audio
                from ..speech.focus import ReadingAudio
                last_reading_audio = None
                try:
                    playback_guard()
                    sync_document()
                    if document is not render_document:
                        raise ValueError('The reading tab changed during synthesis.')
                    current_index = voice_choice.GetSelection()
                    current_voice = voices[current_index] if 0 <= current_index < len(voices) else None
                    if (current_voice is None or current_voice.id != voice.id
                            or synthesis_controls.settings(rate.GetValue(), volume.GetValue(), current_voice) != settings):
                        raise ValueError('The native voice request changed during synthesis. Read again with the current settings.')
                    binding = ReadingAudio.from_render(render_document, render_signature,
                        render_start, render_end, content, result)
                except ValueError:
                    if destination is not None:
                        report(f'WAV saved: {result.path}. The current reading copy or native voice request changed; '
                               'the saved audio was not attached to it.', focus=True)
                        return
                    raise
                last_audio, last_reading_audio = result.path, binding
                if destination is None:
                    if cancel.is_set():
                        return
                    def playing():
                        player.play(result.path)
                        report(f'Playing {result.duration_seconds:.1f} seconds. F7 pauses or resumes; F8 stops. '
                               'Reading focus can estimate position; this voice has no exact word timings.')
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
            playback_guard()
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
            # A modal chooser can outlive the source or its owning library.
            playback_guard()
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
        nonlocal document, save_path, draft_store, recovery_notice, last_audio, last_reading_audio, playback_generation
        if busy:
            report('Stop the current render before opening another document.', focus=True)
            return
        try:
            sync_document()
            if document.dirty and not save_project():
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
            last_audio, last_reading_audio = None, None
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
        nonlocal playback_generation, last_audio, last_reading_audio
        if not alive or dialog_destroyed:
            return False
        try:
            if busy or not source_loaded:
                raise ValueError('Wait for the current reader operation before changing reading history.')
            playback_guard()
            sync_document()
            available = document.can_redo if forward else document.can_undo
            if available:
                playback_generation += 1
                player.stop()
                last_audio, last_reading_audio = None, None
                if progress is not None:
                    progress.refresh_audio_state()
            changed = document.redo() if forward else document.undo()
            editor.ChangeValue(document.text)
            sync_document()
            report(f'Reading revision {document.revision}.' if changed else 'No further reading edit.')
            editor.SetFocus()
            return changed
        except Exception as exc:
            report(str(exc), focus=True)
            return False

    def history_key(event):
        # TextCtrl.ChangeValue deliberately has no native Undo entry. Route the
        # fixed editing chords to the same reading history as the two buttons.
        # The active editor can change when a manual reading tab is selected.
        if (not alive or dialog_destroyed or wx.Window.FindFocus() is not editor
                or event.GetModifiers() != wx.MOD_CONTROL
                or event.GetKeyCode() not in (ord('Z'), ord('Y'))):
            event.Skip()
            return
        history(event, forward=event.GetKeyCode() == ord('Y'))

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

    def capture_edit():
        playback_guard()
        if not source_loaded:
            raise ValueError('Wait for the source reading copy to finish loading.')
        sync_document()
        return ReadingSnapshot.capture(document)

    def verify_edit(snapshot):
        capture_edit()
        snapshot.check(document)

    def apply_edit(snapshot, start, end, replacement, *, label='Reading-copy edit'):
        nonlocal playback_generation, last_audio, last_reading_audio
        if busy:
            raise ValueError('Wait for the current reader operation before applying an edit.')
        verify_edit(snapshot)
        playback_generation += 1
        player.stop()
        last_audio, last_reading_audio = None, None
        if progress is not None:
            progress.refresh_audio_state()
        changed = document.replace(start, end, replacement, label=label)
        editor.ChangeValue(document.text)
        sync_document()
        select_reading_range(editor, start, start + len(replacement))
        return changed

    def editing_voice():
        selected = voice_choice.GetSelection()
        if 0 <= selected < len(voices):
            voice = voices[selected]
            return voice.language, voice.id, voice.name
        return '', '', 'No voice selected'

    edit_host = ReaderEditHost(capture=capture_edit,
        selection=lambda: reading_selection(editor),
        select=lambda first, last: select_reading_range(editor, first, last),
        verify=verify_edit, apply=apply_edit, submit=run, guard=playback_guard,
        report=report, alive=lambda: alive, busy=lambda: busy,
        cancelled=cancel.is_set, voice=editing_voice)
    navigation = ReaderNavigation(edit_host, speak=speak, focus=lambda: editor.SetFocus())
    dialog.reader_navigation = navigation
    dialog.reader_navigation_unit = navigation_unit
    dialog.reader_unit_buttons = read_units

    def navigate_unit(event=None, *, unit=None, direction=0, read_aloud=False):
        if not alive or busy or not source_loaded:
            report('Wait for the current reader operation before navigating text.', focus=True)
            return False
        if unit is None:
            selected = navigation_unit.GetSelection()
            if not 0 <= selected < len(READING_UNITS):
                report('Choose word, logical line, sentence or paragraph navigation.', focus=True)
                return False
            unit = READING_UNITS[selected]
        return navigation.navigate(unit, direction=direction, read_aloud=read_aloud)

    appearance = ReaderPresentationControls(dialog, editor, edit_host, library_root=reader_root)
    dialog.reader_presentation = appearance
    dialog.appearance_button = appearance_button
    dialog.reader_edit_host = edit_host
    dialog.reader_editor = editor

    playback_session = ReaderPlaybackSession(player,
        submit=run, guard=playback_guard, capture=capture_edit, verify=verify_edit,
        alive=lambda: alive, busy=lambda: busy, cancelled=cancel.is_set,
        clear_cancel=cancel.clear, start=start_playback, stop=stop_audio, pause=pause_audio,
        report=report, refresh=transport.refresh_controls,
        state_root=Path(reader_root) / 'state' if reader_root is not None else None,
        broker=getattr(parent, 'resource_broker', None))
    dialog.reader_playback_session = playback_session
    dialog.reader_player = player
    dialog.playback_button = playback_button

    def show_playback(event=None):
        try:
            playback_session.check()
            show_reader_playback(dialog, player,
                execute=playback_session.execute, stop=playback_session.stop,
                pause=playback_session.pause, subscribe=transport.subscribe,
                prepare_listening=playback_session.prepare_listening,
                guard=playback_guard, active=lambda: alive)
        except Exception as exc:
            report(f'Playback controls did not complete. {exc}', focus=True)
        finally:
            if alive and not dialog_destroyed:
                playback_button.SetFocus()

    def find_replace(event=None):
        try:
            capture_edit()
            show_find_replace(dialog, edit_host)
            if alive:
                editor.SetFocus()
        except Exception as exc:
            report(str(exc), focus=True)

    def pronunciation_dictionary(event=None):
        nonlocal reading_rules
        try:
            capture_edit()
            reading_rules = show_pronunciations(dialog, edit_host, reading_rules)
            if alive:
                editor.SetFocus()
        except Exception as exc:
            report(str(exc), focus=True)

    def prepare_reading(event=None):
        try:
            if busy:
                raise ValueError('Wait for the current reader operation before preparing text.')
            capture_edit()
            show_reader_preparation(dialog, host=edit_host)
            if alive and not dialog_destroyed:
                editor.SetFocus()
        except Exception as exc:
            report(str(exc), focus=True)

    def assistant_player_guard():
        playback_guard()
        if busy or not source_loaded or dialog_destroyed:
            raise ValueError('Finish the current reader operation before reviewing playback commands.')

    def assistant_player_snapshot():
        from hashlib import sha256
        from ..assistant.models import canonical
        assistant_player_guard()
        snapshot = capture_edit()
        # Bind this exact live document, visible edit and already-opened audio.
        # Pause/Stop do not change this source identity; another loaded recording does.
        value = {'document': id(document), 'source': snapshot.signature,
                 'visible_text_sha256': sha256(snapshot.text.encode('utf-8')).hexdigest(),
                 'audio': str(player.path) if player.path is not None else None,
                 'audio_identity': getattr(player, '_identity', None)}
        return sha256(canonical(value).encode('utf-8')).hexdigest()

    def choose_player_assistant(event=None):
        from .assistant_dialog import show_assistant
        try:
            assistant_player_guard()
            show_assistant(parent, reader_owner=dialog)
            if alive and not dialog_destroyed:
                editor.SetFocus()
        except Exception as exc:
            report(str(exc), focus=True)

    def check_spelling(event=None):
        try:
            from .reader_spelling import show_reader_spelling
            capture_edit()
            show_reader_spelling(dialog, host=edit_host)
            if alive:
                editor.SetFocus()
        except Exception as exc:
            report(str(exc), focus=True)

    def choose_reading_focus(event=None):
        from ..speech.focus import FocusPlan
        from .reading_focus import show_reading_focus
        try:
            if busy:
                raise ValueError('Wait for the current reader operation before opening reading focus.')
            snapshot = capture_edit()
            focus_document = document
            position = reading_offset(editor, editor.GetInsertionPoint())

            def prepared(plan):
                if cancel.is_set():
                    return
                verify_edit(snapshot)
                plan.check(document)

                def guard():
                    if busy:
                        raise ValueError('The current reader operation changed. Reopen reading focus.')
                    playback_guard()
                    plan.check(document)

                def goto(offset):
                    guard()
                    editor.SetInsertionPoint(native_position(editor, offset))
                    save_recovery(force=True)

                matching = last_reading_audio
                if matching is not None:
                    try:
                        matching.check(plan, getattr(player, 'path', None))
                    except ValueError:
                        matching = None
                show_reading_focus(dialog, plan=plan, cursor=position, guard=guard, authorize=guard,
                    player=player, audio=matching, pause_audio=pause_audio, stop_audio=stop_audio,
                    goto=goto, keyboard_parent=parent)
                if alive:
                    editor.SetFocus()
            run(lambda: FocusPlan(focus_document, cancelled=cancel.is_set), prepared)
        except Exception as exc:
            report(str(exc), focus=True)

    def capture_reading_tab():
        playback_guard()
        sync_document()
        selected = voice_choice.GetSelection()
        voice_id = voices[selected].id if 0 <= selected < len(voices) else remembered_voice_id
        pitch, punctuation = synthesis_controls.snapshot()
        synthesis = (voice_id, rate.GetValue(), volume.GetValue(), pitch, punctuation)
        resume = ReaderResume(document, voice_id, rate.GetValue(), volume.GetValue(),
            reading_offset(editor, editor.GetInsertionPoint()),
            player.position_seconds if last_audio is not None else
            reader_tabs.saved_audio_position(document, synthesis=synthesis) if reader_tabs is not None else 0,
            pitch=pitch, punctuation=punctuation)
        if reader_tabs is not None:
            reader_tabs.current.reading_rules = reading_rules
        return resume, save_path

    def stop_for_reading_tab():
        nonlocal playback_generation, last_audio, last_reading_audio
        playback_generation += 1
        cancel.set()
        player.stop()  # An unconfirmed close must veto a tab change.
        last_audio, last_reading_audio = None, None
        if progress is not None:
            progress.refresh_audio_state()

    def activate_reading_tab(state):
        nonlocal document, editor, recovered, draft_store, save_path, last_recovery_signature
        nonlocal remembered_voice_id, reading_rules
        document, editor, recovered = state.resume.document, state.editor, state.resume
        draft_store, save_path, last_recovery_signature = state.store, state.save_path, None
        remembered_voice_id, reading_rules = recovered.voice_id, state.reading_rules
        rate.SetValue(recovered.rate)
        volume.SetValue(recovered.volume)
        synthesis_controls.restore(recovered.pitch, recovered.punctuation)
        voice_choice.SetSelection(next((i for i, voice in enumerate(voices)
            if voice.id == remembered_voice_id), -1))
        dialog.reader_editor = editor
        appearance.bind_editor(editor)
        editor.SetInsertionPoint(native_position(editor, recovered.cursor))
        sync_document()
        capabilities()
        editor.SetFocus()

    def confirm_current_close():
        sync_document()
        if document.dirty:
            answer = wx.MessageBox('Save the reading copy and its undo history before closing?',
                'Unsaved reading copy', wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION, dialog)
            # Saving requires current authority inside save_project. Declining
            # a write must still allow a revoked source window to close.
            if answer == wx.CANCEL or (answer == wx.YES and not save_project()):
                return False
        return True

    def load_reading_tabs():
        from ..speech.tabs import prepare_tabs
        from .reader_tabs import NativeReaderTabs
        initial = ReaderResume(document)

        def failed(error):
            nonlocal source_loaded, draft_store, recovery_notice
            source_loaded, draft_store = True, None
            editor.Enable(True)
            recovery_notice = ('Reading tabs could not be recovered. All existing drafts were retained; '
                               'use Save As or reopen the reader. ' + str(error) + ' ')
            report(recovery_notice, focus=True)
            if not close_requested and not cancel.is_set():
                reload()

        def prepared(value):
            nonlocal reader_tabs, source_loaded, recovery_notice
            if cancel.is_set():
                failed(InterruptedError('Reading-tab loading cancelled.'))
                return
            playback_guard()
            value.store.guard()
            old_editor = editor
            tabs = NativeReaderTabs(dialog, reader_root, initial, prepared=value, wx_module=wx)
            try:
                tabs.bind(capture=capture_reading_tab, activate=activate_reading_tab,
                    guard=playback_guard, stop=stop_for_reading_tab, report=report,
                    busy=lambda: busy, confirm_close=confirm_current_close)
                row = wx.BoxSizer(wx.HORIZONTAL)
                for button in tabs.buttons.values():
                    row.Add(button, 0, wx.ALL, 4)
                if not layout.Replace(old_editor, tabs.notebook):
                    raise RuntimeError('The native reading workspace could not attach its tabs.')
                # Keep the old native object hidden until its owner is destroyed;
                # queued callbacks cannot target a deleted replacement control.
                old_editor.Hide()
                old_editor.SetName('Inactive initial reading-copy placeholder')
                layout.Insert(7, row, 0, wx.EXPAND)
                reader_tabs, source_loaded = tabs, True
                dialog.reader_tabs = tabs
                recovery_notice = ('Recovered ' + str(len(tabs.states)) +
                    ' independent reading tabs. ' if value.had_recovery else '')
                activate_reading_tab(tabs.current)
                dialog.Layout()
            except BaseException:
                tabs.close()
                raise
            reload()

        run(lambda: prepare_tabs(reader_root, initial, explicit_text=explicit_reader_text,
                                 cancelled=cancel.is_set), prepared, on_error=failed)

    def owner_destroyed(event):
        nonlocal alive, dialog_destroyed
        if event.GetId() == dialog_id:
            # wx can invalidate its original Python wrapper before this
            # event. Revoke callbacks without calling the dying owner.
            alive, dialog_destroyed = False, True
            cancel.set()
        event.Skip()

    def closing(event):
        nonlocal close_requested
        try:
            if busy:
                close_requested = True
                cancel.set()
                report('Stopping the current reader operation before closing; unsaved copies are retained.')
                return
            close_requested = False
            if reader_tabs is not None:
                try:
                    reader_tabs.check()
                except Exception:
                    # A revoked/missing workspace must forbid every write,
                    # while leaving an explicit way to close this window.
                    # Do not switch tabs or publish an index in this branch.
                    sync_document()
                    if any(state.resume.document.dirty for state in reader_tabs.states):
                        answer = wx.MessageBox(
                            'This reading workspace is no longer available for saving. '
                            'Close all reading tabs without saving their current changes? '
                            'Existing recovery files will remain unchanged.',
                            'Close unsaved reading tabs', wx.YES_NO | wx.ICON_QUESTION, dialog)
                        if answer != wx.YES:
                            return
                else:
                    if not reader_tabs.confirm_all_close():
                        return
            elif not confirm_current_close():
                return
            dialog.EndModal(wx.ID_CANCEL)
        except Exception as exc:
            report(f'The reading copy is still open. {exc}', focus=True)

    def save_all_drafts(event=None):
        from .save_actions import save_all_open
        return save_all_open(parent, event, report=lambda message: report(message, focus=True))

    def cancel_all_drafts(event=None):
        from .save_actions import cancel_save_all
        return cancel_save_all(parent, event, report=lambda message: report(message, focus=True))

    for control, handler in ((reload_button, reload), (progress_voice, select_progress_voice), (read, speak),
        (cursor, lambda event: speak(from_cursor=True)), (pause, pause_audio), (stop, stop_audio),
        (export_audio, save_audio), (save_button, save_project), (save_all_button, save_all_drafts), (cancel_all_button, cancel_all_drafts),
        (save_as, lambda event: save_project(choose=True)), (open_button, open_project),
        (undo, history), (redo, lambda event: history(forward=True)),
        (original, compare), (seek, seek_audio), (playback_button, show_playback), (cleanup, clean),
        (find_button, find_replace), (replace_button, find_replace),
        (pronunciation_button, pronunciation_dictionary), (preparation_button, prepare_reading),
        (assistant_button, choose_player_assistant),
        (spelling_button, check_spelling),
        (appearance_button, appearance.show), (focus_button, choose_reading_focus), (close, closing)):
        control.Bind(wx.EVT_BUTTON, handler)
    previous_unit.Bind(wx.EVT_BUTTON, lambda event: navigate_unit(direction=-1))
    current_unit.Bind(wx.EVT_BUTTON, navigate_unit)
    next_unit.Bind(wx.EVT_BUTTON, lambda event: navigate_unit(direction=1))
    for unit, button in read_units.items():
        button.Bind(wx.EVT_BUTTON, lambda event, selected=unit:
                    navigate_unit(unit=selected, read_aloud=True))
    voice_choice.Bind(wx.EVT_CHOICE, capabilities)
    dialog.Bind(wx.EVT_CHAR_HOOK, history_key)
    voice_preferences_button.Bind(wx.EVT_BUTTON, choose_voice_preferences)
    dialog.Bind(wx.EVT_CLOSE, closing)
    dialog.Bind(wx.EVT_WINDOW_DESTROY, owner_destroyed)
    keyboard = VoiceReaderKeyboardAdapter(dialog, parent, {
        'voice_reader.read': (read, speak),
        'voice_reader.pause': (pause, pause_audio),
        'voice_reader.stop': (stop, stop_audio),
        'voice_reader.save': (save_button, save_project),
        'voice_reader.save_as': (save_as, lambda event: save_project(choose=True)),
        'voice_reader.find': (find_button, find_replace),
        'voice_reader.replace': (replace_button, find_replace),
        'voice_reader.pronunciation': (pronunciation_button, pronunciation_dictionary),
        'voice_reader.prepare': (preparation_button, prepare_reading),
        'voice_reader.settings': (keyboard_button, lambda event: keyboard.settings(event)),
        'voice_reader.playback': (playback_button, show_playback),
        'voice_reader.read_word': (read_units['word'], lambda event: navigate_unit(unit='word', read_aloud=True)),
        'voice_reader.read_line': (read_units['line'], lambda event: navigate_unit(unit='line', read_aloud=True)),
        'voice_reader.read_sentence': (read_units['sentence'], lambda event: navigate_unit(unit='sentence', read_aloud=True)),
        'voice_reader.read_paragraph': (read_units['paragraph'], lambda event: navigate_unit(unit='paragraph', read_aloud=True)),
        'voice_reader.previous_unit': (previous_unit, lambda event: navigate_unit(direction=-1)),
        'voice_reader.current_unit': (current_unit, navigate_unit),
        'voice_reader.next_unit': (next_unit, lambda event: navigate_unit(direction=1)),
    }, guard=playback_guard, alive=lambda: alive, report=report, wx=wx)
    keyboard_button.Bind(wx.EVT_BUTTON, keyboard.settings)
    autosave_timer = wx.Timer(dialog)
    dialog.Bind(wx.EVT_TIMER, autosave, autosave_timer)
    from .save_all import SaveResult, coordinator_for
    coordinator = coordinator_for(parent)

    def unsaved():
        if not alive:
            return False
        if not source_loaded:
            return False
        sync_document()
        # Deleting all text is still an edit, including in an inactive tab.
        return document.dirty or (reader_tabs is not None and any(
            state.resume.document.dirty for state in reader_tabs.states))

    def save_registered(completed):
        try:
            if not alive:
                raise ValueError('The reading window closed before saving.')
            playback_guard()
            if busy:
                raise ValueError('Speech is running; stop it before saving the reading copy.')
            saved = bool(reader_tabs.save_all(save_project) if reader_tabs is not None else save_project())
        except Exception as exc:
            completed(SaveResult(False, str(exc)))
            return
        completed(SaveResult(saved, '' if saved else 'The reading copy was not saved.'))

    unregister_save = (coordinator.register('reading.' + str(id(dialog)), 'Voice reading copy',
                        dirty=unsaved, save=save_registered, focus=lambda: editor.SetFocus())
                       if coordinator is not None else lambda: None)
    detach_reader = lambda: None
    try:
        integration = getattr(parent, 'desktop_integration', None)
        if integration is not None:
            detach_reader = integration.attach_reader(dialog, pause, stop,
                lambda: alive and not dialog_destroyed,
                player=player, source_snapshot=assistant_player_snapshot,
                player_guard=assistant_player_guard, pause_action=pause_audio, stop_action=stop_audio)
        capabilities()
        if tabs_requested:
            load_reading_tabs()
        else:
            reload()
        autosave_timer.Start(5000)
        editor.SetFocus()
        dialog.ShowModal()
    finally:
        alive = False
        cancel.set()
        if reader_tabs is not None:
            reader_tabs.close()
        silence_confirmed = False
        worker_finished = False
        listening_finished = False
        notices = []
        try:
            try:
                keyboard.close()
            except Exception as exc:
                notices.append(f'Reading keyboard shortcuts could not be detached: {exc}')
            try:
                output_controls.close()
            except Exception as exc:
                notices.append(f'Reader output controls could not be detached: {exc}')
            try:
                unregister_save()
            except Exception as exc:
                notices.append(f'Save coordinator could not be detached: {exc}')
            try:
                playback_session.close()
            except (OSError, RuntimeError) as exc:
                notices.append(f'Listening-copy cleanup is not confirmed: {exc}')
            try:
                transport.close(stop=False)
            except (OSError, RuntimeError) as exc:
                notices.append(f'Repeat and sleep timer could not be stopped: {exc}')
            try:
                autosave_timer.Stop()
            except (OSError, RuntimeError) as exc:
                notices.append(f'Reading-draft timer could not be stopped: {exc}')
            try:
                player.close()
                silence_confirmed = True
            except (OSError, RuntimeError) as exc:
                notices.append(f'Playback device reported a close error: {exc}')
            if progress is not None:
                try:
                    progress.unregister_player(player, silence_confirmed=silence_confirmed)
                except Exception as exc:
                    notices.append(f'Playback progress feedback could not be refreshed: {exc}')
            # Never remove a worker's output while it may still be writing it.
            try:
                executor.shutdown(wait=True, cancel_futures=True)
                worker_finished = True
            except (OSError, RuntimeError) as exc:
                notices.append(f'The reading worker has not confirmed shutdown: {exc}')
            # A worker can finish after the first close or after a completion
            # was queued. Drain its exact unadopted values after the worker joins.
            try:
                playback_session.close(worker_drained=worker_finished)
                listening_finished = (playback_session.operation is None
                                      and not playback_session._values)
            except (OSError, RuntimeError) as exc:
                notices.append(f'Listening-copy cleanup remains pending: {exc}')
            if not listening_finished:
                retained = getattr(parent, '_reader_listening_retained', None)
                if not isinstance(retained, list):
                    retained = []
                    parent._reader_listening_retained = retained
                retained.append(playback_session)
            removed = False
            if silence_confirmed and worker_finished:
                try:
                    audio_files.cleanup()
                    removed = True
                except (OSError, RuntimeError) as exc:
                    notices.append(f'Temporary reading audio could not be removed: {exc}')
            if not removed:
                # A failed MCI close can leave its WAV open. Keep its private
                # directory alive with the application instead of deleting an
                # uncertain transport's input or losing its cleanup handle.
                retained = getattr(parent, '_reader_playback_retained', None)
                if not isinstance(retained, list):
                    retained = []
                    parent._reader_playback_retained = retained
                retained.append((player, audio_files))
                notices.append('Temporary playback audio is retained until the device and worker are confirmed closed.')
            if notices:
                try:
                    parent.report(' '.join(notices))
                except Exception:
                    # A detached parent reporter must not keep the modal open.
                    pass
        finally:
            # Destroy may defer its native event beyond this modal call. Release
            # only our desktop command token after native/worker cleanup has
            # either completed or retained its exact pending owners above.
            try:
                detach_reader()
            finally:
                if not dialog_destroyed:
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
