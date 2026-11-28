"""Recovery and source integrity at the native studio controller boundary."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.projects import Settings,parse_script
from carr_library.audio.voices import Voice
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.ui.audio_studio import StudioController,prepare_reading_studio


@pytest.fixture
def registry():
    return SimpleNamespace(list_voices=lambda:(Voice('flite:kal','flite','Kal',('en',),(),'', ''),))


def test_unsaved_invalid_script_and_metadata_survive_restart(tmp_path,registry):
    first=StudioController(tmp_path/'data',registry)
    first.save_draft('New title','@not yet valid','uk','flac')
    drafts=first.store.new_drafts()
    assert len(drafts)==1 and drafts[0]['id']==first.draft_id
    reopened=StudioController(tmp_path/'data',registry,draft_id=first.draft_id)
    assert reopened.recovery_draft=='@not yet valid'
    assert reopened.recovery_metadata['title']=='New title'
    assert reopened.recovery_metadata['language']=='uk'
    assert reopened.recovery_metadata['format']=='flac'
    with pytest.raises(ValueError,match='Unknown script directive'):
        reopened.save('New title',reopened.recovery_draft,'uk')
    assert len(first.store.new_drafts())==1
    project=reopened.save('New title','Narrator: Valid text.','en',settings=Settings(format='flac'))
    assert project.settings.format=='flac'
    assert reopened.draft_id != first.draft_id
    assert [row['id'] for row in first.store.new_drafts()] == [first.draft_id]


def test_existing_draft_keeps_unsaved_metadata_and_preserves_stale_writer(tmp_path,registry):
    first=StudioController(tmp_path/'data',registry)
    project=first.save('Original','Narrator: Saved source.','en')
    first.save_draft('Changed title','Narrator: Changed draft.','sk','mp3')
    reopened=StudioController(tmp_path/'data',registry,project.id)
    assert reopened.recovery_draft=='Narrator: Changed draft.'
    assert reopened.recovery_metadata['language']=='sk'
    reopened.save('Changed title',reopened.recovery_draft,'sk',settings=Settings(format='mp3'))
    newer=reopened.project
    reopened.save_draft('Second window','Other unsaved text','en','wav')
    first.save_draft('Stale','Retained old-base draft','en','wav')
    rows={row['id']:row for row in first.store.project_drafts(project.id)}
    assert set(rows)=={first.draft_id,reopened.draft_id}
    assert rows[first.draft_id]['script']=='Retained old-base draft'
    assert rows[first.draft_id]['base_revision']==project.revision
    assert rows[first.draft_id]['base_fingerprint']==project.fingerprint
    assert rows[reopened.draft_id]['base_revision']==newer.revision
    assert first.store.load(project.id)==newer
    assert not (first.store.path(project.id)/'draft.json').exists()


def test_reading_copy_cannot_reinterpret_or_mutate_source(tmp_path,registry):
    app=LibraryApp(tmp_path/'data')
    workspace=app.create_workspace('Source scope')
    raw='@pause 999\nNarrator: These are original source words.'
    imported=app.add_text(workspace,'Untrusted source',raw)
    page=ReadingDesk(app,workspace,imported['edition']).page(1)
    controller=prepare_reading_studio(app.root,page,registry)
    project=controller.save('Reading copy',page.raw_text,'en')
    assert not controller.directives and project.source['text_sha256']==page.location.text_sha256
    assert all(cue.kind=='speech' for scene in project.scenes for cue in scene.cues)
    assert project.scenes[0].cues[0].text=='@pause 999'
    with pytest.raises(ValueError,match='exact original source text'):
        controller.save('Reading copy','Changed original','en')
    assert ReadingDesk(app,workspace,imported['edition']).page(1).raw_text==raw
    with pytest.raises(ValueError,match='exact source page'):
        prepare_reading_studio(app.root,replace(page,raw_text='tampered'),registry)


def test_recovered_source_draft_keeps_source_boundary(tmp_path,registry):
    app=LibraryApp(tmp_path/'data')
    workspace=app.create_workspace('Source scope')
    imported=app.add_text(workspace,'Source','Unchanged original source.')
    page=ReadingDesk(app,workspace,imported['edition']).page(1)
    initial=prepare_reading_studio(app.root,page,registry)
    initial.save_draft('Recovered narration',page.raw_text,'en','wav')
    recovered=StudioController(app.root,registry,draft_id=initial.draft_id)
    assert recovered.source==initial.source and not recovered.directives
    saved=recovered.save('Recovered narration',recovered.recovery_draft,'en')
    assert saved.script==page.raw_text and saved.source==initial.source


@pytest.mark.parametrize('scope', ['role', 'scene', 'master'])
def test_director_effect_controller_commits_a_new_revision_and_reopens_exact_chain(tmp_path, registry, scope):
    from carr_library.audio.models import Effect
    controller = StudioController(tmp_path / 'data', registry)
    original = controller.save('Original', 'Narrator: Kept source.', 'en')
    target = None if scope == 'master' else getattr(original, 'roles' if scope == 'role' else 'scenes')[0].id
    chain = (Effect('equalizer', frequency=1000, gain_db=-3),
             Effect('reverb', enabled=False))
    saved = controller.set_effects(scope, target, chain, expected_fingerprint=original.fingerprint)
    assert saved.revision == original.revision + 1 and saved.script == original.script
    assert controller.base_fingerprint == saved.fingerprint and controller.base_revision == saved.revision
    reopened = StudioController(controller.store.root, registry, saved.id)
    actual = reopened.project.effects if scope == 'master' else getattr(
        reopened.project, 'roles' if scope == 'role' else 'scenes')[0].effects
    assert actual == chain and 'effects, in order:' in reopened.plan_text()
    assert reopened.project.script_sha256 == original.script_sha256
    with pytest.raises(ValueError, match='changed after effect review'):
        controller.set_effects(scope, target, (), expected_fingerprint=original.fingerprint)
    assert controller.store.load(saved.id) == saved


def test_director_effect_controller_rejects_a_foreign_scope_without_writing(tmp_path, registry):
    from carr_library.audio.models import Effect
    controller = StudioController(tmp_path / 'data', registry)
    project = controller.save('Original', 'Narrator: Kept source.', 'en')
    path = controller.store.path(project.id) / 'project.json'
    before = path.read_bytes()
    for scope, target in (('master', 'role-id'), ('role', 'missing'), ('scene', 'missing'), ('filter', None)):
        with pytest.raises(ValueError, match='Choose a role'):
            controller.set_effects(scope, target, (Effect('gain'),), expected_fingerprint=project.fingerprint)
    assert path.read_bytes() == before and controller.project is project


def _director_effect_form(tmp_path, registry, monkeypatch, *, selected=0, picker_result=1):
    from carr_library.ui.audio_studio import StudioDialog
    controller = StudioController(tmp_path / 'data', registry)
    controller.save('Original', 'Narrator: Kept source.', 'en')
    form = StudioDialog.__new__(StudioDialog)
    form.controller, form.dialog = controller, object()
    form.closed = form.busy = form.dirty = False
    form.result = {'state': 'complete', 'historical': True}
    form._save_library = SimpleNamespace(root=controller.store.root)
    form._save_root = controller.store.root
    form.parent = SimpleNamespace(library=form._save_library)
    signature = ('Original', 'Narrator: Kept source.', 'en', 'wav')
    form.editor_signature = lambda: signature
    form.messages, form.refreshed, form.pickers = [], [], []
    form.say = lambda message, focus=False: form.messages.append(message)
    form.refresh = lambda: form.refreshed.append(True)
    form.save = lambda: pytest.fail('An unchanged saved form must not trigger an unrelated save.')
    class Picker:
        def __init__(self, parent, prompt, title, choices):
            assert parent is form.dialog and title == 'Role, scene and master effects'
            assert choices == ['Master output', 'Role: Narrator', 'Scene 1: Original']
            self.destroyed = False
            form.pickers.append(self)
        def ShowModal(self):
            return picker_result
        def GetSelection(self):
            return selected
        def Destroy(self):
            self.destroyed = True
    form.wx = SimpleNamespace(ID_OK=1, SingleChoiceDialog=Picker)
    return form


@pytest.mark.parametrize('selected', [0, 1, 2], ids=['master', 'role', 'scene'])
def test_native_director_handler_reuses_typed_modal_then_commits_only_selected_scope(
        tmp_path, registry, monkeypatch, selected):
    from carr_library.audio.models import Effect
    from carr_library.ui import effects_dialog
    form = _director_effect_form(tmp_path, registry, monkeypatch, selected=selected)
    original = form.controller.project
    chain = (Effect('gain', gain_db=-6),)
    calls = []
    def edit(parent, root, current, **kwargs):
        calls.append((parent, root, current, kwargs))
        assert parent is form.dialog and root == form.controller.store.root and current == ()
        assert kwargs['sample_rate'] == 24000 and 'tempo' not in kwargs['available']
        return chain
    monkeypatch.setattr(effects_dialog, 'show_effect_chain', edit)
    form.edit_effects()
    saved = form.controller.store.load(original.id)
    assert len(calls) == 1 and all(picker.destroyed for picker in form.pickers)
    assert [saved.effects, saved.roles[0].effects, saved.scenes[0].effects][selected] == chain
    assert all(not value for index, value in enumerate(
        [saved.effects, saved.roles[0].effects, saved.scenes[0].effects]) if index != selected)
    assert form.result is None and form.refreshed == [True]
    assert saved.script == original.script and 'Generate applies them' in form.messages[-1]


@pytest.mark.parametrize('boundary', ['cancel-picker', 'cancel-chain', 'closed', 'busy',
                                      'editor-change', 'library-change', 'project-change'])
def test_director_effect_modal_lifetime_and_cancel_never_publish_stale_settings(
        tmp_path, registry, monkeypatch, boundary):
    from carr_library.audio.models import Effect
    from carr_library.ui import effects_dialog
    form = _director_effect_form(tmp_path, registry, monkeypatch,
                                 picker_result=0 if boundary == 'cancel-picker' else 1)
    original = form.controller.project
    path = form.controller.store.path(original.id) / 'project.json'
    before = path.read_bytes()
    prior_result = form.result
    calls = []
    def edit(*args, **kwargs):
        calls.append(True)
        if boundary == 'cancel-chain':
            return None
        if boundary == 'closed':
            form.closed = True
        elif boundary == 'busy':
            form.busy = True
        elif boundary == 'editor-change':
            form.editor_signature = lambda: ('newer unsaved source',)
        elif boundary == 'library-change':
            form.parent.library = SimpleNamespace(root=form._save_root)
        elif boundary == 'project-change':
            form.controller.project = replace(original, title='New in-memory revision')
        return (Effect('gain', gain_db=-9),)
    monkeypatch.setattr(effects_dialog, 'show_effect_chain', edit)
    form.edit_effects()
    assert path.read_bytes() == before and form.result is prior_result and not form.refreshed
    assert all(picker.destroyed for picker in form.pickers)
    assert len(calls) == (0 if boundary == 'cancel-picker' else 1)
    if boundary == 'closed':
        assert form.messages == []


def test_director_effect_disk_cas_failure_keeps_the_reviewed_form_and_newer_saved_project(tmp_path, registry):
    from carr_library.audio.models import Effect
    first = StudioController(tmp_path / 'data', registry)
    original = first.save('Original', 'Narrator: Kept source.', 'en')
    second = StudioController(first.store.root, registry, original.id)
    newer = second.set_effects('master', None, (Effect('gain', gain_db=-2),),
                               expected_fingerprint=original.fingerprint)
    with pytest.raises(ValueError, match='another window'):
        first.set_effects('master', None, (Effect('gain', gain_db=-10),),
                          expected_fingerprint=original.fingerprint)
    assert first.project is original and first.base_fingerprint == original.fingerprint
    assert first.store.load(original.id) == newer


def test_director_effect_editor_is_passive_while_production_is_busy_or_closed(tmp_path, registry, monkeypatch):
    form = _director_effect_form(tmp_path, registry, monkeypatch)
    form.busy = True
    form.edit_effects()
    form.busy, form.closed = False, True
    form.edit_effects()
    assert form.pickers == [] and form.messages == [] and not form.refreshed
