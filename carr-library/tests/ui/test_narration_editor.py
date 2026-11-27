from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.reader.narration import NarrationStore, PronunciationRule, new_project, revise
from carr_library.ui.narration_editor import NarrationController, _parse_rules, show_narration_projects


@pytest.fixture
def controller(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    return NarrationController(NarrationStore(root), new_project('Original', title='Reading copy'))


def test_pending_typing_is_one_revision_and_recovery_does_not_claim_manual_save(controller):
    for text in ['E', 'Ed', 'Edited']:
        controller.set_text(text)
        assert 'Recovery draft' in controller.autosave()
    assert len(controller.project.revisions) == 1
    draft, _ = controller.store.load(controller.project.id, draft=True)
    assert draft.current_text == 'Edited' and len(draft.revisions) == 2
    assert controller.dirty and controller.saved_digest is None
    assert 'project saved' in controller.save()
    assert not controller.dirty and len(controller.project.revisions) == 2
    assert controller.draft_digest is None


def test_failed_save_leaves_pending_text_and_dirty_state(controller, monkeypatch):
    controller.save()
    old_project = controller.project
    controller.set_text('Unsaved text')
    monkeypatch.setattr(controller.store, 'save', lambda *_a, **_k: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        controller.save()
    assert controller.project is old_project
    assert controller.pending_text == 'Unsaved text' and controller.dirty


def test_stale_writer_cannot_silently_overwrite_other_editor(controller):
    controller.save()
    other = NarrationController(controller.store, controller.project, expected_digest=controller.saved_digest)
    controller.set_text('First editor edit')
    controller.save()
    other.set_text('Second editor edit')
    with pytest.raises(ValueError, match='changed elsewhere'):
        other.save()
    assert other.pending_text == 'Second editor edit' and other.dirty
    assert other.store.load(other.project.id)[0].current_text == 'First editor edit'


def test_controller_undo_redo_and_transform_keep_source(controller):
    controller.set_text('Edited')
    controller.undo()
    assert controller.pending_text == 'Original'
    controller.redo()
    assert controller.pending_text == 'Edited'
    controller.apply('Prepared', label='Explicit normalization')
    assert controller.pending_text == 'Prepared' and controller.project.original_text == 'Original'


def test_save_as_new_identity_does_not_overwrite_prior_project(controller):
    controller.save()
    original_id = controller.project.id
    controller.set_text('New copy')
    controller.save_as('Second project')
    assert controller.project.id != original_id
    assert controller.store.load(original_id)[0].current_text == 'Original'
    assert controller.project.original_text == 'Original'
    assert controller.pending_text == 'New copy' and not controller.dirty


def test_revoked_guard_blocks_save_and_draft(controller):
    controller.guard = lambda: (_ for _ in ()).throw(ValueError('Source scope closed'))
    with pytest.raises(ValueError, match='scope closed'):
        controller.save()
    with pytest.raises(ValueError, match='scope closed'):
        controller.autosave()
    assert not (controller.store.root / 'narration-projects').exists()


@pytest.mark.parametrize('existing', [False, True], ids=['new', 'confirmed-existing'])
def test_narration_export_retains_destination_approval_across_candidate_preparation(
        controller, tmp_path, existing):
    from carr_library.ui.export_destination import approve_export_destination
    target = tmp_path / 'reading-copy.txt'
    if existing:
        target.write_bytes(b'Approved previous reading copy')
    approval = approve_export_destination(None, target, private_root=controller.store.root,
                                          confirm=lambda *_: True)
    controller.guard = lambda: target.write_bytes(b'Newer external reading copy')
    with pytest.raises(ValueError, match='destination changed'):
        controller.export(target, overwrite=True, approval=approval)
    assert target.read_bytes() == b'Newer external reading copy'
    assert controller.project.original_text == 'Original'


def test_programmatic_narration_new_export_keeps_file_created_during_preparation(controller, tmp_path):
    target = tmp_path / 'reading-copy.txt'
    controller.guard = lambda: target.write_bytes(b'Competing new reading copy')
    with pytest.raises((ValueError, FileExistsError)):
        controller.export(target)
    assert target.read_bytes() == b'Competing new reading copy'


def test_dictionary_interchange_rejects_unexpected_fields_and_duplicates():
    assert _parse_rules('[{"written":"a","spoken":"b"}]') == (PronunciationRule('a', 'b'),)
    for invalid in ['{}', '[null]', '[{"written":"a","spoken":"b","execute":"shell"}]',
                    '[{"written":"a","spoken":"b"},{"written":"a","spoken":"c"}]']:
        with pytest.raises(ValueError):
            _parse_rules(invalid)


class Dialogs:
    def __init__(self, choices):
        self.choices = iter(choices)
        self.messages = []

    def choose(self, *args):
        self.messages.append(args)
        return next(self.choices)

    def show(self, *args):
        self.messages.append(args)


def test_project_picker_explicitly_recovers_draft_and_preserves_saved_lease(controller, monkeypatch):
    import carr_library.ui.narration_editor as ui
    controller.save()
    saved_digest = controller.saved_digest
    controller.set_text('Recovered words')
    controller.autosave()
    captured = {}
    monkeypatch.setattr(ui, 'open_narration_editor',
                        lambda parent, store, project, **kwargs: captured.update(project=project, **kwargs))
    show_narration_projects(None, controller.store, dialogs=Dialogs([0, 0]))
    assert captured['project'].current_text == 'Recovered words'
    assert captured['expected_digest'] == saved_digest
    assert captured['draft_digest'] == controller.draft_digest


def test_picker_can_choose_last_saved_text_or_cancel_without_deleting_recovery(controller, monkeypatch):
    import carr_library.ui.narration_editor as ui
    controller.save()
    controller.set_text('Draft')
    controller.autosave()
    captured = []
    monkeypatch.setattr(ui, 'open_narration_editor', lambda *args, **kwargs: captured.append(args[2]))
    show_narration_projects(None, controller.store, dialogs=Dialogs([0, 1]))
    assert captured[0].current_text == 'Original'
    show_narration_projects(None, controller.store, dialogs=Dialogs([0, 2]))
    assert len(captured) == 1
    assert controller.store.load(controller.project.id, draft=True)[0].current_text == 'Draft'


def test_draft_only_project_recovery_and_no_saved_project_message(controller, monkeypatch):
    import carr_library.ui.narration_editor as ui
    dialogs = Dialogs([])
    assert show_narration_projects(None, controller.store, dialogs=dialogs) is None
    assert 'no saved' in dialogs.messages[0][-1]
    controller.set_text('Draft only')
    controller.autosave()
    captured = {}
    monkeypatch.setattr(ui, 'open_narration_editor',
                        lambda parent, store, project, **kwargs: captured.update(project=project, **kwargs))
    show_narration_projects(None, controller.store, dialogs=Dialogs([0, 0]))
    assert captured['expected_digest'] is None
    assert captured['project'].current_text == 'Draft only'


@pytest.mark.parametrize('tamper_draft', [False, True])
def test_picker_rechecks_loaded_project_and_recovery_source_workspace(controller, monkeypatch, tamper_draft):
    from dataclasses import replace
    import carr_library.ui.narration_editor as ui
    from carr_library.reader.narration import text_digest
    from carr_library.reader.reading_state import ReadingLocation
    location = ReadingLocation('a', 'edition', 'revision', 'logical_segment', 1,
                               text_digest('Original'), '0' * 64)
    source = new_project('Original', source_location=location)
    controller.store.save(source)
    if tamper_draft:
        controller.store.save(source, draft=True)
    real_load = controller.store.load

    def changed_load(project_id, *, draft=False):
        project, digest = real_load(project_id, draft=draft)
        if draft == tamper_draft:
            project = replace(project, source_location=replace(project.source_location, workspace='b'))
        return project, digest

    class ChangedAfterList(Dialogs):
        def choose(self, *args):
            monkeypatch.setattr(controller.store, 'load', changed_load)
            return 0

    monkeypatch.setattr(ui, 'open_narration_editor', lambda *a, **k: pytest.fail('Cross-library project displayed'))
    with pytest.raises(ValueError, match='different library'):
        show_narration_projects(None, controller.store, workspace='a', dialogs=ChangedAfterList([]))


def test_picker_without_library_only_exposes_independent_authored_projects(controller, monkeypatch):
    import carr_library.ui.narration_editor as ui
    from carr_library.reader.narration import text_digest
    from carr_library.reader.reading_state import ReadingLocation
    source = new_project('Source text', title='Private source copy', source_location=ReadingLocation(
        'archived-library', 'edition', 'revision', 'logical_segment', 1,
        text_digest('Source text'), '0' * 64))
    controller.store.save(source)
    controller.save()
    captured = []
    dialogs = Dialogs([0])
    monkeypatch.setattr(ui, 'open_narration_editor', lambda *args, **kwargs: captured.append(args[2]))
    show_narration_projects(None, controller.store, dialogs=dialogs)
    assert [project.id for project in captured] == [controller.project.id]
    assert 'Private source copy' not in str(dialogs.messages)


def test_save_to_studio_keeps_original_derivative_and_exact_saved_provenance(tmp_path):
    from dataclasses import asdict
    from hashlib import sha256
    from carr_library.application import LibraryApp
    from carr_library.reader.narration import encode_project
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.ui.audio_studio import StudioController

    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Reading')
    original = '# Heading\r\nDate: 2026-11-26\r\n[pause 2]\r\nExact source.'
    book = app.add_text(workspace, 'Book', original)
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    controller = NarrationController(NarrationStore(app.root),
        new_project(page.raw_text, title='Prepared reading', source_location=page.location))
    current = original.replace('Exact source.', 'Explicit reading derivative.')
    controller.set_text(current)
    studio = controller.prepare_audio_studio()
    project, digest = controller.store.load(controller.project.id)
    assert project == controller.project and digest == controller.saved_digest
    assert digest == sha256(encode_project(project)).hexdigest()
    assert studio.draft.source_text == original and studio.script == current
    assert studio.input_kind == 'narration'
    reference = studio.source_reference
    assert reference['project_id'] == project.id
    assert reference['revision'] == project.revisions[project.cursor].number == 1
    assert reference['project_sha256'] == digest
    assert reference['original_sha256'] == project.original_sha256
    assert reference['reading_sha256'] == sha256(current.encode()).hexdigest()
    assert reference['source_location'] == asdict(page.location)
    plan = studio.plan()
    assert plan.original_script == current
    assert all(type(event).__name__ == 'Line' for scene in plan.scenes for event in scene.events)
    assert '# Heading' in ''.join(event.text for scene in plan.scenes for event in scene.events)
    assert '[pause 2]' in ''.join(event.text for scene in plan.scenes for event in scene.events)
    saved_studio = tmp_path / 'audio-project.json'
    studio.save(saved_studio)
    reopened = StudioController(private_root=app.root).open(saved_studio)
    assert reopened.source_reference == reference
    assert reopened.draft.source_text == original and reopened.script == current
    reopened.edit_script('Audio-only follow-up')
    reopened.save()
    assert controller.store.load(project.id) == (project, digest)
    assert ReadingDesk(app, workspace, book['edition']).page(1).raw_text == original


def test_studio_handoff_requires_successful_save_and_current_scope(controller):
    from unittest.mock import Mock
    factory = Mock()
    controller.save()
    other = NarrationController(controller.store, controller.project, expected_digest=controller.saved_digest)
    controller.set_text('Newer saved text')
    controller.save()
    other.set_text('Stale text')
    with pytest.raises(ValueError, match='changed elsewhere'):
        other.prepare_audio_studio(controller_factory=factory)
    factory.assert_not_called()
    controller.guard = lambda: (_ for _ in ()).throw(ValueError('Source scope closed'))
    with pytest.raises(ValueError, match='scope closed'):
        controller.prepare_audio_studio(controller_factory=factory)
    factory.assert_not_called()


@pytest.mark.parametrize('save_all', [False, True])
@pytest.mark.parametrize('edited', [False, True])
def test_native_multiline_normalization_only_enters_saved_text_after_an_edit(tmp_path, monkeypatch, edited, save_all):
    """Drive actual editor callbacks with a control that displays CRLF as LF."""
    import itertools
    import sys
    from hashlib import sha256
    from carr_library.ui.narration_editor import open_narration_editor

    identifiers, queue = itertools.count(1), []
    original = 'Exact original.\r\nДругий рядок.\r\n'
    displayed = original.replace('\r\n', '\n')

    class Window:
        def __init__(self, *args, value='', style=0, **kwargs):
            self.value, self.style = value.replace('\r\n', '\n'), style
            self.handlers, self.identity = {}, next(identifiers)
            self.enabled = True
        def Enable(self, enabled=True):
            self.enabled = bool(enabled)
        def IsEnabled(self):
            return self.enabled
        def Bind(self, event, callback, *args, **kwargs):
            self.handlers[event] = callback
        def GetId(self):
            return self.identity
        def GetValue(self):
            return self.value
        def ChangeValue(self, value):
            self.value = value.replace('\r\n', '\n')
        def SetValue(self, value):
            self.ChangeValue(value)
            self.handlers['text'](SimpleNamespace(Skip=lambda: None))
        def GetWindowStyleFlag(self):
            return self.style
        def SetWindowStyleFlag(self, style):
            self.style = style
        def ShowModal(self):
            while queue:
                queue.pop(0)()
            return 1
        def __getattr__(self, name):
            if name in ('SetName', 'SetMinSize', 'SetInsertionPoint', 'SetFocus', 'SetTitle',
                        'SetAcceleratorTable', 'SetSizer', 'SetEscapeId', 'EndModal', 'Destroy'):
                return lambda *args: None
            raise AttributeError(name)

    class Sizer:
        def __init__(self, *args):
            pass
        def Add(self, *args, **kwargs):
            pass

    class Timer:
        def __init__(self, *args):
            pass
        def StartOnce(self, *args):
            pass
        def Stop(self):
            pass

    constants = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL',
                 'LEFT', 'RIGHT', 'TOP', 'BOTTOM', 'ALL', 'EXPAND', 'TE_MULTILINE',
                 'TE_READONLY', 'TE_PROCESS_TAB', 'ACCEL_CTRL', 'ACCEL_SHIFT', 'ACCEL_ALT')
    wx = SimpleNamespace(**{name: 1 << index for index, name in enumerate(constants)},
        Dialog=Window, TextCtrl=Window, StaticText=Window, Button=Window,
        BoxSizer=Sizer, WrapSizer=Sizer, Timer=Timer, ID_OK=1,
        EVT_BUTTON='button', EVT_MENU='menu', EVT_TIMER='timer', EVT_CLOSE='close', EVT_TEXT='text',
        AcceleratorTable=lambda rows: rows, CallAfter=lambda fn, *args: queue.append(lambda: fn(*args)))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    root = tmp_path / 'library'
    root.mkdir()
    store, project = NarrationStore(root), new_project(original)
    captured = []

    def studio_factory(parent, studio):
        captured.append(studio)
        return SimpleNamespace(show=lambda: studio.edit_script('Audio-only edit'))

    def ready(dialog, controller, controls):
        assert controls['editor'].GetValue() == displayed
        assert controls['editor'].IsEnabled() and controls['save'].IsEnabled()
        dialog.handlers['timer'](None)
        assert store.load(project.id, draft=True)[0].current_text == original
        controls['save_all' if save_all else 'save'].handlers['button']()
        assert store.load(project.id)[0].current_text == original
        assert len(controller.project.revisions) == 1
        if edited:
            controls['editor'].SetValue(displayed + 'Explicit editor change.')
        controls['audio_studio'].handlers['button']()
        assert len(captured) == 1
        controls['close'].handlers['button']()

    result = open_narration_editor(None, store, project, dialogs=Dialogs([]),
                                   on_ready=ready, studio_factory=studio_factory)
    expected = displayed + 'Explicit editor change.' if edited else original
    assert result.original_text == original and result.current_text == expected
    assert store.load(project.id)[0] == result
    assert captured[0].draft.source_text == original
    assert captured[0].source_reference['reading_sha256'] == sha256(expected.encode()).hexdigest()
