from dataclasses import replace
import pytest
from carr_library.ui.search_model import Hit, SearchModel
from carr_library.ui._vendor.wx_grid.model import clamp_column


def hit(workspace='a'):
    return Hit(workspace, 'p1', 'r1', 'Same title', 'Автор', '1st',
               'Printed page unknown; physical 2', 'Точний текст\n“quote”', 'Author, title, physical 2')


def test_grid_model():
    m = SearchModel(); m.set_workspace('a'); m.replace('a', [hit()])
    assert m.row_count() == 1 and m.row_label(0) == 'Same title'
    assert m.cell_text(0, 'quote') == hit().quote
    assert m.hit(0).citation == hit().citation
    assert m.cell_text(0, 'page') == hit().page
    assert clamp_column(4, 1, len(m.columns())) == 4


def test_switch_clears_and_rejects_late_result():
    m = SearchModel(); m.set_workspace('a'); m.replace('a', [hit()])
    m.set_workspace('b')
    with pytest.raises(ValueError): m.replace('a', [hit()])
    assert m.row_count() == 0
    m.replace('b', [hit('b')]); assert m.hit(0).workspace == 'b'


def test_cross_scope_mixed_batch_fails_closed():
    m = SearchModel(); m.set_workspace('a'); m.replace('a', [hit()])
    with pytest.raises(ValueError): m.replace('a', [hit(), hit('b')])
    assert m.row_count() == 0


@pytest.mark.parametrize('field', ['ref', 'revision', 'citation', 'page'])
def test_incomplete_anchor_rejected(field):
    m = SearchModel(); m.set_workspace('a')
    with pytest.raises(ValueError): m.replace('a', [replace(hit(), **{field: ''})])
    assert m.row_count() == 0


def test_empty_and_negative_selection():
    m = SearchModel(); m.set_workspace('a'); m.replace('a', [hit()])
    with pytest.raises(ValueError): m.hit(-1)
    m.clear()
    with pytest.raises(ValueError): m.hit(0)
