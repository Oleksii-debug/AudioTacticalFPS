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


@pytest.mark.parametrize('manager_installed', [False, True], ids=['before-keyboard', 'after-keyboard'])
def test_translation_installer_registers_one_semantic_command_and_keeps_overrides(monkeypatch, manager_installed):
    import sys
    from carr_library.ui import translation_dialog
    from carr_library.ui.command_registry import CommandRegistry, CommandSpec
    existing = CommandSpec('library.existing', 'Existing command', 'main', 'Ctrl+J')
    registry = CommandRegistry((existing,))
    overrides = {'library.existing': 'Ctrl+Shift+J'}
    original = dict(overrides)
    tools, research = SimpleNamespace(items=[]), SimpleNamespace(items=[])
    def append(identifier, caption):
        item = SimpleNamespace(identifier=100 + len(tools.items), caption=caption)
        tools.items.append(item)
        return item
    tools.Append = append
    frame = SimpleNamespace(menu_bar=SimpleNamespace(
        GetMenuCount=lambda: 2, GetMenuLabel=lambda index: ('&Research', '&Tools')[index],
        GetMenu=lambda index: (research, tools)[index]), Bind=Mock())
    registrations = []
    if manager_installed:
        def register(spec, item, *, fallback_unassigned=False):
            assert fallback_unassigned is False
            registry.register(spec)
            registry.validate(overrides)
            registrations.append((spec, item))
        frame.keyboard_commands = SimpleNamespace(register_menu_command=register)
    event_type = object()
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_ANY=-1, EVT_MENU=event_type))
    open_workspace = Mock()
    monkeypatch.setattr(translation_dialog, 'show_translation', open_workspace)
    actions = translation_dialog.install_translation_actions(frame)
    assert translation_dialog.install_translation_actions(frame) is actions
    assert len(tools.items) == 1 and not research.items
    registered = registrations if manager_installed else frame._pending_keyboard_commands
    assert len(registered) == 1
    spec, item = registered[0]
    assert spec.command_id == 'translation.text' and spec.context == 'main'
    assert spec.default_shortcut == '' and item is tools.items[0]
    assert overrides == original
    frame.Bind.assert_called_once_with(event_type, actions.open, item)
    open_workspace.assert_not_called()  # Installing a menu does not create state or send data.
    actions.open()
    open_workspace.assert_called_once_with(frame)
