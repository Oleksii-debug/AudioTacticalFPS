"""Native named workflow recipes; all work continues through existing owners."""
import json
from pathlib import Path
from threading import Event
from uuid import uuid4

from ..assistant.models import canonical
from ..assistant.templates import (KINDS, MAX_TEMPLATE_BYTES, TemplateStore, WorkflowTemplate,
    definition_from_project, review_request)
from ..durable_state import local_path, read_json


def create_workflow_templates_dialog(frame, *, initial=None, selected_id=None, wx_module=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module

    class WorkflowTemplatesDialog(wx.Dialog):
        def __init__(self):
            super().__init__(frame, title='Named workflow templates and keyboard macros',
                             size=(980, 850), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self._native_id = self.GetId()
            self._destroy_requested = False
            self.alive, self.pending = True, False
            self.cancelled = Event()
            self.library = frame.library
            self.root = local_path(self.library.root, exists=True)
            self.store = TemplateStore(self.root)
            self.rows, self.previous = (), None
            box = wx.BoxSizer(wx.VERTICAL)
            notice = wx.StaticText(self, label=(
                'A template stores typed settings only. Every use opens a NEW review with current inputs and outputs.\n'
                'Saved scope, execution approval, credentials and job completion are never inherited. '
                'Keyboard macros open this same review; they do not type keys or run code.'))
            notice.Wrap(920); box.Add(notice, 0, wx.ALL | wx.EXPAND, 5)
            self.fields = {}
            def field(key, label, *, choices=None, value='', height=None):
                box.Add(wx.StaticText(self, label=label), 0, wx.ALL, 3)
                control = (wx.Choice(self, choices=choices) if choices is not None else
                    wx.TextCtrl(self, value=value, style=wx.TE_MULTILINE if height else 0,
                                size=(-1, height or -1)))
                control.SetName(label.replace('&', ''))
                box.Add(control, 1 if height else 0, wx.ALL | wx.EXPAND, 3)
                self.fields[key] = control
                return control
            self.saved = field('saved', 'Saved &workflow', choices=[])
            self.name = field('name', 'Workflow &name', value='New workflow')
            self.kind = field('kind', 'Template &kind', choices=list(KINDS)); self.kind.SetSelection(0)
            self.definition = field('definition', 'Typed operation &definition (JSON)',
                value='{"steps":[{"tool":"library.list","arguments":{}}]}', height=135)
            self.parameters = field('parameters', 'Typed &parameters (JSON; each needs kind, label, example)',
                                    value='{}', height=75)
            self.values = field('values', 'Explicit parameter &values for this use (JSON)', value='{}', height=60)
            self.items = field('items', 'Per-item overrides (JSON array; empty means one item)', value='[]', height=60)
            self.status = field('status', 'Workflow status', value='Nothing has been submitted or run.', height=60)
            self.status.SetEditable(False)
            row = wx.BoxSizer(wx.HORIZONTAL); self.buttons = {}
            for key, label, handler in (
                ('reload', '&Reload', self.reload), ('new', '&New', self.new),
                ('project', 'Use Studio &project...', self.project),
                ('examples', 'Use &example values', self.examples),
                ('save', '&Save template', self.save), ('review', '&Review new use...', self.review),
                ('close', '&Close', self.close)):
                button = wx.Button(self, label=label, **({'id': wx.ID_CANCEL} if key == 'close' else {}))
                button.SetName(label.replace('&', '')); button.Bind(wx.EVT_BUTTON, handler)
                row.Add(button, 0, wx.ALL, 3); self.buttons[key] = button
            box.Add(row, 0, wx.EXPAND); self.SetSizer(box); self.SetEscapeId(wx.ID_CANCEL)
            self.saved.Bind(wx.EVT_CHOICE, self.choose)
            self.Bind(wx.EVT_CLOSE, self.close); self.Bind(wx.EVT_WINDOW_DESTROY, self.on_destroy)
            if initial is not None:
                kind, definition = initial
                self.kind.SetSelection(KINDS.index(kind))
                self.definition.ChangeValue(json.dumps(definition, ensure_ascii=False, indent=2))
                self.report('Reusable settings copied for review. Enter a name and save explicitly.')
            else:
                self.reload(selected_id=selected_id)

        def guard(self):
            if (not self.alive or getattr(frame, '_closing', False) or frame.library is not self.library
                    or local_path(frame.library.root, exists=True) != self.root):
                raise ValueError('This workflow window closed or the library changed. Reopen it.')

        def report(self, value):
            if self.alive:
                self.status.ChangeValue(str(value))

        def enable(self):
            if not self.alive:
                return
            for key, control in self.fields.items():
                if key != 'status':
                    control.Enable(not self.pending)
            for key, button in self.buttons.items():
                button.Enable(key == 'close' or not self.pending)

        def begin(self, label, operation, done, *, committed=False):
            if not self.alive or self.pending or frame.working:
                self.report('Finish the current operation first.'); return False
            try:
                self.guard()
            except Exception as exc:
                self.report(exc); return False
            self.pending = True; self.cancelled.clear(); self.enable()
            def stopped():
                return (self.cancelled.is_set() or frame.cancel_event.is_set()
                        or frame.library is not self.library or getattr(frame, '_closing', False))
            def work():
                if stopped():
                    raise InterruptedError('Workflow operation stopped before publication.')
                result = operation(stopped)
                if not committed and stopped():
                    raise InterruptedError('Workflow inspection was stopped.')
                return result
            def completed(result):
                self.pending = False
                if not self.alive:
                    return
                try:
                    self.guard()
                    if not committed and stopped():
                        raise InterruptedError('Workflow inspection was stopped.')
                    done(result)
                except Exception as exc:
                    self.report(exc)
                self.enable()
            def failed(error):
                self.pending = False
                self.report(error); self.enable()
            try:
                accepted = frame.run_job(label, work, done=completed, failed=failed)
            except (Exception, KeyboardInterrupt) as exc:
                self.pending = False
                self.report(exc); self.enable()
                return False
            if accepted is False or (self.pending and not frame.working):
                self.pending = False
                self.report('The shared operation did not start. Try again.'); self.enable()
                return False
            return True

        def candidate(self):
            self.guard()
            index = self.kind.GetSelection()
            if not 0 <= index < len(KINDS):
                raise ValueError('Choose a template kind.')
            return WorkflowTemplate(self.previous.id if self.previous else uuid4().hex,
                self.name.GetValue(), KINDS[index], self.definition.GetValue(),
                self.parameters.GetValue(), self.previous.revision if self.previous else 1)

        def fill(self, value):
            if not self.alive:
                return
            self.previous = value
            self.name.ChangeValue(value.name); self.kind.SetSelection(KINDS.index(value.kind))
            self.definition.ChangeValue(json.dumps(value.definition, ensure_ascii=False, indent=2))
            self.parameters.ChangeValue(json.dumps(value.parameters, ensure_ascii=False, indent=2))
            self.values.ChangeValue('{}'); self.items.ChangeValue('[]')
            self.report(f'{value.name}; revision {value.revision}. Supply current parameters, then review. '
                        'The template has no saved permissions or runnable job.')

        def reload(self, event=None, *, selected_id=None):
            def loaded(rows):
                self.rows = rows
                self.saved.Set([f'{row.name}; {row.kind}; revision {row.revision}; {row.id[:8]}' for row in rows])
                index = next((i for i, row in enumerate(rows) if row.id == selected_id), -1)
                self.saved.SetSelection(index)
                if index >= 0:
                    self.fill(rows[index])
                else:
                    self.report(f'{len(rows)} named templates. Choose one explicitly.')
                register_saved_macros(frame, rows, wx_module=wx)
            return self.begin('Reading saved workflow settings', lambda stopped: self.store.list(), loaded)

        def choose(self, event=None):
            if not self.alive or self.pending:
                return
            index = self.saved.GetSelection()
            if 0 <= index < len(self.rows):
                self.fill(self.rows[index])

        def new(self, event=None):
            if not self.alive or self.pending:
                return
            self.previous = None; self.saved.SetSelection(-1)
            self.name.ChangeValue('New workflow'); self.kind.SetSelection(0)
            self.definition.ChangeValue('{"steps":[{"tool":"library.list","arguments":{}}]}')
            self.parameters.ChangeValue('{}'); self.values.ChangeValue('{}'); self.items.ChangeValue('[]')
            self.report('Enter typed settings. Saving never executes them.')

        def examples(self, event=None):
            if not self.alive or self.pending:
                return
            try:
                candidate = self.candidate()
                self.values.ChangeValue(json.dumps({name: spec['example'] for name, spec in candidate.parameters.items()},
                                                   ensure_ascii=False, indent=2))
                self.report('Example values copied explicitly. Replace them with the current intended values before review.')
            except Exception as exc:
                self.report(exc)

        def save(self, event=None):
            if not self.alive or self.pending:
                return
            try:
                candidate, previous = self.candidate(), self.previous
                def work(stopped):
                    result = self.store.save(candidate.name, candidate.kind, candidate.definition,
                                             candidate.parameters, previous=previous)
                    return result, self.store.warning
                def saved(result):
                    value, warning = result
                    self.fill(value)
                    register_saved_macros(frame, (value,), wx_module=wx)
                    self.report(warning or 'Template saved. Its keyboard command opens a new explicit review; nothing ran.')
                return self.begin('Saving workflow settings', work, saved, committed=True)
            except Exception as exc:
                self.report(exc); return False

        def project(self, event=None):
            if not self.alive or self.pending:
                return
            try:
                self.guard()
                picker = wx.FileDialog(self, 'Choose a saved Studio project JSON',
                                       wildcard='Studio project (*.json)|*.json', style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
                try:
                    result = picker.ShowModal()
                    if not self.alive or not picker or result != wx.ID_OK:
                        return
                    self.guard(); path = Path(picker.GetPath())
                finally:
                    if picker:
                        picker.Destroy()
                if not self.alive:
                    return
                def work(stopped):
                    from ..studio.project import Project
                    if stopped():
                        raise InterruptedError('Project selection stopped.')
                    return definition_from_project(Project.from_dict(read_json(path, maximum=MAX_TEMPLATE_BYTES)))
                def loaded(definition):
                    self.previous = None; self.saved.SetSelection(-1); self.kind.SetSelection(KINDS.index('project'))
                    self.name.ChangeValue('Project template')
                    self.definition.ChangeValue(json.dumps(definition, ensure_ascii=False, indent=2))
                    self.parameters.ChangeValue('{}'); self.values.ChangeValue('{}'); self.items.ChangeValue('[]')
                    self.report('Project settings copied. Source references and project identity were removed. '
                                'Every use gets a fresh project identity and current input/voice/output review.')
                return self.begin('Reading project template settings', work, loaded)
            except Exception as exc:
                self.report(exc); return False

        def review(self, event=None):
            if not self.alive or self.pending:
                return
            try:
                candidate = self.candidate()
                values, items = json.loads(self.values.GetValue()), json.loads(self.items.GetValue())
                request = review_request(candidate, values, items=None if items == [] else items)
                if candidate.kind == 'document-batch':
                    rows = [{}] if request['items'] is None else request['items']
                    settings = [candidate.resolve({**request['values'], **row}) for row in rows]
                    selected = 0
                    if len(settings) > 1:
                        chooser = wx.SingleChoiceDialog(self, 'Choose one item preset for a new batch review.',
                            'Per-item settings', [f'Item {i + 1}: {row["action"]}; {row["voice_id"]}; {row["format"]}'
                                                 for i, row in enumerate(settings)])
                        try:
                            accepted = chooser.ShowModal()
                            if not self.alive or not chooser or accepted != wx.ID_OK:
                                return
                            self.guard(); selected = chooser.GetSelection()
                        finally:
                            if chooser:
                                chooser.Destroy()
                    if not self.alive or not 0 <= selected < len(settings):
                        return
                    self.guard()
                    from .document_batch import show_document_batches
                    show_document_batches(frame, initial_settings=settings[selected])
                else:
                    from .assistant_dialog import show_assistant
                    self.guard()
                    show_assistant(frame, initial_instruction=json.dumps(request, ensure_ascii=False, indent=2))
            except Exception as exc:
                self.report(exc)

        def dispose(self):
            self.alive = False
            cancelled = getattr(self, 'cancelled', None)
            if cancelled is not None:
                cancelled.set()

        def on_destroy(self, event):
            if event.GetId() == self._native_id:
                self._destroy_requested = True; self.dispose()
            event.Skip()

        def Destroy(self):
            self.dispose()
            if getattr(self, '_destroy_requested', False) or not self:
                return False
            self._destroy_requested = True
            return super().Destroy()

        def close(self, event=None):
            self.dispose()
            if self:
                if self.IsModal():
                    self.EndModal(wx.ID_CANCEL)
                else:
                    self.Hide()

    return WorkflowTemplatesDialog()


def show_workflow_templates(frame, *, initial=None, selected_id=None):
    dialog = create_workflow_templates_dialog(frame, initial=initial, selected_id=selected_id)
    try:
        dialog.ShowModal()
    finally:
        dialog.dispose()
        if dialog:
            dialog.Destroy()


def register_saved_macros(frame, templates, *, wx_module=None):
    """Register only review commands with the same semantic manager as all menus."""
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    state = getattr(frame, '_workflow_macro_menu', None)
    if state is None:
        return
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    menu, registered, library = state
    if frame.library is not library or getattr(frame, '_closing', False):
        return
    for template in templates:
        if template.id in registered:
            continue
        label = 'Review ' + template.name.replace('&', '&&') + ' [' + template.id[:8] + ']...'
        item = menu.Append(wx.ID_ANY, label)
        identity = template.id
        def open_recipe(event, identity=identity):
            if not frame or getattr(frame, '_closing', False):
                return
            if frame.library is not library:
                frame.report('The macro library changed. Reopen workflow templates.'); return
            show_workflow_templates(frame, selected_id=identity)
        frame.Bind(wx.EVT_MENU, open_recipe, item)
        # Register each leaf immediately; no second unknown native leaf is added.
        register_menu_command(frame, CommandSpec(template.command_id, label), item)
        registered.add(identity)


def install_workflow_templates_actions(frame):
    import wx
    if getattr(frame, '_workflow_macro_menu', None) is not None:
        return
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    bar = frame.GetMenuBar()
    menu = next((bar.GetMenu(i) for i in range(bar.GetMenuCount())
                 if bar.GetMenuLabel(i).replace('&', '').casefold() == 'tools'), None)
    if menu is None:
        menu = wx.Menu(); bar.Append(menu, '&Tools')
    item = menu.Append(wx.ID_ANY, 'Workflow &templates and macros...')
    frame.Bind(wx.EVT_MENU, lambda event: show_workflow_templates(frame), item)
    register_menu_command(frame, CommandSpec('tools.workflow_templates', 'Workflow templates and macros...'), item)
    macros = wx.Menu(); menu.AppendSubMenu(macros, 'Saved workflow &macros')
    frame._workflow_macro_menu = (macros, set(), frame.library)
    try:
        register_saved_macros(frame, TemplateStore(frame.library.root).list())
    except (OSError, ValueError, RuntimeError) as exc:
        # Invalid saved data remains untouched and never becomes an executable macro.
        message = 'Workflow shortcut inventory needs attention: ' + str(exc)
        wx.CallAfter(lambda: frame.report(message) if frame and not getattr(frame, '_closing', False) else None)
