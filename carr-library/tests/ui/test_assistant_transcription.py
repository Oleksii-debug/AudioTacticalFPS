"""Current assistant controls with controlled scheduling and actual protocol service work."""
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService
from carr_library.runtime_settings import EngineProfileStore
from carr_library.ui import assistant_templates
from tests.assistant.test_planning_handlers import native_handlers
from tests.assistant.transcription_fixture import make_environment, protocol_interpreter


@pytest.fixture
def selected(native_handlers, tmp_path):
    e = native_handlers
    with protocol_interpreter():
        e.asr = make_environment(tmp_path / 'transcription')
        e.app = e.asr.app
        e.frame.library = e.app
        yield e


def fields(e, *, selected=True):
    e.controls['instruction'].ChangeValue(
        f'Transcribe "{e.asr.source}" as "transcript"')
    e.controls['input'].ChangeValue(str(e.asr.source.parent))
    e.controls['output'].ChangeValue(str(e.asr.outputs))
    e.controls['transcription_profile'].SetSelection(1 if selected else 0)


def changed_profile(e):
    e.controls['transcription_profile'].bindings[e.wx.EVT_CHOICE](
        SimpleNamespace(Skip=lambda: None))


def test_native_profile_choice_is_explicit_none_and_preview_cannot_enable_it(selected):
    def scenario(e):
        choice = e.controls['transcription_profile']
        assert choice.GetSelection() == 0 and len(choice.choices) == 2
        assert 'explicit' in choice.name and 'automatic' in choice.name
        fields(e, selected=False)
        e.press('preview')
        e.finish()
        assert not e.controls['run'].enabled
        assert 'Explicitly select' in e.controls['review'].GetValue()
        assert EngineProfileStore(e.app.root).snapshot().profile(e.asr.profile.id).enabled
        assert AssistantService(e.app).jobs() == ()
    selected.show(scenario)


def test_current_native_selection_previews_then_runs_existing_protocol_service(selected):
    def scenario(e):
        fields(e)
        e.press('preview')
        assert not e.controls['run'].enabled and AssistantService(e.app).jobs() == ()
        e.finish()
        assert e.controls['run'].enabled
        shown = e.controls['review'].GetValue()
        assert e.asr.profile.id in shown and e.asr.profile.manifest_sha256 in shown
        assert '"condition_on_previous_text": true' in shown
        assert not (e.asr.outputs / 'transcript').exists()
        e.press('run')
        assert AssistantService(e.app).jobs() == ()
        e.finish()
        jobs = AssistantService(e.app).jobs()
        assert len(jobs) == 1 and jobs[0].status == 'complete', e.controls['review'].GetValue()
        assert (e.asr.outputs / 'transcript' / 'transcript.txt').is_file()
    selected.show(scenario)


@pytest.mark.parametrize('notify', [False, True])
def test_profile_choice_change_invalidates_preview_even_without_native_event(selected, notify):
    def scenario(e):
        fields(e)
        e.press('preview')
        e.finish()
        assert e.controls['run'].enabled
        e.controls['transcription_profile'].SetSelection(0)
        if notify:
            changed_profile(e)
        e.press('run')
        assert not e.executor.pending and AssistantService(e.app).jobs() == ()
        assert not e.controls['run'].enabled
    selected.show(scenario)


def test_profile_change_during_queued_preview_cannot_replace_captured_registration(selected):
    def scenario(e):
        fields(e)
        e.press('preview')
        EngineProfileStore(e.app.root).set_enabled(e.asr.profile.id, False)
        e.finish()
        assert not e.controls['run'].enabled
        assert 'changed' in e.controls['review'].GetValue()
        assert AssistantService(e.app).jobs() == ()
    selected.show(scenario)


def test_close_before_queued_run_cancels_without_creating_managed_asr_job(selected):
    def scenario(e):
        fields(e)
        e.press('preview')
        e.finish()
        e.press('run')
        e.press('close')
        e.finish()
        assert AssistantService(e.app).jobs() == ()
        assert not (e.app.root / 'transcription-jobs').exists()
    selected.show(scenario)
    assert selected.executor.shutdowns == [{'wait': False, 'cancel_futures': True}]


def test_loading_template_clears_transcription_profile_and_current_authority(selected, monkeypatch):
    loaded = []
    def picker(parent, service, **kwargs):
        assert kwargs['is_current']()
        return SimpleNamespace(template_result=selected.asr.plan().to_json(),
            ShowModal=lambda: loaded.append(True), Destroy=lambda: None,
            template_handlers={'close': lambda: None})
    monkeypatch.setattr(assistant_templates, 'create_templates_dialog', picker)
    def scenario(e):
        fields(e)
        e.press('preview')
        e.finish()
        assert e.controls['run'].enabled
        e.press('templates')
        assert loaded == [True]
        assert e.controls['transcription_profile'].GetSelection() == 0
        assert e.controls['input'].GetValue() == e.controls['output'].GetValue() == ''
        assert not e.controls['run'].enabled and AssistantService(e.app).jobs() == ()
    selected.show(scenario)


def test_library_switch_before_queued_run_never_submits_under_another_owner(selected, tmp_path):
    replacement = LibraryApp(tmp_path / 'replacement-library')
    def scenario(e):
        fields(e)
        e.press('preview')
        e.finish()
        e.press('run')
        e.frame.library = replacement
        e.finish()
        assert AssistantService(e.app).jobs() == ()
        assert AssistantService(replacement).jobs() == ()
        assert not (e.app.root / 'transcription-jobs').exists()
    selected.show(scenario)
