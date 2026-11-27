"""Native keyboard editor for explicitly separate reading/narration copies.

No speech synthesis or automatic linguistic correction is implied. The testable
controller owns pending text and confirmed saves; wx is imported only to open
the dialog. Background autosave never steals keyboard or screen-reader focus.
"""
from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import sqlite3
from threading import Event, Lock

from ..reader.narration import (
    NarrationStore, PronunciationRule, comparison_text, encode_project, export_text,
    import_text, new_project, normalization_preview, pronunciation_preview, redo,
    revise, save_as, undo, regex_preview, text_digest,
)
from ..ingest.writer_guard import _private_root
from .research_actions import NativeDialogs


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

    def export(self, path, *, overwrite=False):
        candidate = self.candidate()
        export_text(candidate, path, private_root=self.store.root, overwrite=overwrite)
        return 'UTF-8 reading text exported. The project remains separate from its original source.'


@dataclass(frozen=True)
class _RegexRequest:
    project: object
    text: str
    cancelled: Event


class RegexPreviewJob:
    """One replaceable preview, bound to the existing controller and data root.

    Only the supplied executor computes substitutions. Completion and explicit
    apply are separate; an old future cannot retire a newer request. This helper
    touches no native controls and creates no persistent job or approval store.
    """
    def __init__(self, controller):
        self.controller = controller
        self.root = Path(controller.store.root)
        self.identity = self._root_identity()
        self._lock = Lock()
        self._request = self._running = self._preview = self._future = None
        self.closed = False

    def _root_identity(self):
        root = _private_root(self.root)
        info = root.stat()
        return info.st_dev, info.st_ino, info.st_mode

    def _ensure(self):
        if self.closed:
            raise ValueError('The reading-copy preview is closed.')
        self.controller.guard()
        if (self.closed or Path(self.controller.store.root) != self.root
                or self._root_identity() != self.identity):
            raise ValueError('The reading-copy data directory changed. Open it again.')

    @property
    def busy(self):
        with self._lock:
            return self._running is not None

    def current(self, request):
        if type(request) is not _RegexRequest:
            return False
        try:
            self._ensure()
            return (request is self._request and not request.cancelled.is_set()
                    and self.controller.project is request.project
                    and self.controller.pending_text == request.text)
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            return False

    def start(self, executor, pattern, replacement, *, ignore_case=False, timeout=2.0):
        self._ensure()
        request = _RegexRequest(self.controller.project, self.controller.pending_text, Event())
        with self._lock:
            if self._running is not None:
                raise RuntimeError('Cancel or wait for the current regular-expression preview.')
            if self._request is not None:
                self._request.cancelled.set()
            self._request = self._running = request
            self._preview = self._future = None

        def stopped():
            if not self.current(request):
                request.cancelled.set()
            return request.cancelled.is_set()

        try:
            future = executor.submit(regex_preview, request.text, pattern, replacement,
                ignore_case=ignore_case, timeout=timeout, cancelled=stopped)
        except Exception:
            request.cancelled.set()
            self.settled(request)
            raise
        self._future = future
        # Settle Python ownership even when a native window has been destroyed
        # and no GUI completion callback can be delivered.
        future.add_done_callback(lambda _future: self.settled(request))
        return request, future

    def settled(self, request):
        with self._lock:
            if self._running is request:
                self._running = None

    def complete(self, request, future):
        if not self.current(request):
            return None
        if future is not self._future:
            raise ValueError('This future does not belong to the requested regex preview.')
        preview = future.result()
        if not self.current(request):
            return None
        from ..reader.regex_preview import RegexPreview
        if type(preview) is not RegexPreview or preview.source_sha256 != text_digest(request.text):
            raise ValueError('The regex proposal does not match the captured reading text.')
        self._preview = request, preview
        return preview

    def apply(self, preview):
        if self._preview is None or self._preview[1] is not preview:
            raise ValueError('Generate and inspect this regular-expression preview first.')
        request = self._preview[0]
        if not self.current(request):
            raise ValueError('The reading text or library changed. Generate a fresh preview.')
        base = self.controller.candidate()
        if not self.current(request):
            raise ValueError('The reading text or library changed. Generate a fresh preview.')
        candidate = revise(base, preview.text, label='Regular-expression replacement')
        if not self.current(request):
            raise ValueError('The reading text or library changed. Generate a fresh preview.')
        self.controller.project, self.controller.pending_text = candidate, candidate.current_text
        self._preview = None
        request.cancelled.set()

    def cancel(self):
        if self._request is not None:
            self._request.cancelled.set()
        self._preview = None

    def close(self):
        self.closed = True
        self.cancel()


def _dictionary_text(rules):
    # JSON interchange makes newlines, tabs and literal arrows unambiguous.
    import json
    return json.dumps([{'written': rule.written, 'spoken': rule.spoken, 'language': rule.language}
                       for rule in rules], ensure_ascii=False, indent=2)


def _parse_rules(value):
    import json
    from ..reader.narration import MAX_RULES
    from ..reader.reading_state import _unique
    if not isinstance(value, str) or len(value.encode('utf-8')) > 1024 * 1024:
        raise ValueError('The pronunciation dictionary exceeds its editing budget.')
    try:
        items = json.loads(value, object_pairs_hook=_unique)
        if type(items) is not list or len(items) > MAX_RULES:
            raise ValueError('Expected a list of pronunciation entries.')
        rules = tuple(PronunciationRule(**item) for item in items)
        if len({item.written for item in rules}) != len(rules):
            raise ValueError('Each written phrase must occur only once.')
        return rules
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError(f'Invalid pronunciation dictionary: {exc}') from exc


def open_narration_editor(parent, store, project, *, expected_digest=None, draft_digest=None,
                          guard=lambda: None, dialogs=None, on_ready=None):
    """Open one reading-copy editor; source provenance is always readable.

    The caller guards the captured workspace/source, not the current global
    selection. Original and editing views are separate native text controls.
    """
    import wx

    controller = NarrationController(store, project, expected_digest=expected_digest,
                                     draft_digest=draft_digest, guard=guard)
    dialogs = dialogs or NativeDialogs()
    dialog = wx.Dialog(parent, title='Reading copy — ' + project.title, size=(960, 820),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    dialog_id = dialog.GetId()
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
    state = {'closed': False, 'window_alive': True, 'refreshing': False,
             'last_autosaved': None, 'regex_request': None, 'close_requested': False}
    timer = wx.Timer(dialog)
    regex_job = RegexPreviewJob(controller)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='reading-regex')

    def alive():
        return not state['closed'] and state['window_alive']

    def idle():
        return alive() and state['regex_request'] is None

    def say(message, *, focus=True):
        if not alive():
            return
        status.ChangeValue(message)
        status.SetInsertionPoint(0)
        if focus:
            status.SetFocus()

    def refresh():
        if not alive():
            return
        state['refreshing'] = True
        try:
            editor.ChangeValue(controller.pending_text)
            origin.ChangeValue(controller.project.provenance)
            original.ChangeValue(controller.project.original_text)
            dialog.SetTitle('Reading copy — ' + controller.project.title)
        finally:
            state['refreshing'] = False

    def capture():
        if not alive():
            raise ValueError('The reading-copy window is closed.')
        regex_job._ensure()
        if not alive():
            raise ValueError('The reading-copy window is closed.')
        controller.set_text(editor.GetValue())

    def run(operation, *, focus=True):
        if not idle():
            return
        try:
            capture()
            result = operation()
            refresh()
            if result:
                say(result, focus=focus)
            timer.StartOnce(1200)
        except Exception as exc:
            say(str(exc), focus=focus)

    def save(event=None):
        run(controller.save)

    def save_new(event=None):
        if not idle():
            return
        title = dialogs.text(dialog, 'Save reading project as', 'Name of the new reading project',
                             controller.project.title)
        if title is not None:
            run(lambda: controller.save_as(title))

    def preview_transform(title, text, description, *, apply=None):
        capture()
        selected_project, selected_text = controller.project, controller.pending_text
        candidate = revise(controller.candidate(), text, label=title)

        def unchanged():
            if not alive():
                return False
            regex_job._ensure()
            if not alive():
                return False
            if (controller.project is not selected_project
                    or controller.pending_text != selected_text or editor.GetValue() != selected_text):
                raise ValueError('The reading text changed while the preview was open. Generate a fresh preview.')
            return True

        if not unchanged():
            return
        choice = dialogs.choose(dialog, title, description + '\nThe source will remain unchanged.',
                                ('Read the proposed text', 'Apply the proposed change', 'Cancel'))
        if not unchanged():
            return
        if choice == 0:
            dialogs.show(dialog, title + ' — preview', text)
            if not unchanged():
                return
            choice = dialogs.choose(dialog, title, 'Apply this reading-only change?', ('Apply', 'Cancel'))
            choice = 1 if choice == 0 else 2
        if choice == 1 and unchanged():
            if apply is None:
                controller.apply(candidate.current_text, label=title)
            else:
                apply()
            if not alive():
                return
            refresh()
            say(description + ' Applied to reading copy. Ctrl+S saves the project.')
            timer.StartOnce(1200)

    def normalize(event=None):
        if not idle():
            return
        try:
            capture()
            preview_transform('Normalize spacing', normalization_preview(controller.pending_text),
                              'Normalize line endings, repeated spaces and extra blank lines. '
                              'Punctuation, accents and words are not guessed.')
        except Exception as exc:
            say(str(exc))

    def pronunciation(event=None):
        if not idle():
            return
        try:
            capture()
            text, count = pronunciation_preview(controller.pending_text, controller.project.rules)
            preview_transform('Apply pronunciation dictionary', text,
                              f'{count} case-sensitive literal replacements; no cascading substitutions.')
        except Exception as exc:
            say(str(exc))

    def dictionary(event=None):
        if not idle():
            return
        try:
            value = dialogs.text(dialog, 'Pronunciation dictionary',
                                 'JSON entries: written phrase, spoken replacement, optional language label',
                                 _dictionary_text(controller.project.rules), multiline=True)
            if value is not None:
                run(lambda: (controller.set_rules(_parse_rules(value)) or
                             'Pronunciation dictionary updated. Use Preview pronunciation to apply it.'))
        except Exception as exc:
            say(str(exc))

    def add_rule(event=None):
        if not idle():
            return
        values = dialogs.fields(dialog, 'Add pronunciation replacement', (
            ('written', 'Written phrase (exact case)', '', False),
            ('spoken', 'Read this replacement instead (empty removes the phrase)', '', False),
            ('language', 'Language label (optional description, not automatic detection)', '', False),
        ))
        if values is not None:
            run(lambda: (controller.set_rules(controller.project.rules + (PronunciationRule(**values),))
                         or 'Literal pronunciation replacement added. Preview before applying.'))

    def compare(event=None):
        if not idle():
            return
        try:
            capture()
            dialogs.show(dialog, 'Original and reading-copy comparison', comparison_text(controller.candidate()))
        except Exception as exc:
            say(str(exc))

    def replace_text(event=None):
        if not idle():
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

    def regex_controls(running):
        if not alive():
            return
        editor.SetEditable(not running)
        for button in native_buttons:
            button.Enable(not running)
        native_buttons[-2].Enable(running)
        native_buttons[-1].Enable(True)

    def cancel_regex(event=None):
        if alive() and state['regex_request'] is not None:
            regex_job.cancel()
            say('Cancelling regular-expression preview. The reading text has not changed.')

    def replace_regex(event=None):
        if not idle():
            return
        try:
            capture()
            fields = dialogs.fields(dialog, 'Preview regular-expression replacement', (
                ('pattern', 'Regular expression (Python syntax, at most 1024 characters)', '', False),
                ('replacement', 'Replacement text (backreferences such as \\1 or \\g<name> are supported)', '', False),
            ), explanation='This creates a reading-only preview. It does not change the source or apply automatically. '
                           'The isolated worker has a two-second deadline and bounded input/output.')
            if fields is None or not idle():
                return
            choice = dialogs.choose(dialog, 'Regular-expression case matching',
                                    'Choose how this pattern compares letter case.',
                                    ('Case sensitive', 'Ignore case', 'Cancel'))
            if type(choice) is not int or choice not in (0, 1) or not idle():
                return
            capture()
            timer.Stop()
            request, future = regex_job.start(executor, fields['pattern'], fields['replacement'],
                                               ignore_case=choice == 1)
            state['regex_request'] = request
            regex_controls(True)
            say('Computing the regular-expression preview. Choose Cancel regex preview to stop it.')

            def completed():
                if not alive() or state['regex_request'] is not request:
                    return
                state['regex_request'] = None
                regex_controls(False)
                if state['close_requested']:
                    state['close_requested'] = False
                    close()
                    return
                try:
                    proposal = regex_job.complete(request, future)
                    if proposal is None:
                        say('Preview cancelled or the reading text/library changed. Nothing was applied.')
                    else:
                        preview_transform('Regular-expression replacement', proposal.text,
                            f'{proposal.match_count} matches. Review the proposed reading text before applying.',
                            apply=lambda: regex_job.apply(proposal))
                except Exception as exc:
                    say(str(exc))
                if idle():
                    timer.StartOnce(1200)

            def worker_finished(_future):
                if alive():
                    try:
                        wx.CallAfter(completed)
                    except RuntimeError:
                        regex_job.close()

            future.add_done_callback(worker_finished)
        except Exception as exc:
            if state['regex_request'] is None and alive():
                regex_controls(False)
                timer.StartOnce(1200)
            say(str(exc))

    def export(event=None):
        if not idle():
            return
        picker = wx.FileDialog(dialog, 'Export UTF-8 reading text', wildcard='Text files (*.txt)|*.txt',
                               style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
        try:
            if picker.ShowModal() == wx.ID_OK:
                run(lambda: controller.export(picker.GetPath(), overwrite=True))
        finally:
            picker.Destroy()

    def close(event=None):
        if not alive():
            return
        if state['regex_request'] is not None:
            state['close_requested'] = True
            cancel_regex()
            if event is not None:
                if not hasattr(event, 'CanVeto') or event.CanVeto():
                    if hasattr(event, 'Veto'):
                        event.Veto()
                else:
                    state['closed'] = True
                    regex_job.close()
                    timer.Stop()
                    event.Skip()
            return
        try:
            capture()
            selected_project, selected_text = controller.project, controller.pending_text

            def unchanged():
                if not alive():
                    return False
                regex_job._ensure()
                if not alive():
                    return False
                if (controller.project is not selected_project
                        or controller.pending_text != selected_text or editor.GetValue() != selected_text):
                    raise ValueError('The reading text changed while the close dialog was open. Review it again.')
                return True

            if controller.dirty:
                if not unchanged():
                    return
                choice = dialogs.choose(dialog, 'Unsaved reading copy',
                                        'Keep your reading edits before closing.',
                                        ('Save project and close', 'Keep recovery draft and close', 'Keep editing'))
                if not unchanged():
                    return
                if choice is None or choice == 2:
                    if event is not None and hasattr(event, 'Veto'):
                        event.Veto()
                    return
                if type(choice) is not int or choice not in (0, 1):
                    raise ValueError('Choose whether to save the project, keep a recovery draft, or keep editing.')
                controller.save() if choice == 0 else controller.autosave()
            elif not unchanged():
                return
            if not alive():
                return
            state['closed'] = True
            regex_job.close()
            timer.Stop()
            dialog.EndModal(wx.ID_OK)
        except Exception as exc:
            say('Reading copy remains open because saving did not complete. ' + str(exc))
            if event is not None and hasattr(event, 'Veto'):
                event.Veto()

    def edited(event):
        if not state['refreshing'] and not state['closed']:
            regex_job.cancel()
            if idle():
                timer.StartOnce(1200)
        event.Skip()

    def auto(event):
        if not idle():
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

    def destroyed(event):
        if event.GetId() == dialog_id:
            state['window_alive'] = False
            state['closed'] = True
            regex_job.close()
            executor.shutdown(wait=False, cancel_futures=True)
        event.Skip()

    actions = (
        ('&Save project (Ctrl+S)', save), ('Save &as (Ctrl+Shift+S)', save_new),
        ('&Undo revision (Ctrl+Z)', lambda event: run(lambda: controller.undo() or 'Earlier revision restored.')),
        ('&Redo revision (Ctrl+Y)', lambda event: run(lambda: controller.redo() or 'Later revision restored.')),
        ('Normalize s&pacing', normalize), ('&Add pronunciation', add_rule),
        ('Pronunciation &dictionary', dictionary), ('Preview pro&nunciation', pronunciation),
        ('&Find and replace', replace_text), ('Compare &original', compare),
        ('&Export UTF-8 text', export), ('Preview regular e&xpression', replace_regex),
        ('Cancel re&gex preview', cancel_regex), ('C&lose', close),
    )
    ids, native_buttons = [], []
    for label, handler in actions:
        button = wx.Button(dialog, label=label)
        button.Bind(wx.EVT_BUTTON, handler)
        buttons.Add(button, 0, wx.ALL, 4)
        ids.append(button.GetId())
        native_buttons.append(button)
        dialog.Bind(wx.EVT_MENU, handler, id=button.GetId())
    native_buttons[-2].Enable(False)
    dialog.SetAcceleratorTable(wx.AcceleratorTable([
        (wx.ACCEL_CTRL, ord('S'), ids[0]), (wx.ACCEL_CTRL | wx.ACCEL_SHIFT, ord('S'), ids[1]),
        (wx.ACCEL_CTRL, ord('Z'), ids[2]), (wx.ACCEL_CTRL, ord('Y'), ids[3]),
        (wx.ACCEL_CTRL, ord('H'), ids[8]),
    ]))
    dialog.Bind(wx.EVT_TIMER, auto, timer)
    dialog.Bind(wx.EVT_CLOSE, close)
    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    editor.Bind(wx.EVT_TEXT, edited)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(ids[-1])
    editor.SetFocus()
    editor.SetInsertionPoint(0)
    try:
        guard()
        if on_ready is not None:
            controls = dict(editor=editor, original=original, origin=origin, status=status,
                            save=native_buttons[0], save_as=native_buttons[1],
                            undo=native_buttons[2], redo=native_buttons[3],
                            normalize=native_buttons[4], pronunciation=native_buttons[7],
                            compare=native_buttons[9], regex=native_buttons[-3],
                            cancel_regex=native_buttons[-2], close=native_buttons[-1])
            wx.CallAfter(lambda: on_ready(dialog, controller, controls) if alive() else None)
        dialog.ShowModal()
    finally:
        state['closed'] = True
        regex_job.close()
        executor.shutdown(wait=False, cancel_futures=True)
        if state['window_alive']:
            timer.Stop()
            dialog.Destroy()
    return controller.project


def show_narration_projects(parent, store, *, workspace=None, guard=lambda: None, dialogs=None):
    """Choose a saved project or explicitly recover its autosaved draft."""
    dialogs = dialogs or NativeDialogs()
    guard()
    rows = store.list_projects(workspace)
    if workspace is None:
        rows = tuple(row for row in rows if row['project'].source_location is None)
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
        if (loaded.source_location is not None
                and (workspace is None or loaded.source_location.workspace != workspace)):
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
