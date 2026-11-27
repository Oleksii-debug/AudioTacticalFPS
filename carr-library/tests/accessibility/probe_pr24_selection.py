"""Regression for toolkits that do not emit selection events on SelectRow."""
import unittest
from unittest.mock import Mock
from probe_pr24 import fixture


class InitialSelection(unittest.TestCase):
    def test_first_result_details_without_programmatic_selection_event(self):
        p, h, *_ = fixture()
        p.query.GetValue.return_value = 'quote'
        p.retrieval, p.result_mode = Mock(), Mock()
        p.retrieval.GetSelection.return_value = 0
        p.result_mode.GetSelection.return_value = 0
        p.search_service = Mock(return_value=[h])
        # SelectRow may update selection without emitting an event. The fixture
        # already returns selected row 0, and select_rows has no event callback.
        p.on_search(None)
        p.grid.select_rows.assert_called_once_with([0])
        p.grid.focus_row.assert_called_once_with(0)
        text = p.details.ChangeValue.call_args.args[0]
        for value in (h.title, h.author, h.edition, h.page, h.quote):
            self.assertIn(value, text)


if __name__ == '__main__':
    unittest.main()
