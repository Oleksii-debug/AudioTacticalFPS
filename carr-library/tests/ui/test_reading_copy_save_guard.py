"""Execute the real save callback around a modal authorization change."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.speech.reading import ReadingDocument


@pytest.mark.parametrize('revocation', ['before_dialog', 'inside_dialog', 'none'])
def test_reading_copy_save_as_reauthorizes_after_native_chooser(tmp_path, revocation):
    source = Path(__file__).parents[2] / 'src/carr_library/ui/speech_dialog.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    host = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'show_speech_reader')
    save = next(node for node in host.body if isinstance(node, ast.FunctionDef) and node.name == 'save_project')
    factory = ast.parse('''
def build(source_guard, sync_document, draft_store, save_recovery, document, wx, dialog, Path, report):
    save_path = None
    return save_project
''')
    factory.body[0].body.insert(1, save)
    environment = {}
    exec(compile(ast.fix_missing_locations(factory), str(source), 'exec'), environment)
    authorized, dialogs, reports = [revocation != 'before_dialog'], [], []
    destination = tmp_path / 'reading.reading.json'
    document = ReadingDocument('Source 🚂.\r\n')
    document.replace(0, 6, 'Reading copy')

    def guard():
        if not authorized[0]:
            raise ValueError('The source authorization changed while choosing a destination.')

    class Chooser:
        def __init__(self, *args, **kwargs): dialogs.append(self)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ShowModal(self):
            if revocation == 'inside_dialog':
                authorized[0] = False
            return 1
        def GetPath(self): return str(destination)

    wx = SimpleNamespace(FileDialog=Chooser, ID_OK=1, FD_SAVE=2)
    callback = environment['build'](guard, lambda: None, None, lambda **kwargs: None,
        document, wx, object(), Path, lambda text, **kwargs: reports.append(text))
    result = callback(choose=True)
    if revocation == 'none':
        assert result is True and ReadingDocument.load(destination).source_text == document.source_text
    else:
        assert result is False and not destination.exists()
        assert any('authorization changed' in report for report in reports)
        assert len(dialogs) == int(revocation == 'inside_dialog')
