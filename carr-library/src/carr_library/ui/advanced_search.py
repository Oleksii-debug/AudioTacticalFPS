"""Native query builder; values are literal terms, never executable query syntax."""
import unicodedata


MAX_QUERY = 500
MAX_TERMS = 32
QUERY_HELP = '''Advanced search matches within one PDF page or logical text segment.

All of these: every nonempty line must match.
Any of these: at least one nonempty line must match.
Exclude: none of the nonempty lines may match.
Each line is one literal word or phrase. AND, OR, NOT, quotation marks and backslashes entered in these fields are treated as source text.

Nearby: enter both phrases and a maximum distance from 0 to 100 intervening words, in either order. Ordinary punctuation does not count as a word. Matches require distinct, nonoverlapping occurrences. Distance 0 allows no word between them. The two phrases form an additional required condition.

Build query shows the exact expression. Review it, then choose Use query to put it in Search. This dialog does not run a search. Cancel leaves the existing query unchanged. Existing expressions are shown for reference and are not parsed into the form.

When typing an advanced expression directly in Search, use uppercase AND, OR and NOT, parentheses for groups, and NEAR/5 between two words or quoted phrases. Double a quotation mark inside a quoted phrase; backslashes remain literal.
Example: "railway" AND ("transport" OR "trade") AND NOT "fiction"
Example: "railway" NEAR/5 "trade"

Include at least one required or alternative positive term, or both Nearby phrases. The finished query may contain at most 500 characters and 32 term occurrences, including both nearby phrases and excluded terms.'''


def _text(value, label, *, multiline=False):
    if not isinstance(value, str):
        raise ValueError(f'{label} must be text.')
    if len(value) > MAX_QUERY:
        raise ValueError(f'{label} must contain at most {MAX_QUERY} characters.')
    if any(unicodedata.category(char) in ('Cc', 'Cs')
           and not (multiline and char in '\r\n') for char in value):
        raise ValueError(f'{label} contains a control character. Enter ordinary text.')
    return value


def build_query(all_terms, any_terms, excluded_terms, near_left='', near_right='', distance=5):
    """Build one bounded expression from literal, newline-separated phrases.

    Quoting belongs to the search grammar, including doubled embedded quotes
    and literal backslashes. No field is interpreted as an existing expression.
    """
    from ..search.advanced import quote_atom

    groups = []
    for value, label in ((all_terms, 'All of these'), (any_terms, 'Any of these'),
                         (excluded_terms, 'Exclude')):
        value = _text(value, label, multiline=True)
        groups.append(tuple(line.strip() for line in value.splitlines() if line.strip()))
    required, alternatives, excluded = groups
    left = _text(near_left, 'Nearby first phrase').strip()
    right = _text(near_right, 'Nearby second phrase').strip()
    if type(distance) is not int or not 0 <= distance <= 100:
        raise ValueError('Nearby distance must be a whole number from 0 to 100.')
    if bool(left) != bool(right):
        raise ValueError('Enter both nearby phrases, or leave both blank.')
    if not (required or alternatives or left):
        raise ValueError('Enter at least one positive term in All, Any, or both Nearby fields.')
    count = sum(map(len, groups)) + (2 if left else 0)
    if count > MAX_TERMS:
        raise ValueError(f'Use at most {MAX_TERMS} terms, including excluded and nearby terms.')
    clauses = [quote_atom(term) for term in required]
    if alternatives:
        clauses.append('(' + ' OR '.join(quote_atom(term) for term in alternatives) + ')')
    if left:
        clauses.append(f'({quote_atom(left)} NEAR/{distance} {quote_atom(right)})')
    clauses.extend('NOT ' + quote_atom(term) for term in excluded)
    query = ' AND '.join(clauses)
    if len(query) > MAX_QUERY:
        raise ValueError(f'The finished query exceeds {MAX_QUERY} characters. Shorten the terms.')
    return query


def _show_help(wx, parent):
    dialog = wx.Dialog(parent, title='Advanced query help', size=(740, 580),
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    try:
        layout = wx.BoxSizer(wx.VERTICAL)
        reader = wx.TextCtrl(dialog, value=QUERY_HELP, style=wx.TE_MULTILINE | wx.TE_READONLY)
        reader.SetName('Advanced query syntax and builder help')
        layout.Add(reader, 1, wx.EXPAND | wx.ALL, 8)
        close = wx.Button(dialog, wx.ID_CANCEL, '&Close help')
        close.SetName('Close advanced query help')
        close.SetDefault()
        layout.Add(close, 0, wx.ALL, 8)
        dialog.SetSizer(layout)
        dialog.SetEscapeId(wx.ID_CANCEL)
        reader.SetInsertionPoint(0)
        reader.SetFocus()
        dialog.ShowModal()
    finally:
        dialog.Destroy()


class QueryBuilderDialog:
    """Use native, named controls; show validation results at a readable focus target."""

    def __init__(self, wx, parent, current_query=''):
        if not isinstance(current_query, str):
            raise ValueError('The current query must be text.')
        self.wx = wx
        self.result = self.built = None
        self.dialog = wx.Dialog(parent, title='Advanced search query builder', size=(820, 700),
                                style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        layout = wx.BoxSizer(wx.VERTICAL)
        explanation = wx.StaticText(self.dialog, label=(
            'Build a replacement query. Enter one literal word or phrase per line.\n'
            'The existing query is kept until you choose Use query.'))
        layout.Add(explanation, 0, wx.EXPAND | wx.ALL, 8)
        self.current = None
        if current_query:
            self.current = self._field(layout, 'Current query; shown for reference only',
                                       current_query, readonly=True, multiline=True)
        lists = wx.BoxSizer(wx.HORIZONTAL)
        self.fields = {}
        for name, label in (('all_terms', 'All of these'), ('any_terms', 'Any of these'),
                            ('excluded_terms', 'Exclude')):
            column = wx.BoxSizer(wx.VERTICAL)
            self.fields[name] = self._field(column, label, multiline=True)
            self.fields[name].SetName(label + '; one word or phrase per line')
            lists.Add(column, 1, wx.EXPAND)
        layout.Add(lists, 1, wx.EXPAND)
        near = wx.BoxSizer(wx.HORIZONTAL)
        for name, label in (('near_left', 'Nearby first phrase; optional'),
                            ('near_right', 'Nearby second phrase; optional')):
            column = wx.BoxSizer(wx.VERTICAL)
            self.fields[name] = self._field(column, label)
            near.Add(column, 1, wx.EXPAND)
        layout.Add(near, 0, wx.EXPAND)
        layout.Add(wx.StaticText(self.dialog, label='Maximum intervening words, from 0 to 100'),
                   0, wx.ALL, 8)
        self.distance = wx.SpinCtrl(self.dialog, value='5', min=0, max=100, initial=5)
        self.distance.SetName('Nearby distance; maximum intervening words, 0 to 100')
        layout.Add(self.distance, 0, wx.EXPAND | wx.ALL, 8)
        self.preview = self._field(layout, 'Generated query preview', readonly=True, multiline=True)
        # wxMSW skips single-line read-only text in Tab traversal. A native
        # multiline reader keeps validation messages reachable in wx 4.2.x.
        self.status = self._field(layout, 'Query builder status',
                                 'Enter terms, then choose Build query.', readonly=True, multiline=True)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.build_button = wx.Button(self.dialog, label='&Build query')
        self.use_button = wx.Button(self.dialog, wx.ID_OK, '&Use query')
        self.cancel_button = wx.Button(self.dialog, wx.ID_CANCEL, '&Cancel')
        self.help_button = wx.Button(self.dialog, label='&Help')
        for button, name in ((self.build_button, 'Build and preview query'),
                             (self.use_button, 'Use generated query in Search'),
                             (self.cancel_button, 'Cancel; keep existing query'),
                             (self.help_button, 'Advanced query help')):
            button.SetName(name)
            buttons.Add(button, 0, wx.ALL, 8)
        self.build_button.SetDefault()
        self.use_button.Enable(False)
        self.build_button.Bind(wx.EVT_BUTTON, self.on_build)
        self.use_button.Bind(wx.EVT_BUTTON, self.on_use)
        self.help_button.Bind(wx.EVT_BUTTON, self.on_help)
        for field in (*self.fields.values(), self.distance):
            field.Bind(wx.EVT_TEXT, self.on_change)
        self.distance.Bind(wx.EVT_SPINCTRL, self.on_change)
        layout.Add(buttons, 0, wx.EXPAND)
        self.dialog.SetSizer(layout)
        self.dialog.SetEscapeId(wx.ID_CANCEL)
        self.fields['all_terms'].SetFocus()

    def _field(self, layout, label, value='', *, readonly=False, multiline=False):
        wx = self.wx
        layout.Add(wx.StaticText(self.dialog, label=label), 0, wx.ALL, 6)
        style = (wx.TE_READONLY if readonly else 0) | (wx.TE_MULTILINE if multiline else 0)
        control = wx.TextCtrl(self.dialog, value=value, style=style)
        control.SetName(label)
        if multiline:
            control.SetMinSize((-1, 56))
        layout.Add(control, 1 if multiline else 0, wx.EXPAND | wx.ALL, 6)
        return control

    def on_change(self, event=None):
        self.built = None
        self.preview.ChangeValue('')
        self.use_button.Enable(False)
        self.status.ChangeValue('Terms changed. Build the query to review it.')
        if event is not None:
            event.Skip()

    def _build(self):
        try:
            query = build_query(**{name: field.GetValue() for name, field in self.fields.items()},
                                distance=self.distance.GetValue())
        except ValueError as exc:
            self.built = None
            self.preview.ChangeValue('')
            self.use_button.Enable(False)
            self.status.ChangeValue(str(exc))
            self.status.SetInsertionPoint(0)
            self.status.SetFocus()
            return None
        self.built = query
        self.preview.ChangeValue(query)
        self.use_button.Enable(True)
        self.status.ChangeValue('Query built. Review the preview, then choose Use query.')
        self.preview.SetInsertionPoint(0)
        self.preview.SetFocus()
        return query

    def on_build(self, event=None):
        self._build()

    def on_use(self, event=None):
        previous = self.built
        query = self._build()
        if query is None or query != previous:
            return
        self.result = query
        self.dialog.EndModal(self.wx.ID_OK)

    def on_help(self, event=None):
        _show_help(self.wx, self.dialog)
        self.help_button.SetFocus()

    def show(self):
        try:
            return self.result if self.dialog.ShowModal() == self.wx.ID_OK else None
        finally:
            self.dialog.Destroy()


def show_query_builder(parent, current_query=''):
    """Return the reviewed replacement expression, or None without changing the host."""
    import wx

    return QueryBuilderDialog(wx, parent, current_query).show()
