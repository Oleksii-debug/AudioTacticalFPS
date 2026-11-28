from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import threading
from uuid import uuid4

import pytest


# CARR-A11Y-014: execute the actual render/jobs closures with controlled
# dialogs and production receipts. Native rendering and NVDA are separate.
class StudioOptionControl:
    def __init__(self, value):
        self.value = value
        self.enabled = True
    def Enable(self, enabled=True):
        self.enabled = bool(enabled)
    def SetLabel(self, label):
        self.label = label
    def SetName(self, name):
        self.name = name
    def GetSelection(self):
        return self.value
    def GetValue(self):
        return self.value
    def SetSelection(self, value):
        self.value = value
    SetValue = ChangeValue = SetSelection


def studio_output_fixture(project=None, store=None):
    N = SimpleNamespace
    rows, calls, reports, decisions, prompts, saves, entries, hooks = [], [], [], [], [], [], [], []
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
            if hooks:
                hooks.pop(0)()
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

    library = N(root=store.root.parent if store is not None else Path('/synthetic-library'))
    frame = N(library=library, _closing=False)
    buttons = {name: StudioOptionControl(None) for name in
               ('render', 'open', 'roles', 'jobs', 'pause', 'preview', 'progress_voice', 'inspect_silence')}
    voice_choice = StudioOptionControl(-1)
    env = dict(wx=N(DirDialog=Dialog, SingleChoiceDialog=Dialog, ID_OK=1),
               Path=Path, dialog=None, service=service, save=save,
               format_choice=StudioOptionControl(0), stems=StudioOptionControl(True),
               formats=['wav', 'flac'], cancel=threading.Event(),
               frame=frame, save_library=library, save_root=library.root,
               buttons=buttons, voice_choice=voice_choice, voices=(),
               report=lambda *a: reports.append(a), Project=project_type,
               controller=controller, editor=editor, title=title,
               editor_signature=lambda: (editor.GetValue(), title.GetValue(), ('test:voice',)),
               replace=replace, uuid4=uuid4, selected_voices=lambda: ('test:voice',),
               run=lambda op, done, **kw: done(op(lambda _: None)),
               rows=rows, calls=calls, reports=reports, decisions=decisions,
               prompts=prompts, saves=saves, entries=entries, hooks=hooks)
    from carr_library.ui import studio_dialog
    source = Path(studio_dialog.__file__).read_text(encoding='utf-8')
    methods = source[source.index('    def render(event=None):'):
                     source.index('    def preview(event=None):')]
    control_source = source[source.index('    def controls():'):
                            source.index('    def run(operation, complete,')]
    exec('def factory():\n    current_job = None\n    busy = False\n    alive = True\n'
         + '    active_production = True\n'
         + '    draft_dirty = False\n    saved_signature = None\n'
         + control_source + methods
         + '    def lifetime(*, open_=True, working=False):\n'
         + '        nonlocal alive, busy\n        alive, busy = open_, working\n'
         + '    return render, jobs, controls, lifetime\n', env)
    env['render'], env['jobs'], env['controls'], env['lifetime'] = env['factory']()
    return N(**env, namespace=env)


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



def test_studio_save_selected_historical_job_retains_latest_and_anchored_draft(tmp_path):
    from carr_library.durable_state import read_json
    from carr_library.studio.project import ProjectStore, parse_script
    from carr_library.ui.studio_dialog import StudioController
    saved = parse_script('Narrator: Original.', title='Test',
                         voice_ids=('test:voice',), project_id='stable-project')
    store = ProjectStore(tmp_path / 'projects')
    f = studio_output_fixture(saved, store)
    f.service.submit(saved, tmp_path, format='wav', stems=True)
    latest = replace(saved, revision=2, title='Newer project')
    store.save(latest, expected_revision=1)
    raw = (store.directory(saved.id) / 'project.json').read_bytes()
    f.jobs()
    assert f.controller.project == saved
    assert 'Historical revision selected' in f.reports[-1][0]
    with pytest.raises(ValueError, match='Project changed'):
        f.save()
    assert (store.directory(saved.id) / 'project.json').read_bytes() == raw
    draft = read_json(f.controller.draft)
    assert draft['project_revision'] == 1
    assert draft['project_fingerprint'] == saved.fingerprint
    reopened = StudioController(SimpleNamespace(root=store.root.parent), service=f.service)
    assert 'historical revision' in reopened.restore_draft_project(draft)
    assert reopened.project == saved
    with pytest.raises(ValueError, match='Project changed'):
        reopened.save('Narrator: Edited old draft.', saved.title, ('test:voice',))
    assert read_json(reopened.draft)['text'] == 'Narrator: Edited old draft.'
    assert store.load(saved.id) == latest


def test_studio_legacy_edited_draft_does_not_overwrite_latest(tmp_path):
    from carr_library.studio.project import ProjectStore, parse_script
    saved = parse_script('Narrator: Original.', title='Test',
                         voice_ids=('test:voice',), project_id='stable-project')
    store = ProjectStore(tmp_path / 'projects')
    f = studio_output_fixture(saved, store)
    store.save(saved)
    legacy = {'schema': 1, 'project_id': saved.id, 'text': 'Narrator: Old draft.',
              'title': saved.title, 'voice_ids': ['test:voice']}
    assert 'separate project' in f.controller.restore_draft_project(legacy)
    assert f.controller.project is None
    fresh = f.controller.save(legacy['text'], legacy['title'], legacy['voice_ids'])
    assert fresh.id != saved.id
    assert store.load(saved.id) == saved


def test_studio_current_rich_snapshot_save_is_noop_and_edit_updates_anchor(tmp_path):
    from carr_library.durable_state import read_json
    from carr_library.studio.project import Cue, ProjectStore, parse_script
    saved = parse_script('Narrator: Original.', title='Test',
                         voice_ids=('test:voice',), project_id='stable-project')
    saved = replace(saved, sample_rate=44100,
                    scenes=(replace(saved.scenes[0], cues=(Cue('old-cue', 'synthetic.wav'),)),))
    store = ProjectStore(tmp_path / 'projects')
    f = studio_output_fixture(saved, store)
    store.save(saved)
    assert f.save() == saved
    assert store.load(saved.id) == saved
    f.editor.SetValue('Narrator: Edited.')
    edited = f.save()
    assert edited.id == saved.id and edited.revision == 2
    draft = read_json(f.controller.draft)
    assert draft['project_revision'] == edited.revision
    assert draft['project_fingerprint'] == edited.fingerprint
    assert f.controller.restore_draft_project(draft) == ''
    assert f.controller.project == edited


def test_studio_concurrent_revision_conflict_keeps_draft(tmp_path, monkeypatch):
    from carr_library.durable_state import read_json
    from carr_library.studio.project import ProjectStore, parse_script
    saved = parse_script('Narrator: Original.', title='Test',
                         voice_ids=('test:voice',), project_id='stable-project')
    store = ProjectStore(tmp_path / 'projects')
    f = studio_output_fixture(saved, store)
    store.save(saved)
    actual_save = store.save
    latest = replace(saved, revision=2, title='Concurrent project')
    def competing_save(project, **kwargs):
        actual_save(latest, expected_revision=1)
        return actual_save(project, **kwargs)
    monkeypatch.setattr(store, 'save', competing_save)
    f.editor.SetValue('Narrator: Edited.')
    with pytest.raises(ValueError, match='Project changed'):
        f.save()
    assert store.load(saved.id) == latest
    assert read_json(f.controller.draft)['text'] == 'Narrator: Edited.'
    assert f.controller.project == saved


@pytest.mark.parametrize('handler', ['render', 'jobs'])
@pytest.mark.parametrize('condition', ['closed', 'busy', 'library', 'closing'])
def test_studio_output_handlers_reject_lost_owner_before_dialog_or_store(handler, condition):
    f = studio_output_fixture()
    if condition == 'closed':
        f.lifetime(open_=False)
    elif condition == 'busy':
        f.lifetime(working=True)
    elif condition == 'library':
        f.frame.library = SimpleNamespace(root=Path('/other-library'))
    else:
        f.frame._closing = True
    f.service.jobs = f.service.get = lambda *_a: pytest.fail('Lost owner read saved jobs.')
    getattr(f, handler)()
    assert not f.rows and not f.calls and not f.prompts and not f.saves


@pytest.mark.parametrize('changed', ['library', 'editor', 'closed'])
def test_new_output_rechecks_owner_and_source_after_the_actual_modal_decision(changed):
    f = studio_output_fixture()
    if changed == 'library':
        f.hooks.append(lambda: setattr(f.frame, 'library', SimpleNamespace(root=Path('/other-library'))))
    elif changed == 'editor':
        f.hooks.append(lambda: f.editor.ChangeValue('A newer unsaved script.'))
    else:
        f.hooks.append(lambda: f.lifetime(open_=False))
    f.render()
    assert len(f.prompts) == 1
    assert not f.rows and not f.calls and not f.saves
    if changed == 'editor':
        assert f.editor.GetValue() == 'A newer unsaved script.'
        assert 'changed while choosing' in f.reports[-1][0]


@pytest.mark.parametrize('condition', ['closed', 'cancelled', 'library'])
def test_queued_production_preserves_intent_without_starting_after_owner_revocation(condition):
    f = studio_output_fixture()
    queued = []
    f.namespace['run'] = lambda work, done, **_kwargs: queued.append((work, done))
    f.render()
    assert len(f.rows) == len(queued) == 1 and not f.calls
    original = deepcopy(f.rows[0])
    if condition == 'closed':
        f.lifetime(open_=False)
    elif condition == 'cancelled':
        f.cancel.set()
    else:
        f.frame.library = SimpleNamespace(root=Path('/other-library'))
    with pytest.raises(InterruptedError, match='before execution'):
        queued[0][0](lambda _message: None)
    assert f.rows == [original] and not f.calls


def test_studio_output_options_follow_existing_worker_busy_state():
    f = studio_output_fixture()
    f.lifetime(working=True)
    f.controls()
    assert not f.format_choice.enabled and not f.stems.enabled
    f.lifetime()
    f.controls()
    assert f.format_choice.enabled and f.stems.enabled


def test_committed_project_reports_failed_draft_refresh_without_losing_revision(tmp_path, monkeypatch):
    from carr_library.durable_state import read_json
    from carr_library.studio.project import ProjectStore, parse_script
    from carr_library.ui import studio_dialog
    saved = parse_script('Narrator: Original.', title='A project',
                         voice_ids=('test:voice',), project_id='stable-project')
    store = ProjectStore(tmp_path / 'projects')
    store.save(saved)
    f = studio_output_fixture(saved, store)
    actual = studio_dialog.atomic_json

    def fail_new_anchor(path, value, **kwargs):
        if Path(path) == f.controller.draft and value.get('project_revision') == 2:
            raise OSError('Synthetic recovery-anchor write failure')
        return actual(path, value, **kwargs)

    monkeypatch.setattr(studio_dialog, 'atomic_json', fail_new_anchor)
    f.editor.SetValue('Narrator: A committed edit.')
    edited = f.save()
    assert edited.revision == 2 and store.load(saved.id) == f.controller.project == edited
    assert 'Project saved' in f.controller.draft_warning
    draft = read_json(f.controller.draft)
    assert draft['text'] == edited.original_script
    assert (draft['project_revision'], draft['project_fingerprint']) == (1, saved.fingerprint)
    reopened = studio_dialog.StudioController(SimpleNamespace(root=store.root.parent), service=f.service)
    assert 'historical revision' in reopened.restore_draft_project(draft)
    assert reopened.project == saved
    with pytest.raises(ValueError, match='Project changed'):
        reopened.save(draft['text'], draft['title'], draft['voice_ids'])
    assert store.load(saved.id) == edited


def test_output_only_clone_retains_current_schema3_effects_and_clip_identity(tmp_path):
    from carr_library.audio import Effect
    from carr_library.studio.project import Cue, Project, ProjectStore, parse_script
    saved = parse_script('Narrator: A saved performance.', voice_ids=('test:voice',),
                         project_id='effects-project')
    effect = Effect('gain', gain_db=-3)
    cue = Cue('microphone-clip', 'retained-recording.wav', role='microphone',
              trim_start=.25, duration=.5, fade_in=.1, title='Exact retake',
              effects=(effect,))
    saved = replace(saved, schema=3, sample_rate=44100, effects=(effect,),
        roles=(replace(saved.roles[0], effects=(effect,), language='uk'),),
        scenes=(replace(saved.scenes[0], cues=(cue,), effects=(effect,)),))
    store = ProjectStore(tmp_path / 'projects')
    f = studio_output_fixture(saved, store)
    f.service.submit(saved, tmp_path, format='wav', stems=True)
    newer = replace(saved, revision=2, title='Newer editing project')
    store.save(newer, expected_revision=1, expected_fingerprint=saved.fingerprint)
    original = (store.directory(saved.id) / 'project.json').read_bytes()
    f.jobs()
    f.format_choice.SetSelection(1)
    f.render()
    assert len(f.rows) == 2 and f.calls == ['1']
    created = Project.from_dict(f.rows[1]['project'])
    assert created.id != saved.id and created.revision == 1
    assert replace(created, id=saved.id) == saved
    assert created.scenes[0].cues[0] == cue
    assert (store.directory(saved.id) / 'project.json').read_bytes() == original
