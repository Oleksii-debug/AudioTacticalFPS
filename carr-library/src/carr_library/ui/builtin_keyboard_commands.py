"""Stable identities for existing native commands and the modal source reader.

The captions below adapt the existing English menu constructors. They are not
persisted identities. New modules should register their explicit CommandSpec
and menu item with KeyboardManager.register_menu_command instead.
"""
from .command_registry import CommandSpec


# id, menu caption, existing command caption, default application shortcut
MAIN_COMMANDS = (
    ('library.new', 'Library', 'New library', 'Ctrl+N'),
    ('library.open', 'Library', 'Open existing library folder', ''),
    ('library.rename', 'Library', 'Rename or describe library', ''),
    ('library.archive', 'Library', 'Archive library', ''),
    ('library.restore', 'Library', 'Restore archived library', ''),
    ('library.import', 'Library', 'Import books', 'Ctrl+I'),
    ('library.import_center', 'Library', 'Import Center: files, folders and queues', ''),
    ('library.resume_import', 'Library', 'Resume interrupted import', 'Ctrl+Shift+I'),
    ('library.review_import', 'Library', 'Review pending imported page', ''),
    ('library.add_text', 'Library', 'Add original text', 'Ctrl+Shift+N'),
    ('library.metadata', 'Library', 'Edit selected book metadata', 'Ctrl+Shift+M'),
    ('library.tools', 'Library', 'Library tools', 'Ctrl+Shift+L'),
    ('library.backup', 'Library', 'Back up all libraries', ''),
    ('library.restore_backup', 'Library', 'Restore backup to a new folder', ''),
    ('library.epub_reader', 'Library', 'Read EPUB book', 'Ctrl+Shift+R'),
    ('library.epub_contents', 'Library', 'Read electronic book contents', 'Ctrl+Shift+O'),
    ('research.export_topic', 'Research', 'Export active topic', 'Ctrl+E'),
    ('research.search_notes', 'Research', 'Search saved research', 'Ctrl+Shift+F'),
    ('research.edit_tags', 'Research', 'Edit tags for selected evidence', 'Ctrl+Shift+T'),
    ('research.view_tags', 'Research', 'View tags for selected evidence', ''),
    ('research.find_tag', 'Research', 'Find evidence by tag', 'Ctrl+Alt+T'),
    ('research.save_query', 'Research', 'Save a topic search', ''),
    ('research.run_query', 'Research', 'Run a saved topic search', ''),
    ('research.manage_queries', 'Research', 'Manage saved topic searches', ''),
    ('research.refresh', 'Research', 'Refresh libraries and topics', 'F5'),
    ('research.export_outline', 'Research', 'Export topic and subtopics', 'Ctrl+Shift+E'),
    ('research.overview', 'Research', 'Library overview', ''),
    ('research.checklist', 'Research', 'Source metadata and indexing checklist', ''),
    ('research.comparison', 'Research', 'Export source comparison matrix', ''),
    ('research.search_center', 'Research', 'Research Search Center', 'Ctrl+Alt+F'),
    ('academic.open', 'Academic', 'Academic library', 'Ctrl+Shift+B'),
    ('academic.link_book', 'Academic', 'Add selected book to academic library', ''),
    ('bibliography.review_metadata', 'Bibliography', 'Review reference metadata for selected book', ''),
    ('bibliography.export', 'Bibliography', 'Export library or collection bibliography', ''),
    ('source.review', 'Source', 'Review selected PDF page', ''),
    ('source.revoke', 'Source', 'Revoke selected PDF page review', ''),
    ('reading.open', 'Reading', 'Read selected book', 'Ctrl+R'),
    ('reading.export_positions', 'Reading', 'Export reading positions and bookmarks', ''),
    ('reading.import_positions', 'Reading', 'Import reading positions and bookmarks', ''),
    ('help.keyboard', 'Help', 'Keyboard guide', 'F1'),
    ('help.installation', 'Help', 'Check this installation', ''),
    ('commands.find', 'Options', 'Find a command', 'Ctrl+Shift+P'),
    ('options.keyboard', 'Options', 'Keyboard shortcuts', 'Ctrl+Alt+K'),
)

READER_COMMANDS = (
    CommandSpec('reader.previous', 'Previous source location', 'reader', 'Ctrl+PageUp'),
    CommandSpec('reader.next', 'Next source location', 'reader', 'Ctrl+PageDown'),
    CommandSpec('reader.goto', 'Go to reading position', 'reader', 'Ctrl+G'),
    CommandSpec('reader.first', 'Start at first source location', 'reader', ''),
    CommandSpec('reader.back', 'Back in reading session', 'reader', 'Alt+Left'),
    CommandSpec('reader.forward', 'Forward in reading session', 'reader', 'Alt+Right'),
    CommandSpec('reader.where', 'Where am I?', 'reader', 'Ctrl+Shift+L'),
    CommandSpec('reader.bookmark_add', 'Add named page bookmark', 'reader', 'Ctrl+B'),
    CommandSpec('reader.bookmarks', 'Manage page bookmarks', 'reader', 'Ctrl+Shift+B'),
    CommandSpec('reader.help', 'Reader keyboard guide', 'reader', 'F1'),
    CommandSpec('reader.audio', 'EPUB synchronized recorded audio', 'reader'),
    CommandSpec('reader.commands', 'Find a reader command', 'reader', 'Ctrl+Shift+P'),
    CommandSpec('reader.settings', 'Reader keyboard shortcuts', 'reader', 'Ctrl+Alt+K'),
)


VOICE_READER_COMMANDS = (
    CommandSpec('voice_reader.read', 'Read selection or all', 'voice_reader', 'F6'),
    CommandSpec('voice_reader.cursor', 'Read from cursor', 'voice_reader'),
    CommandSpec('voice_reader.line', 'Read current logical line', 'voice_reader'),
    CommandSpec('voice_reader.paragraph', 'Read current paragraph', 'voice_reader'),
    CommandSpec('voice_reader.clipboard', 'Read clipboard once', 'voice_reader'),
    CommandSpec('voice_reader.dictionary', 'Pronunciation dictionary', 'voice_reader'),
    CommandSpec('voice_reader.find', 'Find and replace', 'voice_reader', 'Ctrl+F'),
    CommandSpec('voice_reader.playback', 'Playback controls', 'voice_reader'),
    CommandSpec('voice_reader.previous_unit', 'Previous text unit', 'voice_reader'),
    CommandSpec('voice_reader.current_unit', 'Select current text unit', 'voice_reader'),
    CommandSpec('voice_reader.next_unit', 'Next text unit', 'voice_reader'),
    CommandSpec('voice_reader.read_unit', 'Read current text unit', 'voice_reader'),
    CommandSpec('voice_reader.inspect_audio', 'Inspect saved WAV provenance', 'voice_reader'),
    CommandSpec('voice_reader.pause', 'Pause or resume speech', 'voice_reader', 'F7'),
    CommandSpec('voice_reader.stop', 'Stop speech operation', 'voice_reader', 'F8'),
    CommandSpec('voice_reader.save', 'Save reading copy', 'voice_reader', 'Ctrl+S'),
    CommandSpec('voice_reader.save_as', 'Save reading copy as', 'voice_reader', 'Ctrl+Shift+S'),
    CommandSpec('voice_reader.commands', 'Find a voice reader command', 'voice_reader', 'Ctrl+Shift+P'),
    CommandSpec('voice_reader.settings', 'Voice reader keyboard shortcuts', 'voice_reader', 'Ctrl+Alt+K'),
)


def builtin_commands():
    return tuple(CommandSpec(identity, caption, 'main', shortcut)
                 for identity, _, caption, shortcut in MAIN_COMMANDS) + READER_COMMANDS + VOICE_READER_COMMANDS


def plain_caption(label):
    return label.split('\t', 1)[0].replace('&', '').rstrip('.…').strip().casefold()
