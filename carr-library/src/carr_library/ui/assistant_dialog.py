"""Native scoped local assistant and non-secret provider settings."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import threading
from uuid import uuid4

from ..durable_state import atomic_bytes


def create_assistant_dialog(frame, *, wx_module=None, reader_owner=None):
    if wx_module is None:
        import wx as wx_module
    wx = wx_module
    from ..assistant import AssistantService, Scope, HELP, Plan, Step
    from ..studio.render import ProductionService
    from ..assistant.player_tools import LivePlayerBridge
    player_bridge = (LivePlayerBridge(frame.library, getattr(frame, 'desktop_integration', None), reader_owner)
                     if reader_owner is not None else None)
    production = ProductionService(frame.library.root / 'state' / 'studio')
    service = AssistantService(frame.library, production=production.assistant_callback, player=player_bridge)
    dialog = wx.Dialog(reader_owner or frame, title='Local assistant — review and run a bounded plan', size=(900, 850),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-assistant')
    cancel = threading.Event()
    alive, busy, plan, job_id = True, None, None, None
    revision, approved_inputs = 0, None
    library_root = frame.library.root
    from ..assistant.templates import TemplateStore, WorkflowTemplate, KINDS, name_key
    template_store = TemplateStore(library_root)
    template_selection = None
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
            picker = wx.DirDialog(dialog, 'Choose a local folder')
            try:
                if picker.ShowModal() == wx.ID_OK:
                    target.SetValue(picker.GetPath())
            finally:
                picker.Destroy()
        button.Bind(wx.EVT_BUTTON, choose)
        choose_row.Add(button, 0, wx.ALL, 4)
    layout.Add(choose_row)
    full_backup = wx.CheckBox(dialog, label='Allow a complete backup of all my libraries for this plan')
    layout.Add(full_backup, 0, wx.ALL, 5)
    profile_error = ''
    try:
        profiles = service.local_planner_profiles()
    except Exception as exc:
        profiles, profile_error = (), str(exc)
    planning_source = field('Planning &source', wx.Choice, choices=[
        'Guided commands / typed JSON — no model request'] + [
        f'Local model: {profile.name}; {profile.model}; {profile.endpoint}' for profile in profiles])
    planning_source.SetSelection(0)
    planning_notice = wx.StaticText(dialog, label=(
        'Local model proposals require an explicitly selected enabled loopback chat profile. '
        'No server or model starts automatically.\n'
        'Review every proposed operation and scope before Run. Request budget: 120 seconds.\n'
        'Cancel proposal interrupts the pending request through the shared connection owner.') +
        (('\nProfile configuration: ' + profile_error) if profile_error else
         ('\nNo eligible model profile. Add and enable one in AI and API connections.' if not profiles else '')))
    planning_notice.Wrap(850)
    layout.Add(planning_notice, 0, wx.ALL | wx.EXPAND, 5)
    template_name = field('Workflow template &name (saved steps carry no permissions)')
    template_kind = field('Template &category', wx.Choice,
        choices=['Workflow', 'Batch preset', 'Typed keyboard macro'])
    template_kind.SetSelection(0)
    instruction = field('&Goal — guided English/Ukrainian commands or typed JSON plan', proportion=1,
                        style=wx.TE_MULTILINE, value='Покажи бібліотеки')
    review = field('Plan &preview and actual results', proportion=1, style=wx.TE_MULTILINE | wx.TE_READONLY,
                   value='LOCAL ONLY. Nothing executes until you review the plan and choose Run.')
    row = wx.BoxSizer(wx.HORIZONTAL)
    buttons = {}
    for identity, label in [('preview', '&Preview plan'), ('run', '&Run / resume'), ('pause', '&Pause'),
                             ('jobs', 'Saved &jobs'), ('help', '&Help'), ('close', '&Close')]:
        button = wx.Button(dialog, label=label)
        buttons[identity] = button
        row.Add(button, 0, wx.ALL, 4)
    layout.Add(row)
    template_row = wx.BoxSizer(wx.HORIZONTAL)
    for identity, label in [('templates', 'Named &templates...'),
                            ('save_template', '&Save reviewed template'),
                            ('delete_template', '&Delete selected template')]:
        button = wx.Button(dialog, label=label)
        buttons[identity] = button
        template_row.Add(button, 0, wx.ALL, 4)
    layout.Add(template_row)
    dialog.SetSizer(layout)
    buttons['run'].Disable()
    buttons['pause'].Disable()

    def report(value):
        if alive:
            review.ChangeValue(str(value))

    def input_identity():
        return (scope_choice.GetSelection(), input_folder.GetValue(), output_folder.GetValue(),
                full_backup.GetValue(), instruction.GetValue(), planning_source.GetSelection(),
                str(frame.library.root))

    def invalidate(event=None):
        nonlocal plan, job_id, revision, approved_inputs
        revision += 1
        approved_inputs = None
        plan, job_id = None, None
        if busy in ('proposal', 'voices'):
            cancel.set()
        buttons['run'].Disable()
        if event is not None:
            event.Skip()

    def accept_candidate(candidate, selected_inputs, source_label, *, voice_inventory=None):
        nonlocal plan, job_id, approved_inputs, busy
        try:
            if selected_inputs != input_identity():
                raise ValueError('The goal or selected scope changed. Preview the current inputs again.')
            if any(step.tool in ('audio.produce', 'audio.render') and not step.arguments.get('voice_ids') and 'script' not in step.arguments
                   for step in candidate.steps):
                if voice_inventory is None:
                    # Cold private voice discovery can load model metadata.
                    # Keep it on this dialog's existing worker, with explicit
                    # cancellation and the same proposal identity checks.
                    cancel.clear()
                    busy = 'voices'
                    for key in ('preview', 'run', 'jobs'):
                        buttons[key].Disable()
                    buttons['pause'].SetLabel('Cancel voice &discovery')
                    buttons['pause'].Enable()
                    report('Checking configured local voices before the explicit voice choice. Nothing has been submitted.')
                    future = executor.submit(production.discover_voices, cancelled=cancel.is_set)
                    def discovered(done):
                        def apply():
                            nonlocal busy
                            if not alive:
                                return
                            busy = None
                            buttons['preview'].Enable()
                            buttons['jobs'].Enable()
                            buttons['pause'].Disable()
                            buttons['pause'].SetLabel('&Pause')
                            try:
                                if cancel.is_set() or selected_inputs != input_identity():
                                    raise InterruptedError('Voice discovery cancelled or the reviewed inputs changed. Preview again.')
                                found = tuple(done.result().voices())
                            except Exception as exc:
                                invalidate()
                                report(str(exc))
                                return
                            accept_candidate(candidate, selected_inputs, source_label, voice_inventory=found)
                        wx.CallAfter(apply)
                    future.add_done_callback(discovered)
                    return
                voices = voice_inventory
                choice = wx.SingleChoiceDialog(dialog, 'Choose the default local voice for this production.',
                                                'Production voice', [v.name + '; ' + v.language for v in voices])
                try:
                    if choice.ShowModal() != wx.ID_OK:
                        return
                    selected = choice.GetSelection()
                finally:
                    choice.Destroy()
                if not 0 <= selected < len(voices):
                    raise ValueError('Install and choose a local voice before narration.')
                steps = tuple(Step(s.tool, {**s.arguments, 'voice_ids': [voices[selected].id]})
                              if s.tool in ('audio.produce', 'audio.render') and 'script' not in s.arguments else s for s in candidate.steps)
                candidate = Plan(candidate.scope, steps, candidate.summary)
                service.validate(candidate)
            if selected_inputs != input_identity():
                raise ValueError('The goal or selected scope changed. Preview the current inputs again.')
            plan, job_id = candidate, None
            approved_inputs = selected_inputs
            report(source_label + '\nLOCAL ONLY — proposal has not executed.\n\n' +
                   json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) +
                   '\n\nChoose Run to authorize these operations inside these directories.')
            buttons['run'].Enable()
            review.SetFocus()
        except Exception as exc:
            invalidate()
            report(str(exc))
            review.SetFocus()

    def preview(event=None):
        nonlocal busy
        if busy:
            return
        invalidate()
        try:
            if frame.library.root != library_root:
                raise ValueError('The library location changed. Reopen the assistant before planning.')
            index = scope_choice.GetSelection()
            if not 0 <= index <= len(workspaces):
                raise ValueError('Choose the library scope.')
            scope = Scope(workspaces[index - 1].id if index > 0 else None,
                          (Path(input_folder.GetValue()),) if input_folder.GetValue().strip() else (),
                          Path(output_folder.GetValue()) if output_folder.GetValue().strip() else None,
                          allow_create=index == 0, allow_full_backup=full_backup.GetValue())
            selected_inputs, selected_revision = input_identity(), revision
            selected = planning_source.GetSelection()
            if selected == 0:
                accept_candidate(service.preview(instruction.GetValue(), scope), selected_inputs,
                                 'Guided command / typed JSON preview')
                return
            if not 1 <= selected <= len(profiles):
                raise ValueError('Explicitly choose an enabled local model profile.')
            profile = profiles[selected - 1]
            cancel.clear()
            busy = 'proposal'
            for key in ('preview', 'run', 'jobs'):
                buttons[key].Disable()
            buttons['pause'].SetLabel('Cancel &proposal')
            buttons['pause'].Enable()
            report(f'Requesting a local proposal from {profile.name}; model {profile.model}; '
                   f'{profile.endpoint}. Nothing has been submitted. Editing the goal or scope cancels this proposal.')
            future = executor.submit(service.propose_local, instruction.GetValue(), scope,
                                      profile_id=profile.id, expected_profile=profile, cancelled=cancel.is_set)
        except Exception as exc:
            busy = None
            buttons['preview'].Enable()
            buttons['jobs'].Enable()
            buttons['pause'].Disable()
            buttons['pause'].SetLabel('&Pause')
            report(str(exc))
            review.SetFocus()
            return

        def finish(done):
            def apply():
                nonlocal busy
                if not alive:
                    return
                busy = None
                buttons['preview'].Enable()
                buttons['jobs'].Enable()
                buttons['pause'].Disable()
                buttons['pause'].SetLabel('&Pause')
                if (cancel.is_set() or selected_revision != revision
                        or selected_inputs != input_identity()):
                    invalidate()
                    report('Local proposal cancelled or discarded because its goal or scope changed. '
                           'Nothing was submitted. Preview the current inputs again.')
                    review.SetFocus()
                    return
                try:
                    candidate = done.result()
                except Exception as exc:
                    invalidate()
                    report(str(exc))
                    review.SetFocus()
                    return
                accept_candidate(candidate, selected_inputs,
                                 f'Proposal from local profile {profile.name}; requested model {profile.model}; {profile.endpoint}')
            wx.CallAfter(apply)
        future.add_done_callback(finish)

    def run(event=None):
        nonlocal busy, job_id
        if busy or (plan is None and job_id is None):
            return
        try:
            if frame.library.root != library_root:
                raise ValueError('The library location changed. Reopen the assistant before running.')
            if plan is not None and approved_inputs != input_identity():
                invalidate()
                raise ValueError('The reviewed goal or scope changed. Preview the current inputs again.')
            if job_id is None:
                job_id = service.submit(plan)
            identity = job_id
            busy = 'run'
            cancel.clear()
            for key in ('preview', 'run', 'jobs'):
                buttons[key].Disable()
            buttons['pause'].Enable()
            for control in (scope_choice, instruction, input_folder, output_folder, full_backup,
                            planning_source, *folder_buttons):
                control.Disable()
            future = executor.submit(service.resume, identity, cancelled=cancel.is_set,
                                      progress=lambda completed, total, message: wx.CallAfter(report,
                                          f'{completed}/{total}: {message}'))
        except Exception as exc:
            busy = None
            report(str(exc))
            return
        def finish(done):
            def apply():
                nonlocal busy
                if not alive:
                    return
                busy = None
                for key in ('preview', 'run', 'jobs'):
                    buttons[key].Enable()
                buttons['pause'].Disable()
                for control in (scope_choice, instruction, input_folder, output_folder, full_backup,
                                planning_source, *folder_buttons):
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

    def jobs(event=None):
        nonlocal job_id, plan, approved_inputs
        rows = service.jobs()
        choice = wx.SingleChoiceDialog(dialog, 'Choose a saved local plan.', 'Assistant jobs',
            [f'{row.id}; {row.status}; {row.completed}/{row.total}' for row in rows])
        try:
            if choice.ShowModal() == wx.ID_OK and 0 <= choice.GetSelection() < len(rows):
                row = rows[choice.GetSelection()]
                job_id, plan = row.id, None
                approved_inputs = None
                report(f'{row.status}: {row.completed}/{row.total}. {row.message}\nResume uses the original reviewed scope.')
                buttons['run'].Enable()
        finally:
            choice.Destroy()

    def template_guard():
        if not alive or busy:
            raise ValueError('Finish or cancel the current operation before changing templates.')
        if frame.library.root != library_root:
            raise ValueError('The library location changed. Reopen the assistant before using templates.')
        template_store._guard()

    def load_template(event=None):
        nonlocal template_selection
        if not alive or busy:
            return
        try:
            template_guard()
            records, token = template_store.list()
            if not records:
                report('No named templates. Preview a plan, enter a name, then choose Save reviewed template.')
                return
            choice = wx.SingleChoiceDialog(dialog,
                'Choose saved steps to review in a fresh plan. Loading executes nothing.',
                'Named assistant templates',
                [f'{record.name}; {record.kind}; {len(record.steps)} steps' for record in records])
            try:
                if choice.ShowModal() != wx.ID_OK:
                    return
                index = choice.GetSelection()
            finally:
                choice.Destroy()
            template_guard()
            if not 0 <= index < len(records):
                raise ValueError('Select a named template.')
            record, token = template_store.get(records[index].id, expected_token=token)
            # Paths in step arguments are editable references, never renewed
            # permissions. Every folder/complete-backup approval is selected anew.
            input_folder.ChangeValue('')
            output_folder.ChangeValue('')
            full_backup.SetValue(False)
            planning_source.SetSelection(0)
            instruction.ChangeValue(json.dumps(record.proposal(), ensure_ascii=False, indent=2))
            template_name.ChangeValue(record.name)
            template_kind.SetSelection(KINDS.index(record.kind))
            template_selection = (record.id, token)
            invalidate()
            report(f'Loaded "{record.name}" ({record.kind}); nothing was submitted. '
                   'Choose the current library and folders, edit any source references, '
                   'then Preview and Run a new plan. Saved jobs and previous approvals are not reused.')
            instruction.SetFocus()
        except Exception as exc:
            report(str(exc))

    def save_template(event=None):
        nonlocal template_selection
        if not alive or busy:
            return
        try:
            template_guard()
            if plan is None or approved_inputs != input_identity():
                raise ValueError('Preview the current goal and scope before saving a template.')
            selected_plan, selected_inputs, selected_revision = plan, input_identity(), revision
            name, kind_index = template_name.GetValue(), template_kind.GetSelection()
            if not 0 <= kind_index < len(KINDS):
                raise ValueError('Choose the template category.')
            service.validate(selected_plan)
            records, token = template_store.list()
            if template_selection is not None:
                if template_selection[1] != token:
                    raise ValueError('The template catalogue changed. Select the current version again.')
                selected_record = next((row for row in records if row.id == template_selection[0]), None)
                if selected_record is None:
                    raise ValueError('The selected template no longer exists.')
            # A new name saves a separate reusable copy. An existing name
            # updates only its explicitly confirmed current record.
            previous = next((row for row in records if name_key(row.name) == name_key(name)), None)
            record = WorkflowTemplate.capture(name, selected_plan, kind=KINDS[kind_index],
                identity=previous.id if previous is not None else None)
            if previous is not None and wx.MessageBox(
                    f'Replace the saved steps for "{previous.name}" with this reviewed plan?',
                    'Replace named template', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                    dialog) != wx.YES:
                return
            template_guard()
            if (plan is not selected_plan or selected_revision != revision
                    or selected_inputs != input_identity()
                    or template_name.GetValue() != name
                    or template_kind.GetSelection() != kind_index):
                raise ValueError('The reviewed template changed. Preview and save the current version.')
            service.validate(selected_plan)
            token = template_store.save(record, expected_token=token)
            template_selection = (record.id, token)
            report(f'Saved "{record.name}" as {record.kind}. Only typed steps were saved; '
                   'loading it requires fresh scope review and a separate Run.')
        except Exception as exc:
            report(str(exc))

    def delete_template(event=None):
        nonlocal template_selection
        if not alive or busy:
            return
        try:
            template_guard()
            selected = template_selection
            if selected is None:
                raise ValueError('Load the named template to delete first.')
            record, token = template_store.get(selected[0], expected_token=selected[1])
            if wx.MessageBox(f'Delete the saved template "{record.name}"? Existing jobs remain available.',
                    'Delete named template', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
                    dialog) != wx.YES:
                return
            template_guard()
            if selected != template_selection:
                raise ValueError('The selected template changed.')
            template_store.delete(record.id, expected_token=token)
            template_selection = None
            template_name.ChangeValue('')
            invalidate()
            report('Named template deleted. Existing jobs were preserved.')
        except Exception as exc:
            report(str(exc))

    def close(event=None):
        nonlocal alive
        if not alive:
            return
        cancel.set()
        if busy == 'run' and job_id:
            service.pause(job_id)
        alive = False
        if player_bridge is not None:
            player_bridge.close()
        executor.submit(production.close)
        executor.shutdown(wait=False, cancel_futures=False)
        if dialog.IsModal():
            dialog.EndModal(wx.ID_CLOSE)
        else:
            dialog.Hide()

    def help_text(event):
        from .desktop_app import show_text
        show_text(dialog, 'Local assistant commands', HELP)

    for identity, callback in {'preview': preview, 'run': run, 'pause': lambda e: cancel.set(),
                               'jobs': jobs, 'help': help_text, 'close': close,
                               'templates': load_template, 'save_template': save_template,
                               'delete_template': delete_template}.items():
        buttons[identity].Bind(wx.EVT_BUTTON, callback)
    for control in (input_folder, output_folder, instruction):
        control.Bind(wx.EVT_TEXT, invalidate)
    scope_choice.Bind(wx.EVT_CHOICE, invalidate)
    planning_source.Bind(wx.EVT_CHOICE, invalidate)
    full_backup.Bind(wx.EVT_CHECKBOX, invalidate)
    dialog.Bind(wx.EVT_CLOSE, close)
    dialog.assistant_controls = dict(scope=scope_choice, input=input_folder, output=output_folder,
        full_backup=full_backup, instruction=instruction, planning_source=planning_source,
        review=review, buttons=buttons, template_name=template_name, template_kind=template_kind)
    dialog.assistant_handlers = dict(preview=preview, run=run, invalidate=invalidate,
                                    close=close, cancel=cancel.set, templates=load_template,
                                    save_template=save_template, delete_template=delete_template)
    dialog.assistant_service = service
    return dialog


def show_assistant(frame, *, reader_owner=None, templates=False):
    dialog = create_assistant_dialog(frame, reader_owner=reader_owner)
    if templates:
        import wx
        wx.CallAfter(dialog.assistant_handlers['templates'])
    try:
        dialog.ShowModal()
    finally:
        dialog.assistant_handlers['close']()
        dialog.Destroy()


def show_provider_settings(frame):
    import wx
    from ..assistant import ProfileStore, ProviderProfile, WindowsCredentialVault
    from ..assistant.profiles import ADAPTERS, google_oauth_token, save_google_token
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
        form = wx.Dialog(dialog, title='Add connection' if current is None else 'Edit connection', size=(760, 860),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        box = wx.BoxSizer(wx.VERTICAL)
        def field(label, value='', password=False):
            box.Add(wx.StaticText(form, label=label), 0, wx.ALL, 4)
            control = wx.TextCtrl(form, value=value, style=wx.TE_PASSWORD if password else 0)
            control.SetName(label)
            box.Add(control, 0, wx.EXPAND | wx.ALL, 4)
            return control
        name = field('Account name', current.name if current else '')
        box.Add(wx.StaticText(form, label='Adapter'), 0, wx.ALL, 4)
        adapter = wx.Choice(form, choices=list(ADAPTERS))
        adapter.SetStringSelection(current.adapter if current else 'openai-compatible')
        box.Add(adapter, 0, wx.EXPAND | wx.ALL, 4)
        endpoint = field('Official endpoint or explicitly chosen loopback service', current.endpoint if current else 'http://127.0.0.1:8080/v1')
        model = field('Model ID', current.model if current else 'local-model')
        voice = field('Voice ID; blank for language models', current.voice or '' if current else '')
        capability = field('Capabilities: llm.chat, speech.synthesize, music.compose, sfx.generate, translation.text, translation.languages',
                           ','.join(current.capabilities) if current else 'llm.chat')
        secret = field('New credential; API key for Basic v2, OAuth access token for Advanced v3; blank keeps it', password=True)
        token_expiry = field('Advanced v3 token expiry with UTC offset; for example 2026-11-27T15:30:00Z')
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
            elif adapter.GetStringSelection() == 'google-translation-v3':
                endpoint.ChangeValue('https://translation.googleapis.com/v3/projects/PROJECT_NUMBER/locations/global')
                model.ChangeValue('general/nmt')
                capability.ChangeValue('translation.text,translation.languages')
                voice.ChangeValue('')
                local.SetValue(False)
            token_expiry.Enable(adapter.GetStringSelection() == 'google-translation-v3')
            event.Skip()
        adapter.Bind(wx.EVT_CHOICE, adapter_defaults)
        token_expiry.Enable(adapter.GetStringSelection() == 'google-translation-v3')
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
            oauth_saved = bool(secret.GetValue()) and profile.adapter == 'google-translation-v3'
            if oauth_saved:
                profile = save_google_token(store, vault, profile, secret.GetValue(), token_expiry.GetValue(), current=current)
            elif secret.GetValue():
                new_reference = vault.put(profile.id, secret.GetValue())
                profile = replace(profile, credential_ref=new_reference)
            try:
                if not oauth_saved:
                    store.save(profile)
            except Exception:
                if new_reference:
                    vault.delete(profile.id, new_reference)
                raise
            if current and new_reference and current.credential_ref:
                vault.delete(current.id, current.credential_ref)
            secret.ChangeValue('')
            refresh()
            status.ChangeValue('Saved. Credentials are held by Windows Credential Manager. No provider was contacted.' +
                (' OAuth token expiry: ' + token_expiry.GetValue() + '. Re-enter explicitly before expiry; no automatic refresh.' if oauth_saved else ''))
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
            expiry_notice = ''
            if profile.credential_ref:
                record = vault.get(profile.id, profile.credential_ref)
                if profile.adapter == 'google-translation-v3':
                    from datetime import datetime, timezone
                    _, expiry = google_oauth_token(record, profile.endpoint)
                    expiry_notice = ' OAuth expiry: ' + datetime.fromtimestamp(expiry, timezone.utc).isoformat() + '. No automatic refresh.'
                del record
            status.ChangeValue('Local profile and credential configuration is valid. API connectivity, pricing and access were not tested.' + expiry_notice)
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
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    menu = wx.Menu()
    for identity, title, shortcut, handler in [
            ('assistant.local', 'Local assistant...', 'Ctrl+Alt+J', lambda e: show_assistant(frame)),
            ('assistant.templates', 'Named assistant templates...', '',
             lambda e: show_assistant(frame, templates=True)),
            ('assistant.providers', 'AI and API connections...', '', lambda e: show_provider_settings(frame))]:
        item = menu.Append(wx.ID_ANY, title)
        frame.Bind(wx.EVT_MENU, handler, item)
        register_menu_command(frame, CommandSpec(identity, title, 'main', shortcut), item)
    frame.menu_bar.Append(menu, '&Tools')
