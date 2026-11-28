"""Exact native-controller review state; synthetic transport only."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from carr_library.assistant.translation_selection import DocumentSelection
from carr_library.ui.translation_scope_dialog import TranslationScopeController
from tests.assistant.test_translation_scopes import configured


@pytest.fixture
def ready(tmp_path, monkeypatch):
    app, workspace, book, service, profile, transport, selection = configured(tmp_path, monkeypatch)
    controller = TranslationScopeController(app, workspace, service=service)
    values = (profile, (DocumentSelection(book['edition']),), 'document', 'en', 'uk', '2.000003')
    return controller, values, transport


def test_scope_ui_requires_separate_exact_approval_and_never_sends_on_preview(ready):
    controller, values, transport = ready
    with pytest.raises(PermissionError):
        controller.submit(*values, approved=True)
    reviewed = controller.preview(*values, rate='20')
    assert reviewed['preview']['items'][0]['characters'] > 0 and not transport.calls
    with pytest.raises(PermissionError):
        controller.submit(*values, approved=False, rate='20')
    identity = controller.submit(*values, approved=True, rate='20')
    assert controller.service.plans.load(identity)['status'] == 'queued'
    assert controller.reviewed is None and not transport.calls


@pytest.mark.parametrize('change', ['profile', 'document', 'mode', 'source', 'target', 'budget', 'rate', 'glossary'])
def test_scope_ui_change_revokes_review_before_child_creation(ready, change):
    controller, values, transport = ready
    controller.preview(*values, rate='20')
    changed = list(values)
    options = dict(rate='20', glossary='')
    if change == 'profile':
        changed[0] = replace(changed[0], name='Different reviewed account')
    elif change == 'document':
        changed[1] = (DocumentSelection('another-edition'),)
    elif change == 'mode':
        changed[2] = 'batch'
    elif change == 'source':
        changed[3] = 'auto'
    elif change == 'target':
        changed[4] = 'sl'
    elif change == 'budget':
        changed[5] = '3'
    elif change == 'rate':
        options['rate'] = '21'
    else:
        options['glossary'] = 'projects/123456789/locations/us-central1/glossaries/another'
    with pytest.raises((PermissionError, ValueError)):
        controller.submit(*changed, approved=True, **options)
    assert not controller.service.plans.identities() and not transport.calls


def test_failed_preview_revokes_prior_scope_review(ready):
    controller, values, transport = ready
    controller.preview(*values, rate='20')
    broken = list(values)
    broken[5] = '0.000001'
    with pytest.raises(ValueError, match='estimate exceeds'):
        controller.preview(*broken, rate='20')
    assert controller.reviewed is None and not transport.calls


@pytest.mark.parametrize('manager_installed', [False, True])
def test_scope_menu_is_additive_idempotent_and_preserves_existing_shortcuts(monkeypatch, manager_installed):
    from carr_library.ui import translation_scope_dialog as module
    from carr_library.ui.command_registry import CommandRegistry, CommandSpec
    registry = CommandRegistry((CommandSpec('translation.text', 'Translate selected text', 'main', 'Ctrl+J'),))
    overrides = {'translation.text': 'Ctrl+Shift+J'}
    menu = SimpleNamespace(items=[])
    def append(identifier, label):
        item = SimpleNamespace(identifier=100 + len(menu.items), label=label)
        menu.items.append(item)
        return item
    menu.Append = append
    frame = SimpleNamespace(menu_bar=SimpleNamespace(GetMenuCount=lambda: 1,
        GetMenuLabel=lambda index: '&Tools', GetMenu=lambda index: menu), Bind=Mock(), report=Mock())
    registrations = []
    if manager_installed:
        def register(spec, item, *, fallback_unassigned=False):
            assert fallback_unassigned is False
            registry.register(spec)
            registry.validate(overrides)
            registrations.append((spec, item))
        frame.keyboard_commands = SimpleNamespace(register_menu_command=register)
    event = object()
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_ANY=-1, EVT_MENU=event))
    opened = Mock()
    monkeypatch.setattr(module, 'show_translation_scope', opened)
    action = module.install_translation_scope_actions(frame)
    assert module.install_translation_scope_actions(frame) is action and len(menu.items) == 1
    selected = registrations if manager_installed else frame._pending_keyboard_commands
    assert selected[0][0].command_id == 'translation.scope' and selected[0][0].default_shortcut == ''
    assert overrides == {'translation.text': 'Ctrl+Shift+J'}
    opened.assert_not_called()
    action.open()
    opened.assert_called_once_with(frame)
