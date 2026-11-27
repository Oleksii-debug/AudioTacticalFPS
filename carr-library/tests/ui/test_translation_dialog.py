"""Review/approval and native callback logic, without claiming Windows GUI proof."""
import ast
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from carr_library.providers import ProviderProfile, GOOGLE_TRANSLATION_ENDPOINT
from carr_library.ui.translation_dialog import TranslationController


class Service:
    def __init__(self, root):
        self.root = root
        self.calls = []

    def preview(self, profile, value, source, target, *, rate_per_million=None):
        self.calls.append(('preview', profile, value, source, target, rate_per_million))
        return {'estimated_cost': '0.20' if rate_per_million else None,
                'source_sha256': 'a' * 64, 'calls': 1, 'characters': len(value)}

    def submit(self, profile, value, source, target, *, approval, title):
        self.calls.append(('submit', profile, value, source, target, approval, title))
        return approval.job_id


def profile():
    return ProviderProfile('google-profile', 'Synthetic account', 'google-translation-v2',
        GOOGLE_TRANSLATION_ENDPOINT, 'nmt', ('translation',),
        credential_ref='url-api-' + 'a' * 32)


def test_preview_and_approval_are_distinct_and_bind_exact_data(tmp_path):
    service = Service(tmp_path)
    controller = TranslationController(tmp_path, service=service)
    selected = profile()
    chosen = (selected, 'Synthetic text', 'en', 'uk', '1.000001')
    summary = controller.preview(*chosen, title='Source title')
    assert summary['budget_micros'] == 1_000_001
    assert [call[0] for call in service.calls] == ['preview']
    with pytest.raises(PermissionError):
        controller.submit(*chosen, title='Source title')
    identity = controller.submit(*chosen, title='Source title', approved=True)
    permit = service.calls[-1][5]
    assert identity == permit.job_id and permit.budget_micros == 1_000_001
    assert permit.profile_fingerprint == selected.fingerprint
    assert permit.mode == 'APPROVED_REMOTE' and not permit.allow_loopback
    assert permit.max_calls == 1 and permit.currency == 'USD'
    with pytest.raises(PermissionError):
        controller.submit(*chosen, title='Source title', approved=True)


@pytest.mark.parametrize('change', ['profile', 'text', 'source', 'target', 'budget', 'title', 'rate'])
def test_changed_ui_selection_requires_new_preview(tmp_path, change):
    service = Service(tmp_path)
    controller = TranslationController(tmp_path, service=service)
    selected = profile()
    chosen = [selected, 'Synthetic text', 'en', 'uk', '1']
    controller.preview(*chosen, title='Title', rate='2')
    title, rate = 'Title', '2'
    if change == 'profile':
        chosen[0] = replace(selected, name='Different reviewed profile')
    elif change == 'text':
        chosen[1] = 'Different text'
    elif change == 'source':
        chosen[2] = 'auto'
    elif change == 'target':
        chosen[3] = 'sl'
    elif change == 'budget':
        chosen[4] = '2'
    elif change == 'title':
        title = 'Different title'
    else:
        rate = '3'
    with pytest.raises(PermissionError):
        controller.submit(*chosen, approved=True, title=title, rate=rate)
    assert [call[0] for call in service.calls] == ['preview']


def test_estimated_cost_over_budget_revokes_prior_preview(tmp_path):
    service = Service(tmp_path)
    controller = TranslationController(tmp_path, service=service)
    chosen = (profile(), 'Synthetic text', 'en', 'uk')
    controller.preview(*chosen, '1')
    with pytest.raises(ValueError, match='exceeds'):
        controller.preview(*chosen, '0.10', rate='20')
    assert controller.reviewed is None
    with pytest.raises(PermissionError):
        controller.submit(*chosen, '1', approved=True)
    assert not [call for call in service.calls if call[0] == 'submit']


def test_controller_rejects_service_for_another_library(tmp_path):
    with pytest.raises(ValueError, match='selected library'):
        TranslationController(tmp_path, service=Service(tmp_path / 'other'))


def native_callbacks():
    """Execute the real closures with widget/job boundaries replaced."""
    source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/translation_dialog.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    show = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == 'show_translation')
    helpers = {node.name: node for node in show.body if isinstance(node, ast.FunctionDef)}
    factory = ast.parse('''
def callbacks(frame, dialog, status, controls, cancel, wx):
    alive, busy = True, False
    return begin, close, report
''').body[0]
    factory.body[1:1] = [helpers['report'], helpers['begin'], helpers['close']]
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[])),
                 str(source), 'exec'), namespace)
    return namespace['callbacks']


def test_close_is_idempotent_and_late_worker_callbacks_do_not_touch_widgets():
    frame = SimpleNamespace(working=False, _closing=False, run_job=Mock())
    dialog, status, controls, completed = Mock(), Mock(), Mock(), Mock()
    cancel = Event()
    captured = {}
    def start(label, operation, **callbacks):
        frame.working = True
        captured.update(callbacks)
    frame.run_job.side_effect = start
    begin, close, report = native_callbacks()(
        frame, dialog, status, controls, cancel, SimpleNamespace(ID_CLOSE=42))
    assert begin('Translation', lambda: None, completed)
    close()
    close()
    assert cancel.is_set()
    dialog.EndModal.assert_called_once_with(42)
    status.reset_mock()
    controls.reset_mock()
    captured['done']({'status': 'complete'})
    captured['failed'](RuntimeError('late synthetic failure'))
    report('late progress')
    assert not status.mock_calls and not controls.mock_calls and not completed.mock_calls


@pytest.mark.parametrize('busy,closing', [(True, False), (False, True)])
def test_translation_cannot_start_beside_another_shared_operation(busy, closing):
    frame = SimpleNamespace(working=busy, _closing=closing, run_job=Mock())
    dialog, status, controls = Mock(), Mock(), Mock()
    begin, _, _ = native_callbacks()(
        frame, dialog, status, controls, Event(), SimpleNamespace(ID_CLOSE=42))
    assert begin('Translation', lambda: None, Mock()) is False
    frame.run_job.assert_not_called()
    assert status.ChangeValue.called


def test_failed_worker_submission_releases_dialog_controls():
    frame = SimpleNamespace(working=False, _closing=False, run_job=Mock(return_value=False))
    dialog, status, controls = Mock(), Mock(), Mock()
    begin, _, _ = native_callbacks()(
        frame, dialog, status, controls, Event(), SimpleNamespace(ID_CLOSE=42))
    assert begin('Translation', lambda: None, Mock()) is False
    assert controls.call_count == 2


@pytest.mark.parametrize('has_tools', [False, True], ids=['create-tools', 'existing-tools'])
def test_translation_installer_reuses_native_tools_and_is_idempotent(monkeypatch, has_tools):
    import sys
    from carr_library.ui import translation_dialog
    class Menu:
        def __init__(self):
            self.items = []
        def Append(self, identifier, caption):
            item = SimpleNamespace(identifier=100 + len(self.items), caption=caption)
            self.items.append(item)
            return item
    research, tools = Menu(), Menu()
    menus = [('&Research', research)] + ([('&Tools', tools)] if has_tools else [])
    frame = SimpleNamespace(menu_bar=SimpleNamespace(
        GetMenuCount=lambda: len(menus), GetMenuLabel=lambda index: menus[index][0],
        GetMenu=lambda index: menus[index][1], Append=lambda menu, label: menus.append((label, menu))),
        Bind=Mock(), report=Mock(), working=False, _closing=False)
    event_type = object()
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_ANY=-1, EVT_MENU=event_type, Menu=Menu))
    opened = Mock()
    monkeypatch.setattr(translation_dialog, 'show_translation', opened)
    actions = translation_dialog.install_translation_actions(frame)
    assert translation_dialog.install_translation_actions(frame) is actions
    assert len(menus) == 2 and not research.items
    chosen = menus[1][1]
    assert len(chosen.items) == 1
    item = chosen.items[0]
    assert item.caption == 'Translate selected te&xt...'
    assert '\t' not in item.caption  # The existing keyboard manager may assign one.
    frame.Bind.assert_called_once_with(event_type, actions.open, item)
    opened.assert_not_called()
    actions.open()
    opened.assert_called_once_with(frame)


def test_busy_translation_action_does_not_create_state(monkeypatch):
    from carr_library.ui import translation_dialog
    opened = Mock()
    monkeypatch.setattr(translation_dialog, 'show_translation', opened)
    frame = SimpleNamespace(working=True, _closing=False, report=Mock())
    translation_dialog.TranslationActions(frame).open()
    opened.assert_not_called()
    assert frame.report.called


@pytest.mark.parametrize('status,expected', [('complete', 'completed'), ('paused', 'paused'),
                                             ('attention', 'failed')])
def test_translation_outcome_never_promotes_incomplete_chunks(status, expected):
    from carr_library.ui.translation_dialog import translation_outcome
    assert translation_outcome({'status': status}) == expected


@pytest.mark.parametrize('result', [None, (), {}, {'status': 'running'}, {'status': 'unknown'}])
def test_unknown_translation_outcome_is_not_completion(result):
    from carr_library.ui.translation_dialog import translation_outcome
    with pytest.raises(ValueError, match='completion state'):
        translation_outcome(result)


def test_native_begin_passes_shared_tracker_and_typed_outcome():
    from carr_library.progress import ProgressTracker
    from carr_library.ui.translation_dialog import translation_outcome
    frame = SimpleNamespace(working=False, _closing=False, run_job=Mock())
    def start(*args, **kwargs):
        frame.working = True
        return True
    frame.run_job.side_effect = start
    tracker = ProgressTracker('Translation')
    begin, _, _ = native_callbacks()(
        frame, Mock(), Mock(), Mock(), Event(), SimpleNamespace(ID_CLOSE=42))
    assert begin('Translation', lambda: None, Mock(), progress_tracker=tracker,
                 progress_outcome=translation_outcome, done_is_observer=True)
    assert frame.run_job.call_args.kwargs['progress_tracker'] is tracker
    assert frame.run_job.call_args.kwargs['progress_outcome'] is translation_outcome
    assert frame.run_job.call_args.kwargs['done_is_observer'] is True
