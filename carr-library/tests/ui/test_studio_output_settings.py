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


def studio_output_fixture(project=None, store=None):
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
            if len(args) > 3:
                entries.extend(args[3])
        def ShowModal(self):
            return decisions.pop(0) if decisions else 1
        def GetPath(self):
            return '/synthetic'
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

    service = N(submit=submit, run=run_job, get=lambda i: rows[int(i)],
                jobs=lambda: rows, projects=store if store is not None else N(load=lambda i: project))
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

    env = dict(wx=N(DirDialog=Dialog, SingleChoiceDialog=Dialog, ID_OK=1),
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
