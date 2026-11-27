"""Stable identities for existing native commands and the modal source reader.

The captions below adapt the existing English menu constructors. They are not
persisted identities. New modules should register their explicit CommandSpec
and menu item with KeyboardManager.register_menu_command instead.
"""
from .command_registry import CommandSpec


# id, menu caption, existing command caption, default application shortcut
MAIN_COMMANDS = (
    ('file.save', 'File', 'Save current changes', 'Ctrl+S'),
    ('file.save_all', 'File', 'Save all changes', 'Ctrl+Shift+S'),
    ('file.new_reading_copy', 'File', 'New reading copy', ''),
    ('file.import_reading_copy', 'File', 'Import text as a reading copy', ''),
    ('file.open_reading_copy', 'File', 'Open saved reading copy', ''),
    ('library.new', 'Library', 'New library', 'Ctrl+N'),
    ('library.open', 'Library', 'Open existing library folder', ''),
    ('library.rename', 'Library', 'Rename or describe library', ''),
    ('library.archive', 'Library', 'Archive library', ''),
    ('library.restore', 'Library', 'Restore archived library', ''),
    ('library.import', 'Library', 'Import books', 'Ctrl+I'),
    ('library.ocr_models', 'Library', 'OCR language models: Fast and Best', ''),
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
    ('research.advanced_query', 'Research', 'Build advanced query', 'Ctrl+Alt+Q'),
    ('academic.compare_topic', 'Academic', 'Compare sources in active topic', 'Ctrl+Alt+M'),
    ('academic.export_outline', 'Academic', 'Export research outline with subtopics', ''),
    ('academic.import_ris', 'Academic', 'Import bibliographic metadata from RIS', ''),
    ('academic.catalog_quality', 'Academic', 'Check catalog quality', ''),
    ('academic.open', 'Academic', 'Academic library', 'Ctrl+Shift+B'),
    ('academic.doi', 'Academic', 'DOI lookup and open-access sources', ''),
    ('academic.link_book', 'Academic', 'Add selected book to academic library', ''),
    ('bibliography.review_metadata', 'Bibliography', 'Review reference metadata for selected book', ''),
    ('bibliography.export', 'Bibliography', 'Export library or collection bibliography', ''),
    ('source.review', 'Source', 'Review selected PDF page', ''),
    ('source.revoke', 'Source', 'Revoke selected PDF page review', ''),
    ('reading.open', 'Reading', 'Read selected book', 'Ctrl+R'),
    ('reading.export_positions', 'Reading', 'Export reading positions and bookmarks', ''),
    ('reading.import_positions', 'Reading', 'Import reading positions and bookmarks', ''),
    ('reading.new_copy', 'Reading', 'New editable reading copy', ''),
    ('reading.open_copy', 'Reading', 'Open saved reading copy', ''),
    ('voice.list', 'Voice', 'Installed local voices', ''),
    ('voice.narrate_selection', 'Voice', 'Narrate selected text', 'Ctrl+Alt+V'),
    ('voice.dordiai_settings', 'Voice', 'Connect or disable dordiai-voices', ''),
    ('studio.new', 'Audio Studio', 'New narration or dialogue', 'Ctrl+Alt+U'),
    ('studio.open', 'Audio Studio', 'Open saved audio project', ''),
    ('studio.backup', 'Audio Studio', 'Back up audio project', ''),
    ('studio.restore', 'Audio Studio', 'Restore audio project', ''),
    ('studio.convert', 'Audio Studio', 'Convert local audio', ''),
    ('tools.assistant', 'Tools', 'Local assistant: create and import', 'Ctrl+Alt+J'),
    ('tools.providers', 'Tools', 'Optional providers and API keys', ''),
    ('help.keyboard', 'Help', 'Keyboard guide', 'F1'),
    ('help.installation', 'Help', 'Check this installation', ''),
    ('help.formats', 'Help', 'Document formats', ''),
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
    CommandSpec('reader.edit_copy', 'Create editable reading copy', 'reader', ''),
    CommandSpec('reader.speech_all', 'Read current text aloud', 'reader', 'F6'),
    CommandSpec('reader.speech_cursor', 'Read aloud from cursor', 'reader', 'F9'),
    CommandSpec('reader.speech_selection', 'Read selection aloud', 'reader', 'Ctrl+Shift+F6'),
    CommandSpec('reader.speech_pause', 'Pause or resume voice', 'reader', 'F7'),
    CommandSpec('reader.speech_stop', 'Stop voice reading', 'reader', 'F8'),
    CommandSpec('reader.speech_resume', 'Resume saved voice position', 'reader', 'Ctrl+Shift+F8'),
    CommandSpec('reader.speech_voices', 'Discover local voices', 'reader', ''),
    CommandSpec('reader.speech_status', 'Voice reading status', 'reader', 'Ctrl+Alt+F8'),
    CommandSpec('reader.help', 'Reader keyboard guide', 'reader', 'F1'),
    CommandSpec('reader.commands', 'Find a reader command', 'reader', 'Ctrl+Shift+P'),
    CommandSpec('reader.settings', 'Reader keyboard shortcuts', 'reader', 'Ctrl+Alt+K'),
)


def builtin_commands():
    return tuple(CommandSpec(identity, caption, 'main', shortcut)
                 for identity, _, caption, shortcut in MAIN_COMMANDS) + READER_COMMANDS


def plain_caption(label):
    return label.split('\t', 1)[0].replace('&', '').rstrip('.…').strip().casefold()
