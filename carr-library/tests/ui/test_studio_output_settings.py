import ast
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


# CARR-A11Y-014: execute the actual render/jobs closures with controlled
# dialogs and production receipts. Native rendering and NVDA are separate.
class StudioOptionControl:
    def __init__(self, value):
        self.value = value
    def GetSelection(self):
        return self.value
    def GetValue(self):
        return self.value
    def SetSelection(self, value):
        self.value = value
    SetValue = ChangeValue = SetSelection


def studio_output_fixture(project=None, store=None, *, service=None, output_path='/synthetic'):
    N = SimpleNamespace
    rows, calls, reports, decisions, prompts, saves, entries = [], [], [], [], [], [], []
    if store is None:
        project = N(id='p', original_script='test', title='Test', to_dict=lambda: {'title': 'Test'})
        project_type = N(from_dict=lambda _: project)
    else:
        from carr_library.studio.project import Project
        project_type = Project

    class Dialog:
        def __init__(self, *args):
            prompts.append(args[1])
            if len(args) > 3 and isinstance(args[3], (list, tuple)):
                entries.extend(args[3])
        def ShowModal(self):
            return decisions.pop(0) if decisions else 1
        def GetPath(self):
            return str(output_path)
        def GetSelection(self):
            return 0
        def Destroy(self):
            pass

    def submit(p, path, **kwargs):
        if store is not None:
            store.save(p)
        row = dict(id=str(len(rows)), project=p.to_dict(), status='complete',
                   progress=100, error='', **kwargs)
        rows.append(row)
        return row['id']

    def run_job(identity, **kwargs):
        calls.append(identity)
        return {'destination': 'synthetic'}

    if service is None:
        service = N(submit=submit, run=run_job, get=lambda i: rows[int(i)],
                    jobs=lambda: rows, projects=store if store is not None else N(load=lambda i: project),
                    restored_inputs_to_review=lambda identity: ())
    editor = StudioOptionControl(project.original_script)
    title = StudioOptionControl(project.title)
    if store is None:
        controller = N(project=project)
    else:
        from carr_library.ui.studio_dialog import StudioController
        controller = StudioController(N(root=store.root.parent), service=service)
        controller.project = project

    def save():
        saves.append(project)
        if store is not None:
            return controller.save(editor.GetValue(), title.GetValue(), ('test:voice',))
        return project

    env = dict(wx=N(DirDialog=Dialog, SingleChoiceDialog=Dialog, MessageDialog=Dialog,
                    ID_OK=1, ID_YES=2, YES_NO=4, NO_DEFAULT=8, ICON_QUESTION=16),
               Path=Path, dialog=None, service=service, save=save,
               format_choice=StudioOptionControl(0), stems=StudioOptionControl(True),
               formats=['wav', 'flac'], cancel=N(is_set=lambda: False),
               report=lambda *a: reports.append(a), Project=project_type,
               controller=controller, editor=editor, title=title, controls=lambda: None,
               replace=replace, uuid4=uuid4, selected_voices=lambda: ('test:voice',),
               run=lambda op, done, **kw: done(op(lambda _: None)),
               rows=rows, calls=calls, reports=reports, decisions=decisions,
               prompts=prompts, saves=saves, entries=entries)
    from carr_library.ui import studio_dialog
    source = Path(studio_dialog.__file__).read_text(encoding='utf-8')
    methods = source[source.index('    def render(event=None):'):
                     source.index('    def preview(event=None):')]
    exec('def factory():\n    current_job = None\n    busy = False\n'
         + '    draft_dirty = False\n    saved_signature = None\n'
         + methods + '    return render, jobs\n', env)
    env['render'], env['jobs'] = env['factory']()
    return N(**env)


def test_studio_unchanged_output_reuses_job():
    f = studio_output_fixture()
    f.render()
    f.render()
    assert f.calls == ['0', '0'] and len(f.rows) == 1


@pytest.mark.parametrize('option', ['format', 'stems'])
def test_studio_changed_output_creates_explicit_new_job(option):
    f = studio_output_fixture()
    f.render()
    old = f.rows[0].copy()
    if option == 'format':
        f.format_choice.SetSelection(1)
    else:
        f.stems.SetValue(False)
    f.render()
    assert len(f.rows) == 2 and f.calls == ['0', '1'] and f.rows[0] == old
    assert f.rows[1]['format'] == ('flac' if option == 'format' else 'wav')
    assert f.rows[1]['stems'] == (option == 'format')
    assert 'NEW production job' in f.prompts[-1]


def test_studio_saved_job_restores_output_settings():
    f = studio_output_fixture()
    f.format_choice.SetSelection(1)
    f.stems.SetValue(False)
    f.render()
    f.format_choice.SetSelection(0)
    f.stems.SetValue(True)
    f.jobs()
    assert (f.format_choice.GetSelection(), f.stems.GetValue()) == (1, False)
    assert 'Final format: flac; separate stems: No' in f.reports[-1][0]


def test_studio_changed_options_cancel_preserves_prior_job_and_settings():
    f = studio_output_fixture()
    f.render()
    f.format_choice.SetSelection(1)
    f.stems.SetValue(False)
    f.decisions.append(0)
    f.render()
    assert len(f.rows) == 1 and f.calls == ['0'] and len(f.saves) == 1
    assert (f.format_choice.GetSelection(), f.stems.GetValue()) == (0, True)
    f.render()
    assert f.calls == ['0', '0']


def test_studio_invalid_format_selection_fails_closed():
    f = studio_output_fixture()
    f.format_choice.SetSelection(-1)
    f.render()
    assert not f.rows and not f.calls and not f.prompts
    assert 'Choose a final audio format' in f.reports[-1][0]


def test_studio_invalid_saved_output_settings_fail_closed():
    f = studio_output_fixture()
    f.render()
    f.rows[0]['format'] = 'unknown'
    f.jobs()
    assert 'unsupported output settings' in f.reports[-1][0]


def test_studio_distinct_job_labels_include_durable_options_and_identity():
    f = studio_output_fixture()
    f.render()
    f.format_choice.SetSelection(1)
    f.stems.SetValue(False)
    f.render()
    f.jobs()
    assert len(f.entries) == 2 and len(set(f.entries)) == 2
    assert 'wav; separate stems: Yes; job 0' in f.entries[0]
    assert 'flac; separate stems: No; job 1' in f.entries[1]


@pytest.mark.parametrize('change', ['none', 'script', 'voice', 'gain', 'sample_rate', 'cue', 'mode'])
def test_studio_new_output_preserves_saved_revision_and_current_project(tmp_path, change):
    from carr_library.studio.project import Cue, Project, ProjectStore, parse_script
    saved = parse_script('Narrator: Original text.', title='Test',
                         voice_ids=('test:voice',), project_id='stable-project')
    saved = replace(saved, sample_rate=44100,
                    scenes=(replace(saved.scenes[0], cues=(Cue('old-cue', 'synthetic.wav'),)),))
    store = ProjectStore(tmp_path / 'projects')
    f = studio_output_fixture(saved, store)
    f.service.submit(saved, tmp_path, format='wav', stems=True)
    latest = saved
    if change == 'script':
        latest = replace(saved, original_script='Narrator: Revised text.',
                         scenes=(replace(saved.scenes[0], lines=(replace(saved.scenes[0].lines[0], text='Revised text.'),)),))
    elif change == 'voice':
        latest = replace(saved, roles=(replace(saved.roles[0], voice_id='test:new'),))
    elif change == 'gain':
        latest = replace(saved, roles=(replace(saved.roles[0], gain_db=-12, pan=.7, rate=3, volume=60),))
    elif change == 'sample_rate':
        latest = replace(saved, sample_rate=48000)
    elif change == 'cue':
        latest = replace(saved, scenes=(replace(saved.scenes[0], cues=(Cue('new-cue', 'new.wav'),)),))
    elif change == 'mode':
        latest = replace(saved, mode='authored')
    if change != 'none':
        latest = replace(latest, revision=2)
        store.save(latest)
    receipt = deepcopy(f.rows[0])
    raw = (store.directory(saved.id) / 'project.json').read_bytes()
    f.jobs()
    assert f.editor.GetValue() == saved.original_script
    f.format_choice.SetSelection(1)
    f.stems.SetValue(False)
    f.render()
    assert len(f.rows) == 2 and f.calls == ['1'], f.reports
    output_project = Project.from_dict(f.rows[1]['project'])
    assert f.rows[0] == receipt and not f.saves
    assert (f.rows[1]['format'], f.rows[1]['stems']) == ('flac', False)
    assert (store.directory(saved.id) / 'project.json').read_bytes() == raw
    assert store.load(saved.id) == latest
    if change == 'none':
        assert output_project == saved
        assert len(store.list()) == 1
    else:
        assert output_project.id != saved.id and output_project.revision == 1
        assert replace(output_project, id=saved.id, revision=saved.revision) == saved
        assert store.load(output_project.id) == output_project and len(store.list()) == 2
    assert f.controller.project == output_project


@pytest.mark.parametrize('approved', [False, True])
def test_studio_new_output_keeps_explicit_input_approval_before_worker(approved):
    f = studio_output_fixture()
    f.render()
    pending, events = ('selected-local.wav',), []
    f.service.restored_inputs_to_review = lambda identity: pending
    def approve(paths, *, cancelled):
        assert paths == pending and not cancelled()
        events.append('approve')
    original_run = f.service.run
    def run(identity, **kwargs):
        events.append('run')
        return original_run(identity, **kwargs)
    f.service.approve_restored_inputs = approve
    f.service.run = run
    f.format_choice.SetSelection(1)
    f.decisions.extend([f.wx.ID_OK, f.wx.ID_YES if approved else 0])
    f.render()
    assert len(f.rows) == 2 and f.rows[0]['format'] == 'wav'
    assert f.rows[1]['format'] == 'flac'
    assert f.calls == (['0', '1'] if approved else ['0'])
    assert events == (['approve', 'run'] if approved else [])
    assert any('need approval on this machine' in prompt for prompt in f.prompts)


@pytest.mark.parametrize('approved', [False, True])
def test_studio_restored_job_keeps_new_output_directory_review(approved):
    f = studio_output_fixture()
    f.render()
    f.rows[0]['recovery'] = {'requires_output_directory': True}
    moved = []
    def relocate(identity, directory):
        moved.append((identity, directory))
        f.rows[int(identity)]['recovery']['requires_output_directory'] = False
    f.service.relocate_restored_job = relocate
    f.decisions.append(f.wx.ID_OK if approved else 0)
    f.render()
    assert len(f.rows) == 1
    assert f.calls == (['0', '0'] if approved else ['0'])
    assert moved == ([('0', Path('/synthetic'))] if approved else [])
    assert f.rows[0]['recovery']['requires_output_directory'] is (not approved)
    assert 'restored production job' in f.prompts[-1]


@pytest.mark.parametrize('invalid', [0, 'False'])
def test_studio_non_boolean_saved_stems_are_not_adopted(invalid):
    f = studio_output_fixture()
    f.render()
    f.rows[0]['stems'] = invalid
    f.format_choice.SetSelection(1)
    f.jobs()
    assert 'unsupported output settings' in f.reports[-1][0]
    assert f.format_choice.GetSelection() == 1 and f.stems.GetValue() is True
    f.render()
    assert len(f.rows) == 1 and f.calls == ['0']
    assert 'unsupported output settings' in f.reports[-1][0]


def test_studio_output_controls_follow_the_actual_busy_owner():
    from carr_library.ui import studio_dialog
    source = Path(studio_dialog.__file__).read_text(encoding='utf-8')
    owners = [node for node in ast.parse(source).body
              if isinstance(node, ast.FunctionDef) and node.name == 'show_studio']
    assert len(owners) == 1
    functions = [node for node in owners[0].body
                 if isinstance(node, ast.FunctionDef) and node.name == 'controls']
    assert len(functions) == 1
    class Control(StudioOptionControl):
        def Enable(self, value):
            self.enabled = value
    buttons = {name: Control(0) for name in
               ('render', 'open', 'roles', 'jobs', 'pause', 'preview', 'progress_voice')}
    env = dict(busy=True, current_job='saved', buttons=buttons,
               voices=(SimpleNamespace(capabilities={'volume'}),),
               voice_choice=Control(0), editor=Control('text'), title=Control('title'),
               format_choice=Control(0), stems=Control(True))
    exec(compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])),
                 studio_dialog.__file__, 'exec'), env)
    env['controls']()
    assert env['format_choice'].enabled is False and env['stems'].enabled is False
    assert buttons['render'].enabled is False and buttons['pause'].enabled is True
    env['busy'] = False
    env['controls']()
    assert env['format_choice'].enabled is True and env['stems'].enabled is True
    assert buttons['render'].enabled is True and buttons['pause'].enabled is False


@pytest.mark.parametrize('approved', [False, True])
def test_studio_changed_restored_options_use_real_pending_input_admission(tmp_path, monkeypatch, approved):
    import wave
    from hashlib import sha256
    from carr_library.durable_state import atomic_json
    from carr_library.studio.backup_inputs import _waiting_job, input_review
    from carr_library.studio.project import Cue, parse_script
    from carr_library.studio.render import ProductionService

    selected = tmp_path / 'selected'
    selected.mkdir()
    source = selected / 'clip.wav'
    with wave.open(str(source), 'wb') as output:
        output.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        output.writeframes(b'\x00\x00' * 80)
    original = source.read_bytes()
    outputs = tmp_path / 'outputs'
    outputs.mkdir()
    inventory = SimpleNamespace(voices=lambda: (SimpleNamespace(id='test:voice'),))
    service = ProductionService(tmp_path / 'state' / 'studio', voices=inventory)
    project = parse_script('Narrator: Retained source.', title='Restored source',
                           voice_ids=('test:voice',))
    project = replace(project, scenes=(replace(project.scenes[0],
                      cues=(Cue('selected-clip', str(source)),)),))
    reads, events = [], []
    original_open = Path.open
    allowed = False

    def observed_open(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get('mode', 'r')
        if path == source and 'r' in mode:
            assert allowed, 'An unapproved local cue was read before explicit review.'
            reads.append(path)
        return original_open(path, *args, **kwargs)

    real_approve = service.approve_restored_inputs

    def approve(paths, *, cancelled):
        nonlocal allowed
        assert paths == (str(source),) and not reads and not cancelled()
        events.append('approve')
        allowed = True
        return real_approve(paths, cancelled=cancelled)

    def inspected_run(identity, *, cancelled, progress):
        assert allowed and not cancelled()
        state = service.get(identity)
        assert (state['format'], state['stems']) == ('flac', False)
        assert service._cue_source(project.scenes[0].cues[0]) == source
        events.append(('run', identity))
        return {'destination': state['destination']}

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'open', observed_open)
            old_job = service.submit(project, outputs, format='wav', stems=True)
            assert input_review(service.root)['pending'] == [str(source)] and not reads
            restored = _waiting_job(service.get(old_job), project, project, retained=False)
            receipt = service.jobs_root / old_job / 'job.json'
            atomic_json(receipt, restored, replace=True)
            original_receipt = receipt.read_bytes()
            patch.setattr(service, 'approve_restored_inputs', approve)
            patch.setattr(service, 'run', inspected_run)
            f = studio_output_fixture(project, service.projects, service=service, output_path=outputs)
            f.jobs()
            f.format_choice.SetSelection(1)
            f.stems.SetValue(False)
            f.decisions.extend([f.wx.ID_OK, f.wx.ID_YES if approved else 0])
            f.render()

            rows = service.jobs()
            assert len(rows) == 2, f.reports
            fresh = next(row for row in rows if row['id'] != old_job)
            assert (fresh['format'], fresh['stems']) == ('flac', False)
            assert fresh['project'] == json.loads(json.dumps(project.to_dict()))
            assert fresh['status'] == 'pending'
            assert receipt.read_bytes() == original_receipt
            assert service.get(old_job)['recovery']['requires_output_directory'] is True
            assert not f.saves
            assert any('need approval on this machine' in prompt for prompt in f.prompts)
            if approved:
                assert reads and events == ['approve', ('run', fresh['id'])]
                assert input_review(service.root)['approved'][str(source)] == {
                    'size_bytes': len(original), 'sha256': sha256(original).hexdigest()}
                assert input_review(service.root)['pending'] == []
            else:
                assert not reads and not events
                assert input_review(service.root)['pending'] == [str(source)]
                assert str(source) not in input_review(service.root)['approved']
    finally:
        service.close()
