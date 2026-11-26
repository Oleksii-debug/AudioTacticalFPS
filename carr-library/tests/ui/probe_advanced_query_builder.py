"""Explicit headless native-control probes; no Windows or NVDA acceptance claim."""
from types import SimpleNamespace
import sys

import pytest

from carr_library.search.advanced import compile_query
from carr_library.search.literal import _mapped
from carr_library.ui.advanced_search import QUERY_HELP, QueryBuilderDialog, build_query, show_query_builder


def test_builder_compiles_combined_literal_conditions_and_nearby():
    query = build_query('railway\nlocal history', 'transport\ntrade', 'fiction\npoetry',
                        'red', 'blue', 0)
    assert query == ('"railway" AND "local history" AND ("transport" OR "trade") '
                     'AND ("red" NEAR/0 "blue") AND NOT "fiction" AND NOT "poetry"')
    assert compile_query(query) is not None


def test_unicode_quotes_backslashes_and_operator_words_stay_literal():
    query = build_query('Україна\r\nČas\nAND\nOR\nNOT\nNEAR/5', '', '',
                        'He said "hello"', r'C:\books', 100)
    assert query == ('"Україна" AND "Čas" AND "AND" AND "OR" AND "NOT" '
                     'AND "NEAR/5" AND ("He said ""hello""" NEAR/100 "C:\\books")')
    assert compile_query(query) is not None
    assert build_query('  (alpha OR beta)  ', '', '') == '"(alpha OR beta)"'


def test_literal_fields_roundtrip_to_original_source_spans():
    terms = ['AND', 'OR', 'NOT', 'NEAR/5', '(rail OR road)', r'C:\books',
             'He said "hello"', 'Čas', 'Україна']
    raw = ' | '.join(terms)
    compiled = compile_query(build_query('\n'.join(terms), '', ''))
    match = compiled.match(raw, *_mapped(raw))
    assert match is not None
    assert [raw[start:end] for start, end in match.spans] == terms
    without_literal_operator = raw.replace('AND', 'missing')
    assert compiled.match(without_literal_operator, *_mapped(without_literal_operator)) is None


@pytest.mark.parametrize('raw,expected', [
    ('railway trade blue, red', True),
    ('railway transport red very blue', False),
    ('railway trade blue, red fiction', False),
    ('railway poetry blue, red', False),
])
def test_generated_conditions_match_the_actual_engine(raw, expected):
    query = build_query('railway', 'transport\ntrade', 'fiction', 'red', 'blue', 0)
    assert (compile_query(query).match(raw, *_mapped(raw)) is not None) is expected


@pytest.mark.parametrize('values', [('', '', ''), (' \n', '', 'exclude'), ('', '', 'NOT')])
def test_empty_and_negative_only_queries_are_rejected(values):
    with pytest.raises(ValueError, match='positive term'):
        build_query(*values)


def test_any_only_and_nearby_only_are_valid_positive_queries():
    assert compile_query(build_query('', 'alpha\nbeta', 'excluded')) is not None
    assert compile_query(build_query('', '', '', 'word', 'word', 0)) is not None


@pytest.mark.parametrize('left,right', [('left', ''), ('', 'right')])
def test_partial_nearby_condition_is_rejected(left, right):
    with pytest.raises(ValueError, match='both nearby'):
        build_query('positive', '', '', left, right)


@pytest.mark.parametrize('distance', [-1, 101, True, False, 1.5, '5', None])
def test_distance_requires_bounded_integer(distance):
    with pytest.raises(ValueError, match='whole number from 0 to 100'):
        build_query('positive', '', '', distance=distance)


@pytest.mark.parametrize('field', ['all_terms', 'any_terms', 'excluded_terms', 'near_left', 'near_right'])
@pytest.mark.parametrize('invalid', ['bad\x00text', 'bad\ttext', 'bad\x7ftext', 'bad\u0085text', '\ud800'])
def test_controls_and_surrogates_are_rejected_in_every_field(field, invalid):
    values = dict(all_terms='positive', any_terms='', excluded_terms='', near_left='', near_right='')
    values[field] = invalid
    with pytest.raises(ValueError, match='control character'):
        build_query(**values)


def test_nearby_fields_do_not_accept_line_separators():
    with pytest.raises(ValueError, match='control character'):
        build_query('positive', '', '', 'one\ntwo', 'three')


def test_term_and_final_query_limits_include_generated_syntax():
    assert len(build_query('x' * 498, '', '')) == 500
    with pytest.raises(ValueError, match='500 characters'):
        build_query('x' * 499, '', '')
    with pytest.raises(ValueError, match='500 characters'):
        build_query('x' * 501, '', '')
    assert compile_query(build_query('\n'.join(['x'] * 32), '', '')) is not None
    with pytest.raises(ValueError, match='32 terms'):
        build_query('\n'.join(['x'] * 33), '', '')
    with pytest.raises(ValueError, match='32 terms'):
        build_query('\n'.join(['x'] * 31), '', '', 'a', 'b')


@pytest.mark.parametrize('value', [None, [], 42])
def test_non_text_values_are_not_coerced(value):
    with pytest.raises(ValueError, match='must be text'):
        build_query(value, '', '')


def fake_wx():
    wx = SimpleNamespace(VERTICAL=1, HORIZONTAL=2, EXPAND=4, ALL=8, TE_MULTILINE=16,
        TE_READONLY=32, DEFAULT_DIALOG_STYLE=64, RESIZE_BORDER=128,
        ID_OK=5100, ID_CANCEL=5101, EVT_BUTTON='button', EVT_TEXT='text', EVT_SPINCTRL='spin')
    wx.controls, wx.focus = [], None

    class Control:
        def __init__(self, *args, **kwargs):
            self.value = kwargs.get('value', '')
            self.label = kwargs.get('label', '')
            self.style = kwargs.get('style', 0)
            self.kwargs, self.bindings = kwargs, {}
            self.name = ''
            self.enabled, self.default, self.destroyed = True, False, False
            self.modal = wx.ID_CANCEL
            self.interaction = None
            wx.controls.append(self)

        def SetName(self, value): self.name = value
        def GetValue(self): return self.value
        def SetValue(self, value): self.value = value
        def ChangeValue(self, value): self.value = value
        def SetMinSize(self, value): self.minimum = value
        def SetSizer(self, value): self.sizer = value
        def SetEscapeId(self, value): self.escape = value
        def SetInsertionPoint(self, value): self.insertion = value
        def SetFocus(self): wx.focus = self
        def SetDefault(self): self.default = True
        def Enable(self, value): self.enabled = value
        def Bind(self, event, callback): self.bindings[event] = callback
        def Add(self, *args): pass
        def EndModal(self, value): self.modal = value
        def Destroy(self): self.destroyed = True
        def ShowModal(self):
            if self.interaction:
                self.interaction()
            return self.modal

    class Spin(Control):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.value = kwargs['initial']

    for name in ('Dialog', 'TextCtrl', 'StaticText', 'Button', 'BoxSizer'):
        setattr(wx, name, Control)
    wx.SpinCtrl = Spin
    return wx


def test_named_native_controls_and_initial_focus():
    wx = fake_wx()
    form = QueryBuilderDialog(wx, None, 'previous AND (complex OR query)')
    assert wx.focus is form.fields['all_terms']
    assert all(field.name for field in (*form.fields.values(), form.distance, form.preview,
                                        form.status, form.build_button, form.use_button,
                                        form.cancel_button, form.help_button))
    assert all(form.fields[name].style & wx.TE_MULTILINE
               for name in ('all_terms', 'any_terms', 'excluded_terms'))
    assert form.current.style & wx.TE_READONLY and form.preview.style & wx.TE_READONLY
    assert form.current.GetValue() == 'previous AND (complex OR query)'
    assert not any(field.GetValue() for field in form.fields.values())
    assert form.distance.kwargs['min'] == 0 and form.distance.kwargs['max'] == 100
    assert form.build_button.default and not form.use_button.enabled
    assert form.dialog.escape == wx.ID_CANCEL


def test_build_preview_then_use_returns_exact_query_and_destroys_dialog():
    wx = fake_wx()
    form = QueryBuilderDialog(wx, None)

    def interaction():
        form.fields['all_terms'].SetValue('Україна\nAND')
        form.build_button.bindings[wx.EVT_BUTTON](None)
        assert wx.focus is form.preview and form.preview.insertion == 0
        assert form.preview.GetValue() == '"Україна" AND "AND"'
        assert form.use_button.enabled
        form.use_button.bindings[wx.EVT_BUTTON](None)

    form.dialog.interaction = interaction
    assert form.show() == '"Україна" AND "AND"'
    assert form.dialog.destroyed


def test_invalid_input_preserves_edits_and_focuses_readable_error():
    wx = fake_wx()
    form = QueryBuilderDialog(wx, None)
    form.fields['excluded_terms'].SetValue('fiction')
    form.on_build()
    assert 'positive term' in form.status.GetValue()
    assert form.fields['excluded_terms'].GetValue() == 'fiction'
    assert wx.focus is form.status and not form.use_button.enabled
    assert form.preview.GetValue() == '' and form.result is None
    form.on_use()
    assert form.dialog.modal == wx.ID_CANCEL


def test_changed_fields_invalidate_preview_without_stealing_focus():
    wx = fake_wx()
    form = QueryBuilderDialog(wx, None)
    form.fields['all_terms'].SetValue('first')
    form.on_build()
    form.fields['all_terms'].SetValue('second')
    form.fields['all_terms'].SetFocus()
    form.fields['all_terms'].bindings[wx.EVT_TEXT](None)
    assert wx.focus is form.fields['all_terms']
    assert not form.use_button.enabled and form.preview.GetValue() == ''
    form.on_use()
    assert form.result is None and form.dialog.modal == wx.ID_CANCEL
    assert form.preview.GetValue() == '"second"' and wx.focus is form.preview


def test_use_revalidates_changes_even_without_a_text_event():
    wx = fake_wx()
    form = QueryBuilderDialog(wx, None)
    form.fields['all_terms'].SetValue('first')
    form.on_build()
    form.fields['all_terms'].ChangeValue('second')
    form.on_use()
    assert form.result is None and form.dialog.modal == wx.ID_CANCEL
    assert form.preview.GetValue() == '"second"'
    form.on_use()
    assert form.result == '"second"' and form.dialog.modal == wx.ID_OK


def test_cancel_returns_none_and_does_not_touch_host_query(monkeypatch):
    wx = fake_wx()
    monkeypatch.setitem(sys.modules, 'wx', wx)
    host = SimpleNamespace(query='"old query" AND NOT "fiction"')
    assert show_query_builder(host, host.query) is None
    assert host.query == '"old query" AND NOT "fiction"'
    assert next(c for c in wx.controls if c.kwargs.get('title') ==
                'Advanced search query builder').destroyed


def test_help_is_readable_modal_and_restores_button_focus():
    wx = fake_wx()
    form = QueryBuilderDialog(wx, None)
    form.on_help()
    reader = next(c for c in wx.controls if c.name == 'Advanced query syntax and builder help')
    assert reader.GetValue() == QUERY_HELP and reader.style & wx.TE_READONLY
    assert reader.insertion == 0
    help_dialog = next(c for c in wx.controls if c.kwargs.get('title') == 'Advanced query help')
    assert help_dialog.destroyed and help_dialog.escape == wx.ID_CANCEL
    assert wx.focus is form.help_button


def test_dialog_is_destroyed_if_modal_host_fails():
    wx = fake_wx()
    form = QueryBuilderDialog(wx, None)

    def failed():
        raise RuntimeError('Synthetic modal failure')

    form.dialog.interaction = failed
    with pytest.raises(RuntimeError, match='Synthetic modal failure'):
        form.show()
    assert form.dialog.destroyed
