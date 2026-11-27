"""One native local assistant, delegating audio to the existing Studio service."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading


def show_assistant(frame, *, production=None, on_ready=None):
    import wx
    from ..assistant import AssistantService, Scope, HELP, Plan, Step
    from ..assistant.production import LocalProduction
    cancel = threading.Event()
    dialog = wx.Dialog(frame, title='Local assistant — review and run a bounded plan', size=(900, 850),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    layout = wx.BoxSizer(wx.VERTICAL)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='local-assistant')
    alive, busy, plan, job_id = True, False, None, None
    busy_kind, revision, approved_inputs = None, 0, None
    progress = getattr(frame, 'operation_progress', None)
    progress_token = None
    bound_library = frame.library
    bound_root = Path(bound_library.root).absolute()
    root_info = bound_root.stat()
    root_identity = root_info.st_dev, root_info.st_ino
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
        def choose(event, target=target):
            picker = wx.DirDialog(dialog, 'Choose a local folder')
            try:
                if picker.ShowModal() == wx.ID_OK:
                    target.SetValue(picker.GetPath())
            finally:
                picker.Destroy()
        button.Bind(wx.EVT_BUTTON, choose)
        choose_row.Add(button, 0, wx.ALL, 4)
        folder_buttons.append(button)
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
    for identity, label in [('preview', '&Preview plan'), ('model', 'Propose with local &model'),
                             ('run', '&Run / resume'), ('pause', '&Pause'),
                             ('jobs', 'Saved &jobs'), ('help', '&Help'), ('close', '&Close')]:
        button = wx.Button(dialog, label=label)
        button.SetName(label.replace('&', ''))
        buttons[identity] = button
        row.Add(button, 0, wx.ALL, 4)
    layout.Add(row)
    schedule_row = wx.BoxSizer(wx.HORIZONTAL)
    for identity, label in (('schedule', 'Schedule reviewed plan...'),
                            ('schedules', 'Local schedules...'),
                            ('disarm', 'Disarm this window')):
        button = wx.Button(dialog, label=label)
        button.SetName(label)
        buttons[identity] = button
        schedule_row.Add(button, 0, wx.ALL, 4)
    layout.Add(schedule_row)
    buttons['schedule'].Disable()
    buttons['disarm'].Disable()
    dialog.SetSizer(layout)
    buttons['run'].Disable()
    buttons['pause'].Disable()

    def report(value):
        if alive:
            review.ChangeValue(str(value))

    def library_current():
        try:
            info = bound_root.stat()
            return (frame.library is bound_library and Path(bound_library.root).absolute() == bound_root
                    and (info.st_dev, info.st_ino) == root_identity
                    and not getattr(frame, '_closing', False))
        except (OSError, ValueError, RuntimeError, AttributeError):
            return False

    def stopped():
        return cancel.is_set() or not alive or not library_current()

    production = production or LocalProduction(bound_root, cancelled=stopped)
    service = AssistantService(bound_library, production=production)
    from ..assistant.scheduling import ScheduleManager, ScheduleSession
    schedule_manager = ScheduleManager(service)
    schedule_session = ScheduleSession(schedule_manager)
    schedule_timer = None

    def input_identity():
        return (scope_choice.GetSelection(), input_folder.GetValue(), output_folder.GetValue(),
                full_backup.GetValue(), instruction.GetValue(), budget.GetValue(),
                file_budget.GetValue(), deadline.GetValue())

    def invalidate(event=None):
        nonlocal plan, job_id, approved_inputs, revision
        revision += 1
        plan, job_id, approved_inputs = None, None, None
        if busy_kind in ('preview', 'model'):
            cancel.set()
        buttons['run'].Disable()
        buttons['schedule'].Disable()
        if event is not None:
            event.Skip()

    def change_busy(kind):
        nonlocal busy, busy_kind
        busy_kind, busy = kind, kind is not None
        for key in ('preview', 'model', 'jobs', 'schedules'):
            buttons[key].Enable(not busy)
        buttons['run'].Enable(not busy and (plan is not None or job_id is not None))
        buttons['schedule'].Enable(not busy and (plan is not None or job_id is not None))
        buttons['disarm'].Enable(bool(schedule_session.armed) or kind == 'scheduled')
        buttons['pause'].Enable(busy)
        label = ('Cancel &proposal' if kind == 'model' else
                 'Cancel &preview' if kind == 'preview' else '&Pause')
        buttons['pause'].SetLabel(label)
        buttons['pause'].SetName(label.replace('&', ''))
        for control in (scope_choice, instruction, input_folder, output_folder, full_backup,
                        budget, file_budget, deadline, *folder_buttons):
            control.Enable(not busy)

    def post(callback):
        try:
            wx.CallAfter(callback)
        except RuntimeError:
            # The retained job/ledger owns durable completion, not a destroyed UI.
            pass

    def submit_checked(action):
        # ThreadPoolExecutor may enqueue before a native thread-start failure.
        # That retained item must never execute a mutation after the caller has
        # reported failed admission and a later submission starts a worker.
        admission = threading.Event()
        accepted = [False]
        def admitted_action():
            admission.wait()
            if not accepted[0]:
                raise InterruptedError('Assistant worker admission failed; no operation was dispatched.')
            return action()
        try:
            future = executor.submit(admitted_action)
            accepted[0] = True
            return future
        finally:
            admission.set()

    def selected_scope():
        if not library_current():
            raise ValueError('The library location changed. Reopen the assistant before planning.')
        index = scope_choice.GetSelection()
        if not 0 <= index <= len(workspaces):
            raise ValueError('Choose the library scope.')
        return Scope(workspaces[index - 1].id if index > 0 else None,
                     (Path(input_folder.GetValue()),) if input_folder.GetValue().strip() else (),
                     Path(output_folder.GetValue()) if output_folder.GetValue().strip() else None,
                     allow_create=index == 0, allow_full_backup=full_backup.GetValue(),
                     max_bytes=int(budget.GetValue()) * 1024 ** 2,
                     max_files=int(file_budget.GetValue()),
                     max_seconds=float(deadline.GetValue()))

    def preview(event=None):
        nonlocal plan, job_id, approved_inputs
        if busy:
            return
        invalidate()
        try:
            scope = selected_scope()
            goal = instruction.GetValue()
            selected_inputs, selected_revision = input_identity(), revision
            cancel.clear()
            change_busy('preview')
            report('Checking the proposed operations and configured local voices. Nothing has run.')
            def work():
                if stopped():
                    raise InterruptedError('Plan preview cancelled.')
                # Audio plan validation may discover a cold local runtime. Keep
                # that existing service and voice path off the event thread.
                candidate = service.preview(goal, scope)
                voices = None
                if any(step.tool == 'audio.produce' and not step.arguments.get('voice_ids')
                       and 'script' not in step.arguments for step in candidate.steps):
                    from ..audio.engines import _invoke
                    voices = tuple(item for item in _invoke(production.voices, cancelled=stopped)
                                   if item.available)
                if stopped():
                    raise InterruptedError('Plan preview cancelled.')
                return candidate, voices
            future = submit_checked(work)
        except Exception as exc:
            change_busy(None)
            invalidate()
            report(str(exc))
            review.SetFocus()
            return

        def finish(done):
            def apply():
                nonlocal plan, job_id, approved_inputs
                if not alive:
                    return
                if (stopped() or selected_revision != revision
                        or selected_inputs != input_identity()):
                    change_busy(None)
                    invalidate()
                    report('Preview cancelled or discarded because the goal, scope or library changed. Preview again.')
                    return
                try:
                    candidate, voices = done.result()
                    if voices is not None:
                        if not voices:
                            raise ValueError('Install and choose a local voice before narration.')
                        choice = wx.SingleChoiceDialog(dialog,
                            'Choose the default local voice for this production.', 'Production voice',
                            [v.name + '; ' + ', '.join(v.languages) for v in voices])
                        try:
                            if choice.ShowModal() != wx.ID_OK:
                                change_busy(None)
                                report('Preview cancelled before choosing a voice. Nothing has run.')
                                return
                            selected = choice.GetSelection()
                        finally:
                            choice.Destroy()
                        if not 0 <= selected < len(voices):
                            raise ValueError('Choose an available local voice before narration.')
                        steps = tuple(Step(step.tool, {**step.arguments, 'voice_ids': [voices[selected].id]})
                                      if step.tool == 'audio.produce' and not step.arguments.get('voice_ids')
                                      and 'script' not in step.arguments
                                      else step for step in candidate.steps)
                        candidate = Plan(scope, steps, candidate.summary)
                        def validate_selected():
                            if stopped():
                                raise InterruptedError('Selected-voice preview cancelled.')
                            # Even a custom registry or renewed manifest may
                            # need I/O here. Keep revalidation on this worker.
                            service.validate(candidate)
                            if stopped():
                                raise InterruptedError('Selected-voice preview cancelled.')
                            return candidate, None
                        future = submit_checked(validate_selected)
                        future.add_done_callback(finish)
                        return
                    if (stopped() or selected_revision != revision
                            or selected_inputs != input_identity()):
                        raise ValueError('The goal or selected scope changed. Preview the current inputs again.')
                    plan, job_id = candidate, None
                    approved_inputs = selected_inputs, selected_revision
                    change_busy(None)
                    report('LOCAL ONLY — no remote services or charges.\n\n' +
                           json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) +
                           '\n\nChoose Run to authorize these operations inside these directories.')
                    buttons['run'].Enable()
                except Exception as exc:
                    change_busy(None)
                    invalidate()
                    report(str(exc))
                review.SetFocus()
            post(apply)
        future.add_done_callback(finish)

    def propose(event=None):
        if busy:
            return
        from uuid import uuid4
        from ..assistant.llm import local_plan_request, propose_local_plan
        from ..providers import BudgetLedger, JobApproval, ProfileStore, ProviderGateway
        try:
            scope = selected_scope()
            goal = instruction.GetValue()
            selected_inputs = input_identity()
            request = local_plan_request(goal)
            store = ProfileStore(bound_root / 'api-connections.json')
            profiles = tuple(profile for profile in store.profiles()
                             if profile.local and profile.provider == 'openai-compatible'
                             and profile.capabilities == ('llm',))
            if not profiles:
                raise ValueError('Add an explicit loopback language-model profile in AI and API connections first. '
                                 'No model or service is started automatically.')
            picker = wx.SingleChoiceDialog(dialog,
                'Choose the local service that may receive this goal. Only a proposal is requested.',
                'Local model proposal', [profile.name + '; ' + profile.endpoint + '; ' + profile.model
                                         for profile in profiles])
            try:
                if picker.ShowModal() != wx.ID_OK:
                    return
                index = picker.GetSelection()
            finally:
                picker.Destroy()
            if not 0 <= index < len(profiles):
                raise ValueError('Choose an available local model profile.')
            profile = profiles[index]
            confirmation = wx.MessageDialog(dialog,
                'Send this goal to ' + profile.endpoint + ' using ' + profile.model + '?\n\n'
                'Only the goal and fixed planning instructions are sent. No library or source document is attached. '
                'Returned steps remain unapproved until you preview and run them.',
                'Request a local proposal', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
            try:
                if confirmation.ShowModal() != wx.ID_YES:
                    return
            finally:
                confirmation.Destroy()
            if not library_current() or selected_inputs != input_identity():
                raise ValueError('The goal or selected scope changed before the proposal was sent.')
            approval = JobApproval(uuid4().hex, profile.fingerprint,
                (request.fingerprint(profile),), allow_loopback=True)
            invalidate()
            selected_revision = revision
            cancel.clear()
            change_busy('model')
            report('Waiting for the selected local model. No assistant operation has been approved.')
            operation_id = uuid4().hex
            def work():
                if stopped():
                    raise InterruptedError('Local proposal cancelled before contacting the selected service.')
                gateway = ProviderGateway(BudgetLedger(bound_root / 'api-operations.sqlite'))
                return propose_local_plan(service, gateway, profile, goal, scope,
                    approval=approval, operation_id=operation_id, cancelled=stopped, profile_store=store)
            future = submit_checked(work)
        except Exception as exc:
            change_busy(None)
            invalidate()
            report(str(exc))
            return

        def finish(done):
            def apply():
                if not alive:
                    return
                change_busy(None)
                if (stopped() or selected_revision != revision
                        or selected_inputs != input_identity()):
                    invalidate()
                    report('Local proposal cancelled or discarded because the goal, scope or library changed. Nothing was submitted.')
                    return
                invalidate()
                try:
                    candidate = done.result()
                    draft = {key: value for key, value in candidate.to_dict().items() if key != 'scope'}
                    instruction.ChangeValue(json.dumps(draft, ensure_ascii=False, indent=2))
                    report('Local model proposal received and structurally checked. Nothing has run.\n\n'
                           + json.dumps(draft, ensure_ascii=False, indent=2)
                           + '\n\nReview or edit the goal, then choose Preview plan to bind your current permissions. '
                             'Run remains disabled until that separate preview succeeds.')
                except Exception as exc:
                    report(str(exc))
                review.SetFocus()
            post(apply)
        future.add_done_callback(finish)

    def run(event=None, *, scheduled=False):
        nonlocal busy, job_id, progress_token
        if busy or (not scheduled and plan is None and job_id is None):
            return
        try:
            if not library_current():
                invalidate()
                raise ValueError('The library location changed. Reopen the assistant before running.')
            if not scheduled and plan is not None and approved_inputs != (input_identity(), revision):
                invalidate()
                raise ValueError('The reviewed goal or scope changed. Preview the current inputs again.')
            selected_plan, selected_identity = plan, job_id
            cancel.clear()
            if not scheduled and selected_identity is not None:
                schedule_session.disarm((selected_identity,))
            change_busy('scheduled' if scheduled else 'run')
            label = 'Running one explicitly armed local schedule' if scheduled else 'Running reviewed local plan'
            token = progress.start(label, unit='operations').token if progress is not None else None
            progress_token = token
            def advanced(completed, total, message):
                if progress is not None:
                    progress.update(completed, total, message, token=token)
                def display():
                    if alive and frame.library is bound_library and progress_token == token:
                        report(f'{completed}/{total}: {message}')
                wx.CallAfter(display)
            def work():
                nonlocal job_id
                if stopped():
                    raise InterruptedError('Assistant stopped before submitting its reviewed plan.')
                if scheduled:
                    result = schedule_session.tick(cancelled=stopped, progress=advanced)
                    if result is None:
                        raise InterruptedError('No armed schedule is due. Nothing was dispatched.')
                    return result
                identity = selected_identity
                if identity is None:
                    # submit revalidates the captured plan and may touch local
                    # runtimes or durable state; it belongs on the worker too.
                    identity = service.submit(selected_plan)
                    job_id = identity
                return service.resume(identity, cancelled=stopped, progress=advanced)
            future = submit_checked(work)
        except Exception as exc:
            change_busy(None)
            if progress is not None and progress_token is not None:
                progress.fail(exc, token=progress_token)
            report(str(exc))
            return
        def finish(done):
            def apply():
                nonlocal busy
                # The shared view still receives the durable checkpoint after
                # the dialog closes. Only dialog widgets require its liveness.
                try:
                    result = done.result()
                except Exception as exc:
                    if progress is not None:
                        if isinstance(exc, InterruptedError):
                            progress.cancel(str(exc), token=token)
                        else:
                            progress.fail(exc, token=token)
                else:
                    if progress is not None:
                        progress.finish_result(result, cancelled=cancel.is_set(), token=token)
                if not alive or not library_current() or progress_token != token:
                    return
                change_busy(None)
                try:
                    result = done.result()
                    report(f'{result.status}: {result.completed} of {result.total} verified operations.\n' +
                           json.dumps(result.results, ensure_ascii=False, indent=2) + '\n' + result.message)
                except Exception as exc:
                    report(str(exc))
                review.SetFocus()
            wx.CallAfter(apply)
        future.add_done_callback(finish)

    def schedule_work(label, action, complete):
        """Small metadata operations use the already-owned assistant executor."""
        if busy or not alive:
            return False
        if not library_current():
            report('The library changed. Reopen the assistant to review schedules.')
            return False
        cancel.clear()
        change_busy('schedule-review')
        report(label)
        def work():
            if stopped():
                raise InterruptedError('Schedule action cancelled before publication.')
            return action()
        try:
            future = submit_checked(work)
        except Exception as error:
            change_busy(None)
            report(error)
            return False
        def finish(done):
            def apply():
                if not alive:
                    return
                change_busy(None)
                if not library_current():
                    schedule_session.disarm()
                    change_busy(None)
                    report('The library changed. Saved schedules remain unarmed.')
                    return
                try:
                    result = done.result()
                    if stopped():
                        schedule_session.disarm()
                        if schedule_timer is not None:
                            schedule_timer.Stop()
                        change_busy(None)
                        report('Schedule action stopped. Inspect the retained job before another action.')
                        return
                    complete(result)
                except Exception as error:
                    schedule_session.disarm()
                    if schedule_timer is not None:
                        schedule_timer.Stop()
                    change_busy(None)
                    report(error)
                review.SetFocus()
            post(apply)
        future.add_done_callback(finish)
        return True

    def schedule_current(event=None):
        nonlocal job_id
        if busy or (plan is None and job_id is None):
            return
        selected_plan, selected_id = plan, job_id
        selected_inputs, selected_revision = input_identity(), revision
        if selected_plan is not None and approved_inputs != (selected_inputs, selected_revision):
            invalidate()
            report('Preview the current exact goal and scope before saving a time.')
            return
        def read_review():
            if selected_id is None:
                from ..assistant.schedule_model import fingerprint
                value = selected_plan.to_dict()
                return value, fingerprint(value), None
            record = service.store.load(selected_id)
            from ..assistant.schedule_model import fingerprint
            return record['plan'], record['plan_sha256'], (
                None if 'schedule' not in record else fingerprint(record['schedule']))
        def reviewed(captured):
            if (selected_revision != revision or selected_inputs != input_identity()
                    or selected_id != job_id or selected_plan is not plan):
                report('The selected job or plan changed. Review its time again.')
                return
            from .assistant_schedule import choose_time
            value, plan_sha, previous_sha = captured
            selected_time = choose_time(dialog, value, plan_sha)
            if selected_time is None:
                return
            if (not alive or not library_current() or selected_revision != revision
                    or selected_inputs != input_identity()
                    or selected_id != job_id or selected_plan is not plan):
                report('The selected job or scope changed during review. Nothing was scheduled.')
                return
            due, lateness = selected_time
            def save():
                nonlocal job_id
                identity = selected_id
                if identity is None:
                    identity = service.submit(selected_plan)
                    # Preserve a real committed job even if schedule publication
                    # or a later read fails. It remains available in Saved jobs.
                    job_id = identity
                if stopped():
                    raise InterruptedError('The queued job is retained; schedule saving was cancelled.')
                return schedule_manager.create(identity, due, expected_plan_sha256=plan_sha,
                    expected_schedule_sha256=previous_sha, lateness_seconds=lateness)
            def saved(view):
                report(f'Job {view.id}: saved UTC time {view.due}; expires {view.expires}.\n'
                       'Nothing ran or was armed. Open Local schedules, review the exact plan and explicitly arm it '
                       'for this assistant window. Closing this window disarms it.')
            schedule_work('Saving the reviewed concrete UTC window; no execution.', save, saved)
        schedule_work('Reading the exact saved plan for schedule review.', read_review, reviewed)

    def scheduled_due(event=None):
        if (not alive or busy or getattr(frame, 'working', False)
                or getattr(frame, 'busy', False)):
            return
        if not library_current():
            schedule_session.disarm()
            if schedule_timer is not None:
                schedule_timer.Stop()
            change_busy(None)
            report('The library changed. All foreground schedules were disarmed.')
            return
        try:
            if schedule_session.has_due():
                run(scheduled=True)
        except (OSError, ValueError, RuntimeError) as error:
            schedule_session.disarm()
            change_busy(None)
            report(error)

    def schedules(event=None):
        def reviewed(rows):
            from .assistant_schedule import choose_schedule
            chosen = choose_schedule(dialog, rows, schedule_session.armed)
            if chosen is None or not alive or not library_current():
                return
            action, identity, pin = chosen
            def apply_action():
                if action == 'arm':
                    return schedule_session.arm({identity: pin})
                if action == 'disable':
                    schedule_session.disarm((identity,))
                    return schedule_manager.disable(identity, expected_schedule_sha256=pin)
                raise ValueError('Unknown local schedule action.')
            def applied(value):
                nonlocal schedule_timer
                if action == 'arm':
                    if schedule_timer is None:
                        schedule_timer = wx.Timer(dialog)
                        dialog.Bind(wx.EVT_TIMER, scheduled_due, source=schedule_timer)
                    schedule_timer.Start(1000)
                    report('Armed for one attempt in this window: ' + ', '.join(value)
                           + '. Closing or Disarm removes this foreground permission.')
                else:
                    report(f'Job {identity}: saved time disabled. No operation was started.')
                change_busy(None)
            schedule_work('Applying the explicitly selected local schedule action.', apply_action, applied)
        schedule_work('Reading saved local schedules and their exact approved plans.', lambda: schedule_manager.review(cancelled=stopped), reviewed)

    def disarm(event=None):
        schedule_session.disarm()
        if schedule_timer is not None:
            schedule_timer.Stop()
        if busy_kind == 'scheduled':
            cancel.set()
        buttons['disarm'].Disable()
        report('This window is disarmed. Any active scheduled operation is stopping through its existing owner; '
               'inspect its saved checkpoint before ordinary Resume.')

    def jobs(event=None):
        nonlocal job_id, plan, approved_inputs
        if busy:
            return
        try:
            if not library_current():
                raise ValueError('The library location changed. Reopen the assistant.')
            rows = service.jobs()
        except (ValueError, OSError) as exc:
            job_id, plan = None, None
            buttons['run'].Disable()
            buttons['schedule'].Disable()
            report(str(exc)[:1000])
            review.SetFocus()
            return
        choice = wx.SingleChoiceDialog(dialog, 'Choose a saved local plan.', 'Assistant jobs',
            [f'{row.id}; {row.status}; {row.completed}/{row.total}' for row in rows])
        try:
            if choice.ShowModal() == wx.ID_OK and 0 <= choice.GetSelection() < len(rows):
                row = rows[choice.GetSelection()]
                try:
                    record = service.store.load(row.id)
                except (ValueError, OSError) as exc:
                    job_id, plan = None, None
                    buttons['run'].Disable()
                    report(row.message + '\n' + str(exc)[:1000])
                    review.SetFocus()
                    return
                if record.get('requires_reapproval') or record.get('data_dir') != str(frame.library.root):
                    job_id, plan = None, None
                    instruction.ChangeValue(service.review_instruction(row.id))
                    report(row.message + '\nOriginal approved scope and steps:\n' +
                           json.dumps(record['plan'], ensure_ascii=False, indent=2) +
                           '\n\nThe goal field contains an unapproved copy of the original steps. '
                           'Choose the current library and input/output folders, edit path arguments '
                           'if necessary, then choose Preview and Run to approve a new job. '
                           'The saved job and its original permissions remain unchanged.')
                    buttons['run'].Disable()
                    buttons['schedule'].Disable()
                    review.SetFocus()
                    return
                job_id, plan, approved_inputs = row.id, None, None
                report(f'{row.status}: {row.completed}/{row.total}. {row.message}\nResume uses the original reviewed scope.')
                change_busy(None)
        finally:
            choice.Destroy()

    def pause(event=None):
        cancel.set()
        if busy_kind in ('schedule-review', 'scheduled'):
            schedule_session.disarm()
            if schedule_timer is not None:
                schedule_timer.Stop()

    def close(event=None):
        nonlocal alive
        if not alive:
            return
        alive = False
        cancel.set()
        schedule_session.close()
        if schedule_timer is not None:
            schedule_timer.Stop()
        if progress is not None and progress_token is not None:
            progress.request_cancel()
        # The worker persists its paused checkpoint after releasing snapshot
        # locks. A concurrent control write here can be refused by that same
        # snapshot and must not prevent the native dialog from closing.
        executor.shutdown(wait=False, cancel_futures=True)
        if dialog.IsModal():
            dialog.EndModal(wx.ID_CLOSE)
        else:
            dialog.Hide()

    def help_text(event):
        from .desktop_app import show_text
        show_text(dialog, 'Local assistant commands', HELP)

    for identity, callback in {'preview': preview, 'model': propose, 'run': run, 'pause': pause,
                               'jobs': jobs, 'help': help_text, 'close': close,
                               'schedule': schedule_current, 'schedules': schedules, 'disarm': disarm}.items():
        buttons[identity].Bind(wx.EVT_BUTTON, callback)
    for control in (input_folder, output_folder, instruction, budget, file_budget, deadline):
        control.Bind(wx.EVT_TEXT, invalidate)
    scope_choice.Bind(wx.EVT_CHOICE, invalidate)
    full_backup.Bind(wx.EVT_CHECKBOX, invalidate)
    dialog.Bind(wx.EVT_CLOSE, close)
    try:
        if on_ready is not None:
            wx.CallAfter(on_ready, dialog, {'instruction': instruction, 'review': review,
                'budget': budget, 'deadline': deadline, 'preview': buttons['preview'],
                'run': buttons['run'], 'model': buttons['model'], 'close': buttons['close'], 'output': output_folder,
                'scope': scope_choice, 'input': input_folder, 'full_backup': full_backup,
                'file_budget': file_budget, 'pause': buttons['pause'], 'jobs': buttons['jobs'],
                'folder_buttons': tuple(folder_buttons),
                'schedule': buttons['schedule'], 'schedules': buttons['schedules'],
                'disarm': buttons['disarm']})
        dialog.ShowModal()
    finally:
        if alive:
            close()
        dialog.Destroy()


def install_assistant_actions(frame):
    """Add one action; existing API settings remain in their current host."""
    import wx
    menu_bar = frame.GetMenuBar()
    index = menu_bar.FindMenu('Tools')
    if index == wx.NOT_FOUND:
        menu = wx.Menu()
        menu_bar.Append(menu, '&Tools')
    else:
        menu = menu_bar.GetMenu(index)
    item = menu.Append(wx.ID_ANY, 'Local assistant...\tCtrl+Alt+J')
    frame.Bind(wx.EVT_MENU, lambda event: show_assistant(frame), item)
