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


def show_speech_reader(parent, *, text='', title='Reading copy', source_loader=None,
                       source_guard=lambda: None, recovery_slot='manual'):
    import wx

    dialog = wx.Dialog(parent, title='Voice reader and reading copy', size=(900, 800),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-speech')
    cancel = threading.Event()
    audio_files = TemporaryDirectory(prefix='research-reader-')
    player = WavePlayer()
    progress = getattr(parent, 'operation_progress', None)
    if progress is not None:
        progress.register_player(player)
    document = ReadingDocument(text, title=title)
    registry = None
    voices = ()
    alive = True
    busy = False
    playback_generation = 0
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
    layout.Add(controls, 0, wx.EXPAND)
    editor = labelled('&Reading copy (editable; Ctrl+S saves)', wx.TextCtrl,
                      value=text, style=wx.TE_MULTILINE | wx.TE_DONTWRAP, proportion=1)
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
    cleanup = wx.Button(dialog, label='Normalize s&paces')
    close = wx.Button(dialog, wx.ID_CANCEL, 'C&lose')
    for control in (seek, cleanup, close):
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
        if event is not None and current:
            report(f'{current.name}; {current.language}; {current.engine}. '
                   f'Controls: {", ".join(current.capabilities) or "voice defaults only"}. {current.timing}')

    def run(work, on_done):
        nonlocal busy
        if busy:
            report('A speech operation is running. Stop it before starting another.', focus=True)
            return
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
                report(f'Operation did not complete. {exc}', focus=True)
            capabilities()
        executor.submit(work).add_done_callback(lambda future: wx.CallAfter(finish, future))

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

    def start_playback(action):
        generation = playback_generation
        play_when_quiet((progress,), action, schedule=wx.CallLater,
            alive=lambda: alive and generation == playback_generation,
            on_error=lambda exc: report(f'Playback did not start. {exc}', focus=True))

    def reload(event=None):
        def discover_configured():
            library_root = getattr(getattr(parent, 'library', None), 'root', None)
            if library_root is not None:
                from ..runtime_settings import voice_registry
                return voice_registry(Path(library_root) / 'state')
            return VoiceRegistry.discover()

        def loaded(value):
            nonlocal registry, voices, document, source_loaded, draft_store, recovered, recovery_notice
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
                    editor.SetInsertionPoint(min(recovered.cursor, len(editor.GetValue())))
                report(recovery_notice + f'{len(voices)} installed voices available. Select text or press F6 to read. '
                       'Voice pitch, emotional delivery and word timing depend on the selected engine.')
            else:
                report(recovery_notice + 'No local voice is available. Install a SAPI 5 voice on Windows or an optional '
                       'local engine. ' + ' '.join(registry.notices), focus=True)
        run(lambda: (discover_configured(),
                     source_loader(cancel.is_set) if not source_loaded else None), loaded)

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
        cursor_offset = min(editor.GetInsertionPoint(), len(document.text))
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
            return editor.GetValue()[editor.GetInsertionPoint():]
        selected = editor.GetStringSelection()
        return selected or editor.GetValue()

    def speak(event=None, *, from_cursor=False, destination=None):
        nonlocal last_audio, playback_generation
        try:
            if registry is None or not voices:
                raise ValueError('Choose an installed local voice first.')
            source_guard()
            content = selected_text(from_cursor)
            voice = voices[voice_choice.GetSelection()]
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
        nonlocal document, save_path, draft_store, recovery_notice
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
        (original, compare), (seek, seek_audio), (cleanup, clean), (close, closing)):
        control.Bind(wx.EVT_BUTTON, handler)
    voice_choice.Bind(wx.EVT_CHOICE, capabilities)
    dialog.Bind(wx.EVT_CLOSE, closing)
    accelerators = []
    for flags, key, handler in ((wx.ACCEL_CTRL, ord('S'), save_project),
        (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('S'), lambda event: save_project(choose=True)),
        (0, wx.WXK_F6, speak), (0, wx.WXK_F7, pause_audio), (0, wx.WXK_F8, stop_audio)):
        identifier = wx.NewIdRef()
        accelerators.append((flags, key, int(identifier)))
        dialog.Bind(wx.EVT_MENU, handler, id=int(identifier))
    dialog.SetAcceleratorTable(wx.AcceleratorTable(accelerators))
    autosave_timer = wx.Timer(dialog)
    dialog.Bind(wx.EVT_TIMER, autosave, autosave_timer)
    try:
        capabilities()
        reload()
        autosave_timer.Start(5000)
        editor.SetFocus()
        dialog.ShowModal()
    finally:
        alive = False
        cancel.set()
        silence_confirmed = False
        worker_finished = False
        notices = []
        try:
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
