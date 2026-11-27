"""One native local assistant, delegating audio to the existing Studio service."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from .assistant_actions import AssistantActions


def show_assistant(frame, *, production=None, local_planner=None, on_ready=None):
    import wx
    from ..assistant import AssistantService, Scope, HELP, Plan, Step
    from ..assistant.production import LocalProduction
    from ..assistant.planner import backup_notice
    production = production or LocalProduction(frame.library.root)
    service = AssistantService(frame.library, production=production)
    actions = AssistantActions(frame, service=service)
    from .local_planner_actions import LocalPlanningActions
    model_actions = LocalPlanningActions(actions, planner=local_planner)
    dialog = wx.Dialog(frame, title='Local assistant — review and run a bounded plan', size=(900, 850),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-assistant')
    alive, busy, plan, job_id = True, False, None, None
    window_alive = True
    measured = None
    owned_work = None
    workspaces = frame.library.workspaces()

    def dispose(window):
        try:
            if window and not window.IsBeingDeleted():
                window.Destroy()
        except RuntimeError:
            pass  # Its native parent may already have destroyed this wrapper.

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
    for label, target in [('Choose input folder', input_folder), ('Choose output folder', output_folder)]:
        button = wx.Button(dialog, label=label)
        def choose(event, target=target):
            if not alive or busy or not actions.current():
                return
            generation = actions.generation
            picker = wx.DirDialog(dialog, 'Choose a local folder')
            try:
                if picker.ShowModal() == wx.ID_OK and alive and actions.current(generation):
                    target.SetValue(picker.GetPath())
            finally:
                dispose(picker)
        button.Bind(wx.EVT_BUTTON, choose)
        choose_row.Add(button, 0, wx.ALL, 4)
    layout.Add(choose_row)
    full_backup = wx.CheckBox(dialog, label='Allow a complete backup of all my libraries for this plan')
    full_backup.SetName('Allow a complete backup of all libraries for this plan')
    layout.Add(full_backup, 0, wx.ALL, 5)
    budget = field('Per-operation output and audio working-disk budget (MiB)', value='1024')
    file_budget = field('Maximum files per operation', value='5000')
    deadline = field('Time budget for each run or resume (seconds)', value='3600')
    instruction = field('&Goal — guided English/Ukrainian commands or typed JSON plan', proportion=1,
                        style=wx.TE_MULTILINE, value='Покажи бібліотеки')
    review = field('Plan &preview and actual results', proportion=1, style=wx.TE_MULTILINE | wx.TE_READONLY,
                   value='LOCAL ONLY. Nothing executes until you review the plan and choose Run.')
    row = wx.BoxSizer(wx.HORIZONTAL)
    buttons = {}
    for identity, label in [('preview', '&Preview plan'), ('model', 'Review local &model...'),
                             ('run', '&Run / resume'), ('pause', '&Pause'),
                             ('jobs', 'Saved &jobs'), ('help', '&Help'), ('close', '&Close')]:
        button = wx.Button(dialog, label=label)
        button.SetName(label.replace('&', ''))
        buttons[identity] = button
        row.Add(button, 0, wx.ALL, 4)
    layout.Add(row)
    dialog.SetSizer(layout)
    buttons['run'].Disable()
    buttons['pause'].Disable()

    def report(value):
        if alive and window_alive and actions.current():
            review.ChangeValue(str(value))

    def post(callback):
        if alive and actions.current():
            def guarded():
                if alive and actions.current():
                    callback()
            try:
                wx.CallAfter(guarded)
            except RuntimeError:
                actions.close()

    def invalidate(event=None):
        nonlocal plan, job_id
        plan, job_id = None, None
        actions.invalidate()
        model_actions.invalidate()
        if alive and window_alive and actions.current():
            buttons['run'].Disable()
            model_button_label()
        if event is not None:
            event.Skip()

    def scope_from_controls():
        index = scope_choice.GetSelection()
        if not 0 <= index <= len(workspaces):
            raise ValueError('Choose the library scope again.')
        return Scope(workspaces[index - 1].id if index > 0 else None,
                     (Path(input_folder.GetValue()),) if input_folder.GetValue().strip() else (),
                     Path(output_folder.GetValue()) if output_folder.GetValue().strip() else None,
                     allow_create=index == 0, allow_full_backup=full_backup.GetValue(),
                     max_bytes=int(budget.GetValue()) * 1024 ** 2,
                     max_files=int(file_budget.GetValue()),
                     max_seconds=float(deadline.GetValue()))

    def model_button_label():
        if alive and window_alive and actions.current():
            label = ('Approve local &model request' if model_actions.review is not None
                     else 'Review local &model...')
            buttons['model'].SetLabel(label)
            buttons['model'].SetName(label.replace('&', ''))
            dialog.Layout()

    def accept_candidate(candidate, *, expected_generation, notice=''):
        nonlocal plan, job_id
        if (not alive or not window_alive or not actions.current(expected_generation)
                or candidate.scope.to_dict() != scope_from_controls().to_dict()):
            raise PermissionError('The reviewed permissions changed; prepare the plan again.')
        scope = candidate.scope
        if any(step.tool == 'audio.produce' and not step.arguments.get('voice_ids') and 'script' not in step.arguments
               for step in candidate.steps):
            voices = tuple(item for item in production.voices() if item.available)
            choice = wx.SingleChoiceDialog(dialog, 'Choose the default local voice for this production.',
                                            'Production voice', [v.name + '; ' + ', '.join(v.languages) for v in voices])
            try:
                if choice.ShowModal() != wx.ID_OK:
                    return False
                if (not alive or not window_alive or not actions.current(expected_generation)
                        or scope.to_dict() != scope_from_controls().to_dict()):
                    raise PermissionError('The reviewed permissions changed while choosing a voice.')
                selected = choice.GetSelection()
            finally:
                dispose(choice)
            if not 0 <= selected < len(voices):
                raise ValueError('Install and choose a local voice before narration.')
            steps = tuple(Step(s.tool, {**s.arguments, 'voice_ids': [voices[selected].id]})
                          if s.tool == 'audio.produce' and 'script' not in s.arguments else s for s in candidate.steps)
            candidate = Plan(scope, steps, candidate.summary)
        service.validate(candidate)
        actions.ensure()
        plan, job_id = actions.accept(candidate), None
        model_actions.invalidate()
        model_button_label()
        report((notice + '\n\n' if notice else '') +
               'LOCAL ONLY — the following operations have not run.\n\n' +
               backup_notice(plan) + '\n\n' +
               json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) +
               '\n\nChoose Run to authorize these exact operations inside these directories.')
        buttons['run'].Enable()
        review.SetFocus()
        return True

    def preview(event=None):
        try:
            if busy or not alive or not window_alive:
                return
            invalidate()
            actions.ensure()
            generation = actions.generation
            candidate = service.preview(instruction.GetValue(), scope_from_controls())
            accept_candidate(candidate, expected_generation=generation)
        except Exception as exc:
            invalidate()
            report(str(exc))
            if alive and window_alive and actions.current():
                review.SetFocus()

    def start_worker(owner, label, phase, completed):
        """One captured future owns progress and cleanup until its worker ends."""
        nonlocal measured, owned_work
        if busy or not alive or not window_alive:
            return False
        observer = None
        try:
            actions.ensure()
            if getattr(frame, 'progress_actions', None) is not None:
                from .operation_progress import OperationProgress
                observer = OperationProgress(frame, label, phase=phase)
            measured = observer
            def progress(generation, done, total, message):
                if observer is not None and owner.current(generation):
                    observer.update(phase=message, completed=done, total=total)
                def deliver():
                    if alive and window_alive and owner.current(generation):
                        report(message if total is None else f'{done}/{total}: {message}')
                post(deliver)
            generation, future = owner.start(executor, progress=progress, set_running=set_running)
        except Exception as exc:
            set_running(False)
            if observer is not None:
                observer.failed(exc)
            measured = None
            report(str(exc))
            model_button_label()
            return False
        owned = object()
        owned_work = owned

        def finished(done):
            # This always runs after the real worker, including withdrawn or
            # destroyed windows. It releases Python state, never native controls.
            accepted = owner.finish(generation)
            if not accepted or not alive or not window_alive:
                if observer is not None:
                    observer.finish(state='cancelled')
            def apply():
                nonlocal measured, owned_work
                if owned_work is not owned:
                    if observer is not None and accepted:
                        observer.finish(state='cancelled')
                    return
                owned_work = None
                if measured is observer:
                    measured = None
                current = alive and window_alive and actions.current()
                if current:
                    set_running(False)
                    model_button_label()
                deliverable = accepted and owner.finish(generation)
                if not current or not deliverable:
                    if observer is not None and accepted:
                        observer.finish(state='cancelled')
                    if current and not deliverable and owner.current(generation):
                        report('The local model request was cancelled. Review a new request before sending again.')
                    return
                publication_generation = actions.generation
                reported = False
                try:
                    result = done.result()
                    completed(result, observer)
                    reported = True
                except Exception as exc:
                    if observer is not None:
                        if actions.cancel_event.is_set():
                            observer.finish(state='cancelled')
                        else:
                            observer.failed(exc)
                    if owned_work is None and actions.current(publication_generation):
                        report(str(exc))
                        reported = True
                if reported and owned_work is None and not busy and alive and window_alive and actions.current():
                    review.SetFocus()
            try:
                wx.CallAfter(apply)
            except RuntimeError:
                if observer is not None and accepted:
                    observer.finish(state='cancelled')
                actions.close()
                model_actions.close()
        future.add_done_callback(finished)
        return True

    def run(event=None):
        nonlocal job_id
        if busy or (plan is None and job_id is None):
            return
        def completed(result, observer):
            if actions.cancel_event.is_set() and result.status == 'complete':
                result = service.get(result.id)
            if observer is not None:
                if result.status == 'complete':
                    observer.complete()
                elif result.status == 'paused':
                    observer.update(phase='Paused; review and resume the saved task', state='paused')
                else:
                    observer.finish(state='cancelled' if result.status == 'cancelled' else 'failed')
            report(f'{result.status}: {result.completed} of {result.total} completed operations.\n' +
                   json.dumps(result.results, ensure_ascii=False, indent=2) + '\n' + result.message)
        if start_worker(actions, 'Local assistant', 'Starting the reviewed plan', completed):
            job_id = actions.job_id

    def model_request(event=None):
        if busy or not alive or not window_alive or not actions.current():
            return
        try:
            selected = model_actions.review
            if selected is None:
                invalidate()
                profiles = model_actions.planner.available_profiles()
                if not profiles:
                    raise ValueError('Add a local OpenAI-compatible loopback profile in Settings > AI and API connections. '
                                     'No model is downloaded or started by this action.')
                generation = actions.generation
                choice = wx.SingleChoiceDialog(dialog, 'Choose the local model already running on this computer.',
                    'Local planning connection', [f'{p.name}; {p.model}; {p.endpoint}' for p in profiles])
                try:
                    if choice.ShowModal() != wx.ID_OK:
                        return
                    if not alive or not window_alive or not actions.current(generation):
                        return
                    index = choice.GetSelection()
                finally:
                    dispose(choice)
                if not 0 <= index < len(profiles):
                    return
                selected = model_actions.prepare(instruction.GetValue(), scope_from_controls(), profiles[index].id)
                report('REVIEW ONE LOCAL MODEL REQUEST — nothing has been sent or executed.\n'
                       'Only the explicit goal and permission strings below are sent; selected files are not read. '
                       'The endpoint is loopback only; the local server controls its own retention and forwarding. '
                       'One request, remote-provider spending budget 0, no cloud fallback or automatic retry.\n\n' +
                       json.dumps(selected.data, ensure_ascii=False, indent=2) +
                       '\n\nChoose Approve local model request to send this exact prompt. '
                       'The returned proposal requires a separate plan review and Run approval.')
                model_button_label()
                buttons['run'].Disable()
                review.SetFocus()
                return
            data = selected.data
            if (data['scope'] != scope_from_controls().to_dict()
                    or data['goal'] != instruction.GetValue()):
                raise PermissionError('The goal or permissions changed; review a new exact local-model request.')
            parent_generation = actions.generation
            def completed(result, observer):
                if (data['scope'] != scope_from_controls().to_dict()
                        or data['goal'] != instruction.GetValue()):
                    raise PermissionError('The goal or permissions changed while the model was responding.')
                notice = ('UNTRUSTED LOCAL MODEL PROPOSAL — no tools executed.\n'
                          f'Model: {result.model}\nEndpoint: {data["endpoint"]}\n'
                          f'Local operation: {result.operation_id}\nRequest SHA256: {result.request_sha256}\n'
                          f'Response SHA256: {result.response_sha256}\n'
                          'Review every tool argument and then choose Run only if you approve this plan.')
                accepted = accept_candidate(result.plan, expected_generation=parent_generation, notice=notice)
                if observer is not None:
                    if accepted:
                        observer.complete()
                    else:
                        observer.finish(state='cancelled')
            start_worker(model_actions, 'Local model plan proposal',
                         'Sending one explicitly approved local request', completed)
        except Exception as exc:
            invalidate()
            report(str(exc))
            if alive and window_alive and actions.current():
                review.SetFocus()

    def set_running(value):
        nonlocal busy
        busy = value
        if not alive or not window_alive or not actions.current():
            return
        for key in ('preview', 'model', 'jobs'):
            buttons[key].Enable(not value)
        buttons['run'].Enable(not value and (plan is not None or job_id is not None))
        buttons['pause'].Enable(value)
        pause_label = 'Cancel &model request' if value and model_actions.busy else '&Pause'
        buttons['pause'].SetLabel(pause_label)
        buttons['pause'].SetName(pause_label.replace('&', ''))
        dialog.Layout()
        for control in (scope_choice, instruction, input_folder, output_folder, full_backup, budget, file_budget, deadline):
            control.Enable(not value)

    def jobs(event=None):
        nonlocal job_id, plan
        if busy or not alive or not actions.current():
            return
        try:
            model_actions.invalidate()
            model_button_label()
            rows = service.jobs()
        except (ValueError, OSError) as exc:
            report(str(exc))
            return
        choice = wx.SingleChoiceDialog(dialog, 'Choose a saved local plan.', 'Assistant jobs',
            [f'{row.id}; {row.status}; {row.completed}/{row.total}' for row in rows])
        try:
            generation = actions.generation
            if (choice.ShowModal() == wx.ID_OK and alive and actions.current(generation)
                    and 0 <= choice.GetSelection() < len(rows)):
                row = rows[choice.GetSelection()]
                record = service.store.load(row.id)
                if record.get('requires_reapproval') or record.get('data_dir') != str(frame.library.root):
                    job_id, plan = None, None
                    actions.invalidate()
                    report(row.message + '\nOriginal approved scope and steps:\n' +
                           json.dumps(record['plan'], ensure_ascii=False, indent=2))
                    buttons['run'].Disable()
                    return
                candidate = actions.review_job(row.id)
                job_id, plan = row.id, candidate
                report(f'{row.status}: {row.completed}/{row.total}. {row.message}\n' +
                       'Review this exact saved plan, then choose Run to resume:\n' +
                       backup_notice(candidate) + '\n\n' + candidate.to_json())
                buttons['run'].Enable()
        except (ValueError, OSError) as exc:
            invalidate()
            report(str(exc))
        finally:
            dispose(choice)

    def close(event=None):
        nonlocal alive
        if not alive:
            return
        actions.close()
        model_actions.close()
        if measured is not None and busy:
            measured.cancel_requested()
        alive = False
        executor.shutdown(wait=False, cancel_futures=True)
        if window_alive and dialog.IsModal():
            dialog.EndModal(wx.ID_CLOSE)

    def destroyed(event):
        nonlocal alive, window_alive
        if event.GetEventObject() is dialog:
            window_alive = alive = False
            actions.close()
            model_actions.close()
            if measured is not None and busy:
                measured.cancel_requested()
            executor.shutdown(wait=False, cancel_futures=True)
        event.Skip()

    def help_text(event):
        from .desktop_app import show_text
        if alive and actions.current():
            show_text(dialog, 'Local assistant commands', HELP)

    def pause(event=None):
        actions.cancel_event.set()
        if measured is not None:
            measured.cancel_requested()

    for identity, callback in {'preview': preview, 'model': model_request, 'run': run, 'pause': pause,
                               'jobs': jobs, 'help': help_text, 'close': close}.items():
        buttons[identity].Bind(wx.EVT_BUTTON, callback)
    for control in (input_folder, output_folder, instruction, budget, file_budget, deadline):
        control.Bind(wx.EVT_TEXT, invalidate)
    scope_choice.Bind(wx.EVT_CHOICE, invalidate)
    full_backup.Bind(wx.EVT_CHECKBOX, invalidate)
    dialog.Bind(wx.EVT_CLOSE, close)
    dialog.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
    try:
        if on_ready is not None:
            controls = {'instruction': instruction, 'review': review,
                'budget': budget, 'deadline': deadline, 'preview': buttons['preview'],
                'run': buttons['run'], 'close': buttons['close'], 'output': output_folder,
                'actions': actions, 'model_actions': model_actions, 'model': buttons['model'],
                'pause': buttons['pause'], 'scope': scope_choice, 'input': input_folder,
                'full_backup': full_backup, 'file_budget': file_budget}
            post(lambda: on_ready(dialog, controls))
        dialog.ShowModal()
    finally:
        if alive:
            close()
        if window_alive:
            dialog.Destroy()


def install_assistant_actions(frame):
    """Add one action; existing API settings remain in their current host."""
    import wx
    existing = getattr(frame, '_assistant_menu_item', None)
    if existing is not None:
        return existing
    menu_bar = frame.GetMenuBar()
    index = menu_bar.FindMenu('Tools')
    if index == wx.NOT_FOUND:
        menu = wx.Menu()
        menu_bar.Append(menu, '&Tools')
    else:
        menu = menu_bar.GetMenu(index)
    item = menu.Append(wx.ID_ANY, 'Local assistant...\tCtrl+Alt+J')
    frame.Bind(wx.EVT_MENU, lambda event: show_assistant(frame), item)
    from .command_registry import CommandSpec
    from .keyboard_settings import register_menu_command
    register_menu_command(frame, CommandSpec('assistant.open', 'Local assistant',
                                            default_shortcut='Ctrl+Alt+J'), item)
    frame._assistant_menu_item = item
    return item
