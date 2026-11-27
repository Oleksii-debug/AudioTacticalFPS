from pathlib import Path
from types import SimpleNamespace

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


def studio_output_fixture():
    N = SimpleNamespace
    rows, calls, reports, decisions, prompts, saves, entries = [], [], [], [], [], [], []
    project = N(id='p', original_script='test', title='Test')

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
        row = dict(id=str(len(rows)), project={'title': 'Test'}, status='complete',
                   progress=100, error='', **kwargs)
        rows.append(row)
        return row['id']

    def run_job(identity, **kwargs):
        calls.append(identity)
        return {'destination': 'synthetic'}

    service = N(submit=submit, run=run_job, get=lambda i: rows[int(i)],
                jobs=lambda: rows, projects=N(load=lambda i: project))

    def save():
        saves.append(project)
        return project

    env = dict(wx=N(DirDialog=Dialog, SingleChoiceDialog=Dialog, ID_OK=1),
               Path=Path, dialog=None, service=service, save=save,
               format_choice=StudioOptionControl(0), stems=StudioOptionControl(True),
               formats=['wav', 'flac'], cancel=N(is_set=lambda: False),
               report=lambda *a: reports.append(a), Project=N(from_dict=lambda _: project),
               controller=N(project=project), editor=StudioOptionControl('test'),
               title=StudioOptionControl('Test'), controls=lambda: None,
               run=lambda op, done, **kw: done(op(lambda _: None)),
               rows=rows, calls=calls, reports=reports, decisions=decisions,
               prompts=prompts, saves=saves, entries=entries)
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/studio_dialog.py'
    source = path.read_text(encoding='utf-8')
    methods = source[source.index('    def render(event=None):'):
                     source.index('    def preview(event=None):')]
    exec('def factory():\n    current_job = None\n    busy = False\n'
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
