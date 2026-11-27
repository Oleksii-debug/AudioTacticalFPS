"""Run against separate PR24 checkout: handler logic only, no real wx or NVDA."""
import ast
import os
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from carr_library.ui.search_model import Hit, SearchModel

SOURCE = Path(os.environ['CARR_UI_SOURCE'])
# Execute the exact class body with toolkit doubles, without importing wx.
# This exercises real event-handler logic, not native focus or speech.

def fixture():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'SearchPanel')
    clip = Mock()
    clip.Open.return_value = True
    clip.SetData.return_value = True
    clip.Flush.return_value = True
    dialog, reader = Mock(), Mock()
    wx = NS(Panel=object, TheClipboard=clip, TextDataObject=lambda x: x,
            Dialog=Mock(return_value=dialog), TextCtrl=Mock(return_value=reader),
            BoxSizer=Mock(), Button=Mock(), VERTICAL=1, TE_READONLY=2,
            TE_MULTILINE=4, EXPAND=8, ALL=16, ID_CANCEL=32)
    env = {'wx': wx, 'SearchModel': SearchModel}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(SOURCE), 'exec'), env)
    panel = object.__new__(env['SearchPanel'])
    model = SearchModel()
    model.set_workspace('A')
    hit = Hit('A', 'passage', 'revision', 'Title Z', 'Автор Y', 'Edition X',
              'Printed page ix; physical 12', 'Точний текст\n“quote”', 'Citation ID')
    model.replace('A', [hit])
    panel.model = model
    panel.grid = Mock()
    panel.grid.selected_rows.return_value = [0]
    panel.status, panel.details, panel.query, panel.go = Mock(), Mock(), Mock(), Mock()
    panel.more_service = Mock(return_value='Before. Точний текст\n“quote”. After.')
    return panel, hit, wx, clip, dialog, reader


class Handlers(unittest.TestCase):
    def test_more_restores_selected_row_and_focus_request(self):
        p, h, wx, clip, dlg, reader = fixture()
        p.on_more(None)
        p.more_service.assert_called_once_with(h.workspace, h.ref, h.revision)
        reader.SetInsertionPoint.assert_called_once_with(0)
        reader.SetFocus.assert_called_once()
        dlg.ShowModal.assert_called_once()
        dlg.Destroy.assert_called_once()
        p.grid.select_rows.assert_called_once_with([0])
        p.grid.focus_row.assert_called_once_with(0)

    def test_copy_quote_preserves_unicode_and_closes(self):
        p, h, wx, clip, dlg, reader = fixture()
        p.on_quote(None)
        clip.SetData.assert_called_once_with(h.quote)
        clip.Close.assert_called_once()
        p.status.ChangeValue.assert_called_once_with('Copied quote')
        p.status.SetFocus.assert_called_once()

    def test_copy_citation_preserves_exact_service_value(self):
        p, h, wx, clip, dlg, reader = fixture()
        p.on_citation(None)
        clip.SetData.assert_called_once_with(h.citation)

    def test_clipboard_rejection_exposes_error(self):
        p, h, wx, clip, dlg, reader = fixture()
        clip.SetData.return_value = False
        p.on_citation(None)
        clip.Close.assert_called_once()
        self.assertIn('failed', p.status.ChangeValue.call_args.args[0].lower())
        p.status.SetFocus.assert_called_once()

    def test_workspace_switch_clears_details_and_selection_data(self):
        p, h, wx, clip, dlg, reader = fixture()
        p.set_workspace('B')
        self.assertEqual(p.model.row_count(), 0)
        p.details.ChangeValue.assert_called_once_with('')
        p.on_quote(None)
        clip.Open.assert_not_called()

    def test_details_fallback_exposes_all_metadata(self):
        p, h, wx, clip, dlg, reader = fixture()
        p.on_selection(Mock())
        details = p.details.ChangeValue.call_args.args[0]
        for text in (h.title, h.author, h.edition, h.page, h.quote):
            self.assertIn(text, details)

    def test_flush_failure_reports_persistence_warning(self):
        p, h, wx, clip, dlg, reader = fixture()
        clip.Flush.return_value = False
        p.on_citation(None)
        clip.Close.assert_called_once()
        self.assertNotEqual(p.status.ChangeValue.call_args.args[0], 'Copied citation',
                            'Copy may work now but persistence after exit is not confirmed')


if __name__ == '__main__':
    unittest.main()
