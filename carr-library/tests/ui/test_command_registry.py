"""Context conflicts and native key preservation, independent of wx."""
import pytest

from carr_library.ui.command_registry import (
    CommandRegistry, CommandSpec, ShortcutError, normalize_shortcut,
)


@pytest.fixture
def commands():
    return CommandRegistry((
        CommandSpec('library.import', 'Import books', default_shortcut='Ctrl+I'),
        CommandSpec('academic.library', 'Academic library', default_shortcut='Ctrl+Shift+B'),
        CommandSpec('reader.bookmarks', 'Manage bookmarks', 'reader', 'Ctrl+Shift+B'),
        CommandSpec('reader.next', 'Next source location', 'reader', 'Ctrl+PageDown'),
        CommandSpec('reader.previous', 'Previous source location', 'reader', 'Ctrl+PageUp'),
        CommandSpec('reader.back', 'Back in session', 'reader', 'Alt+Left'),
        CommandSpec('reader.forward', 'Forward in session', 'reader', 'Alt+Right'),
    ))


@pytest.mark.parametrize(('raw', 'expected'), [
    ('', ''), ('   ', ''), ('control + shift + k', 'Ctrl+Shift+K'),
    ('shift+alt+control+4', 'Ctrl+Alt+Shift+4'), ('ctrl+pgup', 'Ctrl+PageUp'),
    ('Ctrl+PgDn', 'Ctrl+PageDown'), ('ctrl+PGDOWN', 'Ctrl+PageDown'),
    ('alt+left', 'Alt+Left'), ('Alt+Right', 'Alt+Right'),
    ('f1', 'F1'), ('F5', 'F5'), ('F12', 'F12'), ('Shift+F6', 'Shift+F6'),
    ('F13', 'F13'), ('F24', 'F24'), ('ctrl+f23', 'Ctrl+F23'),
    ('Ctrl+S', 'Ctrl+S'), ('Ctrl+Shift+S', 'Ctrl+Shift+S'),
])
def test_normalization(raw, expected):
    assert normalize_shortcut(raw) == expected


@pytest.mark.parametrize('raw', [
    'A', '1', 'Shift+A', 'Shift+1', 'Space', 'Backspace', 'Delete',
    'Ctrl+A', 'Ctrl+C', 'Ctrl+X', 'Ctrl+V', 'Ctrl+Z', 'Ctrl+Y',
    'Ctrl+Shift+A', 'Ctrl+Shift+C', 'Ctrl+Shift+V', 'Ctrl+Shift+Z',
    'Tab', 'Shift+Tab', 'Ctrl+Tab', 'Ctrl+Shift+Tab',
    'Escape', 'Ctrl+Escape', 'Alt+Escape', 'Ctrl+Shift+Esc',
    'Enter', 'Ctrl+Enter', 'Alt+Enter', 'Insert', 'Ctrl+Insert', 'Shift+Insert',
    'CapsLock', 'Ctrl+CapsLock', 'Alt+F4', 'Ctrl+Alt+F4', 'Alt+Space',
    'Ctrl+Alt+Delete', 'F10', 'Shift+F10', 'Alt+L', 'Alt+Shift+L',
    'Left', 'Up', 'Home', 'PageUp', 'Shift+Left', 'Shift+Home', 'Shift+PageDown',
    'Ctrl+Left', 'Ctrl+Shift+Left', 'Ctrl+Home', 'Ctrl+Shift+End',
    'Ctrl+Backspace', 'Ctrl+Delete', 'Shift+Delete', 'Alt+Up', 'Alt+Down',
    'Alt+Backspace', 'Alt+Shift+Backspace',
])
def test_native_editing_navigation_screenreader_and_os_combinations_are_reserved(raw):
    with pytest.raises(ShortcutError, match='reserved'):
        normalize_shortcut(raw)


@pytest.mark.parametrize('raw', [
    None, True, 7, [], {}, 'Ctrl++K', '+K', 'Ctrl+', 'Control+Ctrl+K',
    'Ctrl+Alt+Shift+Shift+K', 'Win+K', 'NVDA+K', 'Meta+K',
    'Ctrl+;', 'Ctrl+Ж', 'F25', 'Ctrl+F99', 'Ctrl+K\n', 'Ctrl+\tK',
    'Ctrl+K' + ' ' * 64,
])
def test_malformed_unsupported_or_oversized_shortcuts_are_rejected(raw):
    with pytest.raises(ShortcutError):
        normalize_shortcut(raw)


def test_same_key_is_valid_in_distinct_contexts(commands):
    effective = commands.effective({})
    assert effective['academic.library'] == effective['reader.bookmarks'] == 'Ctrl+Shift+B'
    assert commands.shortcut('reader.next', {}) == 'Ctrl+PageDown'
    assert [spec.command_id for spec in commands.commands('main')] == ['library.import', 'academic.library']


def test_conflicts_consider_overrides_and_unmodified_defaults(commands):
    previous = commands.effective({})
    with pytest.raises(ShortcutError, match='Academic library.*Import books|Import books.*Academic library'):
        commands.validate({'library.import': 'Ctrl+Shift+B'})
    assert commands.effective({}) == previous


def test_clear_old_assignment_and_move_binding_are_validated_together(commands):
    updated = commands.validate({'academic.library': '', 'library.import': 'control+shift+b'})
    assert updated == {'academic.library': '', 'library.import': 'Ctrl+Shift+B'}
    assert commands.effective(updated)['reader.bookmarks'] == 'Ctrl+Shift+B'


def test_swapping_two_bindings_is_atomic(commands):
    swapped = {'library.import': 'Ctrl+Shift+B', 'academic.library': 'Ctrl+I'}
    assert commands.validate(swapped) == swapped
    assert commands.effective({})['library.import'] == 'Ctrl+I'


def test_unknown_commands_and_bad_maps_fail_without_registry_mutation(commands):
    before = commands.commands()
    for overrides in ({'library.improt': 'Ctrl+K'}, [], None, {'library.import': False}):
        with pytest.raises(ShortcutError):
            commands.validate(overrides)
    assert commands.commands() == before


def test_conflicting_registration_and_repeated_ids_leave_registry_intact(commands):
    before = commands.commands()
    with pytest.raises(ShortcutError, match='already registered'):
        commands.register(CommandSpec('library.import', 'Changed', 'reader', 'Ctrl+K'))
    with pytest.raises(ShortcutError, match='assigned to both'):
        commands.register(CommandSpec('new.command', 'Duplicate default', default_shortcut='Ctrl+I'))
    assert commands.commands() == before


@pytest.mark.parametrize(('identifier', 'label', 'context'), [
    ('123', 'Label', 'main'), ('Translated Command', 'Label', 'main'),
    ('reader..next', 'Label', 'reader'), ('reader.next', '', 'reader'),
    ('reader.next', 'Label\tCtrl+K', 'reader'), ('reader.next', 'Label', 'Reader Context'),
])
def test_command_identity_and_labels_are_validated(identifier, label, context):
    with pytest.raises(ShortcutError):
        CommandSpec(identifier, label, context)


def test_command_collection_is_bounded_and_read_only():
    commands = CommandRegistry(CommandSpec(f'command.c{index}', f'Command {index}') for index in range(512))
    with pytest.raises(ShortcutError, match='full'):
        commands.register(CommandSpec('command.extra', 'Extra'))
    with pytest.raises(TypeError):
        commands.specs['command.other'] = CommandSpec('command.other', 'Other')
