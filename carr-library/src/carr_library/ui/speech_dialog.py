"""Native local voice reader with keyboard controls and reversible reading copy."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from uuid import uuid4

from ..speech import ReadingDocument, SpeechSettings, VoiceRegistry
from ..speech.player import WavePlayer
from ..speech.progress_speech import activate_progress_voice, play_when_quiet
from ..speech.session import ReaderDraftStore, ReaderResume, open_source_draft


def show_speech_reader(parent, *, text='', title='Reading copy', source_loader=None,
                       source_guard=lambda: None, recovery_slot='manual'):
    import wx

    class ReaderWindow(wx.Dialog):
        def Destroy(self):
            detach = getattr(self, '_reader_detach', None)
            if detach is not None:
                detach()
            if getattr(self, '_reader_destroy_requested', False) or not self:
                return False
            self._reader_destroy_requested = True
            return super().Destroy()

    dialog = ReaderWindow(parent, title='Voice reader and reading copy', size=(900, 800),
                          style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    native_id = dialog.GetId()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-speech')
    cancel = threading.Event()
    audio_files = TemporaryDirectory(prefix='research-reader-')
    player = WavePlayer()
    progress = getattr(parent, 'operation_progress', None)
    if progress is not None:
        progress.register_player(player)
    document = ReadingDocument(text, title=title)
    registry = None
    owned_registries = set()
    voices = ()
    alive = True
    busy = False
    playback_generation = 0

    def detach_reader():
        nonlocal alive, playback_generation
        if not alive:
            return
        alive = False
        playback_generation += 1
        cancel.set()

    def reader_destroyed(event):
        if event.GetId() == native_id:
            dialog._reader_destroy_requested = True
            detach_reader()
        event.Skip()

    dialog._reader_detach = detach_reader
    dialog.Bind(wx.EVT_WINDOW_DESTROY, reader_destroyed)
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
            if recovered is not None and recovered.document.format_notice and source_loader is None:
                if document is recovered.document:
                    recovery_notice += document.format_notice
                else:
                    recovery_notice += ('A pre-release reading draft was preserved separately. '
                                        'Use Save As for this new reading copy. ')
                # Explicitly supplied text must not autosave over a retained
                # earlier-format draft that was not selected for this reader.
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
    recover_button = wx.Button(dialog, label='Saved dra&fts')
    recover_button.SetName('Recover saved reading draft')
    save_button = wx.Button(dialog, label='&Save reading copy')
    save_as = wx.Button(dialog, label='Save &as')
    undo = wx.Button(dialog, label='&Undo reading edit')
    redo = wx.Button(dialog, label='R&edo reading edit')
    original = wx.Button(dialog, label='Original and &diff')
    for control in (open_button, recover_button, save_button, save_as, undo, redo, original):
        row.Add(control, 0, wx.ALL, 4)
    layout.Add(row, 0, wx.EXPAND)
    row = wx.BoxSizer(wx.HORIZONTAL)
    seek = wx.Button(dialog, label='Seek to &time')
    cleanup = wx.Button(dialog, label='Normalize s&paces')
    pronunciation = wx.Button(dialog, label='Pronunciatio&n rules')
    pronunciation.SetName('Pronunciation rules')
    close = wx.Button(dialog, wx.ID_CANCEL, 'C&lose')
    for control in (seek, cleanup, pronunciation, close):
        row.Add(control, 0, wx.ALL, 4)
    layout.Add(row, 0, wx.ALIGN_RIGHT)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(wx.ID_CANCEL)

    def report(message, *, focus=False):
        if alive:
            status.ChangeValue(message)
            if focus:
                status.SetFocus()

    def with_recovery_warning(message):
        warning = getattr(draft_store, 'discovery_warning', '')
        return message + (f' {warning}' if warning else '')

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
        pronunciation.Enable(source_loaded and not busy)
        if event is not None and current:
            report(f'{current.name}; {current.language}; {current.engine}. '
                   f'Controls: {", ".join(current.capabilities) or "voice defaults only"}. {current.timing}')

    def run(work, on_done, *, on_failed=None):
        nonlocal busy
        if not alive or busy:
            report('A speech operation is running. Stop it before starting another.', focus=True)
            return False
        busy = True
        cancel.clear()
        capabilities()
        reload_button.Disable()

        def release_unapplied(future):
            if future.cancelled() or future.exception() is not None:
                return
            result = future.result()
            release = getattr(result, 'close', None)
            if callable(release):
                try:
                    if release() is not False:
                        owned_registries.discard(result)
                except (Exception, KeyboardInterrupt):
                    # The normal reader cleanup retains failed owners for retry.
                    pass

        def finish(future):
            nonlocal busy
            if not alive:
                release_unapplied(future)
                return
            busy = False
            reload_button.Enable()
            try:
                result = future.result()
                on_done(result)
                if callable(getattr(result, 'close', None)):
                    owned_registries.discard(result)
            except Exception as exc:
                if not alive:
                    return
                if on_failed is not None:
                    on_failed(exc)
                else:
                    report(f'Operation did not complete. {exc}', focus=True)
            if alive:
                capabilities()
        def completed(future):
            if not future.cancelled() and future.exception() is None:
                result = future.result()
                if callable(getattr(result, 'close', None)):
                    # The existing owner collection also covers a result whose
                    # queued UI handoff never runs after native destruction.
                    owned_registries.add(result)
            if not alive:
                release_unapplied(future)
                return
            wx.CallAfter(finish, future)

        try:
            executor.submit(work).add_done_callback(completed)
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
        library_root = Path(parent.library.root)
        voice_id = voices[selected].id

        def activated(provider):
            try:
                progress.set_speech_provider(provider, root=library_root)
            except Exception:
                try:
                    if provider.close() is not False:
                        owned_registries.discard(provider)
                except (Exception, KeyboardInterrupt):
                    # Preserve the handoff error and retry this owned selection
                    # during the normal reader cleanup.
                    pass
                raise
            # OperationProgress now owns this selection. A nested settings
            # modal may destroy the reader without closing the accepted owner.
            owned_registries.discard(provider)
            progress.settings_dialog()
            report(f'Progress voice selected for this session: {provider.label}. '
                   f'Sound mode: {progress.preferences.mode}.')

        run(lambda: activate_progress_voice(library_root, voice_id, cancelled=cancel.is_set), activated)

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
                found = voice_registry(Path(library_root) / 'state', cancelled=cancel.is_set)
            else:
                found = VoiceRegistry.discover(cancelled=cancel.is_set)
            owned_registries.add(found)
            return found

        def loaded(value):
            nonlocal registry, voices, document, source_loaded, draft_store, recovered
            nonlocal source_guard, source_loader, recovery_notice
            previous = registry
            registry, source = value
            if previous is not None and previous is not registry:
                def retire():
                    if previous.close() is not False:
                        owned_registries.discard(previous)
                executor.submit(retire)
            voices = registry.voices()
            if source is not None:
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
                    if not alive:
                        return
                    document = recovered.document
                else:
                    document = source.reading_document()
                if detached:
                    # The user explicitly chose their saved work, not a new
                    # attachment to the currently selected library/edition.
                    source_guard = lambda: None
                    source_loader = None
                    recovery_notice = ('Recovered the saved draft separately. Its original recovery file '
                                       'is unchanged. Use Save As to keep a portable reading project. ')
                if document.format_notice:
                    recovery_notice += document.format_notice
                    # Resolve exact source identity before disabling recovery:
                    # the imported draft remains readable without replacing it.
                    draft_store = None
                editor.ChangeValue(document.text)
                source_loaded = True
                if detached:
                    sync_document()
                else:
                    source_status.ChangeValue(f'{source.title}; {len(source.positions)} source reading positions. '
                        + (' '.join(source.warnings[:3]) if source.warnings else 'Source positions authenticated.')
                        + ' This is a separate narration copy; it does not approve quotations.')
                if recovered is not None:
                    rate.SetValue(recovered.rate)
                    volume.SetValue(recovered.volume)
            voice_choice.Set([f'{voice.name}; {voice.language}; {voice.engine}' for voice in voices])
            if voices:
                selected = next((i for i, voice in enumerate(voices)
                                 if recovered is not None and voice.id == recovered.voice_id), 0)
                voice_choice.SetSelection(selected)
                if recovered is not None:
                    editor.SetInsertionPoint(min(recovered.cursor, len(editor.GetValue())))
                report(with_recovery_warning(recovery_notice + f'{len(voices)} installed voices available. '
                       'Select text or press F6 to read. Voice pitch, emotional delivery and word timing '
                       'depend on the selected engine.'))
            else:
                report(with_recovery_warning(recovery_notice + 'No local voice is available. Install a SAPI 5 voice '
                       'on Windows or an optional local engine. ' + ' '.join(registry.notices)), focus=True)
        def load():
            source = source_loader(cancel.is_set) if not source_loaded else None
            return discover_configured(), source
        run(load, loaded)

    def sync_document():
        document.set_text(editor.GetValue())
        lineage = ('Library source identity retained; reopening does not reapprove it.' if document.source_bound
                   else 'Unbound reading copy; no library source identity was recorded.')
        source_status.ChangeValue(f'Original text preserved; reading revision {document.revision}; '
            f'{"unsaved changes" if document.dirty else "saved"}. '
            + lineage + ' The reading copy is not quotation evidence. ' + recovery_notice)

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
        if warning := getattr(draft_store, 'discovery_warning', ''):
            report(warning)
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
            playback_generation += 1
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
            nonlocal document, draft_store, recovered, save_path, last_audio
            nonlocal source_guard, source_loader, source_loaded, last_recovery_signature, playback_generation
            resumed, separate_store = result
            playback_generation += 1
            player.stop()
            if progress is not None:
                progress.refresh_audio_state()
            document, draft_store, recovered = resumed.document, separate_store, resumed
            # Explicitly opening one's saved text does not attach it to the
            # selected library source or inherit that source's authorization.
            source_guard, source_loader, source_loaded = lambda: None, None, True
            save_path, last_audio, last_recovery_signature = None, None, None
            rate.SetValue(resumed.rate)
            volume.SetValue(resumed.volume)
            if voices:
                voice_choice.SetSelection(next((i for i, voice in enumerate(voices)
                                                if voice.id == resumed.voice_id), 0))
            editor.ChangeValue(document.text)
            editor.SetInsertionPoint(min(resumed.cursor, len(document.text)))
            sync_document()
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
            run(lambda: recovery_store.open_saved(summary, cancelled=cancel.is_set), opened)

        run(lambda: recovery_store.list_saved(cancelled=cancel.is_set), choose_saved)

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
                native_start, native_end = editor.GetSelection()
                prefix = editor.GetRange(0, native_start)
                selected = editor.GetRange(native_start, native_end)
                start, end = len(prefix), len(prefix) + len(selected)
                if not document.text.startswith(prefix) or document.text[start:end] != selected:
                    raise ValueError('The native text selection changed. Select the reading range again.')
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
                return with_recovery_warning('')

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
                if review:
                    review.Destroy()
            if alive:
                capabilities(); editor.SetFocus()
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
        (recover_button, recover_saved_draft),
        (undo, history), (redo, lambda event: history(forward=True)),
        (original, compare), (seek, seek_audio), (cleanup, clean), (pronunciation, pronunciations), (close, closing)):
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
            for owned in tuple(owned_registries):
                try:
                    if owned.close() is False:
                        notices.append('A local voice worker has not confirmed cleanup; its owner is retained for shutdown retry.')
                    else:
                        owned_registries.discard(owned)
                except (OSError, RuntimeError) as exc:
                    notices.append(f'The local voice worker could not confirm shutdown: {exc}')
            if owned_registries:
                retained = getattr(parent, '_reader_registry_retained', None)
                if not isinstance(retained, list):
                    retained = []
                    parent._reader_registry_retained = retained
                retained.extend(owned_registries)
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
