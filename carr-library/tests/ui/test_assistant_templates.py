"""Execute real dialog handlers; native controls/NVDA remain separate gates."""
import ast
from concurrent.futures import Future
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Scope
from carr_library.assistant.templates import TemplateStore
from carr_library.ui import assistant_dialog


class Control:
    def __init__(self, value=''):
        self.value, self.enabled = value, True
        self.focused = False
    def GetValue(self):
        return self.value
    def ChangeValue(self, value):
        self.value = value
    def Enable(self, value=True):
        self.enabled = value
    def Disable(self):
        self.enabled = False
    def IsEnabled(self):
        return self.enabled
    def SetFocus(self):
        self.focused = True


def harness(tmp_path, *, busy=False, approved=True, saved_job=False):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    store = TemplateStore(service.store.directory)
    store.save('Saved instruction', 'List libraries')
    names = ('preview', 'run', 'jobs', 'pause', 'template_save', 'template_load', 'template_delete')
    plan = service.preview('List libraries', Scope()) if approved else None
    env = SimpleNamespace(busy=busy, alive=True, plan=plan,
        job_id=service.submit(plan) if saved_job else None,
        buttons={name: Control() for name in names}, templates=store, service=service,
        instruction=Control('Old instruction'), scope_choice=Control('current library'),
        input_folder=Control('current input folder'), output_folder=Control('current output folder'),
        full_backup=Control(False), dialog=object(), cancel=Event(), messages=[], submitted=[],
        picker_count=0, confirmation=1, future=Future())
    class Picker:
        def __init__(self, *args, **kwargs):
            env.picker_count += 1
        def ShowModal(self):
            return 1
        def GetSelection(self):
            return 0
        def GetValue(self):
            return 'New instruction'
        def Destroy(self):
            pass
    env.wx = SimpleNamespace(SingleChoiceDialog=Picker, TextEntryDialog=Picker,
        MessageBox=lambda *args: env.confirmation, ID_OK=1, YES=1, YES_NO=2, NO_DEFAULT=4,
        CallAfter=lambda callback, *args: callback(*args))
    def submit(*args, **kwargs):
        env.submitted.append((args, kwargs))
        return env.future
    env.executor = SimpleNamespace(submit=submit)
    source = ast.parse(Path(assistant_dialog.__file__).read_text(encoding='utf-8'))
    show = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == 'show_assistant')
    required = {'invalidate', 'choose_template', 'save_template', 'run'}
    handlers = [node for node in show.body if isinstance(node, ast.FunctionDef) and node.name in required]
    assert {node.name for node in handlers} == required
    factory = ast.parse('''def create(env):
    busy, alive, plan, job_id = env.busy, env.alive, env.plan, env.job_id
    buttons, templates, service = env.buttons, env.templates, env.service
    instruction, scope_choice = env.instruction, env.scope_choice
    input_folder, output_folder, full_backup = env.input_folder, env.output_folder, env.full_backup
    dialog, cancel, executor, wx = env.dialog, env.cancel, env.executor, env.wx
    report = env.messages.append
    def state():
        return busy, plan, job_id
''').body[0]
    factory.body.extend(handlers)
    factory.body.append(ast.parse('return SimpleNamespace(load=choose_template, save=save_template, run=run, state=state)').body[0])
    namespace = {'SimpleNamespace': SimpleNamespace, 'json': json}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[])),
                 str(Path(assistant_dialog.__file__)), 'exec'), namespace)
    return namespace['create'](env), env


def test_loading_clears_both_prior_authorities_without_job_creation_or_scope_change(tmp_path):
    actions, env = harness(tmp_path, saved_job=True)
    before_jobs = env.service.jobs()
    scopes = [control.GetValue() for control in (env.scope_choice, env.input_folder, env.output_folder, env.full_backup)]
    assert actions.state()[1:] == (env.plan, env.job_id)
    actions.load()
    assert actions.state()[1:] == (None, None)
    assert not env.buttons['run'].IsEnabled()
    assert env.instruction.GetValue() == 'List libraries' and env.instruction.focused
    assert scopes == [control.GetValue() for control in (env.scope_choice, env.input_folder, env.output_folder, env.full_backup)]
    actions.run()  # Even a directly delivered event cannot reuse the old plan/job.
    assert env.service.jobs() == before_jobs and env.submitted == []
    assert 'Preview plan' in env.messages[-1]


def test_running_job_disables_all_template_actions_and_callbacks_refuse_changes(tmp_path):
    actions, env = harness(tmp_path)
    actions.run()
    identity = actions.state()[2]
    assert len(env.submitted) == 1
    assert env.service.jobs()[0].id == identity
    for name in ('template_save', 'template_load', 'template_delete'):
        assert not env.buttons[name].IsEnabled()
    before = (env.instruction.GetValue(), env.templates.path.read_bytes(), env.picker_count)
    actions.load()
    actions.load(deleting=True)
    actions.save()
    assert (env.instruction.GetValue(), env.templates.path.read_bytes(), env.picker_count) == before
    env.future.set_result(env.service.resume(identity))
    assert not actions.state()[0]
    for name in ('template_save', 'template_load', 'template_delete'):
        assert env.buttons[name].IsEnabled()


def test_save_and_confirmed_delete_change_only_template_text(tmp_path):
    actions, env = harness(tmp_path, approved=False)
    env.instruction.ChangeValue('An unfinished instruction; do not execute it.')
    actions.save()
    assert env.templates.load('New instruction') == env.instruction.GetValue()
    assert env.service.jobs() == () and env.submitted == []
    before = env.templates.path.read_bytes()
    env.confirmation = 0
    actions.load(deleting=True)
    assert env.templates.path.read_bytes() == before
    env.confirmation = 1
    actions.load(deleting=True)
    assert [item['name'] for item in env.templates.list()] == ['Saved instruction']
    assert env.service.jobs() == () and env.submitted == []
