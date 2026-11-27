"""Native profile actions use the existing supervised modal lifecycle."""
from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock
import sys

from carr_library.ui import engine_settings as ui
from carr_library.ui.transcription_dialog import TranscriptionSession
from tests.ui.test_transcription_dialog import Control, Frame


def view_fixture(tmp_path):
    frame, queue = Frame(tmp_path), deque()
    view = object.__new__(ui.EngineSettingsDialog)
    view.frame, view.store = frame, SimpleNamespace(snapshot=lambda: SimpleNamespace(profiles=(), legacy=False))
    view.reviewed, view.profiles = None, ()
    view.wx = SimpleNamespace(CallAfter=lambda callback, *args: queue.append((callback, args)))
    for name in ('list', 'name', 'details', 'status'):
        setattr(view, name, Control(''))
    view.buttons = {name: Control() for name in ('choose', 'register', 'review', 'toggle', 'remove', 'refresh', 'close')}
    view.session = TranscriptionSession(frame, dispatch=view.wx.CallAfter,
        changed=view.controls, report=view.report, close=Mock())
    def finish():
        frame.finish()
        while queue:
            callback, args = queue.popleft()
            callback(*args)
    return view, finish


def profile():
    return SimpleNamespace(id='a' * 32, name='Selected local profile', engine='piper',
                           manifest_path='/reviewed/manifest.json', manifest_sha256='b' * 64, enabled=True)


def test_selecting_another_profile_clears_previous_registration_authority(tmp_path):
    view, finish = view_fixture(tmp_path)
    view.reviewed = object()
    view.profiles = (profile(),)
    view.list.SetSelection(0)
    view.selected()
    assert view.reviewed is None and not view.buttons['register'].enabled
    assert view.name.GetValue() == 'Selected local profile'
    assert 'Selected local profile' in view.details.GetValue()


def test_saving_uses_the_exact_reviewed_manifest_pin_in_the_shared_worker(tmp_path):
    view, finish = view_fixture(tmp_path)
    path = tmp_path / 'installation' / 'manifest.json'
    view.reviewed = SimpleNamespace(manifest=SimpleNamespace(path=path, manifest_sha256='b' * 64))
    view.name.ChangeValue('Reviewed name')
    saved = profile()
    view.store.register = Mock(return_value=saved)
    view.register()
    view.store.register.assert_not_called()
    assert view.session.busy
    finish()
    view.store.register.assert_called_once_with(path, name='Reviewed name',
        expected_sha256='b' * 64, cancelled=view.session.stopped)
    assert view.reviewed is None and 'Registered' in view.status.GetValue()


def test_changed_manifest_error_preserves_registration_review_and_never_reports_success(tmp_path):
    view, finish = view_fixture(tmp_path)
    review = SimpleNamespace(manifest=SimpleNamespace(path=tmp_path / 'manifest.json', manifest_sha256='b' * 64))
    view.reviewed = review
    view.name.ChangeValue('Reviewed name')
    view.store.register = Mock(side_effect=ValueError('The local model manifest has changed.'))
    view.register()
    finish()
    assert view.reviewed is review and not view.session.busy
    assert 'changed' in view.status.GetValue() and 'Registered' not in view.status.GetValue()


def test_close_waits_for_cancelled_verification_before_releasing_modal(tmp_path):
    view, finish = view_fixture(tmp_path)
    result = Mock()
    def verification():
        assert view.session.stopped()
        raise InterruptedError('Stopped inventory verification.')
    view.session.submit('Verifying', verification, result, kind='review')
    event = SimpleNamespace(CanVeto=lambda: True, Veto=Mock())
    view.close(event)
    event.Veto.assert_called_once()
    assert view.session.busy and not view.session.closed
    finish()
    assert view.session.closed and not view.session.busy
    result.assert_not_called()


def test_profile_settings_menu_is_semantic_idempotent_and_does_not_open_on_install(tmp_path, monkeypatch):
    from carr_library.ui import keyboard_settings
    menu = SimpleNamespace(items=[])
    def append(identifier, caption):
        item = SimpleNamespace(identifier=100 + len(menu.items), caption=caption)
        menu.items.append(item)
        return item
    menu.Append = append
    frame = SimpleNamespace(menu_bar=SimpleNamespace(GetMenuCount=lambda: 1,
        GetMenuLabel=lambda index: '&Tools', GetMenu=lambda index: menu), Bind=Mock())
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_ANY=-1, EVT_MENU=object()))
    registered, show = Mock(), Mock()
    monkeypatch.setattr(keyboard_settings, 'register_menu_command', registered)
    monkeypatch.setattr(ui, 'show_engine_settings', show)
    action = ui.install_engine_settings_actions(frame)
    assert ui.install_engine_settings_actions(frame) is action
    assert len(menu.items) == registered.call_count == 1
    assert registered.call_args.args[1].command_id == 'engines.local'
    show.assert_not_called()
    action()
    show.assert_called_once_with(frame)


def test_studio_default_pipeline_resolves_music_only_through_local_profiles(tmp_path, monkeypatch):
    from carr_library.audio import production
    from carr_library import runtime_settings
    from carr_library.ui.audio_studio import StudioController
    library = tmp_path / 'library'
    library.mkdir()
    output = tmp_path / 'audio'
    output.mkdir()
    ffmpeg = tmp_path / 'ffmpeg-placeholder'
    ffmpeg.write_text('Never executed in this handler test.', encoding='utf-8')
    expected = object()
    resolver = Mock(return_value=expected)
    monkeypatch.setattr(runtime_settings, 'EngineProfileStore',
                        lambda root: SimpleNamespace(music_manifest=resolver))
    registry = SimpleNamespace(voices=lambda: (), capabilities=lambda: ())
    controller = StudioController(private_root=library, registry=registry, ffmpeg=ffmpeg)
    controller.new('Retained source text.')
    controller.configure(output_dir=output)
    cancelled = lambda: False
    def render(plan, folder, *, engine, ffmpeg, cancelled, on_status, music_manifest_resolver):
        assert music_manifest_resolver('profiles/' + 'a' * 32 + '.json', 'fluidsynth',
                                       cancelled=cancelled) is expected
        target = folder / 'complete.wav'
        target.write_bytes(b'Owned synthetic handler output; no quality assertion.')
        return {'status': 'complete', 'output': str(target)}
    monkeypatch.setattr(production, 'render_project', render)
    controller.render(cancelled=cancelled)
    resolver.assert_called_once_with('profiles/' + 'a' * 32 + '.json', 'fluidsynth', cancelled=cancelled)
