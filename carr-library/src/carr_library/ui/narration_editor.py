"""Native keyboard editor for explicitly separate reading/narration copies.

No speech synthesis or automatic linguistic correction is implied. The testable
controller owns pending text and confirmed saves; wx is imported only to open
the dialog. Background autosave never steals keyboard or screen-reader focus.
"""
from dataclasses import replace
from hashlib import sha256
import threading

from ..research.export_file import capture_export_destination, write_export
from ..reader.narration import (
    NarrationStore, PronunciationRule, comparison_text, encode_project, export_text,
    import_text, new_project, normalization_preview, pronunciation_preview, redo,
    decode_pronunciation_dictionary, encode_pronunciation_dictionary, load_pronunciation_dictionary,
    protect_original_path,
    revise, save_as, undo,
)
from .research_actions import NativeDialogs
from .export_destination import approve_export_destination


class PronunciationPreviewTask:
    """One cancellable preview; late callbacks cannot change a closed editor."""
    def __init__(self, *, dispatch, completed):
        self.dispatch, self.completed = dispatch, completed
        self.cancel = threading.Event()
        self.busy = self.closed = False
        self.generation = 0

    def start(self, text, rules, *, language='', voice_id='', timeout=1.0):
        if self.closed or self.busy:
            raise ValueError('Finish or cancel the active pronunciation preview first.')
        rules = tuple(rules)
        self.cancel.clear()
        self.busy = True
        self.generation += 1
        generation = self.generation
        def work():
            result = error = None
            try:
                result = pronunciation_preview(text, rules, language=language, voice_id=voice_id,
                                               timeout=timeout, cancelled=self.cancel.is_set)
            except Exception as exc:
                error = exc
            try:
                self.dispatch(self._finish, generation, result, error)
            except RuntimeError:
                self.cancel.set()
        try:
            threading.Thread(target=work, name='pronunciation-preview', daemon=True).start()
        except Exception:
            self.busy = False
            self.cancel.set()
            raise

    def _finish(self, generation, result, error):
        if self.closed or generation != self.generation:
            return
        self.busy = False
        if self.cancel.is_set() and error is None:
            result, error = None, InterruptedError('Pronunciation preview cancelled. The reading text is unchanged.')
        self.completed(result, error)

    def stop(self):
        self.cancel.set()

    def dispose(self):
        self.closed = True
        self.generation += 1
        self.cancel.set()


class NarrationController:
    def __init__(self, store, project, *, expected_digest=None, draft_digest=None, guard=lambda: None):
        self.store, self.project, self.guard = store, project, guard
        self.saved_digest, self.draft_digest = expected_digest, draft_digest
        self.pending_text = project.current_text
        self.warning = ''

    def candidate(self, *, label='Edited reading text'):
        self.guard()
        return revise(self.project, self.pending_text, label=label)

    @property
    def dirty(self):
        return sha256(encode_project(self.candidate())).hexdigest() != self.saved_digest

    def set_text(self, text):
        self.guard()
        self.pending_text = text

    def autosave(self):
        project = self.candidate()
        digest = self.store.save(project, expected_digest=self.draft_digest, draft=True)
        self.draft_digest = digest
        return 'Recovery draft saved. Press Ctrl+S to save the reading project.'

    def save(self):
        project = self.candidate()
        digest = self.store.save(project, expected_digest=self.saved_digest)
        self.project, self.saved_digest, self.pending_text = project, digest, project.current_text
        self.warning = ''
        if self.draft_digest is not None:
            try:
                self.store.clear_draft(project.id, expected_digest=self.draft_digest)
                self.draft_digest = None
            except Exception as exc:
                self.warning = f' The recovery draft was retained: {exc}'
        return 'Reading project saved. Original source unchanged.' + self.warning

    def save_as(self, title):
        project = save_as(self.candidate(), title=title)
        digest = self.store.save(project)
        self.project, self.saved_digest = project, digest
        self.pending_text, self.draft_digest = project.current_text, None
        return 'A new reading project was saved. The earlier project and recovery draft were kept.'

    def apply(self, text, *, label):
        candidate = revise(self.candidate(), text, label=label)
        self.project, self.pending_text = candidate, candidate.current_text

    def undo(self):
        candidate = undo(self.candidate())
        self.project, self.pending_text = candidate, candidate.current_text

    def redo(self):
        if self.pending_text != self.project.current_text:
            raise ValueError('Save or undo the new edit before redoing an earlier revision.')
        candidate = redo(self.project)
        self.project, self.pending_text = candidate, candidate.current_text

    def set_rules(self, rules):
        candidate = replace(self.candidate(), rules=tuple(rules))
        self.project, self.pending_text = candidate, candidate.current_text

    def export(self, path, *, overwrite=False, approval=None):
        if approval is None:
            approval = capture_export_destination(path, private_root=self.store.root)
        candidate = self.candidate()
        export_text(candidate, path, private_root=self.store.root, overwrite=overwrite,
                    approval=approval)
        return 'UTF-8 reading text exported. The project remains separate from its original source.'

    def import_dictionary(self, path):
        self.guard()
        rules = load_pronunciation_dictionary(path)
        self.set_rules(rules)
        return 'Pronunciation dictionary imported. Preview before applying it.'

    def export_dictionary(self, path, *, overwrite=False, approval=None):
        if approval is None:
            approval = capture_export_destination(path, private_root=self.store.root)
        candidate = self.candidate()
        protect_original_path(candidate.original_path, path)
        raw = encode_pronunciation_dictionary(candidate.rules)
        write_export(path, raw.decode('utf-8'), private_root=self.store.root,
                     require_absent=not overwrite, approval=approval)
        return 'Pronunciation dictionary exported without runtime paths or voice resources.'

    def prepare_audio_studio(self, *, controller_factory=None):
        """Explicitly save, then hand off a separate literal-narration snapshot."""
        self.save()
        self.guard()
        if controller_factory is None:
            from .audio_studio import StudioController
            controller_factory = StudioController
        studio = controller_factory(private_root=self.store.root)
        studio.from_reading_project(self.project, saved_digest=self.saved_digest)
        return studio


def _dictionary_text(rules):
    return encode_pronunciation_dictionary(rules).decode('utf-8')


def _parse_rules(value):
    return decode_pronunciation_dictionary(value)


def open_narration_editor(parent, store, project, *, expected_digest=None, draft_digest=None,
                          guard=lambda: None, dialogs=None, on_ready=None, studio_factory=None,
                          spelling_factory=None, appearance_factory=None, pacing_factory=None,
                          speech_factory=None):
    """Open one reading-copy editor; source provenance is always readable.

    The caller guards the captured workspace/source, not the current global
    selection. Original and editing views are separate native text controls.
    """
    import wx
    from .save_all import SaveCoordinator, SaveResult, coordinator_for, defer_close

    controller = NarrationController(store, project, expected_digest=expected_digest,
                                     draft_digest=draft_digest, guard=guard)
    dialogs = dialogs or NativeDialogs()
    dialog = wx.Dialog(parent, title='Reading copy — ' + project.title, size=(960, 820),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)

    def text_control(label, value='', *, readonly=False, proportion=0, height=None):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.LEFT | wx.RIGHT | wx.TOP, 7)
        style = wx.TE_MULTILINE | (wx.TE_READONLY if readonly else wx.TE_PROCESS_TAB)
        control = wx.TextCtrl(dialog, value=value, style=style)
        control.SetName(label.replace('&', ''))
        if height:
            control.SetMinSize((-1, height))
        layout.Add(control, proportion, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 7)
        return control

    origin = text_control('Original source and reading-copy status', project.provenance,
                          readonly=True, height=75)
    original = text_control('Original text (read only)', project.original_text, readonly=True, height=100)
    editor = text_control('&Editable reading copy (not quotation evidence)', project.current_text,
                          proportion=1)
    # Native multiline text must allow Tab to leave the editor; no TE_PROCESS_TAB.
    editor.SetWindowStyleFlag(editor.GetWindowStyleFlag() & ~wx.TE_PROCESS_TAB)
    status = text_control('Reading-copy save and operation status',
                          'Edit the reading copy. Ctrl+S saves; Ctrl+Shift+S creates a new copy.',
                          readonly=True, height=70)
    buttons = wx.WrapSizer(wx.HORIZONTAL)
    layout.Add(buttons, 0, wx.EXPAND | wx.ALL, 5)
    state = {'closed': False, 'refreshing': False, 'last_autosaved': None, 'text_dirty': False,
             'close_after_preview': False, 'preview_snapshot': None,
             'pronunciation_language': '', 'pronunciation_voice': ''}
    timer = wx.Timer(dialog)
    appearance = None
    coordinator = coordinator_for(parent) or SaveCoordinator()
    dialog._save_coordinator = coordinator

    def say(message, *, focus=True):
        status.ChangeValue(message)
        status.SetInsertionPoint(0)
        if focus:
            status.SetFocus()

    def refresh():
        state['refreshing'] = True
        try:
            editor.ChangeValue(controller.pending_text)
            origin.ChangeValue(controller.project.provenance)
            original.ChangeValue(controller.project.original_text)
            dialog.SetTitle('Reading copy — ' + controller.project.title)
        finally:
            state['refreshing'] = False
            state['text_dirty'] = False

    def capture():
        # A native multiline control may display CRLF as LF. Merely saving,
        # exporting or opening Studio must not turn that display into an edit.
        if state['text_dirty']:
            controller.set_text(editor.GetValue())
            state['text_dirty'] = False

    def run(operation, *, focus=True, completed=None):
        if state['closed']:
            if completed is not None:
                completed(SaveResult(False, 'The reading editor is closed.'))
            return
        if preview.busy:
            outcome = SaveResult(False, 'Cancel or finish the pronunciation preview before saving.')
            say(outcome.message, focus=focus)
            if completed is not None:
                completed(outcome)
            return
        try:
            capture()
            result = operation()
            refresh()
            if result:
                say(result, focus=focus)
            timer.StartOnce(1200)
            outcome = SaveResult(True)
        except Exception as exc:
            say(str(exc), focus=focus)
            outcome = SaveResult(False, str(exc))
        if completed is not None:
            completed(outcome)

    def save(event=None, *, completed=None, focus=True):
        run(controller.save, completed=completed, focus=focus)

    def save_all_open(event=None):
        if state['closed']:
            return
        def completed(summary):
            if not state['closed']:
                say(summary.message())
        if not coordinator.save_all(completed, cancelled=lambda: state['closed']):
            say('Save All is already waiting for a durable save result.')

    def save_new(event=None):
        title = dialogs.text(dialog, 'Save reading project as', 'Name of the new reading project',
                             controller.project.title)
        if title is not None:
            run(lambda: controller.save_as(title))

    def open_audio_studio(event=None):
        if state['closed'] or preview.busy:
            return
        timer.Stop()
        try:
            capture()
            studio = controller.prepare_audio_studio()
            refresh()
            guard()
            factory = studio_factory
            if factory is None:
                from .audio_studio import AudioStudioDialog
                factory = AudioStudioDialog
            factory(dialog, studio).show()
            guard()
            say('Reading project saved. Audio Studio uses a separate literal narration project; '
                'its edits do not replace this reading copy or the original source.')
        except Exception as exc:
            say('Audio Studio was not opened or could not finish: ' + str(exc))
        finally:
            if not state['closed']:
                timer.StartOnce(1200)

    def read_aloud(event=None):
        if state['closed'] or preview.busy:
            return
        timer.Stop()
        try:
            from .live_reading import show_live_reader, selected_source_offsets, source_offset
            guard()
            current = editor.GetValue()
            selection = selected_source_offsets(editor, current)
            cursor = source_offset(current, editor.GetRange(0, editor.GetInsertionPoint()))
            # Unsaved edits are spoken as an independent reading copy. They
            # never borrow the original source's verified location or digest.
            location = (controller.project.source_location
                        if current == controller.project.original_text else None)
            warning = show_live_reader(dialog, controller.store.root, current,
                title=controller.project.title, cursor=cursor, selection=selection,
                location=location, guard=guard)
            say(warning or 'Live reading closed. Your editor and original source are unchanged.')
        except Exception as exc:
            say(str(exc))
        finally:
            if not state['closed']:
                timer.StartOnce(1200)

    def preview_transform(title, text, description):
        capture()
        candidate = revise(controller.candidate(), text, label=title)
        choice = dialogs.choose(dialog, title, description + '\nThe source will remain unchanged.',
                                ('Read the proposed text', 'Apply the proposed change', 'Cancel'))
        if choice == 0:
            dialogs.show(dialog, title + ' — preview', text)
            choice = dialogs.choose(dialog, title, 'Apply this reading-only change?', ('Apply', 'Cancel'))
            choice = 1 if choice == 0 else 2
        if choice == 1:
            controller.apply(candidate.current_text, label=title)
            refresh()
            say(description + ' Applied to reading copy. Ctrl+S saves the project.')
            timer.StartOnce(1200)

    def normalize(event=None):
        try:
            capture()
            preview_transform('Normalize spacing', normalization_preview(controller.pending_text),
                              'Normalize line endings, repeated spaces and extra blank lines. '
                              'Punctuation, accents and words are not guessed.')
        except Exception as exc:
            say(str(exc))

    def preview_controls():
        if state['closed']:
            return
        editor.Enable(not preview.busy)
        for button in native_buttons:
            button.Enable(not preview.busy)
        native_buttons[-1].Enable(True)  # Close waits for cancellation to settle.
        native_buttons[-2].Enable(preview.busy and not preview.cancel.is_set())

    def completed_preview(result, error):
        if state['closed']:
            return
        try:
            preview_controls()
            if state['close_after_preview']:
                state['close_after_preview'] = False
                close()
                return
            if error is not None:
                say(str(error))
                return
            controller.guard()
            current = (controller.pending_text, controller.project.rules, controller.project.id)
            if current != state['preview_snapshot']:
                raise ValueError('The reading copy changed during preview. Run the preview again.')
            text, count = result
            preview_transform('Apply pronunciation dictionary', text,
                f'{count} replacements for the selected context; one non-cascading pass. '
                'Original sources and unselected language/voice rules remain unchanged.')
        except Exception as exc:
            say(str(exc))
        finally:
            # Preview stops the owned autosave timer before launching. Every
            # settled outcome must restore recovery, including Cancel, failure,
            # a declined proposal, and a cancelled close confirmation.
            if not state['closed'] and not preview.busy:
                timer.StartOnce(1200)

    preview = PronunciationPreviewTask(dispatch=wx.CallAfter, completed=completed_preview)

    def cancel_preview(event=None):
        if preview.busy:
            preview.stop()
            preview_controls()
            say('Cancelling pronunciation preview; waiting for its isolated worker.', focus=False)

    def pronunciation(event=None):
        if preview.busy or state['closed']:
            return
        try:
            capture()
            rules = controller.project.rules
            if any(rule.language_scope or rule.voice_id for rule in rules):
                context = dialogs.fields(dialog, 'Pronunciation context', (
                    ('language', 'Exact language identifier (empty uses only unscoped rules)',
                     state['pronunciation_language'], False),
                    ('voice_id', 'Exact installed voice ID (empty uses only rules without a voice scope)',
                     state['pronunciation_voice'], False),
                ))
                if context is None:
                    return
                state['pronunciation_language'], state['pronunciation_voice'] = context['language'], context['voice_id']
            controller.guard()
            state['preview_snapshot'] = (controller.pending_text, rules, controller.project.id)
            timer.Stop()
            preview.start(controller.pending_text, rules, language=state['pronunciation_language'],
                          voice_id=state['pronunciation_voice'])
            preview_controls()
            say('Preparing pronunciation preview. Cancel preview or Close stops it without applying partial edits.',
                focus=False)
        except Exception as exc:
            preview_controls()
            say(str(exc))

    def dictionary(event=None):
        if preview.busy:
            return
        try:
            value = dialogs.text(dialog, 'Pronunciation dictionary',
                'Versioned JSON dictionary; explicit language_scope, voice_id, regex and ignore_case. '
                'Old language labels remain descriptive unless language_scope is true.',
                _dictionary_text(controller.project.rules), multiline=True)
            if value is not None:
                run(lambda: (controller.set_rules(_parse_rules(value)) or
                             'Pronunciation dictionary updated. Use Preview pronunciation to apply it.'))
        except Exception as exc:
            say(str(exc))

    def add_rule(event=None):
        if preview.busy:
            return
        mode = dialogs.choose(dialog, 'Pronunciation match mode', 'Choose how the written phrase is matched.',
                              ('Literal, case sensitive', 'Literal, ignore case',
                               'Regular expression, case sensitive', 'Regular expression, ignore case'))
        if mode is None:
            return
        if type(mode) is not int or not 0 <= mode < 4:
            say('Choose a pronunciation match mode.')
            return
        values = dialogs.fields(dialog, 'Add pronunciation replacement', (
            ('written', 'Written phrase or regular-expression pattern', '', False),
            ('spoken', 'Replacement (regex mode supports captured groups; empty removes a match)', '', False),
            ('language', 'Only this exact language identifier (empty means any)', '', False),
            ('voice_id', 'Only this exact voice ID (empty means any)', '', False),
        ))
        if values is not None:
            run(lambda: (controller.set_rules(controller.project.rules + (
                PronunciationRule(**values, regex=mode >= 2, ignore_case=bool(mode % 2),
                                  language_scope=bool(values['language'])),))
                or 'Pronunciation replacement added. Preview before applying.'))

    def import_dictionary(event=None):
        if preview.busy:
            return
        picker = wx.FileDialog(dialog, 'Import pronunciation dictionary',
                               wildcard='JSON dictionaries (*.json)|*.json',
                               style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        try:
            if picker.ShowModal() == wx.ID_OK:
                run(lambda: controller.import_dictionary(picker.GetPath()))
        finally:
            picker.Destroy()

    def export_dictionary(event=None):
        if preview.busy:
            return
        picker = wx.FileDialog(dialog, 'Export pronunciation dictionary',
                               wildcard='JSON dictionaries (*.json)|*.json', style=wx.FD_SAVE)
        try:
            if picker.ShowModal() == wx.ID_OK:
                path = picker.GetPath()
                approval = approve_export_destination(dialog, path, private_root=controller.store.root)
                if approval is not None:
                    run(lambda: controller.export_dictionary(path, overwrite=True, approval=approval))
        except Exception as exc:
            say(str(exc))
        finally:
            picker.Destroy()

    def compare(event=None):
        try:
            capture()
            dialogs.show(dialog, 'Original and reading-copy comparison', comparison_text(controller.candidate()))
        except Exception as exc:
            say(str(exc))

    def replace_text(event=None):
        if preview.busy:
            return
        fields = dialogs.fields(dialog, 'Find and replace literal text', (
            ('written', 'Find exact text (case sensitive)', '', False),
            ('spoken', 'Replace with', '', False),
        ))
        if fields is None:
            return
        try:
            capture()
            text, count = pronunciation_preview(controller.pending_text, (PronunciationRule(**fields),))
            preview_transform('Replace literal text', text, f'{count} literal replacements.')
        except Exception as exc:
            say(str(exc))

    def export(event=None):
        picker = wx.FileDialog(dialog, 'Export UTF-8 reading text', wildcard='Text files (*.txt)|*.txt',
                               style=wx.FD_SAVE)
        try:
            if picker.ShowModal() == wx.ID_OK:
                path = picker.GetPath()
                approval = approve_export_destination(dialog, path, private_root=controller.store.root)
                if approval is not None:
                    run(lambda: controller.export(path, overwrite=True, approval=approval))
        except Exception as exc:
            say(str(exc))
        finally:
            picker.Destroy()

    def prepare_reading_speech(event=None):
        if state['closed'] or preview.busy:
            return
        timer.Stop()
        try:
            capture()
            controller.guard()
            from .live_reading import native_offset, selected_source_offsets, _normalized
            from .speech_preparation import (apply_speech, capture_speech, reading_frame,
                show_speech_preparation, undo_speech)
            frame = reading_frame(dialog, store.root)

            def current():
                if state['closed']:
                    raise InterruptedError('The reading editor is closed.')
                controller.guard()
                if state['text_dirty'] or _normalized(editor.GetValue()) != _normalized(controller.pending_text):
                    raise ValueError('The visible reading copy changed. Preview speech preparation again.')
                return capture_speech(controller,
                    selected_source_offsets(editor, controller.pending_text))

            def keep_draft():
                try:
                    return controller.autosave()
                except Exception as exc:
                    return 'The change remains in this open reading copy; recovery saving failed: ' + str(exc)

            def apply_reviewed(proposal):
                if not apply_speech(controller, proposal, current()):
                    raise ValueError('The proposed reading text is unchanged.')
                refresh()
                start = proposal.bounds[0]
                editor.SetSelection(native_offset(editor, controller.pending_text, start),
                    native_offset(editor, controller.pending_text, start + len(proposal.result.text)))
                accepted = current()
                return accepted, keep_draft()

            def undo_reviewed(applied):
                undo_speech(controller, applied, current())
                refresh()
                editor.SetInsertionPoint(0)
                return keep_draft()

            (speech_factory or show_speech_preparation)(dialog, frame=frame,
                library_root=store.root, capture=current, apply=apply_reviewed,
                undo=undo_reviewed, guard=controller.guard)
        except Exception as exc:
            say(str(exc), focus=False)
        finally:
            if not state['closed']:
                timer.StartOnce(1200)

    def pace_reading_copy(event=None):
        if state['closed'] or preview.busy:
            return
        timer.Stop()
        try:
            capture()
            controller.guard()
            from .live_reading import native_offset, selected_source_offsets, _normalized
            from .paced_reading import show_paced_reader
            source = controller.pending_text
            selection = selected_source_offsets(editor, source)
            initial_native = editor.GetSelection()

            def current():
                if state['closed']:
                    raise ValueError('The reading editor is closed.')
                controller.guard()
                if _normalized(editor.GetValue()) != _normalized(controller.pending_text):
                    raise ValueError('The visible reading copy changed. Reopen paced reading.')
                return controller.pending_text

            def highlight(first, last):
                if current() != source:
                    raise ValueError('The source text changed. Reopen paced reading.')
                editor.SetSelection(native_offset(editor, source, first),
                                    native_offset(editor, source, last))

            def restore():
                if current() != source:
                    raise ValueError('The source text changed. The current selection was kept.')
                editor.SetSelection(*initial_native)

            (pacing_factory or show_paced_reader)(dialog, store.root, source, selection,
                current_text=current, highlight=highlight, restore_selection=restore,
                guard=controller.guard)
        except Exception as exc:
            say(str(exc), focus=False)
        finally:
            if not state['closed']:
                timer.StartOnce(1200)

    def review_appearance(event=None):
        if state['closed'] or preview.busy or appearance is None:
            return
        timer.Stop()
        try:
            controller.guard()
            appearance.show()
        finally:
            if not state['closed']:
                timer.StartOnce(1200)

    def review_spelling_copy(event=None):
        if state['closed'] or preview.busy:
            return
        timer.Stop()
        try:
            capture()
            controller.guard()
            from .live_reading import native_offset, selected_source_offsets
            from .spellcheck_dialog import (apply_spelling_preview, capture_spelling,
                show_spellcheck_dialog, spelling_frame, undo_spelling_preview)
            frame = spelling_frame(dialog, store.root)

            def current():
                if state['closed']:
                    raise InterruptedError('The reading editor is closed.')
                controller.guard()
                return capture_spelling(controller,
                    selected_source_offsets(editor, controller.pending_text))

            def keep_draft():
                try:
                    return controller.autosave()
                except Exception as exc:
                    return 'The change remains in this open reading copy; recovery saving failed: ' + str(exc)

            def apply_reviewed(proposal):
                apply_spelling_preview(controller, proposal, current())
                refresh()
                editor.SetSelection(native_offset(editor, controller.pending_text, proposal.start),
                    native_offset(editor, controller.pending_text, proposal.start + len(proposal.replacement)))
                result = current()
                warning = keep_draft()
                return result, warning

            def undo_reviewed(applied):
                undo_spelling_preview(controller, applied, current())
                refresh()
                editor.SetInsertionPoint(0)
                return keep_draft()

            factory = spelling_factory or show_spellcheck_dialog
            factory(dialog, frame=frame, library_root=store.root, capture=current,
                apply=apply_reviewed, undo=undo_reviewed, guard=controller.guard)
        except Exception as exc:
            say(str(exc))
        finally:
            if not state['closed']:
                timer.StartOnce(1200)

    def close(event=None):
        if state['closed']:
            return
        if preview.busy:
            state['close_after_preview'] = True
            cancel_preview()
            if event is not None and callable(getattr(event, 'CanVeto', None)) and event.CanVeto():
                event.Veto()
            return
        if defer_close(dialog, coordinator, close=close, report=say,
                       alive=lambda: not state['closed'], post=wx.CallAfter, event=event):
            return
        try:
            capture()
            if controller.dirty:
                choice = dialogs.choose(dialog, 'Unsaved reading copy',
                                        'Keep your reading edits before closing.',
                                        ('Save project and close', 'Keep recovery draft and close', 'Keep editing'))
                if choice is None or choice == 2:
                    if event is not None and hasattr(event, 'Veto'):
                        event.Veto()
                    return
                controller.save() if choice == 0 else controller.autosave()
            state['closed'] = True
            timer.Stop()
            dialog.EndModal(wx.ID_OK)
        except Exception as exc:
            say('Reading copy remains open because saving did not complete. ' + str(exc))
            if event is not None and hasattr(event, 'Veto'):
                event.Veto()

    def edited(event):
        if not state['refreshing'] and not state['closed']:
            state['text_dirty'] = True
            timer.StartOnce(1200)
        event.Skip()

    def auto(event):
        if state['closed'] or preview.busy:
            return
        try:
            capture()
            if not controller.dirty:
                return
            candidate = controller.candidate()
            signature = (candidate.current_text, candidate.rules, candidate.cursor, candidate.title)
            if signature != state['last_autosaved']:
                message = controller.autosave()
                state['last_autosaved'] = signature
                say(message, focus=False)
        except Exception as exc:
            say('Recovery draft could not be saved. Your editor remains open: ' + str(exc), focus=False)

    actions = (
        ('&Save project (Ctrl+S)', save), ('Save &as (Ctrl+Shift+S)', save_new),
        ('&Undo revision (Ctrl+Z)', lambda event: run(lambda: controller.undo() or 'Earlier revision restored.')),
        ('&Redo revision (Ctrl+Y)', lambda event: run(lambda: controller.redo() or 'Later revision restored.')),
        ('Normalize s&pacing', normalize), ('&Add pronunciation', add_rule),
        ('Pronunciation &dictionary', dictionary), ('Preview pro&nunciation', pronunciation),
        ('&Find and replace', replace_text), ('Compare &original', compare),
        ('&Export UTF-8 text', export), ('Save and open Audio S&tudio', open_audio_studio),
        ('Save all open drafts', save_all_open), ('Import dictionary...', import_dictionary),
        ('Export dictionary...', export_dictionary), ('Read current copy aloud...', read_aloud),
        ('Review spelling...', review_spelling_copy), ('Reader appearance...', review_appearance),
        ('Paced reading...', pace_reading_copy), ('Prepare speech text...', prepare_reading_speech),
        ('Cancel preview', cancel_preview), ('C&lose', close),
    )
    ids, native_buttons = [], []
    for label, handler in actions:
        button = wx.Button(dialog, label=label)
        button.Bind(wx.EVT_BUTTON, handler)
        buttons.Add(button, 0, wx.ALL, 4)
        ids.append(button.GetId())
        native_buttons.append(button)
        dialog.Bind(wx.EVT_MENU, handler, id=button.GetId())
    dialog.SetAcceleratorTable(wx.AcceleratorTable([
        (wx.ACCEL_CTRL, ord('S'), ids[0]), (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('S'), ids[1]),
        (wx.ACCEL_CTRL, ord('Z'), ids[2]), (wx.ACCEL_CTRL, ord('Y'), ids[3]),
        (wx.ACCEL_CTRL, ord('H'), ids[8]),
        (wx.ACCEL_CTRL | wx.ACCEL_ALT, ord('A'), ids[11]),
    ]))
    dialog.Bind(wx.EVT_TIMER, auto, timer)
    dialog.Bind(wx.EVT_CLOSE, close)
    editor.Bind(wx.EVT_TEXT, edited)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(ids[-1])
    preview_controls()
    editor.SetFocus()
    editor.SetInsertionPoint(0)
    unregister = coordinator.register(
        f'reading.{id(dialog)}', 'Reading copy — ' + project.title,
        dirty=lambda: not state['closed'] and (state['text_dirty'] or controller.dirty),
        save=lambda completed: save(completed=completed, focus=False), focus=editor.SetFocus)
    try:
        guard()
        from .reader_appearance import ReaderAppearance
        appearance = (appearance_factory or ReaderAppearance)(
            dialog, editor, store.root, guard=controller.guard,
            additional_controls=(original,), alive=lambda: not state['closed'],
            report=lambda message: say(message, focus=False), wx_module=wx)
        dialog._reader_appearance = appearance
        if appearance.notice:
            say(appearance.notice, focus=False)
        if on_ready is not None:
            controls = dict(editor=editor, original=original, origin=origin, status=status,
                            save=native_buttons[0], save_as=native_buttons[1],
                            normalize=native_buttons[4], pronunciation=native_buttons[7],
                            compare=native_buttons[9], audio_studio=native_buttons[11],
                            save_all=native_buttons[12], cancel_preview=native_buttons[-2],
                            import_dictionary=native_buttons[13], export_dictionary=native_buttons[14],
                            live_reading=native_buttons[15], spelling=native_buttons[16],
                            appearance=native_buttons[17], paced_reading=native_buttons[18],
                            speech_preparation=native_buttons[19],
                            close=native_buttons[-1])
            wx.CallAfter(on_ready, dialog, controller, controls)
        dialog.ShowModal()
    finally:
        state['closed'] = True
        if appearance is not None:
            appearance.close()
        preview.dispose()
        unregister()
        timer.Stop()
        dialog.Destroy()
    return controller.project


def show_narration_projects(parent, store, *, workspace=None, guard=lambda: None, dialogs=None):
    """Choose a saved project or explicitly recover its autosaved draft."""
    dialogs = dialogs or NativeDialogs()
    guard()
    rows = store.list_projects(workspace)
    if workspace is None:
        rows = tuple(row for row in rows if row['project'].workspace is None)
    if not rows:
        dialogs.show(parent, 'Reading copies', 'There are no saved reading copies or recovery drafts.')
        return None
    labels = [row['project'].title + (' — recovery draft available' if row['draft'] else '')
              + ' — ' + row['project'].id for row in rows]
    index = dialogs.choose(parent, 'Open reading copy', 'Choose a reading project.', labels)
    if index is None:
        return None
    if type(index) is not int or not 0 <= index < len(rows):
        raise ValueError('Choose one reading project.')
    guard()
    row = rows[index]
    project_id = row['project'].id
    project, saved_digest = store.load(project_id) if row['saved'] else (row['project'], None)
    def authorize(loaded):
        guard()
        if (loaded.workspace is not None
                and (workspace is None or loaded.workspace != workspace)):
            raise ValueError('The reading project belongs to a different library. Open it in that library.')
    # Re-read identities after the selection dialog. A file or recovery draft
    # may have changed since the initial list was constructed.
    authorize(project)
    draft_digest = None
    if row['draft']:
        draft, draft_digest = store.load(project_id, draft=True)
        authorize(draft)
        options = ('Recover saved draft', 'Open last saved project', 'Cancel') if row['saved'] else (
            'Recover saved draft', 'Cancel')
        selection = dialogs.choose(parent, 'Recover reading draft',
                                   'A recovery draft exists. Choose explicitly which text to open.', options)
        if selection is None or selection == len(options) - 1:
            return None
        if selection == 0:
            project = draft
        # Opening the saved version still retains the draft until an explicit
        # save or subsequent autosave, protected by its original digest lease.
    authorize(project)
    return open_narration_editor(parent, store, project, expected_digest=saved_digest,
                                 draft_digest=draft_digest, guard=guard, dialogs=dialogs)


def import_reading_text(parent, store, *, guard=lambda: None):
    import wx
    picker = wx.FileDialog(parent, 'Import UTF-8 text as a reading copy',
                           wildcard='UTF-8 text files (*.txt)|*.txt|All files (*.*)|*.*',
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
    try:
        if picker.ShowModal() != wx.ID_OK:
            return None
        guard()
        project = import_text(picker.GetPath())
    finally:
        picker.Destroy()
    return open_narration_editor(parent, store, project, guard=guard)
