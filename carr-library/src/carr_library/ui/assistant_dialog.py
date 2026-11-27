"""Native scoped local assistant and non-secret provider settings."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import threading
from uuid import uuid4

from ..durable_state import atomic_bytes


def show_assistant(frame, *, on_ready=None):
    import wx
    from ..assistant import AssistantService, Scope, HELP, Plan, Step
    from ..assistant.workflow_access import WorkflowAccess, workflow_name
    from ..studio.render import ProductionService
    library, active_scope = frame.library, frame.active
    production = ProductionService(frame.library.root / 'state' / 'studio')
    owns_production_registry = production._voices is None
    service = AssistantService(frame.library, production=production.assistant_callback)
    workflows = WorkflowAccess(service)
    dialog = wx.Dialog(frame, title='Local assistant — review and run a bounded plan', size=(900, 850),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-assistant')
    cancel = threading.Event()
    alive, busy, running, plan, job_id = True, False, False, None, None
    preview_generation = 0
    workspaces = frame.library.workspaces()

    def field(label, cls=wx.TextCtrl, proportion=0, **kwargs):
        layout.Add(wx.StaticText(dialog, label=label), 0, wx.LEFT | wx.TOP, 5)
        control = cls(dialog, **kwargs)
        control.SetName(label.replace('&', ''))
        layout.Add(control, proportion, wx.ALL | wx.EXPAND, 5)
        return control

    scope_choice = field('&Library scope', wx.Choice,
        choices=['Create a new library / list libraries only'] + [row.name for row in workspaces])
    current = frame.active[0] if frame.active else None
    scope_choice.SetSelection(next((i + 1 for i, row in enumerate(workspaces) if row.id == current), 0))
    input_folder = field('Allowed &input folder (optional; no other folders are readable)')
    output_folder = field('Chosen &output folder (required for audio, extraction or backup)')
    choose_row = wx.BoxSizer(wx.HORIZONTAL)
    folder_buttons = []
    for label, target in [('Choose input folder', input_folder), ('Choose output folder', output_folder)]:
        button = wx.Button(dialog, label=label)
        folder_buttons.append(button)
        def choose(event, target=target):
            if busy:
                return
            guard()
            picker = wx.DirDialog(dialog, 'Choose a local folder')
            try:
                if picker.ShowModal() == wx.ID_OK:
                    guard()
                    target.SetValue(picker.GetPath())
            finally:
                picker.Destroy()
        button.Bind(wx.EVT_BUTTON, choose)
        choose_row.Add(button, 0, wx.ALL, 4)
    layout.Add(choose_row)
    full_backup = wx.CheckBox(dialog, label='Allow a complete backup of all my libraries for this plan')
    layout.Add(full_backup, 0, wx.ALL, 5)
    saved_name = field('Workflow &name (optional until Save named workflow)')
    saved_name.SetMaxLength(200)
    instruction = field('&Goal — guided English/Ukrainian commands or typed JSON plan', proportion=1,
                        style=wx.TE_MULTILINE, value='Покажи бібліотеки')
    review = field('Plan preview and actual resul&ts', proportion=1, style=wx.TE_MULTILINE | wx.TE_READONLY,
                   value='LOCAL ONLY. Nothing executes until you review the plan and choose Run.')
    row = wx.BoxSizer(wx.HORIZONTAL)
    buttons = {}
    for identity, label in [('preview', '&Preview plan'), ('run', '&Run / resume'), ('pause', 'Pa&use'),
                             ('save', '&Save named workflow'), ('reuse', 'Reuse saved &workflow'),
                             ('jobs', 'Saved &jobs'), ('help', '&Help'), ('close', '&Close')]:
        button = wx.Button(dialog, wx.ID_CANCEL if identity == 'close' else wx.ID_ANY, label=label)
        buttons[identity] = button
        row.Add(button, 0, wx.ALL, 4)
    layout.Add(row)
    dialog.SetSizer(layout)
    dialog.SetEscapeId(wx.ID_CANCEL)
    buttons['run'].Disable()
    buttons['pause'].Disable()
    buttons['save'].Disable()

    def guard():
        if (not alive or getattr(frame, '_closing', False)
                or frame.library is not library or frame.active != active_scope):
            raise ValueError('The library or topic changed. Reopen the saved workflow window.')

    def report(value):
        if alive:
            review.ChangeValue(str(value))

    def invalidate(event=None):
        nonlocal plan, job_id, preview_generation
        plan, job_id = None, None
        preview_generation += 1
        buttons['run'].Disable()
        buttons['save'].Disable()
        if event is not None:
            event.Skip()

    def preview(event=None):
        if busy:
            return
        try:
            guard()
            invalidate()
            cancel.clear()
            generation = preview_generation
            index = scope_choice.GetSelection()
            scope = Scope(workspaces[index - 1].id if index > 0 else None,
                          (Path(input_folder.GetValue()),) if input_folder.GetValue().strip() else (),
                          Path(output_folder.GetValue()) if output_folder.GetValue().strip() else None,
                          allow_create=index == 0, allow_full_backup=full_backup.GetValue())
            candidate = service.preview(instruction.GetValue(), scope)
            name = saved_name.GetValue()

            def current_preview():
                guard()
                if cancel.is_set():
                    raise InterruptedError('Plan preview was cancelled; no workflow was authorized.')
                if generation != preview_generation:
                    raise ValueError('The plan changed during review. Preview the current instructions again.')

            def reviewed(value):
                nonlocal plan, job_id
                current_preview()
                if name.strip():
                    value = replace(value, summary=workflow_name(name))
                plan, job_id = value, None
                report('LOCAL ONLY — no remote services or charges.\n\n' +
                       json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) +
                       '\n\nChoose Run to authorize these operations inside these directories.')
                buttons['run'].Enable()
                buttons['save'].Enable()
                review.SetFocus()

            if any(step.tool == 'audio.produce' and not step.arguments.get('voice_ids') and 'script' not in step.arguments
                   for step in candidate.steps):
                def discover():
                    current_preview()
                    voices = production.voices.voices()
                    current_preview()
                    return voices

                def choose_voice(voices):
                    current_preview()
                    if not voices:
                        raise ValueError('Install and choose a local voice before narration.')
                    choice = wx.SingleChoiceDialog(dialog, 'Choose the default local voice for this production.',
                        'Production voice', [v.name + '; ' + v.language for v in voices])
                    try:
                        answer = choice.ShowModal()
                        selected = choice.GetSelection()
                    finally:
                        choice.Destroy()
                    current_preview()
                    if answer != wx.ID_OK:
                        return
                    if not 0 <= selected < len(voices):
                        raise ValueError('Install and choose a local voice before narration.')
                    steps = tuple(Step(s.tool, {**s.arguments, 'voice_ids': [voices[selected].id]})
                        if s.tool == 'audio.produce' and not s.arguments.get('voice_ids')
                        and 'script' not in s.arguments else s for s in candidate.steps)
                    selected_plan = Plan(scope, steps, candidate.summary)
                    service.validate(selected_plan)
                    reviewed(selected_plan)

                review_task(discover, choose_voice,
                    'Reviewing the configured local voices. Pause or Close cancels this preview; no audio is rendered.')
            else:
                reviewed(candidate)
        except Exception as exc:
            invalidate()
            report(str(exc))
            review.SetFocus()

    def run(event=None):
        nonlocal busy, running, job_id
        if busy or (plan is None and job_id is None):
            return
        try:
            guard()
            if job_id is None:
                job_id = service.submit(plan)
            identity = job_id
            busy = running = True
            cancel.clear()
            for key in ('preview', 'run', 'jobs', 'save', 'reuse'):
                buttons[key].Disable()
            buttons['pause'].Enable()
            for control in (scope_choice, instruction, input_folder, output_folder, full_backup, saved_name, *folder_buttons):
                control.Disable()
            future = executor.submit(service.resume, identity, cancelled=cancel.is_set,
                                      progress=lambda completed, total, message: wx.CallAfter(report,
                                          f'{completed}/{total}: {message}'))
        except Exception as exc:
            busy = running = False
            report(str(exc))
            return
        def finish(done):
            def apply():
                nonlocal busy, running
                if not alive:
                    return
                busy = running = False
                for key in ('preview', 'run', 'jobs', 'reuse'):
                    buttons[key].Enable()
                buttons['save'].Enable(plan is not None)
                buttons['pause'].Disable()
                for control in (scope_choice, instruction, input_folder, output_folder, full_backup, saved_name, *folder_buttons):
                    control.Enable()
                try:
                    result = done.result()
                    report(f'{result.status}: {result.completed} of {result.total} verified operations.\n' +
                           json.dumps(result.results, ensure_ascii=False, indent=2) + '\n' + result.message)
                except Exception as exc:
                    report(str(exc))
                review.SetFocus()
            wx.CallAfter(apply)
        future.add_done_callback(finish)

    def review_task(action, complete, message):
        nonlocal busy
        if busy:
            return
        guard()
        busy = True
        cancel.clear()
        for key in ('preview', 'run', 'jobs', 'save', 'reuse'):
            buttons[key].Disable()
        buttons['pause'].Enable()
        for control in (scope_choice, instruction, input_folder, output_folder, full_backup, saved_name, *folder_buttons):
            control.Disable()
        report(message)
        try:
            future = executor.submit(action)
        except Exception:
            busy = False
            for key in ('preview', 'jobs', 'reuse'):
                buttons[key].Enable()
            buttons['run'].Enable(plan is not None or job_id is not None)
            buttons['save'].Enable(plan is not None)
            buttons['pause'].Disable()
            for control in (scope_choice, instruction, input_folder, output_folder, full_backup, saved_name, *folder_buttons):
                control.Enable()
            raise
        def finished(result):
            def apply():
                nonlocal busy
                if not alive:
                    return
                busy = False
                for key in ('preview', 'jobs', 'reuse'):
                    buttons[key].Enable()
                buttons['run'].Enable(plan is not None or job_id is not None)
                buttons['save'].Enable(plan is not None)
                buttons['pause'].Disable()
                for control in (scope_choice, instruction, input_folder, output_folder, full_backup, saved_name, *folder_buttons):
                    control.Enable()
                try:
                    guard()
                    complete(result.result())
                except Exception as exc:
                    if alive:
                        report(str(exc))
                        review.SetFocus()
            wx.CallAfter(apply)
        future.add_done_callback(finished)

    def save_workflow(event=None):
        nonlocal job_id
        try:
            guard()
            if plan is None:
                raise ValueError('Preview the typed plan before saving it.')
            name = workflow_name(saved_name.GetValue())
            candidate = plan
            def action():
                if cancel.is_set():
                    raise InterruptedError('Workflow save was cancelled before publication.')
                return workflows.save(candidate, name)
            def complete(identity):
                nonlocal job_id
                job_id = identity
                buttons['run'].Enable()
                report('Named workflow saved. No operation has run. Choose Run to execute this reviewed plan, '
                       'or Reuse saved workflow later to choose new permissions.\n\n' + candidate.to_json())
                review.SetFocus()
            review_task(action, complete, 'Saving the reviewed named workflow. An already completed save remains saved.')
        except Exception as exc:
            report(str(exc))

    def choose_saved(event=None, *, reuse=False):
        def picked(rows):
            nonlocal job_id, plan
            if cancel.is_set():
                raise InterruptedError('Saved workflow review was cancelled.')
            if not rows:
                report('No saved typed plans. Preview a plan, give it a name and choose Save named workflow.')
                return
            choice = wx.SingleChoiceDialog(dialog,
                'Choose a recipe to review with new permissions.' if reuse else 'Choose an original job to inspect.',
                'Reusable typed workflows' if reuse else 'Assistant jobs', [row.label for row in rows])
            selected = None
            try:
                if choice.ShowModal() == wx.ID_OK and 0 <= choice.GetSelection() < len(rows):
                    selected = rows[choice.GetSelection()]
            finally:
                choice.Destroy()
            guard()
            if selected is None:
                return
            def loaded(value):
                nonlocal job_id, plan
                if cancel.is_set():
                    raise InterruptedError('Saved workflow review was cancelled.')
                if reuse:
                    instruction.ChangeValue(json.dumps(value, ensure_ascii=False, indent=2))
                    saved_name.ChangeValue(selected.name[:200])
                    input_folder.ChangeValue('')
                    output_folder.ChangeValue('')
                    full_backup.SetValue(False)
                    invalidate()
                    report('Typed recipe loaded. Historical folder permissions and completed results were removed. '
                           'Choose the current library and input/output folders, edit the steps if needed, '
                           'then Preview. Run remains a separate explicit action.')
                    scope_choice.SetFocus()
                else:
                    record, _ = value
                    plan = None
                    job_id = None if record.get('restore_requires_review', False) else selected.identity
                    buttons['run'].Enable(job_id is not None)
                    buttons['save'].Disable()
                    report(json.dumps(record['plan'], ensure_ascii=False, indent=2) +
                           '\n\n' + ('Restored job: choose Reuse saved workflow for a new review. Original execution is blocked.'
                                      if job_id is None else 'Resume uses this original scope and its existing checkpoints. '
                                      'Reuse saved workflow starts a new reviewed job.'))
                    review.SetFocus()
            action = (lambda: workflows.recipe(selected)) if reuse else (
                lambda: service.review_saved_plan(selected.identity))
            review_task(action, loaded, 'Reading the saved typed plan without activating its old folders.')
        try:
            review_task(lambda: workflows.entries(cancelled=cancel.is_set), picked,
                        'Reading saved typed workflows. No tools are running.')
        except Exception as exc:
            report(str(exc))

    def close(event=None):
        nonlocal alive
        if not alive:
            return
        cancel.set()
        # The running service observes this same cancellation token and owns
        # its checkpoint. Closing must not block on a second filesystem review.
        alive = False
        def release_owned_registry():
            # This service was constructed here without a borrowed registry.
            # Its single worker serializes cleanup after every active probe or
            # render, including a probe that finishes after the window closes.
            if owns_production_registry and production._voices is not None:
                production._voices.close()
        executor.submit(release_owned_registry)
        executor.shutdown(wait=False, cancel_futures=False)
        dialog.EndModal(wx.ID_CLOSE)

    def help_text(event):
        from .desktop_app import show_text
        show_text(dialog, 'Local assistant commands', HELP)

    for identity, callback in {'preview': preview, 'run': run, 'pause': lambda e: cancel.set(),
                               'save': save_workflow, 'reuse': lambda e: choose_saved(reuse=True),
                               'jobs': choose_saved, 'help': help_text, 'close': close}.items():
        buttons[identity].Bind(wx.EVT_BUTTON, callback)
    for control in (input_folder, output_folder, instruction, saved_name):
        control.Bind(wx.EVT_TEXT, invalidate)
    scope_choice.Bind(wx.EVT_CHOICE, invalidate)
    full_backup.Bind(wx.EVT_CHECKBOX, invalidate)
    dialog.Bind(wx.EVT_CLOSE, close)
    try:
        if on_ready is not None:
            wx.CallAfter(on_ready, dialog)
        dialog.ShowModal()
    finally:
        if alive:
            close()
        dialog.Destroy()


def show_provider_settings(frame):
    import wx
    from ..assistant import ProfileStore, ProviderProfile, WindowsCredentialVault
    store = ProfileStore(frame.library.root / 'state' / 'providers')
    vault = WindowsCredentialVault()
    dialog = wx.Dialog(frame, title='AI and API connections — optional', size=(760, 650),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    layout.Add(wx.StaticText(dialog, label='Local operation is the default. Saving a profile makes no network request.\n'
        'Remote use requires a separate scoped job authorization and budget.'), 0, wx.ALL, 6)
    listing = wx.ListBox(dialog)
    listing.SetName('Provider accounts')
    layout.Add(listing, 1, wx.EXPAND | wx.ALL, 6)
    status = wx.TextCtrl(dialog, style=wx.TE_READONLY | wx.TE_MULTILINE)
    status.SetName('Connection configuration status')
    layout.Add(status, 0, wx.EXPAND | wx.ALL, 6)
    rows = ()

    def refresh():
        nonlocal rows
        rows = store.list()
        listing.SetItems([f'{r.name}; {r.adapter}; {"local" if r.local else "remote"}; '
                          f'{"enabled" if r.enabled else "disabled"}' for r in rows])

    def edit(event=None, *, new=False):
        index = listing.GetSelection()
        current = rows[index] if not new and 0 <= index < len(rows) else None
        form = wx.Dialog(dialog, title='Add connection' if current is None else 'Edit connection', size=(650, 700))
        box = wx.BoxSizer(wx.VERTICAL)
        def field(label, value='', password=False):
            box.Add(wx.StaticText(form, label=label), 0, wx.ALL, 4)
            control = wx.TextCtrl(form, value=value, style=wx.TE_PASSWORD if password else 0)
            control.SetName(label)
            box.Add(control, 0, wx.EXPAND | wx.ALL, 4)
            return control
        name = field('Account name', current.name if current else '')
        box.Add(wx.StaticText(form, label='Adapter'), 0, wx.ALL, 4)
        adapter = wx.Choice(form, choices=['openai-compatible', 'elevenlabs', 'google-translation-v2'])
        adapter.SetStringSelection(current.adapter if current else 'openai-compatible')
        box.Add(adapter, 0, wx.EXPAND | wx.ALL, 4)
        endpoint = field('Official endpoint or explicitly chosen loopback service', current.endpoint if current else 'http://127.0.0.1:8080/v1')
        model = field('Model ID', current.model if current else 'local-model')
        voice = field('Voice ID; blank for language models', current.voice or '' if current else '')
        capability = field('Capabilities: llm.chat, speech.synthesize, music.compose, sfx.generate, translation.text, translation.languages',
                           ','.join(current.capabilities) if current else 'llm.chat')
        secret = field('New API key; leave empty to keep existing key', password=True)
        local = wx.CheckBox(form, label='This is a local loopback service')
        local.SetValue(current.local if current else True)
        enabled = wx.CheckBox(form, label='Enable this profile for explicitly authorized jobs')
        enabled.SetValue(current.enabled if current else False)
        def adapter_defaults(event):
            if adapter.GetStringSelection() == 'google-translation-v2':
                endpoint.ChangeValue('https://translation.googleapis.com/language/translate/v2')
                model.ChangeValue('nmt')
                capability.ChangeValue('translation.text,translation.languages')
                voice.ChangeValue('')
                local.SetValue(False)
            event.Skip()
        adapter.Bind(wx.EVT_CHOICE, adapter_defaults)
        box.Add(local, 0, wx.ALL, 4)
        box.Add(enabled, 0, wx.ALL, 4)
        box.Add(form.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.ALL, 5)
        form.SetSizer(box)
        try:
            if form.ShowModal() != wx.ID_OK:
                return
            identity = current.id if current else uuid4().hex
            profile = ProviderProfile(identity, name.GetValue(), adapter.GetStringSelection(), endpoint.GetValue(),
                model.GetValue(), tuple(x.strip() for x in capability.GetValue().split(',') if x.strip()),
                current.credential_ref if current else None, local.GetValue(), enabled.GetValue(), voice.GetValue() or None)
            authority_changed = current and (current.endpoint, current.adapter, current.local) != (profile.endpoint, profile.adapter, profile.local)
            if authority_changed:
                profile = replace(profile, credential_ref=None, enabled=False)
            new_reference = None
            if secret.GetValue():
                new_reference = vault.put(profile.id, secret.GetValue())
                profile = replace(profile, credential_ref=new_reference)
            try:
                store.save(profile)
            except Exception:
                if new_reference:
                    vault.delete(profile.id, new_reference)
                raise
            if current and new_reference and current.credential_ref:
                vault.delete(current.id, current.credential_ref)
            secret.ChangeValue('')
            refresh()
            status.ChangeValue('Saved. Credentials are held by Windows Credential Manager. No provider was contacted.')
        except Exception as exc:
            status.ChangeValue(str(exc))
        finally:
            secret.ChangeValue('')
            form.Destroy()
        status.SetFocus()

    def remove(event):
        index = listing.GetSelection()
        if not 0 <= index < len(rows):
            return
        profile = rows[index]
        if wx.MessageBox('Remove this connection and its stored credential?', 'Remove connection',
                          wx.YES_NO | wx.NO_DEFAULT, dialog) != wx.YES:
            return
        try:
            store.remove(profile.id, vault=vault)
            refresh()
            status.ChangeValue('Connection removed.')
        except Exception as exc:
            status.ChangeValue(str(exc))

    def check(event):
        index = listing.GetSelection()
        if not 0 <= index < len(rows):
            return
        try:
            profile = store.get(rows[index].id)
            if profile.credential_ref:
                vault.get(profile.id, profile.credential_ref)
            status.ChangeValue('Local profile and credential configuration is valid. API connectivity, pricing and access were not tested.')
        except Exception as exc:
            status.ChangeValue(str(exc))
        status.SetFocus()

    def exchange(event, *, importing=False):
        picker = wx.FileDialog(dialog, 'Import non-secret profiles' if importing else 'Export non-secret profiles',
            defaultFile='provider-profiles.json', wildcard='JSON (*.json)|*.json',
            style=(wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) if importing else (wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT))
        try:
            if picker.ShowModal() != wx.ID_OK:
                return
            path = Path(picker.GetPath())
            if importing:
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError('Profile file exceeds the import budget.')
                store.import_json(path.read_text(encoding='utf-8'))
                refresh()
                status.ChangeValue('Profiles imported disabled. Reconnect each account to its credential.')
            else:
                atomic_bytes(path, store.export_json().encode('utf-8'), replace=path.exists())
                status.ChangeValue('Exported non-secret profiles. No API keys or vault references are included.')
        except Exception as exc:
            status.ChangeValue(str(exc))
        finally:
            picker.Destroy()

    row = wx.BoxSizer(wx.HORIZONTAL)
    for label, handler in [('&Add', lambda e: edit(e, new=True)), ('&Edit', edit), ('&Remove', remove),
        ('&Check local configuration', check), ('&Import', lambda e: exchange(e, importing=True)), ('E&xport', exchange)]:
        button = wx.Button(dialog, label=label)
        button.Bind(wx.EVT_BUTTON, handler)
        row.Add(button, 0, wx.ALL, 3)
    layout.Add(row)
    layout.Add(dialog.CreateButtonSizer(wx.CLOSE), 0, wx.ALL, 5)
    dialog.SetEscapeId(wx.ID_CLOSE)
    dialog.Bind(wx.EVT_BUTTON, lambda event: dialog.EndModal(wx.ID_CLOSE), id=wx.ID_CLOSE)
    dialog.SetSizer(layout)
    refresh()
    try:
        dialog.ShowModal()
    finally:
        dialog.Destroy()


def install_assistant_actions(frame):
    import wx
    menu = wx.Menu()
    for title, handler in [('Local assistant...\tCtrl+Alt+J', lambda e: show_assistant(frame)),
                            ('AI and API connections...', lambda e: show_provider_settings(frame))]:
        item = menu.Append(wx.ID_ANY, title)
        frame.Bind(wx.EVT_MENU, handler, item)
    frame.menu_bar.Append(menu, '&Tools')
